"""Enhanced exact score prediction with bivariate Poisson, Bayesian decision rules, and ensemble."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from models import Markets

# Import base model functions
from laboratory import LOG_LOSS_FLOOR
from score_model import (
    K,
    TAIL_TOLERANCE,
    MAX_GRID_GOALS,
    MINIMUM_HALF_TIME_SAMPLES,
    JOINT_RATIO_ITERATIONS,
    MIN_RHO_TRAINING_MATCHES,
    RHO_GRID,
    SCHEDULE_BIAS_NOTE,
    JOINT_RATIOS_NOTE,
    INDEPENDENCE_NOTE,
    DIXON_COLES_NOTE,
    RatioDetail,
    ScoreEstimate,
    _PriorMatch,
    _prior_matches,
    _merged_priors,
    _parse_priors,
    _half_time_shares,
    _club_half_time_count,
    _club_share,
    _joint_fixture_ratios,
    _joint_venue_strengths,
    _update_strength,
    _normalize_geometric_mean,
    _normalized_prior_means,
    _ratio,
    club_venue_ratios,
    prior_means_for_fixture,
    _weighted_per_game,
    _weighted_mean,
    _prior_weights,
    _age_days,
    _parse_kickoff,
    _season_start_year,
    _usable_half_time,
    _one_pass_lambdas,
    _dixon_coles_tau,
    _apply_dixon_coles,
    _score_probability,
    _fit_dixon_coles_rho,
    _probability_from_indep,
    _rho_is_feasible,
    inspect_grid,
    _matrix,
    _matrix_with_tail,
    _poisson,
    _mode,
    _market_probabilities,
    _normalize_draw_weights,
    _validate_draw_pick_params,
    _validate_tightness_params,
    _tightness_factor,
    select_1x2_outcome,
    _argmax_1x2,
    _boost_draw_scorelines,
    ranked_exact_scores,
    _markets,
    _pick,
    _winner_label,
    _percent,
    _has_full_time,
    _has_half_time,
    _is_int,
)


# ============================================================================
# NEW: Bivariate Poisson and Copula Dependence Models
# ============================================================================

def _bivariate_poisson_pmf(
    home_goals: int,
    away_goals: int,
    lambda_home: float,
    lambda_away: float,
    lambda_cov: float,
) -> float:
    """Bivariate Poisson PMF (Karlis & Ntzoufras 2003).

    X ~ Pois(lambda_home - lambda_cov), Y ~ Pois(lambda_away - lambda_cov),
    Z ~ Pois(lambda_cov), all independent.
    Then Home = X + Z, Away = Y + Z.
    """
    if lambda_cov <= 0:
        return _poisson(home_goals, lambda_home) * _poisson(away_goals, lambda_away)

    lambda1 = lambda_home - lambda_cov
    lambda2 = lambda_away - lambda_cov
    lambda3 = lambda_cov

    if lambda1 <= 0 or lambda2 <= 0:
        return _poisson(home_goals, lambda_home) * _poisson(away_goals, lambda_away)

    total = 0.0
    max_k = min(home_goals, away_goals)
    for k in range(max_k + 1):
        prob = (
            _poisson(home_goals - k, lambda1) *
            _poisson(away_goals - k, lambda2) *
            _poisson(k, lambda3)
        )
        total += prob
    return total


def _build_bivariate_matrix(
    lambda_home: float,
    lambda_away: float,
    lambda_cov: float,
    tail_tolerance: float,
) -> tuple[tuple[tuple[float, ...], ...], int]:
    """Build probability matrix using bivariate Poisson."""
    lambda_home = max(lambda_home, 0.0)
    lambda_away = max(lambda_away, 0.0)
    lambda_cov = max(lambda_cov, 0.0)

    max_goals = 0
    raw_tail = 1.0
    while True:
        home_mass = sum(_poisson(goals, lambda_home) for goals in range(max_goals + 1))
        away_mass = sum(_poisson(goals, lambda_away) for goals in range(max_goals + 1))
        raw_tail = 1.0 - (home_mass * away_mass)
        if raw_tail <= tail_tolerance or max_goals >= MAX_GRID_GOALS:
            break
        max_goals += 1

    raw: list[list[float]] = []
    total = 0.0
    for home_goals in range(max_goals + 1):
        row: list[float] = []
        for away_goals in range(max_goals + 1):
            probability = _bivariate_poisson_pmf(home_goals, away_goals, lambda_home, lambda_away, lambda_cov)
            row.append(probability)
            total += probability
        raw.append(row)

    if total <= 0:
        share = 1.0 / ((max_goals + 1) ** 2)
        matrix = tuple(tuple(share for _ in range(max_goals + 1)) for _ in range(max_goals + 1))
        return matrix, max_goals

    matrix = tuple(tuple(cell / total for cell in row) for row in raw)
    return matrix, max_goals


def _fit_bivariate_lambda_cov(
    priors: list[_PriorMatch],
    *,
    k: float,
) -> float | None:
    """Estimate covariance parameter for bivariate Poisson using method of moments / MLE."""
    if len(priors) < 20:
        return None

    # Method of moments: cov(Home, Away) ≈ lambda_cov
    home_goals = [m.ft_home for m in priors]
    away_goals = [m.ft_away for m in priors]
    n = len(priors)

    mean_home = sum(home_goals) / n
    mean_away = sum(away_goals) / n
    cov = sum((h - mean_home) * (a - mean_away) for h, a in zip(home_goals, away_goals)) / n

    # Shrink toward 0 with prior strength
    lambda_cov = max(0.0, cov)
    shrunk = (n * lambda_cov) / (n + k * 10)  # stronger shrinkage for covariance
    return min(shrunk, 0.5)  # cap at reasonable value


def _frank_copula_pmf(
    home_goals: int,
    away_goals: int,
    lambda_home: float,
    lambda_away: float,
    theta: float,
) -> float:
    """Frank copula with Poisson marginals."""
    if abs(theta) < 1e-10:
        return _poisson(home_goals, lambda_home) * _poisson(away_goals, lambda_away)

    # CDF values
    def poisson_cdf(k: int, lam: float) -> float:
        if lam <= 0:
            return 1.0 if k >= 0 else 0.0
        cdf = 0.0
        prob = math.exp(-lam)
        for i in range(k + 1):
            cdf += prob
            prob *= lam / (i + 1) if i < k else 1
        return min(1.0, max(0.0, cdf))

    u = poisson_cdf(home_goals, lambda_home)
    u_prev = poisson_cdf(home_goals - 1, lambda_home) if home_goals > 0 else 0.0
    v = poisson_cdf(away_goals, lambda_away)
    v_prev = poisson_cdf(away_goals - 1, lambda_away) if away_goals > 0 else 0.0

    # Frank copula C(u,v) = -1/theta * log(1 + (e^{-theta*u}-1)(e^{-theta*v}-1)/(e^{-theta}-1))
    def C(u_val: float, v_val: float) -> float:
        if u_val <= 0 or v_val <= 0:
            return 0.0
        if u_val >= 1 and v_val >= 1:
            return 1.0
        eu = math.exp(-theta * u_val)
        ev = math.exp(-theta * v_val)
        denom = math.exp(-theta) - 1
        if abs(denom) < 1e-12:
            return u_val * v_val
        return -math.log(1 + (eu - 1) * (ev - 1) / denom) / theta

    prob = C(u, v) - C(u_prev, v) - C(u, v_prev) + C(u_prev, v_prev)
    return max(0.0, prob)


def _fit_frank_copula_theta(
    priors: list[_PriorMatch],
) -> float | None:
    """Estimate Frank copula parameter using Kendall's tau."""
    if len(priors) < 30:
        return None

    home_goals = [m.ft_home for m in priors]
    away_goals = [m.ft_away for m in priors]
    n = len(priors)

    # Kendall's tau
    concordant = 0
    discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            sign_h = 1 if home_goals[i] > home_goals[j] else (-1 if home_goals[i] < home_goals[j] else 0)
            sign_a = 1 if away_goals[i] > away_goals[j] else (-1 if away_goals[i] < away_goals[j] else 0)
            if sign_h * sign_a > 0:
                concordant += 1
            elif sign_h * sign_a < 0:
                discordant += 1

    total_pairs = concordant + discordant
    tau = (concordant - discordant) / total_pairs if total_pairs > 0 else 0.0

    # Frank copula: tau = 1 - 4/theta * (1 - D_1(theta)) where D_1 is Debye function
    # Approximate inverse for small |tau|: theta ≈ 4*tau/(1-tau) for tau>0, theta ≈ 4*tau/(1+tau) for tau<0
    if abs(tau) < 0.01:
        return 0.0
    if tau > 0:
        theta = 4 * tau / (1 - tau + 1e-10) * 1.5  # rough approximation
    else:
        theta = 4 * tau / (1 + tau + 1e-10) * 1.5
    return max(-10.0, min(10.0, theta))  # clamp


