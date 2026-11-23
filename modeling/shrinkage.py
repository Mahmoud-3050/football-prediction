"""Vectorized Shrinkage One-Pass and Joint Venue Strength models."""

from __future__ import annotations

import numpy as np

from domain.entities import (
    DEFAULT_CONSTANTS,
    EncodedDataset,
    FittedModelState,
    ModelConfig,
    ScoringConstants,
)
from domain.features import soft_cap_goals_vec
from modeling.base import BaseScoreModel


def _normalize_geom_mean(values: np.ndarray, active_mask: np.ndarray, eps: float = 1e-9) -> np.ndarray:
    """Geometric-mean normalization over active clubs only, matching score_model.py."""
    if not np.any(active_mask):
        return values
    pos = np.maximum(values[active_mask], eps)
    center = float(np.exp(np.mean(np.log(pos))))
    if center <= 0.0:
        return values
    out = values.copy()
    out[active_mask] = pos / center
    return out


def compute_joint_venue_strengths_vec(
    home_idx: np.ndarray,
    away_idx: np.ndarray,
    ft_home: np.ndarray,
    ft_away: np.ndarray,
    weights: np.ndarray,
    *,
    n_teams: int,
    league_home: float,
    league_away: float,
    iterations: int = DEFAULT_CONSTANTS.joint_ratio_iterations,
    goal_cap: float | None = None,
    goal_cap_slope: float = 0.5,
    constants: ScoringConstants = DEFAULT_CONSTANTS,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Vectorized O(M) per-iteration replacement for ``score_model._joint_venue_strengths``.

    Uses ``np.bincount`` instead of nested Python loops over clubs and matches.
    Returns ``(att_h, def_a, att_a, def_h, n_att_h, n_def_a, n_att_a, n_def_h)``.
    """
    n_att_h = np.bincount(home_idx, weights=weights, minlength=n_teams)
    n_def_h = n_att_h.copy()
    n_att_a = np.bincount(away_idx, weights=weights, minlength=n_teams)
    n_def_a = n_att_a.copy()

    active = (n_att_h > 0) | (n_att_a > 0)
    lh = max(float(league_home), constants.eps_strength)
    la = max(float(league_away), constants.eps_strength)

    g_h = soft_cap_goals_vec(ft_home, goal_cap, goal_cap_slope)
    g_a = soft_cap_goals_vec(ft_away, goal_cap, goal_cap_slope)
    wg_h = weights * g_h
    wg_a = weights * g_a

    num_att_h = np.bincount(home_idx, weights=wg_h, minlength=n_teams)
    num_def_a = np.bincount(away_idx, weights=wg_h, minlength=n_teams)
    num_att_a = np.bincount(away_idx, weights=wg_a, minlength=n_teams)
    num_def_h = np.bincount(home_idx, weights=wg_a, minlength=n_teams)

    att_h = np.ones(n_teams, dtype=np.float64)
    def_a = np.ones(n_teams, dtype=np.float64)
    att_a = np.ones(n_teams, dtype=np.float64)
    def_h = np.ones(n_teams, dtype=np.float64)

    for _ in range(max(1, iterations)):
        den = np.bincount(home_idx, weights=weights * lh * def_a[away_idx], minlength=n_teams)
        raw = np.where(den > constants.eps_clip, num_att_h / np.where(den > constants.eps_clip, den, 1.0), 1.0)
        att_h = _normalize_geom_mean(raw, active, constants.eps_strength)

        den = np.bincount(away_idx, weights=weights * lh * att_h[home_idx], minlength=n_teams)
        raw = np.where(den > constants.eps_clip, num_def_a / np.where(den > constants.eps_clip, den, 1.0), 1.0)
        def_a = _normalize_geom_mean(raw, active, constants.eps_strength)

        den = np.bincount(away_idx, weights=weights * la * def_h[home_idx], minlength=n_teams)
        raw = np.where(den > constants.eps_clip, num_att_a / np.where(den > constants.eps_clip, den, 1.0), 1.0)
        att_a = _normalize_geom_mean(raw, active, constants.eps_strength)

        den = np.bincount(home_idx, weights=weights * la * att_a[away_idx], minlength=n_teams)
        raw = np.where(den > constants.eps_clip, num_def_h / np.where(den > constants.eps_clip, den, 1.0), 1.0)
        def_h = _normalize_geom_mean(raw, active, constants.eps_strength)

    return att_h, def_a, att_a, def_h, n_att_h, n_def_a, n_att_a, n_def_h


class ShrinkageOnePassModel(BaseScoreModel):
    """Vectorized one-pass multiplicative Poisson shrinkage model."""

    def fit(
        self,
        dataset: EncodedDataset,
        weights: np.ndarray | None = None,
        *,
        config: ModelConfig,
        prior_means: dict[str, float] | None = None,
    ) -> FittedModelState:
        n = dataset.n_matches
        t = dataset.n_teams
        if n == 0:
            ones = np.ones(t, dtype=np.float64)
            return FittedModelState(1.0, 1.0, ones, ones.copy(), ones.copy(), ones.copy())

        w = np.ones(n, dtype=np.float64) if weights is None else weights
        w_sum = float(np.sum(w))
        g_h = soft_cap_goals_vec(dataset.ft_home, config.goal_cap, config.goal_cap_slope)
        g_a = soft_cap_goals_vec(dataset.ft_away, config.goal_cap, config.goal_cap_slope)
        lh = float(np.sum(g_h * w) / w_sum) if w_sum > 0 else 1.0
        la = float(np.sum(g_a * w) / w_sum) if w_sum > 0 else 1.0

        pm = prior_means or {}
        pm_ah = float(pm.get("attack_home", 1.0))
        pm_da = float(pm.get("defence_away", 1.0))
        pm_aa = float(pm.get("attack_away", 1.0))
        pm_dh = float(pm.get("defence_home", 1.0))

        h = dataset.home_idx
        a = dataset.away_idx
        n_h = np.bincount(h, weights=w, minlength=t)
        n_a = np.bincount(a, weights=w, minlength=t)
        s_hh = np.bincount(h, weights=g_h * w, minlength=t)
        s_ha = np.bincount(h, weights=g_a * w, minlength=t)
        s_aa = np.bincount(a, weights=g_a * w, minlength=t)
        s_ah = np.bincount(a, weights=g_h * w, minlength=t)

        k = config.k

        def _shrink(sums: np.ndarray, cnt: np.ndarray, base: float, pmean: float) -> np.ndarray:
            if base == 0.0:
                return np.full(t, pmean, dtype=np.float64)
            valid = cnt > 0
            raw = np.where(valid, (sums / np.where(valid, cnt, 1.0)) / base, pmean)
            return np.where(valid, (cnt * raw + k * pmean) / (cnt + k), pmean)

        att_h = _shrink(s_hh, n_h, lh, pm_ah)
        def_a = _shrink(s_ah, n_a, lh, pm_da)
        att_a = _shrink(s_aa, n_a, la, pm_aa)
        def_h = _shrink(s_ha, n_h, la, pm_dh)

        return FittedModelState(
            league_home=lh,
            league_away=la,
            attack_home=att_h,
            defence_home=def_h,
            attack_away=att_a,
            defence_away=def_a,
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
        if config.neutral_venue and state.league_home > 0 and state.league_away > 0:
            avg = 0.5 * (state.league_home + state.league_away)
            lh *= avg / state.league_home
            la *= avg / state.league_away
        return lh, la


class ShrinkageJointModel(BaseScoreModel):
    """Vectorized joint iterative venue strength shrinkage model."""

    def fit(
        self,
        dataset: EncodedDataset,
        weights: np.ndarray | None = None,
        *,
        config: ModelConfig,
        prior_means: dict[str, float] | None = None,
    ) -> FittedModelState:
        n = dataset.n_matches
        t = dataset.n_teams
        if n == 0:
            ones = np.ones(t, dtype=np.float64)
            return FittedModelState(1.0, 1.0, ones, ones.copy(), ones.copy(), ones.copy())

        w = np.ones(n, dtype=np.float64) if weights is None else weights
        w_sum = float(np.sum(w))
        g_h = soft_cap_goals_vec(dataset.ft_home, config.goal_cap, config.goal_cap_slope)
        g_a = soft_cap_goals_vec(dataset.ft_away, config.goal_cap, config.goal_cap_slope)
        lh = float(np.sum(g_h * w) / w_sum) if w_sum > 0 else 1.0
        la = float(np.sum(g_a * w) / w_sum) if w_sum > 0 else 1.0

        raw_ah, raw_da, raw_aa, raw_dh, n_ah, n_da, n_aa, n_dh = compute_joint_venue_strengths_vec(
            dataset.home_idx,
            dataset.away_idx,
            dataset.ft_home,
            dataset.ft_away,
            w,
            n_teams=t,
            league_home=lh,
            league_away=la,
            goal_cap=config.goal_cap,
            goal_cap_slope=config.goal_cap_slope,
        )

        pm = prior_means or {}
        k = config.k

        def _shrink_raw(raw: np.ndarray, cnt: np.ndarray, pmean: float) -> np.ndarray:
            return np.where(cnt <= 0.0, pmean, (cnt * raw + k * pmean) / (cnt + k))

        return FittedModelState(
            league_home=lh,
            league_away=la,
            attack_home=_shrink_raw(raw_ah, n_ah, float(pm.get("attack_home", 1.0))),
            defence_away=_shrink_raw(raw_da, n_da, float(pm.get("defence_away", 1.0))),
            attack_away=_shrink_raw(raw_aa, n_aa, float(pm.get("attack_away", 1.0))),
            defence_home=_shrink_raw(raw_dh, n_dh, float(pm.get("defence_home", 1.0))),
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
        if config.neutral_venue and state.league_home > 0 and state.league_away > 0:
            avg = 0.5 * (state.league_home + state.league_away)
            lh *= avg / state.league_home
            la *= avg / state.league_away
        return lh, la
