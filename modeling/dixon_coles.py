"""Vectorized Dixon-Coles rho fitter (4-cell closed-form) & Analytical-Gradient L-BFGS-B MLE."""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np
from scipy.optimize import minimize

from domain.entities import DEFAULT_CONSTANTS, EncodedDataset, FittedModelState, ModelConfig, ScoringConstants
from modeling.base import BaseScoreModel


def _poisson_trunc_sum(lam: np.ndarray, max_goals: int) -> np.ndarray:
    """Compute sum_{g=0}^{max_goals} exp(-lam) * lam^g / g! vectorized."""
    term = np.exp(-lam)
    total = term.copy()
    for g in range(1, max_goals + 1):
        term = term * (lam / g)
        total = total + term
    return total


def _poisson_pmf_at(goals: np.ndarray, lam: np.ndarray) -> np.ndarray:
    """Vectorized Poisson PMF at integer goal counts."""
    out = np.empty_like(lam, dtype=np.float64)
    zero_lam = lam <= 0.0
    out[zero_lam] = np.where(goals[zero_lam] == 0, 1.0, 0.0)
    nz = ~zero_lam
    if np.any(nz):
        from scipy.special import gammaln

        g = goals[nz].astype(np.float64)
        l = lam[nz]
        out[nz] = np.exp(-l + g * np.log(l) - gammaln(g + 1.0))
    return out