def _build_copula_matrix(
    lambda_home: float,
    lambda_away: float,
    copula_type: str,
    copula_param: float,
    tail_tolerance: float,
) -> tuple[tuple[tuple[float, ...], ...], int]:
    """Build matrix using copula dependence."""
    lambda_home = max(lambda_home, 0.0)
    lambda_away = max(lambda_away, 0.0)

    max_goals = 0
    raw_tail = 1.0
    while True:
        home_mass = sum(_poisson(goals, lambda_home) for goals in range(max_goals + 1))
        away_mass = sum(_poisson(goals, lambda_away) for goals in range(max_goals + 1))
        raw_tail = 1.0 - (home_mass * away_mass)
        if raw_tail <= tail_tolerance or max_goals >= MAX_GRID_GOALS:
            break
        max_goals += 1

    raw: list[list[float]] = []
    total = 0.0
    for home_goals in range(max_goals + 1):
        row: list[float] = []
        for away_goals in range(max_goals + 1):
            if copula_type == "frank":
                probability = _frank_copula_pmf(home_goals, away_goals, lambda_home, lambda_away, copula_param)
            else:
                probability = _poisson(home_goals, lambda_home) * _poisson(away_goals, lambda_away)
            row.append(probability)
            total += probability
        raw.append(row)

    if total <= 0:
        share = 1.0 / ((max_goals + 1) ** 2)
        matrix = tuple(tuple(share for _ in range(max_goals + 1)) for _ in range(max_goals + 1))
        return matrix, max_goals

    matrix = tuple(tuple(cell / total for cell in row) for row in raw)
    return matrix, max_goals


# ============================================================================
# NEW: Bayesian Decision Rule for Exact Score
# ============================================================================

def _bayesian_exact_score(
    matrix: tuple[tuple[float, ...], ...],
    loss_type: str = "zero_one",
) -> tuple[int, int, float]:
    """Select exact score using Bayesian decision theory.

    loss_type:
    - "zero_one": standard 0-1 loss (equivalent to mode)
    - "absolute": absolute goal difference |home_goals - pred_home| + |away_goals - pred_away|
    - "weighted": weighted by inverse probability (penalize confident wrong predictions more)
    """
    if loss_type == "zero_one":
        return _mode(matrix)

    best_home = 0
    best_away = 0
    best_expected_loss = float("inf")

    # Precompute all cell probabilities
    cells = []
    for hg, row in enumerate(matrix):
        for ag, prob in enumerate(row):
            cells.append((hg, ag, prob))

    for pred_hg, pred_ag, _ in cells:
        expected_loss = 0.0
        for true_hg, true_ag, prob in cells:
            if loss_type == "absolute":
                loss = abs(true_hg - pred_hg) + abs(true_ag - pred_ag)
            elif loss_type == "weighted":
                loss = 1.0 / max(prob, 1e-10)
            else:
                loss = 0.0 if (true_hg == pred_hg and true_ag == pred_ag) else 1.0
            expected_loss += prob * loss

        if expected_loss < best_expected_loss:
            best_expected_loss = expected_loss
            best_home = pred_hg
            best_away = pred_ag

    best_prob = matrix[best_home][best_away] if best_home < len(matrix) and best_away < len(matrix[0]) else 0.0
    return best_home, best_away, best_prob