def compute_sequential_one_pass_lambdas(
    home_idx: np.ndarray,
    away_idx: np.ndarray,
    ft_home: np.ndarray,
    ft_away: np.ndarray,
    *,
    n_teams: int,
    k: float,
    min_earlier: int = DEFAULT_CONSTANTS.min_rho_earlier_matches,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Compute season-to-date one-pass (lambda_h, lambda_a) for matches i >= min_earlier in O(N)."""
    n = home_idx.shape[0]
    if n <= min_earlier:
        empty_f = np.empty(0, dtype=np.float64)
        empty_i = np.empty(0, dtype=np.int16)
        return empty_f, empty_f, empty_i, empty_i

    lh_list: list[float] = []
    la_list: list[float] = []
    gh_list: list[int] = []
    ga_list: list[int] = []

    sum_h_goals = np.zeros(n_teams, dtype=np.float64)
    cnt_h = np.zeros(n_teams, dtype=np.float64)
    sum_h_conc = np.zeros(n_teams, dtype=np.float64)
    sum_a_goals = np.zeros(n_teams, dtype=np.float64)
    cnt_a = np.zeros(n_teams, dtype=np.float64)
    sum_a_conc = np.zeros(n_teams, dtype=np.float64)
    tot_h = 0.0
    tot_a = 0.0

    for i in range(n):
        hi = int(home_idx[i])
        ai = int(away_idx[i])
        fh = int(ft_home[i])
        fa = int(ft_away[i])
        if i >= min_earlier:
            league_h = tot_h / i
            league_a = tot_a / i
            if league_h > 0.0 and league_a > 0.0:
                ch = cnt_h[hi]
                att_h = 1.0 if ch == 0.0 else (ch * ((sum_h_goals[hi] / ch) / league_h) + k) / (ch + k)
                ca = cnt_a[ai]
                def_a = 1.0 if ca == 0.0 else (ca * ((sum_a_conc[ai] / ca) / league_h) + k) / (ca + k)
                att_a = 1.0 if ca == 0.0 else (ca * ((sum_a_goals[ai] / ca) / league_a) + k) / (ca + k)
                def_h = 1.0 if ch == 0.0 else (ch * ((sum_h_conc[hi] / ch) / league_a) + k) / (ch + k)
                lh_list.append(league_h * att_h * def_a)
                la_list.append(league_a * att_a * def_h)
                gh_list.append(fh)
                ga_list.append(fa)

        tot_h += fh
        tot_a += fa
        sum_h_goals[hi] += fh
        sum_h_conc[hi] += fa
        cnt_h[hi] += 1.0
        sum_a_goals[ai] += fa
        sum_a_conc[ai] += fh
        cnt_a[ai] += 1.0

    return (
        np.asarray(lh_list, dtype=np.float64),
        np.asarray(la_list, dtype=np.float64),
        np.asarray(gh_list, dtype=np.int16),
        np.asarray(ga_list, dtype=np.int16),
    )


def fit_dixon_coles_rho_vec(
    dataset: EncodedDataset,
    *,
    k: float,
    rho_grid: Sequence[float] = DEFAULT_CONSTANTS.rho_grid,
    constants: ScoringConstants = DEFAULT_CONSTANTS,
) -> float | None:
    """Vectorized 4-cell closed-form Dixon-Coles rho grid search matching score_model.py."""
    lam_h, lam_a, gh, ga = compute_sequential_one_pass_lambdas(
        dataset.home_idx,
        dataset.away_idx,
        dataset.ft_home,
        dataset.ft_away,
        n_teams=dataset.n_teams,
        k=k,
        min_earlier=constants.min_rho_earlier_matches,
    )
    if lam_h.shape[0] < constants.min_rho_training_matches:
        return None

    max_g = constants.rho_max_goals
    # Filter matches outside 0..max_goals (probability 0 under _probability_from_indep)
    if np.any((gh > max_g) | (ga > max_g)):
        return None

    s_h = _poisson_trunc_sum(lam_h, max_g)
    s_a = _poisson_trunc_sum(lam_a, max_g)
    s0 = s_h * s_a  # independent sum over 0..max_g x 0..max_g

    p00 = np.exp(-(lam_h + lam_a))
    p01 = p00 * lam_a
    p10 = p00 * lam_h
    p11 = p00 * lam_h * lam_a

    p_indep_obs = _poisson_pmf_at(gh, lam_h) * _poisson_pmf_at(ga, lam_a)
    m00 = (gh == 0) & (ga == 0)
    m01 = (gh == 0) & (ga == 1)
    m10 = (gh == 1) & (ga == 0)
    m11 = (gh == 1) & (ga == 1)

    rhos = np.asarray(rho_grid, dtype=np.float64)[:, None]  # (R, 1)
    lh_r = lam_h[None, :]
    la_r = lam_a[None, :]

    t00 = 1.0 - lh_r * la_r * rhos
    t01 = 1.0 + lh_r * rhos
    t10 = 1.0 + la_r * rhos
    t11 = 1.0 - rhos

    feasible = np.all((t00 > 0.0) & (t01 > 0.0) & (t10 > 0.0) & (t11 > 0.0), axis=1)
    if not np.any(feasible):
        return None

    # Closed-form 4-cell renormalization sum Z(rho)
    z = (
        s0[None, :]
        + p00[None, :] * (t00 - 1.0)
        + p01[None, :] * (t01 - 1.0)
        + p10[None, :] * (t10 - 1.0)
        + p11[None, :] * (t11 - 1.0)
    )
    tau_obs = np.ones_like(z)
    tau_obs[:, m00] = t00[:, m00]
    tau_obs[:, m01] = t01[:, m01]
    tau_obs[:, m10] = t10[:, m10]
    if np.any(m11):
        tau_obs[:, m11] = t11

    probs = (p_indep_obs[None, :] * tau_obs) / z
    valid_rows = feasible & np.all(probs > 0.0, axis=1)
    if not np.any(valid_rows):
        return None

    ll = np.full(len(rho_grid), float("-inf"), dtype=np.float64)
    ll[valid_rows] = np.sum(np.log(probs[valid_rows]), axis=1)
    best_idx = int(np.argmax(ll))
    if not math.isfinite(float(ll[best_idx])):
        return None
    return float(rho_grid[best_idx])


def dixon_coles_joint_nll_and_grad(
    theta: np.ndarray,
    home_idx: np.ndarray,
    away_idx: np.ndarray,
    ft_home: np.ndarray,
    ft_away: np.ndarray,
    weights: np.ndarray,
    *,
    n_teams: int,
    m00: np.ndarray,
    m01: np.ndarray,
    m10: np.ndarray,
    m11: np.ndarray,
    l2_reg: float = 1e-4,
    eps: float = 1e-12,
) -> tuple[float, np.ndarray]:
    """Exact NLL (without constant log(k!) terms) and analytical gradient for L-BFGS-B (jac=True).

    Reparameterizes ``alpha`` by subtracting ``mean(alpha)`` inside the objective to
    remove linear identifiability symmetry without expensive SLSQP constraints.
    """
    alpha_raw = theta[:n_teams]
    alpha = alpha_raw - np.mean(alpha_raw)
    beta = theta[n_teams : 2 * n_teams]
    gamma = float(theta[2 * n_teams])
    rho = float(theta[2 * n_teams + 1])

    eta_h = np.clip(alpha[home_idx] - beta[away_idx] + gamma, -10.0, 10.0)
    eta_a = np.clip(alpha[away_idx] - beta[home_idx], -10.0, 10.0)
    lam_h = np.exp(eta_h)
    lam_a = np.exp(eta_a)

    # Low-score tau factors using precomputed boolean masks
    tau = np.ones_like(lam_h)
    dtau_dlh = np.zeros_like(lam_h)
    dtau_dla = np.zeros_like(lam_h)
    dtau_drho = np.zeros_like(lam_h)

    if np.any(m00):
        tau[m00] = 1.0 - lam_h[m00] * lam_a[m00] * rho
        dtau_dlh[m00] = -lam_a[m00] * rho
        dtau_dla[m00] = -lam_h[m00] * rho
        dtau_drho[m00] = -lam_h[m00] * lam_a[m00]
    if np.any(m01):
        tau[m01] = 1.0 + lam_h[m01] * rho
        dtau_dlh[m01] = rho
        dtau_drho[m01] = lam_h[m01]
    if np.any(m10):
        tau[m10] = 1.0 + lam_a[m10] * rho
        dtau_dla[m10] = rho
        dtau_drho[m10] = lam_a[m10]
    if np.any(m11):
        tau[m11] = 1.0 - rho
        dtau_drho[m11] = -1.0

    tau_safe = np.maximum(tau, eps)
    active_tau = tau > eps

    # Constant-eliminated Poisson NLL: lam_h + lam_a - ft_h*eta_h - ft_a*eta_a - log(tau)
    nll_vec = lam_h + lam_a - ft_home * eta_h - ft_away * eta_a - np.log(tau_safe)
    nll = float(np.sum(weights * nll_vec))
    if l2_reg > 0.0:
        nll += 0.5 * l2_reg * float(np.sum(alpha * alpha) + np.sum(beta * beta))

    # Analytical derivatives w.r.t. eta_h, eta_a, rho
    inv_tau = np.where(active_tau, 1.0 / tau_safe, 0.0)
    d_nll_d_eta_h = weights * (lam_h - ft_home - inv_tau * dtau_dlh * lam_h)
    d_nll_d_eta_a = weights * (lam_a - ft_away - inv_tau * dtau_dla * lam_a)
    grad_rho = float(np.sum(weights * (-inv_tau * dtau_drho)))

    # Vectorized O(M) accumulation via np.bincount
    grad_alpha_centered = (
        np.bincount(home_idx, weights=d_nll_d_eta_h, minlength=n_teams)
        + np.bincount(away_idx, weights=d_nll_d_eta_a, minlength=n_teams)
    )
    grad_beta = (
        -np.bincount(away_idx, weights=d_nll_d_eta_h, minlength=n_teams)
        - np.bincount(home_idx, weights=d_nll_d_eta_a, minlength=n_teams)
    )
    if l2_reg > 0.0:
        grad_alpha_centered = grad_alpha_centered + l2_reg * alpha
        grad_beta = grad_beta + l2_reg * beta

    # Chain rule through centering alpha = alpha_raw - mean(alpha_raw)
    grad_alpha_raw = grad_alpha_centered - np.mean(grad_alpha_centered)
    grad_gamma = float(np.sum(d_nll_d_eta_h))

    grad = np.empty_like(theta)
    grad[:n_teams] = grad_alpha_raw
    grad[n_teams : 2 * n_teams] = grad_beta
    grad[2 * n_teams] = grad_gamma
    grad[2 * n_teams + 1] = grad_rho
    return nll, grad


class JointDixonColesMLEModel(BaseScoreModel):
    """Joint Dixon-Coles MLE Strategy solved via L-BFGS-B with analytical gradients & warm-start."""

    def __init__(self, *, l2_reg: float = 1e-4) -> None:
        self.l2_reg = l2_reg

    def fit(
        self,
        dataset: EncodedDataset,
        weights: np.ndarray | None = None,
        *,
        config: ModelConfig,
        prior_means: dict[str, float] | None = None,
        x0: np.ndarray | None = None,
    ) -> FittedModelState:
        _ = (config, prior_means)
        t = dataset.n_teams
        n = dataset.n_matches
        if n == 0 or t == 0:
            ones = np.ones(t, dtype=np.float64)
            return FittedModelState(1.0, 1.0, ones, ones.copy(), ones.copy(), ones.copy(), rho=0.0)
        state, _ = self.fit_with_warm_start(dataset, weights, x0=x0)
        return state

    def fit_with_warm_start(
        self,
        dataset: EncodedDataset,
        weights: np.ndarray | None = None,
        *,
        x0: np.ndarray | None = None,
    ) -> tuple[FittedModelState, np.ndarray]:
        t = dataset.n_teams
        n = dataset.n_matches
        w = np.ones(n, dtype=np.float64) if weights is None else weights
        gh = dataset.ft_home.astype(np.float64)
        ga = dataset.ft_away.astype(np.float64)
        m00 = (dataset.ft_home == 0) & (dataset.ft_away == 0)
        m01 = (dataset.ft_home == 0) & (dataset.ft_away == 1)
        m10 = (dataset.ft_home == 1) & (dataset.ft_away == 0)
        m11 = (dataset.ft_home == 1) & (dataset.ft_away == 1)

        dim = 2 * t + 2
        init = np.zeros(dim, dtype=np.float64) if x0 is None or x0.shape[0] != dim else x0.astype(np.float64, copy=True)
        if x0 is None:
            init[2 * t] = 0.2

        bounds = [(-3.0, 3.0)] * (2 * t) + [(-1.5, 1.5), (-0.20, 0.15)]
        res = minimize(
            lambda th: dixon_coles_joint_nll_and_grad(
                th,
                dataset.home_idx,
                dataset.away_idx,
                gh,
                ga,
                w,
                n_teams=t,
                m00=m00,
                m01=m01,
                m10=m10,
                m11=m11,
                l2_reg=self.l2_reg,
            ),
            init,
            method="L-BFGS-B",
            jac=True,
            bounds=bounds,
        )
        state = self._from_theta(res.x, t, bool(res.success))
        return state, res.x

    def _from_theta(self, theta: np.ndarray, t: int, converged: bool) -> FittedModelState:
        alpha = theta[:t] - np.mean(theta[:t])
        beta = theta[t : 2 * t]
        gamma = float(theta[2 * t])
        rho = float(theta[2 * t + 1])
        att = np.exp(alpha)
        dfn = np.exp(-beta)
        return FittedModelState(
            league_home=math.exp(gamma),
            league_away=1.0,
            attack_home=att,
            defence_home=dfn,
            attack_away=att,
            defence_away=dfn,
            rho=rho,
            extra={"converged": converged, "theta": theta},
        )

    def predict_lambdas(
        self,
        state: FittedModelState,
        home_idx: int,
        away_idx: int,
        *,
        config: ModelConfig,
    ) -> tuple[float, float]:
        lh = state.league_home * float(state.attack_home[home_idx]) * float(state.defence_away[away_idx])
        la = state.league_away * float(state.attack_away[away_idx]) * float(state.defence_home[home_idx])
        return lh, la