def _prediction_interval_scores(
    matrix: tuple[tuple[float, ...], ...],
    confidence: float = 0.8,
) -> list[tuple[int, int, float]]:
    """Return smallest set of scores covering `confidence` probability mass."""
    cells = ranked_exact_scores(matrix, limit=100)
    cumulative = 0.0
    interval = []
    for hg, ag, prob in cells:
        interval.append((hg, ag, prob))
        cumulative += prob
        if cumulative >= confidence:
            break
    return interval


# ============================================================================
# NEW: Ensemble Prediction
# ============================================================================

@dataclass(frozen=True)
class EnsembleConfig:
    """Configuration for one ensemble member."""
    name: str
    k: float = K
    ratio_mode: str = "one_pass"
    dependence: str = "independent"
    half_time_share_mode: str = "club_shrunk"
    draw_boost: float = 1.0
    draw_weights: Sequence[float] | None = None
    draw_tightness_scale: float | None = None
    decay_half_life_days: float | None = None
    prior_mean_mode: str = "one"
    weight: float = 1.0


DEFAULT_ENSEMBLE = (
    EnsembleConfig(name="baseline", dependence="independent", weight=1.0),
    EnsembleConfig(name="dixon_coles", dependence="dixon_coles", weight=1.1),
    EnsembleConfig(name="bivariate", dependence="bivariate_poisson", weight=1.3),
    EnsembleConfig(name="frank_copula", dependence="copula_frank", weight=1.2),
    EnsembleConfig(name="decay_28", decay_half_life_days=28, dependence="independent", weight=1.1),
    EnsembleConfig(name="last_season", prior_mean_mode="last_season", dependence="independent", weight=1.0),
    EnsembleConfig(name="draw_boost", draw_boost=1.15, draw_tightness_scale=1.0, dependence="independent", weight=1.1),
    EnsembleConfig(name="weighted_draws", draw_boost=1.1, draw_weights=[1.4, 1.25, 1.05, 0.7, 0.4, 0.25], dependence="independent", weight=1.15),
)


def estimate_score_ensemble(
    *,
    competition_finished_payload: dict[str, Any],
    home_finished_payload: dict[str, Any],
    away_finished_payload: dict[str, Any],
    home_team_id: int,
    away_team_id: int,
    include_half_time: bool,
    kickoff: str | None = None,
    ensemble: tuple[EnsembleConfig, ...] = DEFAULT_ENSEMBLE,
    tail_tolerance: float = TAIL_TOLERANCE,
    exact_score_method: str = "bayesian_absolute",
    confidence_interval: float = 0.8,
) -> ScoreEstimate:
    """Run ensemble of models and combine predictions."""

    estimates = []
    total_weight = 0.0

    for config in ensemble:
        try:
            if config.dependence in ("bivariate_poisson", "copula_frank"):
                # Need to handle special dependence models
                est = estimate_score_special(
                    competition_finished_payload=competition_finished_payload,
                    home_finished_payload=home_finished_payload,
                    away_finished_payload=away_finished_payload,
                    home_team_id=home_team_id,
                    away_team_id=away_team_id,
                    include_half_time=include_half_time,
                    kickoff=kickoff,
                    k=config.k,
                    tail_tolerance=tail_tolerance,
                    decay_half_life_days=config.decay_half_life_days,
                    prior_mean_mode=config.prior_mean_mode,
                    ratio_mode=config.ratio_mode,
                    dependence=config.dependence,
                    half_time_share_mode=config.half_time_share_mode,
                    draw_boost=config.draw_boost,
                    draw_weights=config.draw_weights,
                    draw_tightness_scale=config.draw_tightness_scale,
                )
            else:
                est = estimate_score(
                    competition_finished_payload=competition_finished_payload,
                    home_finished_payload=home_finished_payload,
                    away_finished_payload=away_finished_payload,
                    home_team_id=home_team_id,
                    away_team_id=away_team_id,
                    include_half_time=include_half_time,
                    kickoff=kickoff,
                    k=config.k,
                    tail_tolerance=tail_tolerance,
                    decay_half_life_days=config.decay_half_life_days,
                    prior_mean_mode=config.prior_mean_mode,
                    ratio_mode=config.ratio_mode,
                    dependence=config.dependence,
                    half_time_share_mode=config.half_time_share_mode,
                    draw_boost=config.draw_boost,
                    draw_weights=config.draw_weights,
                    draw_tightness_scale=config.draw_tightness_scale,
                )
            estimates.append((est, config.weight))
            total_weight += config.weight
        except Exception:
            continue

    if not estimates:
        # Fallback to baseline
        return estimate_score(
            competition_finished_payload=competition_finished_payload,
            home_finished_payload=home_finished_payload,
            away_finished_payload=away_finished_payload,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            include_half_time=include_half_time,
            kickoff=kickoff,
        )

    # Combine matrices with weights
    combined_matrix = _combine_matrices(estimates)

    # Select exact score using Bayesian decision rule
    home_goals, away_goals, score_prob = _bayesian_exact_score(combined_matrix, exact_score_method)

    # Calculate market probabilities from combined matrix
    probabilities = _market_probabilities(combined_matrix)
    markets = _markets(probabilities)

    # Get prediction interval
    pred_interval = _prediction_interval_scores(combined_matrix, confidence_interval)

    # Use first estimate for metadata
    base_est = estimates[0][0]

    return ScoreEstimate(
        lambda_home=base_est.lambda_home,
        lambda_away=base_est.lambda_away,
        full_time_score=f"{home_goals}-{away_goals}",
        half_time_score=base_est.half_time_score,
        half_time_prediction=base_est.half_time_prediction,
        confidence_percentage=_percent(score_prob),
        markets=markets,
        probabilities=probabilities,
        limitations=base_est.limitations + (f"Ensemble of {len(estimates)} models; {exact_score_method} decision rule",),
        matrix=combined_matrix,
        k=base_est.k,
        tail_tolerance=base_est.tail_tolerance,
        dependence=base_est.dependence,
        league_home_avg=base_est.league_home_avg,
        league_away_avg=base_est.league_away_avg,
        ratios=base_est.ratios,
        half_time_share_home=base_est.half_time_share_home,
        half_time_share_away=base_est.half_time_share_away,
        half_time_league_share=base_est.half_time_league_share,
        half_time_probabilities=base_est.half_time_probabilities,
        prior_match_count=base_est.prior_match_count,
        max_goals=base_est.max_goals,
        prior_mean_mode=base_est.prior_mean_mode,
        decay_half_life_days=base_est.decay_half_life_days,
        ratio_mode=base_est.ratio_mode,
        rho=base_est.rho,
        half_time_share_mode=base_est.half_time_share_mode,
        draw_boost=base_est.draw_boost,
        draw_weights=base_est.draw_weights,
        draw_pick_threshold=base_est.draw_pick_threshold,
        draw_pick_margin=base_est.draw_pick_margin,
        draw_tightness_scale=base_est.draw_tightness_scale,
        draw_tightness_ref=base_est.draw_tightness_ref,
        effective_draw_boost=base_est.effective_draw_boost,
    )


def _combine_matrices(estimates: list[tuple[ScoreEstimate, float]]) -> tuple[tuple[float, ...], ...]:
    """Weighted average of probability matrices."""
    if not estimates:
        return ((1.0,),)

    total_weight = sum(w for _, w in estimates)
    max_goals = max(len(e.matrix) for e, _ in estimates) - 1

    combined = [[0.0] * (max_goals + 1) for _ in range(max_goals + 1)]

    for est, weight in estimates:
        w = weight / total_weight
        for hg, row in enumerate(est.matrix):
            for ag, prob in enumerate(row):
                if hg <= max_goals and ag <= max_goals:
                    combined[hg][ag] += w * prob

    # Renormalize
    total = sum(sum(row) for row in combined)
    if total > 0:
        combined = [[cell / total for cell in row] for row in combined]

    return tuple(tuple(row) for row in combined)


def estimate_score_special(
    *,
    competition_finished_payload: dict[str, Any],
    home_finished_payload: dict[str, Any],
    away_finished_payload: dict[str, Any],
    home_team_id: int,
    away_team_id: int,
    include_half_time: bool,
    kickoff: str | None = None,
    k: float = K,
    tail_tolerance: float = TAIL_TOLERANCE,
    decay_half_life_days: float | None = None,
    prior_mean_mode: str = "one",
    ratio_mode: str = "one_pass",
    dependence: str = "independent",
    half_time_share_mode: str = "club_shrunk",
    draw_boost: float = 1.0,
    draw_weights: Sequence[float] | None = None,
    draw_tightness_scale: float | None = None,
) -> ScoreEstimate:
    """Estimate score with special dependence models (bivariate Poisson, copula)."""

    # Reuse the lambda estimation from base model
    if k < 0 or not math.isfinite(k):
        raise ValueError("k must be a finite non-negative number.")

    all_priors, prior_notes = _prior_matches(
        competition_finished_payload,
        home_finished_payload,
        away_finished_payload,
        kickoff=kickoff,
    )
    limitations = list(prior_notes)

    priors = all_priors
    weights = _prior_weights(priors, kickoff=kickoff, half_life_days=decay_half_life_days)

    if not priors:
        limitations.append("No prior finished league matches available; rates fall back to league averages of 1.0.")
        league_home = 1.0
        league_away = 1.0
    else:
        league_home = _weighted_mean([(m.ft_home, weights[i]) for i, m in enumerate(priors)])
        league_away = _weighted_mean([(m.ft_away, weights[i]) for i, m in enumerate(priors)])

    # Get ratios (reuse existing logic)
    if ratio_mode == "joint" and priors:
        ratios = _joint_fixture_ratios(
            priors, weights, league_home=league_home, league_away=league_away,
            home_team_id=home_team_id, away_team_id=away_team_id, k=k,
            prior_means=_normalized_prior_means(None),
        )
        attack_home = ratios["attack_home"].shrunk
        defence_away = ratios["defence_away"].shrunk
        attack_away = ratios["attack_away"].shrunk
        defence_home = ratios["defence_home"].shrunk
    else:
        means = _normalized_prior_means(None)
        attack_home, _ = _ratio(
            goals=_weighted_per_game([(m.ft_home, weights[i]) for i, m in enumerate(priors) if m.home_id == home_team_id]),
            baseline=league_home, k=k, prior_mean=means["attack_home"],
        )
        defence_away, _ = _ratio(
            goals=_weighted_per_game([(m.ft_home, weights[i]) for i, m in enumerate(priors) if m.away_id == away_team_id]),
            baseline=league_home, k=k, prior_mean=means["defence_away"],
        )
        attack_away, _ = _ratio(
            goals=_weighted_per_game([(m.ft_away, weights[i]) for i, m in enumerate(priors) if m.away_id == away_team_id]),
            baseline=league_away, k=k, prior_mean=means["attack_away"],
        )
        defence_home, _ = _ratio(
            goals=_weighted_per_game([(m.ft_away, weights[i]) for i, m in enumerate(priors) if m.home_id == home_team_id]),
            baseline=league_away, k=k, prior_mean=means["defence_home"],
        )
        ratios = {
            "attack_home": RatioDetail(n=1.0, raw=attack_home, shrunk=attack_home),
            "defence_away": RatioDetail(n=1.0, raw=defence_away, shrunk=defence_away),
            "attack_away": RatioDetail(n=1.0, raw=attack_away, shrunk=attack_away),
            "defence_home": RatioDetail(n=1.0, raw=defence_home, shrunk=defence_home),
        }

    lambda_home = league_home * attack_home * defence_away
    lambda_away = league_away * attack_away * defence_home

    # Build matrix based on dependence model
    if dependence == "bivariate_poisson":
        lambda_cov = _fit_bivariate_lambda_cov(priors, k=k) or 0.0
        matrix, max_goals = _build_bivariate_matrix(lambda_home, lambda_away, lambda_cov, tail_tolerance)
        used_dependence = "bivariate_poisson"
        rho = None
    elif dependence == "copula_frank":
        theta = _fit_frank_copula_theta(priors) or 0.0
        matrix, max_goals = _build_copula_matrix(lambda_home, lambda_away, "frank", theta, tail_tolerance)
        used_dependence = "copula_frank"
        rho = None
    else:
        matrix, max_goals = _matrix(lambda_home, lambda_away, tail_tolerance)
        used_dependence = "independent"
        rho = None

    # Apply draw boost
    normalized_weights = _normalize_draw_weights(draw_weights)
    _validate_draw_pick_params(None, None)
    tightness_ref = _validate_tightness_params(draw_tightness_scale, 1.0)
    tightness = _tightness_factor(lambda_home, lambda_away, tightness_scale=draw_tightness_scale, tightness_ref=tightness_ref)
    effective_boost = 1.0 + (float(draw_boost) - 1.0) * tightness
    matrix = _boost_draw_scorelines(matrix, float(draw_boost), draw_weights=normalized_weights, tightness_factor=tightness)

    # Bayesian exact score selection
    home_goals, away_goals, score_prob = _bayesian_exact_score(matrix, "absolute")

    probabilities = _market_probabilities(matrix)
    markets = _markets(probabilities)

    # Half-time
    share_home = 0.45
    share_away = 0.45
    league_share = None
    price_half_time = include_half_time
    if include_half_time:
        share_home, share_away, league_share, ht_notes = _half_time_shares(
            priors, weights, home_team_id=home_team_id, away_team_id=away_team_id, k=k, share_mode=half_time_share_mode
        )
        limitations.extend(ht_notes)
        if league_share is None:
            price_half_time = False

    half_score = None
    half_label = None
    half_probabilities = None
    if price_half_time:
        half_matrix, _ = _matrix(lambda_home * share_home, lambda_away * share_away, tail_tolerance)
        half_home, half_away, _ = _mode(half_matrix)
        half_score = f"{half_home}-{half_away}"
        half_label = _winner_label(half_home, half_away)
        half_markets = _market_probabilities(half_matrix)
        half_probabilities = {"home": half_markets["home"], "draw": half_markets["draw"], "away": half_markets["away"]}

    return ScoreEstimate(
        lambda_home=lambda_home,
        lambda_away=lambda_away,
        full_time_score=f"{home_goals}-{away_goals}",
        half_time_score=half_score,
        half_time_prediction=half_label,
        confidence_percentage=_percent(score_prob),
        markets=markets,
        probabilities=probabilities,
        limitations=tuple(limitations),
        matrix=matrix,
        k=k,
        tail_tolerance=tail_tolerance,
        dependence=used_dependence,
        league_home_avg=league_home,
        league_away_avg=league_away,
        ratios=dict(ratios),
        half_time_share_home=share_home if price_half_time else None,
        half_time_share_away=share_away if price_half_time else None,
        half_time_league_share=league_share if price_half_time else None,
        half_time_probabilities=half_probabilities,
        prior_match_count=len(priors),
        max_goals=max_goals,
        prior_mean_mode=prior_mean_mode,
        decay_half_life_days=decay_half_life_days,
        ratio_mode=ratio_mode,
        rho=rho,
        half_time_share_mode=half_time_share_mode,
        draw_boost=float(draw_boost),
        draw_weights=normalized_weights,
        draw_pick_threshold=None,
        draw_pick_margin=None,
        draw_tightness_scale=draw_tightness_scale,
        draw_tightness_ref=1.0,
        effective_draw_boost=float(effective_boost),
    )


def _get_1x2_pred(matrix):
    """Get 1X2 prediction from matrix."""
    home = draw = away = 0.0
    for hg, row in enumerate(matrix):
        for ag, prob in enumerate(row):
            if hg > ag:
                home += prob
            elif hg < ag:
                away += prob
            else:
                draw += prob
    if home >= draw and home >= away:
        return "home"
    elif draw >= away:
        return "draw"
    return "away"


def _get_1x2_label(hg, ag):
    if hg > ag:
        return "home"
    elif hg == ag:
        return "draw"
    return "away"