"""Multiplicative independent Poisson score distribution for one fixture.

Rates use same-competition season-to-date priors with shrinkage toward 1.
The grid grows until the raw tail is under the tolerance, then renormalizes.
Gemini does not choose the score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from core.models import Markets

K = 6.0
TAIL_TOLERANCE = 0.001
MAX_GRID_GOALS = 25
MINIMUM_HALF_TIME_SAMPLES = 3
JOINT_RATIO_ITERATIONS = 25
MIN_RHO_TRAINING_MATCHES = 30
RHO_GRID = tuple(round(-0.20 + 0.01 * step, 2) for step in range(0, 36))
SCHEDULE_BIAS_NOTE = "One-pass ratios keep schedule bias."
JOINT_RATIOS_NOTE = "Attack and defence are solved jointly to reduce schedule bias."
INDEPENDENCE_NOTE = "Home and away goals are independent given the rates."
DIXON_COLES_NOTE = "Low-score cells use a Dixon-Coles factor with ρ fit on earlier matches."


@dataclass(frozen=True)
class RatioDetail:
    n: float
    raw: float
    shrunk: float


@dataclass(frozen=True)
class ScoreEstimate:
    """One computed scoreline and the markets summed from its grid."""

    lambda_home: float
    lambda_away: float
    full_time_score: str
    half_time_score: str | None
    half_time_prediction: str | None
    confidence_percentage: int
    markets: Markets
    probabilities: dict[str, float]
    limitations: tuple[str, ...]
    matrix: tuple[tuple[float, ...], ...]
    k: float = K
    tail_tolerance: float = TAIL_TOLERANCE
    dependence: str = "independent"
    league_home_avg: float = 0.0
    league_away_avg: float = 0.0
    ratios: dict[str, RatioDetail] = field(default_factory=dict)
    half_time_share_home: float | None = None
    half_time_share_away: float | None = None
    half_time_league_share: float | None = None
    half_time_probabilities: dict[str, float] | None = None
    prior_match_count: int = 0
    max_goals: int = 0
    prior_mean_mode: str = "one"
    decay_half_life_days: float | None = None
    ratio_mode: str = "one_pass"
    rho: float | None = None
    half_time_share_mode: str = "club_shrunk"
    draw_boost: float = 1.0
    draw_weights: tuple[float, ...] | None = None
    draw_pick_threshold: float | None = None
    draw_pick_margin: float | None = None
    draw_tightness_scale: float | None = None
    draw_tightness_ref: float = 1.0
    effective_draw_boost: float = 1.0
    goal_cap: float | None = None
    goal_cap_slope: float = 0.5
    nb_alpha: float = 0.0
    promoted_attack_prior: float | None = None
    promoted_defence_prior: float | None = None
    venue_k: float | None = None
    short_rest_days: int | None = None
    rest_attack_factor: float = 0.94
    rest_defence_factor: float = 1.05
    target_kickoff: str | None = None
    standings_blend: float = 0.0
    standings_min_games: int = 6
    stakes_blend: float = 0.0
    stakes_min_games: int = 15

    def probability(self, home_goals: int, away_goals: int) -> float:
        if home_goals >= len(self.matrix) or away_goals >= len(self.matrix[0]):
            return 0.0
        return self.matrix[home_goals][away_goals]

    def public(self) -> dict[str, Any]:
        return {
            "lambda_home": round(self.lambda_home, 4),
            "lambda_away": round(self.lambda_away, 4),
            "full_time_score": self.full_time_score,
            "half_time_score": self.half_time_score,
            "half_time_prediction": self.half_time_prediction,
            "half_time_probabilities": (
                None
                if self.half_time_probabilities is None
                else {
                    key: round(value, 4)
                    for key, value in self.half_time_probabilities.items()
                }
            ),
            "confidence_percentage": self.confidence_percentage,
            "markets": {
                "result_1x2": self.markets.result_1x2,
                "btts": self.markets.btts,
                "over_under_25": self.markets.over_under_25,
                "result_probability": self.markets.result_probability,
                "btts_probability": self.markets.btts_probability,
                "over_under_probability": self.markets.over_under_probability,
            },
            "probabilities": {key: round(value, 4) for key, value in self.probabilities.items()},
            "limitations": list(self.limitations),
            "k": self.k,
            "tail_tolerance": self.tail_tolerance,
            "dependence": self.dependence,
            "rho": None if self.rho is None else round(self.rho, 4),
            "league_home_avg": round(self.league_home_avg, 4),
            "league_away_avg": round(self.league_away_avg, 4),
            "ratios": {
                name: {
                    "n": round(detail.n, 4),
                    "raw": round(detail.raw, 4),
                    "shrunk": round(detail.shrunk, 4),
                }
                for name, detail in self.ratios.items()
            },
            "half_time_share_home": (
                None
                if self.half_time_share_home is None
                else round(self.half_time_share_home, 4)
            ),
            "half_time_share_away": (
                None
                if self.half_time_share_away is None
                else round(self.half_time_share_away, 4)
            ),
            "half_time_league_share": (
                None
                if self.half_time_league_share is None
                else round(self.half_time_league_share, 4)
            ),
            "prior_match_count": self.prior_match_count,
            "max_goals": self.max_goals,
            "assumptions": [
                JOINT_RATIOS_NOTE if self.ratio_mode == "joint" else SCHEDULE_BIAS_NOTE,
                (
                    DIXON_COLES_NOTE
                    if self.dependence == "dixon_coles"
                    else INDEPENDENCE_NOTE
                ),
                f"Grid tail tolerance is {self.tail_tolerance}.",
                f"Shrinkage prior strength k={self.k}.",
                (
                    "Season-to-date sample; shrinkage prior means use last season's "
                    "shrunk club ratios when available."
                    if self.prior_mean_mode == "last_season"
                    else "Shrinkage prior mean is 1.0."
                ),
                *(
                    [f"Dixon-Coles ρ={self.rho:.4f}."]
                    if self.dependence == "dixon_coles" and self.rho is not None
                    else []
                ),
                *(
                    [
                        f"Season-to-date priors use exponential decay with half-life "
                        f"{self.decay_half_life_days:g} days."
                    ]
                    if self.decay_half_life_days is not None
                    else []
                ),
                (
                    "Half-time rates use a fixed first-half share of 0.45 for both clubs."
                    if self.half_time_share_mode == "fixed_0_45"
                    else "Half-time rates use a fixed first-half share of 0.50 for both clubs."
                    if self.half_time_share_mode == "fixed_0_50"
                    else "Half-time rates use one league first-half share for both clubs."
                    if self.half_time_share_mode == "league_only"
                    else "Half-time rates use club shares shrunk toward the league share."
                ),
                *(
                    [f"Draw scorelines are boosted by ×{self.draw_boost:g} before markets."]
                    if abs(self.draw_boost - 1.0) > 1e-9
                    else []
                ),
                *(
                    [
                        "Draw boost uses scoreline weights "
                        f"{list(self.draw_weights)} on the diagonal."
                    ]
                    if self.draw_weights
                    else []
                ),
                *(
                    [
                        "Draw boost decays with match tightness "
                        f"(scale={self.draw_tightness_scale:g}, "
                        f"ref={self.draw_tightness_ref:g}; "
                        f"effective×{self.effective_draw_boost:g})."
                    ]
                    if self.draw_tightness_scale is not None
                    and abs(self.effective_draw_boost - self.draw_boost) > 1e-9
                    else []
                ),
                *(
                    [
                        "1X2 draw picks use threshold/margin "
                        f"(thr={self.draw_pick_threshold}, "
                        f"margin={self.draw_pick_margin})."
                    ]
                    if self.draw_pick_threshold is not None
                    or self.draw_pick_margin is not None
                    else []
                ),
                *(
                    [
                        f"Prior-match goals use soft cap={self.goal_cap:g} "
                        f"(slope={self.goal_cap_slope:g})."
                    ]
                    if self.goal_cap is not None
                    else []
                ),
                *(
                    [
                        f"Score marginals use Negative Binomial overdispersion "
                        f"nb_alpha={self.nb_alpha:g}."
                    ]
                    if self.nb_alpha > 0
                    else []
                ),
                *(
                    [
                        "Promoted-team prior fallback uses "
                        f"attack={self.promoted_attack_prior}, "
                        f"defence={self.promoted_defence_prior}."
                    ]
                    if self.promoted_attack_prior is not None
                    or self.promoted_defence_prior is not None
                    else []
                ),
            ],
            "draw_boost": round(self.draw_boost, 4),
            "effective_draw_boost": round(self.effective_draw_boost, 4),
            "draw_weights": (
                None if self.draw_weights is None else [round(w, 4) for w in self.draw_weights]
            ),
            "draw_pick_threshold": self.draw_pick_threshold,
            "draw_pick_margin": self.draw_pick_margin,
            "draw_tightness_scale": self.draw_tightness_scale,
            "draw_tightness_ref": self.draw_tightness_ref,
        }


def estimate_from_lambdas(
    lambda_home: float,
    lambda_away: float,
    *,
    include_half_time: bool = False,
    half_time_share_home: float = 0.45,
    half_time_share_away: float = 0.45,
    limitations: list[str] | None = None,
    tail_tolerance: float = TAIL_TOLERANCE,
    k: float = K,
    league_home_avg: float = 0.0,
    league_away_avg: float = 0.0,
    ratios: dict[str, RatioDetail] | None = None,
    half_time_league_share: float | None = None,
    prior_match_count: int = 0,
    prior_mean_mode: str = "one",
    decay_half_life_days: float | None = None,
    ratio_mode: str = "one_pass",
    dependence: str = "independent",
    rho: float | None = None,
    half_time_share_mode: str = "club_shrunk",
    draw_boost: float = 1.0,
    draw_weights: Sequence[float] | None = None,
    draw_pick_threshold: float | None = None,
    draw_pick_margin: float | None = None,
    draw_tightness_scale: float | None = None,
    draw_tightness_ref: float = 1.0,
    nb_alpha: float = 0.0,
    goal_cap: float | None = None,
    goal_cap_slope: float = 0.5,
    promoted_attack_prior: float | None = None,
    promoted_defence_prior: float | None = None,
    venue_k: float | None = None,
    short_rest_days: int | None = None,
    rest_attack_factor: float = 0.94,
    rest_defence_factor: float = 1.05,
    target_kickoff: str | None = None,
    standings_blend: float = 0.0,
    standings_min_games: int = 6,
    standings_full_games: float = 19.0,
    stakes_blend: float = 0.0,
    stakes_min_games: int = 15,
    neutral_venue: bool = False,
) -> ScoreEstimate:
    """Price a full-time grid, and a half-time grid when that market is eligible."""

    if dependence not in {"independent", "dixon_coles"}:
        raise ValueError("dependence must be 'independent' or 'dixon_coles'.")
    if half_time_share_mode not in {
        "club_shrunk",
        "league_only",
        "fixed_0_45",
        "fixed_0_50",
    }:
        raise ValueError(
            "half_time_share_mode must be 'club_shrunk', 'league_only', "
            "'fixed_0_45', or 'fixed_0_50'."
        )
    if draw_boost <= 0 or not math.isfinite(draw_boost):
        raise ValueError("draw_boost must be a finite positive number.")
    nb_alpha_val = _validate_nb_alpha(nb_alpha)
    normalized_weights = _normalize_draw_weights(draw_weights)
    _validate_draw_pick_params(draw_pick_threshold, draw_pick_margin)
    tightness_ref = _validate_tightness_params(draw_tightness_scale, draw_tightness_ref)
    matrix, max_goals = _matrix(lambda_home, lambda_away, tail_tolerance, nb_alpha=nb_alpha_val)
    used_rho = None
    used_dependence = "independent"
    if dependence == "dixon_coles" and rho is not None:
        matrix = _apply_dixon_coles(matrix, lambda_home, lambda_away, rho)
        used_rho = float(rho)
        used_dependence = "dixon_coles"
    tightness = _tightness_factor(
        lambda_home,
        lambda_away,
        tightness_scale=draw_tightness_scale,
        tightness_ref=tightness_ref,
    )
    # Report scalar boost after tightness; per-cell weights still apply inside.
    effective_boost = 1.0 + (float(draw_boost) - 1.0) * tightness
    matrix = _boost_draw_scorelines(
        matrix,
        float(draw_boost),
        draw_weights=normalized_weights,
        tightness_factor=tightness,
    )
    home_goals, away_goals, score_probability = _mode(matrix)
    probabilities = _market_probabilities(matrix)
    markets = _markets(
        probabilities,
        draw_pick_threshold=draw_pick_threshold,
        draw_pick_margin=draw_pick_margin,
    )
    half_score: str | None = None
    half_label: str | None = None
    half_probabilities: dict[str, float] | None = None
    share_home = half_time_share_home if include_half_time else None
    share_away = half_time_share_away if include_half_time else None
    if include_half_time:
        half_matrix, _half_max = _matrix(
            lambda_home * half_time_share_home,
            lambda_away * half_time_share_away,
            tail_tolerance,
            nb_alpha=nb_alpha_val,
        )
        half_home, half_away, _half_probability = _mode(half_matrix)
        half_score = f"{half_home}-{half_away}"
        half_label = _winner_label(half_home, half_away)
        half_markets = _market_probabilities(half_matrix)
        half_probabilities = {
            "home": half_markets["home"],
            "draw": half_markets["draw"],
            "away": half_markets["away"],
        }
    return ScoreEstimate(
        lambda_home=lambda_home,
        lambda_away=lambda_away,
        full_time_score=f"{home_goals}-{away_goals}",
        half_time_score=half_score,
        half_time_prediction=half_label,
        confidence_percentage=_percent(score_probability),
        markets=markets,
        probabilities=probabilities,
        limitations=tuple(limitations or ()),
        matrix=matrix,
        k=k,
        tail_tolerance=tail_tolerance,
        dependence=used_dependence,
        league_home_avg=league_home_avg,
        league_away_avg=league_away_avg,
        ratios=dict(ratios or {}),
        half_time_share_home=share_home,
        half_time_share_away=share_away,
        half_time_league_share=half_time_league_share if include_half_time else None,
        half_time_probabilities=half_probabilities,
        prior_match_count=prior_match_count,
        max_goals=max_goals,
        prior_mean_mode=prior_mean_mode,
        decay_half_life_days=decay_half_life_days,
        ratio_mode=ratio_mode,
        rho=used_rho,
        half_time_share_mode=half_time_share_mode,
        draw_boost=float(draw_boost),
        draw_weights=normalized_weights,
        draw_pick_threshold=draw_pick_threshold,
        draw_pick_margin=draw_pick_margin,
        draw_tightness_scale=(
            None if draw_tightness_scale is None else float(draw_tightness_scale)
        ),
        draw_tightness_ref=float(tightness_ref),
        effective_draw_boost=float(effective_boost),
        goal_cap=goal_cap,
        goal_cap_slope=float(goal_cap_slope),
        nb_alpha=nb_alpha_val,
        promoted_attack_prior=promoted_attack_prior,
        promoted_defence_prior=promoted_defence_prior,
        venue_k=venue_k,
        short_rest_days=short_rest_days,
        rest_attack_factor=rest_attack_factor,
        rest_defence_factor=rest_defence_factor,
        target_kickoff=target_kickoff,
        standings_blend=standings_blend,
        standings_min_games=standings_min_games,
        stakes_blend=stakes_blend,
        stakes_min_games=stakes_min_games,
    )


def estimate_score(
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
    prior_means: dict[str, float] | None = None,
    prior_mean_mode: str = "one",
    ratio_mode: str = "one_pass",
    dependence: str = "independent",
    half_time_share_mode: str = "club_shrunk",
    draw_boost: float = 1.0,
    draw_weights: Sequence[float] | None = None,
    draw_pick_threshold: float | None = None,
    draw_pick_margin: float | None = None,
    draw_tightness_scale: float | None = None,
    draw_tightness_ref: float = 1.0,
    goal_cap: float | None = None,
    goal_cap_slope: float = 0.5,
    nb_alpha: float = 0.0,
    promoted_attack_prior: float | None = None,
    promoted_defence_prior: float | None = None,
    venue_k: float | None = None,
    short_rest_days: int | None = None,
    rest_attack_factor: float = 0.94,
    rest_defence_factor: float = 1.05,
    target_kickoff: str | None = None,
    last_kickoff_by_team: dict[int, str] | None = None,
    standings_blend: float = 0.0,
    standings_min_games: int = 6,
    standings_full_games: float = 19.0,
    stakes_blend: float = 0.0,
    stakes_min_games: int = 15,
    neutral_venue: bool = False,
) -> ScoreEstimate:
    """Expected goals from shrunk multiplicative rates, then modal scores and markets."""

    if k < 0 or not math.isfinite(k):
        raise ValueError("k must be a finite non-negative number.")
    if decay_half_life_days is not None and (
        decay_half_life_days <= 0 or not math.isfinite(decay_half_life_days)
    ):
        raise ValueError("decay_half_life_days must be a finite positive number when set.")
    if prior_mean_mode not in {"one", "last_season"}:
        raise ValueError("prior_mean_mode must be 'one' or 'last_season'.")
    if ratio_mode not in {"one_pass", "joint"}:
        raise ValueError("ratio_mode must be 'one_pass' or 'joint'.")
    if dependence not in {"independent", "dixon_coles"}:
        raise ValueError("dependence must be 'independent' or 'dixon_coles'.")
    if half_time_share_mode not in {
        "club_shrunk",
        "league_only",
        "fixed_0_45",
        "fixed_0_50",
    }:
        raise ValueError(
            "half_time_share_mode must be 'club_shrunk', 'league_only', "
            "'fixed_0_45', or 'fixed_0_50'."
        )
    if draw_boost <= 0 or not math.isfinite(draw_boost):
        raise ValueError("draw_boost must be a finite positive number.")
    cap_val, slope_val = _validate_goal_cap(goal_cap, goal_cap_slope)
    nb_alpha_val = _validate_nb_alpha(nb_alpha)
    promo_atk, promo_def = _validate_promoted_priors(
        promoted_attack_prior, promoted_defence_prior
    )
    normalized_weights = _normalize_draw_weights(draw_weights)
    _validate_draw_pick_params(draw_pick_threshold, draw_pick_margin)
    tightness_ref = _validate_tightness_params(draw_tightness_scale, draw_tightness_ref)

    all_priors, prior_notes = _prior_matches(
        competition_finished_payload,
        home_finished_payload,
        away_finished_payload,
        kickoff=kickoff,
    )
    limitations = list(prior_notes)
    means = _normalized_prior_means(prior_means)
    priors = all_priors
    if prior_mean_mode == "last_season":
        # Club histories often retain previous-season games after the competition
        # list has rolled to the new season; merge them for the prior-mean lookup.
        history_pool = _merged_priors(
            competition_finished_payload,
            home_finished_payload,
            away_finished_payload,
            kickoff=kickoff,
        )
        current_season = _season_start_year(kickoff)
        if current_season is not None:
            priors = [
                match
                for match in history_pool
                if _season_start_year(match.kickoff) == current_season
            ]
            if prior_means is None:
                previous = [
                    match
                    for match in history_pool
                    if _season_start_year(match.kickoff) == current_season - 1
                ]
                club_ratios = club_venue_ratios(
                    [
                        {
                            "home_id": match.home_id,
                            "away_id": match.away_id,
                            "ft_home": match.ft_home,
                            "ft_away": match.ft_away,
                        }
                        for match in previous
                    ],
                    k=k,
                    goal_cap=cap_val,
                    goal_cap_slope=slope_val,
                )
                means = _normalized_prior_means(
                    prior_means_for_fixture(
                        club_ratios,
                        home_id=str(home_team_id),
                        away_id=str(away_team_id),
                        promoted_attack_prior=promo_atk,
                        promoted_defence_prior=promo_def,
                    )
                )
    weights = _prior_weights(priors, kickoff=kickoff, half_life_days=decay_half_life_days)
    if not priors:
        limitations.append(
            "No prior finished league matches were available; rates fall back to league averages of 1.0."
        )
        league_home = 1.0
        league_away = 1.0
    else:
        league_home = _weighted_mean(
            [
                (_soft_cap_goals(match.ft_home, cap_val, slope_val), weights[index])
                for index, match in enumerate(priors)
            ]
        )
        league_away = _weighted_mean(
            [
                (_soft_cap_goals(match.ft_away, cap_val, slope_val), weights[index])
                for index, match in enumerate(priors)
            ]
        )

    # Pre-kickoff standings blend (optional, default 0 => no-op).
    if standings_blend and standings_blend > 0.0 and priors:
        # Filter to strictly earlier matches.
        stand_priors = priors
        if target_kickoff is not None:
            stand_priors = [m for m in priors if m.kickoff < target_kickoff]
        # tally per club
        stats: dict[int, dict[str, float]] = {}
        for m in stand_priors:
            for tid, gf, ga in ((m.home_id, m.ft_home, m.ft_away), (m.away_id, m.ft_away, m.ft_home)):
                st = stats.setdefault(tid, {"games": 0, "points": 0, "gf": 0, "ga": 0})
                st["games"] += 1
                st["gf"] += m.ft_home if tid == m.home_id else m.ft_away
                st["ga"] += m.ft_away if tid == m.home_id else m.ft_home
                if gf > ga:
                    st["points"] += 3
                elif gf == ga:
                    st["points"] += 1
        if stats:
            total_games = sum(v["games"] for v in stats.values())
            avg_ppg = (sum(v["points"] for v in stats.values()) / total_games) if total_games else 1.0
            avg_gpg = (sum(v["gf"] for v in stats.values()) / total_games) if total_games else 1.0
            if avg_ppg <= 0:
                avg_ppg = 1.0
            # blend into means for home/away clubs
            def _blend(club_id, att_key, def_key):
                st = stats.get(club_id)
                if not st or st["games"] < standings_min_games:
                    return
                ppg = st["points"] / st["games"] if st["games"] else 0.0
                ppg_ratio = ppg / avg_ppg
                ppg_ratio = max(0.65, min(1.45, ppg_ratio))
                w = standings_blend * min(1.0, st["games"] / max(1.0, standings_full_games))
                base_att = means.get(att_key, 1.0)
                base_def = means.get(def_key, 1.0)
                means[att_key] = (1.0 - w) * base_att + w * (ppg_ratio ** 0.35)
                means[def_key] = (1.0 - w) * base_def + w * (ppg_ratio ** -0.35)
            _blend(home_team_id, "attack_home", "defence_home")
            _blend(away_team_id, "attack_away", "defence_away")

    if ratio_mode == "joint" and priors:
        ratios = _joint_fixture_ratios(
            priors,
            weights,
            league_home=league_home,
            league_away=league_away,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            k=k,
            prior_means=means,
            goal_cap=cap_val,
            goal_cap_slope=slope_val,
        )
        attack_home = ratios["attack_home"].shrunk
        defence_away = ratios["defence_away"].shrunk
        attack_away = ratios["attack_away"].shrunk
        defence_home = ratios["defence_home"].shrunk
    else:
        attack_home, attack_home_detail = _ratio(
            goals=_weighted_per_game(
                [
                    (_soft_cap_goals(match.ft_home, cap_val, slope_val), weights[index])
                    for index, match in enumerate(priors)
                    if match.home_id == home_team_id
                ]
            ),
            baseline=league_home,
            k=k,
            prior_mean=means["attack_home"],
        )
        defence_away, defence_away_detail = _ratio(
            goals=_weighted_per_game(
                [
                    (_soft_cap_goals(match.ft_home, cap_val, slope_val), weights[index])
                    for index, match in enumerate(priors)
                    if match.away_id == away_team_id
                ]
            ),
            baseline=league_home,
            k=k,
            prior_mean=means["defence_away"],
        )
        attack_away, attack_away_detail = _ratio(
            goals=_weighted_per_game(
                [
                    (_soft_cap_goals(match.ft_away, cap_val, slope_val), weights[index])
                    for index, match in enumerate(priors)
                    if match.away_id == away_team_id
                ]
            ),
            baseline=league_away,
            k=k,
            prior_mean=means["attack_away"],
        )
        defence_home, defence_home_detail = _ratio(
            goals=_weighted_per_game(
                [
                    (_soft_cap_goals(match.ft_away, cap_val, slope_val), weights[index])
                    for index, match in enumerate(priors)
                    if match.home_id == home_team_id
                ]
            ),
            baseline=league_away,
            k=k,
            prior_mean=means["defence_home"],
        )
        ratios = {
            "attack_home": attack_home_detail,
            "defence_away": defence_away_detail,
            "attack_away": attack_away_detail,
            "defence_home": defence_home_detail,
        }
    # Venue-specific shrinkage (optional, default None => bit-for-bit identical).
    if venue_k is not None and venue_k > 0:
        home_venue_items_att = [
            (_soft_cap_goals(match.ft_home, cap_val, slope_val), weights[index])
            for index, match in enumerate(priors)
            if match.home_id == home_team_id
        ]
        home_venue_items_def = [
            (_soft_cap_goals(match.ft_away, cap_val, slope_val), weights[index])
            for index, match in enumerate(priors)
            if match.home_id == home_team_id
        ]
        away_venue_items_att = [
            (_soft_cap_goals(match.ft_away, cap_val, slope_val), weights[index])
            for index, match in enumerate(priors)
            if match.away_id == away_team_id
        ]
        away_venue_items_def = [
            (_soft_cap_goals(match.ft_home, cap_val, slope_val), weights[index])
            for index, match in enumerate(priors)
            if match.away_id == away_team_id
        ]
        w_home = sum(w for _, w in home_venue_items_att) if home_venue_items_att else 0.0
        w_away = sum(w for _, w in away_venue_items_att) if away_venue_items_att else 0.0

        # empirical venue ratios
        def _venue_empirical(items, baseline):
            res = _weighted_per_game(items)
            if res is None or baseline == 0:
                return 1.0
            per_game, _ = res
            return per_game / baseline

        r_home_att_venue = _venue_empirical(home_venue_items_att, league_home)
        r_home_def_venue = _venue_empirical(home_venue_items_def, league_away)
        r_away_att_venue = _venue_empirical(away_venue_items_att, league_away)
        r_away_def_venue = _venue_empirical(away_venue_items_def, league_home)

        if w_home == 0:
            home_att_venue = attack_home
            home_def_venue = defence_home
        else:
            home_att_venue = (w_home * r_home_att_venue + venue_k * attack_home) / (w_home + venue_k)
            home_def_venue = (w_home * r_home_def_venue + venue_k * defence_home) / (w_home + venue_k)
        if w_away == 0:
            away_att_venue = attack_away
            away_def_venue = defence_away
        else:
            away_att_venue = (w_away * r_away_att_venue + venue_k * attack_away) / (w_away + venue_k)
            away_def_venue = (w_away * r_away_def_venue + venue_k * defence_away) / (w_away + venue_k)

        lambda_home = league_home * home_att_venue * away_def_venue
        lambda_away = league_away * away_att_venue * home_def_venue
    else:
        lambda_home = league_home * attack_home * defence_away
        lambda_away = league_away * attack_away * defence_home

    if neutral_venue and league_home > 0 and league_away > 0:
        neutral_avg = 0.5 * (league_home + league_away)
        lambda_home = lambda_home * (neutral_avg / league_home)
        lambda_away = lambda_away * (neutral_avg / league_away)

    if stakes_blend and stakes_blend > 0.0 and priors:
        from domain.features import compute_match_stakes, encode_matches

        comp_meta = competition_finished_payload.get("competition")
        comp_code = comp_meta.get("code") if isinstance(comp_meta, dict) else None
        ds_stakes = encode_matches(priors)
        h_idx = ds_stakes.team_to_idx.get(str(home_team_id), -1)
        a_idx = ds_stakes.team_to_idx.get(str(away_team_id), -1)
        stakes = compute_match_stakes(
            ds_stakes,
            h_idx,
            a_idx,
            stakes_blend=stakes_blend,
            stakes_min_games=stakes_min_games,
            competition=comp_code,
        )
        lambda_home = lambda_home * stakes.home_factor
        lambda_away = lambda_away * stakes.away_factor
        if stakes.context_label != "early":
            limitations.append(
                f"Match stakes context ({stakes.context_label}, progress={stakes.season_progress:.0%}) adjusted expected goals."
            )

    # Fixture congestion / rest-days adjustment (optional).
    if short_rest_days is not None and target_kickoff is not None:
        target_dt = _parse_kickoff(target_kickoff)
        if target_dt is not None:
            # Determine last kickoff per team.
            last_map: dict[int, str] = {}
            if last_kickoff_by_team:
                last_map = {int(k): v for k, v in last_kickoff_by_team.items()}
            else:
                for m in priors:
                    if m.kickoff >= target_kickoff:
                        continue
                    for tid in (m.home_id, m.away_id):
                        if tid not in last_map or m.kickoff > last_map[tid]:
                            last_map[tid] = m.kickoff
            for team_id, is_home in ((home_team_id, True), (away_team_id, False)):
                last_kc = last_map.get(team_id)
                if not last_kc or last_kc >= target_kickoff:
                    continue
                last_dt = _parse_kickoff(last_kc)
                if last_dt is None:
                    continue
                rest_days = (target_dt - last_dt).total_seconds() / 86400.0
                if rest_days <= short_rest_days:
                    if is_home:
                        lambda_home = lambda_home * rest_attack_factor
                        lambda_away = lambda_away * rest_defence_factor
                    else:
                        lambda_away = lambda_away * rest_attack_factor
                        lambda_home = lambda_home * rest_defence_factor

    share_home = 0.45
    share_away = 0.45
    league_share: float | None = None
    price_half_time = include_half_time
    if include_half_time:
        share_home, share_away, league_share, ht_notes = _half_time_shares(
            priors,
            weights,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            k=k,
            share_mode=half_time_share_mode,
        )
        limitations.extend(ht_notes)
        if league_share is None:
            price_half_time = False

    rho = None
    used_dependence = "independent"
    if dependence == "dixon_coles":
        rho = _fit_dixon_coles_rho(priors, k=k, tail_tolerance=tail_tolerance)
        if rho is None:
            limitations.append(
                "Dixon-Coles ρ could not be fit on earlier matches; independence was kept."
            )
        else:
            used_dependence = "dixon_coles"

    return estimate_from_lambdas(
        lambda_home,
        lambda_away,
        include_half_time=price_half_time,
        half_time_share_home=share_home,
        half_time_share_away=share_away,
        limitations=limitations,
        tail_tolerance=tail_tolerance,
        k=k,
        league_home_avg=league_home,
        league_away_avg=league_away,
        ratios=ratios,
        half_time_league_share=league_share,
        prior_match_count=len(priors),
        prior_mean_mode=prior_mean_mode,
        decay_half_life_days=decay_half_life_days,
        ratio_mode=ratio_mode,
        dependence=used_dependence,
        rho=rho,
        half_time_share_mode=half_time_share_mode,
        draw_boost=draw_boost,
        draw_weights=normalized_weights,
        draw_pick_threshold=draw_pick_threshold,
        draw_pick_margin=draw_pick_margin,
        draw_tightness_scale=(
            None if draw_tightness_scale is None else float(draw_tightness_scale)
        ),
        draw_tightness_ref=float(tightness_ref),
        nb_alpha=nb_alpha_val,
        goal_cap=cap_val,
        goal_cap_slope=slope_val,
        promoted_attack_prior=promo_atk,
        promoted_defence_prior=promo_def,
        venue_k=venue_k,
        stakes_blend=stakes_blend,
        stakes_min_games=stakes_min_games,
    )


@dataclass(frozen=True)
class _PriorMatch:
    match_id: int | str
    kickoff: str
    home_id: int
    away_id: int
    ft_home: int
    ft_away: int
    ht_home: int | None = None
    ht_away: int | None = None


def _prior_matches(
    competition_finished_payload: dict[str, Any],
    home_finished_payload: dict[str, Any],
    away_finished_payload: dict[str, Any],
    *,
    kickoff: str | None,
) -> tuple[list[_PriorMatch], list[str]]:
    notes: list[str] = []
    parsed = _parse_priors(competition_finished_payload.get("matches") or [], kickoff=kickoff)
    if parsed:
        return parsed, notes
    notes.append(
        "Competition finished-match list was empty; rates use the two clubs' finished matches."
    )
    combined = list(home_finished_payload.get("matches") or []) + list(
        away_finished_payload.get("matches") or []
    )
    return _parse_priors(combined, kickoff=kickoff), notes


def _merged_priors(
    competition_finished_payload: dict[str, Any],
    home_finished_payload: dict[str, Any],
    away_finished_payload: dict[str, Any],
    *,
    kickoff: str | None,
) -> list[_PriorMatch]:
    combined = (
        list(competition_finished_payload.get("matches") or [])
        + list(home_finished_payload.get("matches") or [])
        + list(away_finished_payload.get("matches") or [])
    )
    return _parse_priors(combined, kickoff=kickoff)


def _parse_priors(
    raw_matches: list[Any],
    *,
    kickoff: str | None,
) -> list[_PriorMatch]:
    by_id: dict[int | str, _PriorMatch] = {}
    for raw in raw_matches:
        if not isinstance(raw, dict) or raw.get("status") != "FINISHED":
            continue
        if not _has_full_time(raw):
            continue
        utc = raw.get("utcDate") or ""
        if kickoff and utc and utc >= kickoff:
            continue
        home = raw.get("homeTeam") or {}
        away = raw.get("awayTeam") or {}
        home_id = home.get("id")
        away_id = away.get("id")
        if not isinstance(home_id, int) or not isinstance(away_id, int):
            continue
        full_time = raw["score"]["fullTime"]
        ht_home = ht_away = None
        if _has_half_time(raw):
            half_time = raw["score"]["halfTime"]
            if half_time["home"] <= full_time["home"] and half_time["away"] <= full_time["away"]:
                ht_home = half_time["home"]
                ht_away = half_time["away"]
        match_id = raw.get("id")
        if match_id is None:
            match_id = f"{home_id}-{away_id}-{utc}"
        by_id[match_id] = _PriorMatch(
            match_id=match_id,
            kickoff=utc,
            home_id=home_id,
            away_id=away_id,
            ft_home=full_time["home"],
            ft_away=full_time["away"],
            ht_home=ht_home,
            ht_away=ht_away,
        )
    priors = list(by_id.values())
    priors.sort(key=lambda match: match.kickoff)
    return priors


def _half_time_shares(
    priors: list[_PriorMatch],
    weights: list[float],
    *,
    home_team_id: int,
    away_team_id: int,
    k: float,
    share_mode: str = "club_shrunk",
) -> tuple[float, float, float | None, list[str]]:
    usable = [
        (match, weights[index])
        for index, match in enumerate(priors)
        if _usable_half_time(match)
    ]
    full_time_goals = sum((match.ft_home + match.ft_away) * weight for match, weight in usable)
    notes: list[str] = []
    if not usable or full_time_goals == 0:
        notes.append("No usable prior half-time sample; half-time market is skipped.")
        return 0.45, 0.45, None, notes
    league_share = (
        sum(((match.ht_home or 0) + (match.ht_away or 0)) * weight for match, weight in usable)
        / full_time_goals
    )
    home_n = _club_half_time_count(usable, home_team_id)
    away_n = _club_half_time_count(usable, away_team_id)
    if home_n < MINIMUM_HALF_TIME_SAMPLES or away_n < MINIMUM_HALF_TIME_SAMPLES:
        notes.append(
            "Half-time scores on fewer than 3 same-league finished matches for at least "
            "one club. Half-time result and half-time score are skipped."
        )
        return league_share, league_share, None, notes
    if share_mode == "league_only":
        return league_share, league_share, league_share, notes
    if share_mode == "fixed_0_45":
        return 0.45, 0.45, 0.45, notes
    if share_mode == "fixed_0_50":
        return 0.50, 0.50, 0.50, notes
    share_home, _ = _club_share(usable, home_team_id, league_share, k)
    share_away, _ = _club_share(usable, away_team_id, league_share, k)
    return share_home, share_away, league_share, notes


def _club_half_time_count(usable: list[tuple[_PriorMatch, float]], club_id: int) -> int:
    return sum(
        1
        for match, _weight in usable
        if match.home_id == club_id or match.away_id == club_id
    )


def _club_share(
    usable: list[tuple[_PriorMatch, float]],
    club_id: int,
    league_share: float,
    k: float,
) -> tuple[float, int]:
    scored_ht = 0.0
    scored_ft = 0.0
    played = 0
    n_eff = 0.0
    for match, weight in usable:
        if match.home_id == club_id:
            scored_ht += (match.ht_home or 0) * weight
            scored_ft += match.ft_home * weight
            played += 1
            n_eff += weight
        elif match.away_id == club_id:
            scored_ht += (match.ht_away or 0) * weight
            scored_ft += match.ft_away * weight
            played += 1
            n_eff += weight
    if scored_ft == 0 or n_eff == 0:
        return league_share, played
    raw = scored_ht / scored_ft
    shrunk = (n_eff * raw + k * league_share) / (n_eff + k)
    return shrunk, played


RATIO_KEYS = ("attack_home", "defence_away", "attack_away", "defence_home")


def _joint_fixture_ratios(
    priors: list[_PriorMatch],
    weights: list[float],
    *,
    league_home: float,
    league_away: float,
    home_team_id: int,
    away_team_id: int,
    k: float,
    prior_means: dict[str, float],
    goal_cap: float | None = None,
    goal_cap_slope: float = 0.5,
) -> dict[str, RatioDetail]:
    """Iteratively solve venue attack/defence, then shrink the fixture's four ratios."""

    raw, counts = _joint_venue_strengths(
        priors,
        weights,
        league_home=league_home,
        league_away=league_away,
        goal_cap=goal_cap,
        goal_cap_slope=goal_cap_slope,
    )
    mapping = {
        "attack_home": (home_team_id, "attack_home"),
        "defence_away": (away_team_id, "defence_away"),
        "attack_away": (away_team_id, "attack_away"),
        "defence_home": (home_team_id, "defence_home"),
    }
    details: dict[str, RatioDetail] = {}
    for key, (club_id, family) in mapping.items():
        n_eff = float(counts.get(club_id, {}).get(family, 0.0))
        value = float(raw.get(club_id, {}).get(family, 1.0))
        if n_eff <= 0:
            details[key] = RatioDetail(n=0.0, raw=prior_means[key], shrunk=prior_means[key])
        else:
            shrunk = (n_eff * value + k * prior_means[key]) / (n_eff + k)
            details[key] = RatioDetail(n=n_eff, raw=value, shrunk=shrunk)
    return details


def _joint_venue_strengths(
    priors: list[_PriorMatch],
    weights: list[float],
    *,
    league_home: float,
    league_away: float,
    iterations: int = JOINT_RATIO_ITERATIONS,
    goal_cap: float | None = None,
    goal_cap_slope: float = 0.5,
) -> tuple[dict[int, dict[str, float]], dict[int, dict[str, float]]]:
    """Return per-club venue strengths and effective sample sizes via vectorized np.bincount."""
    import numpy as np
    from modeling.shrinkage import compute_joint_venue_strengths_vec

    if not priors:
        return {}, {}

    club_list = sorted({m.home_id for m in priors} | {m.away_id for m in priors})
    club_to_idx = {cid: idx for idx, cid in enumerate(club_list)}
    n_teams = len(club_list)

    home_idx = np.fromiter((club_to_idx[m.home_id] for m in priors), dtype=np.int32, count=len(priors))
    away_idx = np.fromiter((club_to_idx[m.away_id] for m in priors), dtype=np.int32, count=len(priors))
    ft_home = np.fromiter((m.ft_home for m in priors), dtype=np.int16, count=len(priors))
    ft_away = np.fromiter((m.ft_away for m in priors), dtype=np.int16, count=len(priors))
    w_arr = np.asarray(weights, dtype=np.float64)

    att_h, def_a, att_a, def_h, n_ah, n_da, n_aa, n_dh = compute_joint_venue_strengths_vec(
        home_idx,
        away_idx,
        ft_home,
        ft_away,
        w_arr,
        n_teams=n_teams,
        league_home=league_home,
        league_away=league_away,
        iterations=iterations,
        goal_cap=goal_cap,
        goal_cap_slope=goal_cap_slope,
    )

    raw = {
        club: {
            "attack_home": float(att_h[idx]),
            "defence_away": float(def_a[idx]),
            "attack_away": float(att_a[idx]),
            "defence_home": float(def_h[idx]),
        }
        for club, idx in club_to_idx.items()
    }
    counts = {
        club: {
            "attack_home": float(n_ah[idx]),
            "defence_away": float(n_da[idx]),
            "attack_away": float(n_aa[idx]),
            "defence_home": float(n_dh[idx]),
        }
        for club, idx in club_to_idx.items()
    }
    return raw, counts


def _update_strength(
    clubs: set[int],
    *,
    numerator: Any,
    denominator: Any,
) -> dict[int, float]:
    values: dict[int, float] = {}
    for club in clubs:
        num = float(numerator(club))
        den = float(denominator(club))
        values[club] = (num / den) if den > 1e-12 else 1.0
    return _normalize_geometric_mean(values)


def _normalize_geometric_mean(values: dict[int, float]) -> dict[int, float]:
    if not values:
        return values
    positive = [max(value, 1e-9) for value in values.values()]
    log_mean = sum(math.log(value) for value in positive) / len(positive)
    center = math.exp(log_mean)
    if center <= 0:
        return values
    return {club: max(value, 1e-9) / center for club, value in values.items()}


def _normalized_prior_means(prior_means: dict[str, float] | None) -> dict[str, float]:
    means = {key: 1.0 for key in RATIO_KEYS}
    if not prior_means:
        return means
    for key in RATIO_KEYS:
        value = float(prior_means.get(key, 1.0))
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"prior mean for {key} must be a finite positive number.")
        means[key] = value
    return means


def _ratio(
    *,
    goals: tuple[float, float] | None,
    baseline: float,
    k: float,
    prior_mean: float = 1.0,
) -> tuple[float, RatioDetail]:
    if goals is None:
        return prior_mean, RatioDetail(n=0.0, raw=prior_mean, shrunk=prior_mean)
    per_game, n_eff = goals
    if n_eff == 0 or baseline == 0:
        return prior_mean, RatioDetail(n=n_eff, raw=prior_mean, shrunk=prior_mean)
    raw = per_game / baseline
    shrunk = (n_eff * raw + k * prior_mean) / (n_eff + k)
    return shrunk, RatioDetail(n=n_eff, raw=raw, shrunk=shrunk)


def club_venue_ratios(
    matches: list[dict[str, Any]],
    *,
    k: float = K,
    goal_cap: float | None = None,
    goal_cap_slope: float = 0.5,
) -> dict[str, dict[str, float]]:
    """Shrunk venue attack/defence ratios by club id from one finished-season sample."""

    cap_val, slope_val = _validate_goal_cap(goal_cap, goal_cap_slope)
    finished = [
        match
        for match in matches
        if match.get("ft_home") is not None and match.get("ft_away") is not None
    ]
    if not finished:
        return {}
    league_home = sum(
        _soft_cap_goals(int(match["ft_home"]), cap_val, slope_val) for match in finished
    ) / len(finished)
    league_away = sum(
        _soft_cap_goals(int(match["ft_away"]), cap_val, slope_val) for match in finished
    ) / len(finished)
    clubs = {
        str(match["home_id"]) for match in finished
    } | {str(match["away_id"]) for match in finished}
    ratios: dict[str, dict[str, float]] = {}
    for club_id in clubs:
        attack_home, _ = _ratio(
            goals=_weighted_per_game(
                [
                    (_soft_cap_goals(int(match["ft_home"]), cap_val, slope_val), 1.0)
                    for match in finished
                    if str(match["home_id"]) == club_id
                ]
            ),
            baseline=league_home,
            k=k,
        )
        defence_home, _ = _ratio(
            goals=_weighted_per_game(
                [
                    (_soft_cap_goals(int(match["ft_away"]), cap_val, slope_val), 1.0)
                    for match in finished
                    if str(match["home_id"]) == club_id
                ]
            ),
            baseline=league_away,
            k=k,
        )
        attack_away, _ = _ratio(
            goals=_weighted_per_game(
                [
                    (_soft_cap_goals(int(match["ft_away"]), cap_val, slope_val), 1.0)
                    for match in finished
                    if str(match["away_id"]) == club_id
                ]
            ),
            baseline=league_away,
            k=k,
        )
        defence_away, _ = _ratio(
            goals=_weighted_per_game(
                [
                    (_soft_cap_goals(int(match["ft_home"]), cap_val, slope_val), 1.0)
                    for match in finished
                    if str(match["away_id"]) == club_id
                ]
            ),
            baseline=league_home,
            k=k,
        )
        ratios[club_id] = {
            "attack_home": attack_home,
            "defence_home": defence_home,
            "attack_away": attack_away,
            "defence_away": defence_away,
        }
    return ratios


def prior_means_for_fixture(
    club_ratios: dict[str, dict[str, float]],
    *,
    home_id: str,
    away_id: str,
    promoted_attack_prior: float | None = None,
    promoted_defence_prior: float | None = None,
) -> dict[str, float]:
    """Map last-season club ratios onto the four fixture prior means.

    When a club has no previous-season ratio (e.g. newly promoted), falls back to
    ``promoted_attack_prior`` / ``promoted_defence_prior`` if configured, else 1.0.
    """

    promo_atk, promo_def = _validate_promoted_priors(
        promoted_attack_prior, promoted_defence_prior
    )
    atk_default = 1.0 if promo_atk is None else promo_atk
    def_default = 1.0 if promo_def is None else promo_def
    home = club_ratios.get(str(home_id)) or {}
    away = club_ratios.get(str(away_id)) or {}
    return {
        "attack_home": float(home.get("attack_home", atk_default)),
        "defence_home": float(home.get("defence_home", def_default)),
        "attack_away": float(away.get("attack_away", atk_default)),
        "defence_away": float(away.get("defence_away", def_default)),
    }


def _weighted_per_game(items: list[tuple[int | float, float]]) -> tuple[float, float] | None:
    if not items:
        return None
    n_eff = sum(weight for _, weight in items)
    if n_eff <= 0:
        return None
    return sum(goals * weight for goals, weight in items) / n_eff, n_eff


def _weighted_mean(items: list[tuple[int | float, float]]) -> float:
    n_eff = sum(weight for _, weight in items)
    if n_eff <= 0:
        return 0.0
    return sum(value * weight for value, weight in items) / n_eff


def _prior_weights(
    priors: list[_PriorMatch],
    *,
    kickoff: str | None,
    half_life_days: float | None,
) -> list[float]:
    if half_life_days is None:
        return [1.0 for _ in priors]
    as_of = _parse_kickoff(kickoff) if kickoff else None
    weights: list[float] = []
    for match in priors:
        age_days = _age_days(match.kickoff, as_of)
        weights.append(0.5 ** (age_days / half_life_days))
    return weights


def _age_days(prior_kickoff: str, as_of: datetime | None) -> float:
    prior = _parse_kickoff(prior_kickoff)
    if prior is None or as_of is None:
        return 0.0
    return max(0.0, (as_of - prior).total_seconds() / 86400.0)


def _parse_kickoff(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _season_start_year(value: str | None) -> int | None:
    """Premier-League-style season label: August–July uses the August calendar year."""

    parsed = _parse_kickoff(value)
    if parsed is None:
        return None
    return parsed.year if parsed.month >= 8 else parsed.year - 1


def _usable_half_time(match: _PriorMatch) -> bool:
    if match.ht_home is None or match.ht_away is None:
        return False
    return match.ht_home <= match.ft_home and match.ht_away <= match.ft_away


def _one_pass_lambdas(
    priors: list[_PriorMatch],
    *,
    home_team_id: int,
    away_team_id: int,
    k: float,
) -> tuple[float, float] | None:
    """Season-to-date one-pass λ for a historical match while fitting ρ."""

    if not priors:
        return None
    weights = [1.0 for _ in priors]
    league_home = _weighted_mean([(match.ft_home, 1.0) for match in priors])
    league_away = _weighted_mean([(match.ft_away, 1.0) for match in priors])
    if league_home <= 0 or league_away <= 0:
        return None
    attack_home, _ = _ratio(
        goals=_weighted_per_game(
            [(match.ft_home, 1.0) for match in priors if match.home_id == home_team_id]
        ),
        baseline=league_home,
        k=k,
    )
    defence_away, _ = _ratio(
        goals=_weighted_per_game(
            [(match.ft_home, 1.0) for match in priors if match.away_id == away_team_id]
        ),
        baseline=league_home,
        k=k,
    )
    attack_away, _ = _ratio(
        goals=_weighted_per_game(
            [(match.ft_away, 1.0) for match in priors if match.away_id == away_team_id]
        ),
        baseline=league_away,
        k=k,
    )
    defence_home, _ = _ratio(
        goals=_weighted_per_game(
            [(match.ft_away, 1.0) for match in priors if match.home_id == home_team_id]
        ),
        baseline=league_away,
        k=k,
    )
    return league_home * attack_home * defence_away, league_away * attack_away * defence_home


def _dixon_coles_tau(
    home_goals: int,
    away_goals: int,
    lambda_home: float,
    lambda_away: float,
    rho: float,
) -> float:
    if home_goals == 0 and away_goals == 0:
        return 1.0 - lambda_home * lambda_away * rho
    if home_goals == 0 and away_goals == 1:
        return 1.0 + lambda_home * rho
    if home_goals == 1 and away_goals == 0:
        return 1.0 + lambda_away * rho
    if home_goals == 1 and away_goals == 1:
        return 1.0 - rho
    return 1.0


def _apply_dixon_coles(
    matrix: tuple[tuple[float, ...], ...],
    lambda_home: float,
    lambda_away: float,
    rho: float,
) -> tuple[tuple[float, ...], ...]:
    adjusted: list[list[float]] = []
    total = 0.0
    for home_goals, row in enumerate(matrix):
        new_row: list[float] = []
        for away_goals, probability in enumerate(row):
            tau = _dixon_coles_tau(home_goals, away_goals, lambda_home, lambda_away, rho)
            value = max(0.0, float(probability) * tau)
            new_row.append(value)
            total += value
        adjusted.append(new_row)
    if total <= 0:
        return matrix
    return tuple(tuple(value / total for value in row) for row in adjusted)


def _score_probability(
    home_goals: int,
    away_goals: int,
    lambda_home: float,
    lambda_away: float,
    rho: float,
    *,
    max_goals: int = 10,
) -> float:
    cells: list[list[float]] = []
    total = 0.0
    for home in range(max_goals + 1):
        row: list[float] = []
        for away in range(max_goals + 1):
            tau = _dixon_coles_tau(home, away, lambda_home, lambda_away, rho)
            value = max(0.0, tau * _poisson(home, lambda_home) * _poisson(away, lambda_away))
            row.append(value)
            total += value
        cells.append(row)
    if total <= 0:
        return 0.0
    if home_goals > max_goals or away_goals > max_goals:
        return 0.0
    return cells[home_goals][away_goals] / total


def _fit_dixon_coles_rho(
    priors: list[_PriorMatch],
    *,
    k: float,
    tail_tolerance: float,
) -> float | None:
    """Estimate ρ on earlier matches only using vectorized 4-cell closed-form NumPy evaluation."""
    _ = tail_tolerance
    if len(priors) <= 6:
        return None

    from domain.features import encode_matches
    from modeling.dixon_coles import fit_dixon_coles_rho_vec

    dataset = encode_matches(priors)
    return fit_dixon_coles_rho_vec(dataset, k=k, rho_grid=RHO_GRID)


def _probability_from_indep(
    indep: tuple[tuple[float, ...], ...],
    home_goals: int,
    away_goals: int,
    lambda_home: float,
    lambda_away: float,
    rho: float,
) -> float:
    total = 0.0
    target = 0.0
    for home, row in enumerate(indep):
        for away, probability in enumerate(row):
            tau = _dixon_coles_tau(home, away, lambda_home, lambda_away, rho)
            value = max(0.0, float(probability) * tau)
            total += value
            if home == home_goals and away == away_goals:
                target = value
    if total <= 0 or home_goals >= len(indep) or away_goals >= len(indep[0]):
        return 0.0
    return target / total


def _rho_is_feasible(
    samples: list[tuple[float, float, int, int, tuple[tuple[float, ...], ...]]]
    | list[tuple[float, float, int, int]],
    rho: float,
) -> bool:
    for sample in samples:
        lambda_home, lambda_away = sample[0], sample[1]
        for home_goals, away_goals in ((0, 0), (0, 1), (1, 0), (1, 1)):
            if _dixon_coles_tau(home_goals, away_goals, lambda_home, lambda_away, rho) <= 0:
                return False
    return True


def inspect_grid(
    lambda_home: float,
    lambda_away: float,
    *,
    tail_tolerance: float = TAIL_TOLERANCE,
    nb_alpha: float = 0.0,
) -> dict[str, float | int | bool]:
    """Arithmetic check for D7: raw omitted mass and renormalized sums."""

    matrix, max_goals, raw_tail, hit_cap = _matrix_with_tail(
        lambda_home, lambda_away, tail_tolerance, nb_alpha=nb_alpha
    )
    renormalized_sum = sum(sum(row) for row in matrix)
    markets = _market_probabilities(matrix)
    result_sum = markets["home"] + markets["draw"] + markets["away"]
    return {
        "lambda_home": float(lambda_home),
        "lambda_away": float(lambda_away),
        "max_goals": max_goals,
        "raw_tail": raw_tail,
        "hit_cap": hit_cap,
        "tail_within_tolerance": bool(raw_tail <= tail_tolerance),
        "renormalized_sum": renormalized_sum,
        "result_1x2_sum": result_sum,
    }


def _matrix(
    lambda_home: float,
    lambda_away: float,
    tail_tolerance: float,
    *,
    nb_alpha: float = 0.0,
) -> tuple[tuple[tuple[float, ...], ...], int]:
    matrix, max_goals, _raw_tail, _hit_cap = _matrix_with_tail(
        lambda_home, lambda_away, tail_tolerance, nb_alpha=nb_alpha
    )
    return matrix, max_goals


def _matrix_with_tail(
    lambda_home: float,
    lambda_away: float,
    tail_tolerance: float,
    *,
    nb_alpha: float = 0.0,
) -> tuple[tuple[tuple[float, ...], ...], int, float, bool]:
    lambda_home = max(lambda_home, 0.0)
    lambda_away = max(lambda_away, 0.0)
    alpha = _validate_nb_alpha(nb_alpha)
    max_goals = 0
    raw_tail = 1.0
    if alpha == 0.0:
        while True:
            home_mass = sum(_poisson(goals, lambda_home) for goals in range(max_goals + 1))
            away_mass = sum(_poisson(goals, lambda_away) for goals in range(max_goals + 1))
            raw_tail = 1.0 - (home_mass * away_mass)
            if raw_tail <= tail_tolerance or max_goals >= MAX_GRID_GOALS:
                break
            max_goals += 1
        home_pmf = [_poisson(goals, lambda_home) for goals in range(max_goals + 1)]
        away_pmf = [_poisson(goals, lambda_away) for goals in range(max_goals + 1)]
    else:
        home_full = _nb_pmf(MAX_GRID_GOALS, lambda_home, alpha)
        away_full = _nb_pmf(MAX_GRID_GOALS, lambda_away, alpha)
        home_mass = 0.0
        away_mass = 0.0
        while True:
            home_mass += home_full[max_goals]
            away_mass += away_full[max_goals]
            raw_tail = 1.0 - (home_mass * away_mass)
            if raw_tail <= tail_tolerance or max_goals >= MAX_GRID_GOALS:
                break
            max_goals += 1
        home_pmf = home_full[: max_goals + 1]
        away_pmf = away_full[: max_goals + 1]
    hit_cap = max_goals >= MAX_GRID_GOALS and raw_tail > tail_tolerance
    raw: list[list[float]] = []
    total = 0.0
    for home_goals in range(max_goals + 1):
        row: list[float] = []
        p_h = home_pmf[home_goals]
        for away_goals in range(max_goals + 1):
            probability = p_h * away_pmf[away_goals]
            row.append(probability)
            total += probability
        raw.append(row)
    if total <= 0:
        share = 1.0 / ((max_goals + 1) ** 2)
        matrix = tuple(tuple(share for _ in range(max_goals + 1)) for _ in range(max_goals + 1))
        return matrix, max_goals, raw_tail, hit_cap
    matrix = tuple(tuple(cell / total for cell in row) for row in raw)
    return matrix, max_goals, raw_tail, hit_cap


def _poisson(goals: int, expected: float) -> float:
    if expected == 0:
        return 1.0 if goals == 0 else 0.0
    probability = math.exp(-expected)
    for count in range(1, goals + 1):
        probability *= expected / count
    return probability


def _nb_pmf(max_goals: int, expected: float, alpha: float) -> list[float]:
    """Negative Binomial PMF (mean=λ, Var=λ+αλ²) via exact stdlib-math recurrence."""

    if expected <= 0.0:
        return [1.0 if g == 0 else 0.0 for g in range(max_goals + 1)]
    p0 = math.exp(-math.log1p(alpha * expected) / alpha)
    pmf = [p0]
    denom_base = 1.0 + alpha * expected
    for g in range(1, max_goals + 1):
        prev = pmf[-1]
        pmf.append(prev * (expected * (1.0 + alpha * (g - 1))) / (g * denom_base))
    return pmf


def _soft_cap_goals(goals: float, cap: float | None, slope: float = 0.5) -> float:
    if cap is None or goals <= cap:
        return float(goals)
    return float(cap) + float(slope) * (float(goals) - float(cap))


def _validate_goal_cap(
    goal_cap: float | None,
    goal_cap_slope: float,
) -> tuple[float | None, float]:
    cap_val: float | None = None
    if goal_cap is not None:
        cap_val = float(goal_cap)
        if not math.isfinite(cap_val) or cap_val < 0.0:
            raise ValueError("goal_cap must be a finite non-negative number when set.")
    slope_val = float(goal_cap_slope)
    if not math.isfinite(slope_val) or slope_val < 0.0:
        raise ValueError("goal_cap_slope must be a finite non-negative number.")
    return cap_val, slope_val


def _validate_nb_alpha(nb_alpha: float) -> float:
    alpha = float(nb_alpha)
    if not math.isfinite(alpha) or alpha < 0.0:
        raise ValueError("nb_alpha must be a finite non-negative number.")
    return alpha


def _validate_promoted_priors(
    promoted_attack_prior: float | None,
    promoted_defence_prior: float | None,
) -> tuple[float | None, float | None]:
    atk = None if promoted_attack_prior is None else float(promoted_attack_prior)
    if atk is not None and (not math.isfinite(atk) or atk <= 0.0):
        raise ValueError("promoted_attack_prior must be a finite positive number when set.")
    dfn = None if promoted_defence_prior is None else float(promoted_defence_prior)
    if dfn is not None and (not math.isfinite(dfn) or dfn <= 0.0):
        raise ValueError("promoted_defence_prior must be a finite positive number when set.")
    return atk, dfn


def _mode(matrix: tuple[tuple[float, ...], ...]) -> tuple[int, int, float]:
    best_home = 0
    best_away = 0
    best_probability = matrix[0][0]
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            higher = probability > best_probability + 1e-15
            tied = abs(probability - best_probability) <= 1e-15 and (
                home_goals + away_goals,
                home_goals,
            ) < (best_home + best_away, best_home)
            if higher or tied:
                best_home = home_goals
                best_away = away_goals
                best_probability = probability
    return best_home, best_away, best_probability


def _market_probabilities(matrix: tuple[tuple[float, ...], ...]) -> dict[str, float]:
    home = draw = away = btts_yes = over_25 = over_35 = 0.0
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            if home_goals > away_goals:
                home += probability
            elif home_goals < away_goals:
                away += probability
            else:
                draw += probability
            if home_goals >= 1 and away_goals >= 1:
                btts_yes += probability
            total = home_goals + away_goals
            if total >= 3:
                over_25 += probability
            if total >= 4:
                over_35 += probability
    return {
        "home": home,
        "draw": draw,
        "away": away,
        "btts_yes": btts_yes,
        "over_25": over_25,
        "over_35": over_35,
    }


def _normalize_draw_weights(
    draw_weights: Sequence[float] | None,
) -> tuple[float, ...] | None:
    if draw_weights is None:
        return None
    weights = tuple(float(value) for value in draw_weights)
    if not weights:
        return None
    if any(value <= 0 or not math.isfinite(value) for value in weights):
        raise ValueError("draw_weights must be finite positive numbers.")
    return weights


def _validate_draw_pick_params(
    draw_pick_threshold: float | None,
    draw_pick_margin: float | None,
) -> None:
    for name, value in (
        ("draw_pick_threshold", draw_pick_threshold),
        ("draw_pick_margin", draw_pick_margin),
    ):
        if value is None:
            continue
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"{name} must be a finite non-negative number.")


def _validate_tightness_params(
    draw_tightness_scale: float | None,
    draw_tightness_ref: float,
) -> float:
    if draw_tightness_scale is not None:
        scale = float(draw_tightness_scale)
        if not math.isfinite(scale) or scale < 0:
            raise ValueError("draw_tightness_scale must be a finite non-negative number.")
    ref = float(draw_tightness_ref)
    if not math.isfinite(ref) or ref <= 0:
        raise ValueError("draw_tightness_ref must be a finite positive number.")
    return ref


def _tightness_factor(
    lambda_home: float,
    lambda_away: float,
    *,
    tightness_scale: float | None,
    tightness_ref: float,
) -> float:
    """1.0 when evenly matched; decays toward 0 as |λh−λa| grows."""

    if tightness_scale is None or float(tightness_scale) <= 0:
        return 1.0
    gap = abs(float(lambda_home) - float(lambda_away))
    return math.exp(-float(tightness_scale) * gap / float(tightness_ref))


def select_1x2_outcome(
    home: float,
    draw: float,
    away: float,
    *,
    draw_pick_threshold: float | None = None,
    draw_pick_margin: float | None = None,
) -> str:
    """Choose FT 1X2 label, optionally promoting draws without changing probabilities.

    Draw is selected when both configured gates pass:
    ``P(D) >= draw_pick_threshold`` and ``max(P(H), P(A)) - P(D) <= draw_pick_margin``.
    Omitted gates are inactive (threshold defaults to 0, margin to +inf).
    """

    _validate_draw_pick_params(draw_pick_threshold, draw_pick_margin)
    default = _argmax_1x2(home, draw, away)
    if draw_pick_threshold is None and draw_pick_margin is None:
        return default
    threshold = 0.0 if draw_pick_threshold is None else float(draw_pick_threshold)
    margin = math.inf if draw_pick_margin is None else float(draw_pick_margin)
    favorite = max(float(home), float(away))
    if float(draw) >= threshold and (favorite - float(draw)) <= margin:
        return "draw"
    return default


def _argmax_1x2(home: float, draw: float, away: float) -> str:
    if home >= draw and home >= away:
        return "home"
    if draw >= away:
        return "draw"
    return "away"


def _boost_draw_scorelines(
    matrix: tuple[tuple[float, ...], ...],
    draw_boost: float,
    draw_weights: Sequence[float] | None = None,
    *,
    tightness_factor: float = 1.0,
) -> tuple[tuple[float, ...], ...]:
    """Multiply diagonal (draw) cells and renormalize.

    Per-cell multiplier is ``draw_boost * draw_weights[i]`` when weights are set and
    ``i < len(weights)``; otherwise ``draw_boost``. Match tightness then blends each
    multiplier toward 1.0: ``1 + (mult - 1) * tightness_factor``.
    """

    weights = _normalize_draw_weights(draw_weights)
    factor = float(tightness_factor)
    if not math.isfinite(factor) or factor < 0:
        raise ValueError("tightness_factor must be a finite non-negative number.")
    if weights is None and abs(draw_boost - 1.0) <= 1e-12 and abs(factor - 1.0) <= 1e-12:
        return matrix
    raw = [list(row) for row in matrix]
    size = len(raw)
    changed = False
    for index in range(size):
        if index >= len(raw[index]):
            continue
        if weights is None:
            base = float(draw_boost)
        elif index < len(weights):
            base = float(draw_boost) * float(weights[index])
        else:
            base = float(draw_boost)
        multiplier = 1.0 + (base - 1.0) * factor
        if abs(multiplier - 1.0) > 1e-12:
            changed = True
        raw[index][index] *= multiplier
    if not changed:
        return matrix
    total = sum(sum(row) for row in raw)
    if total <= 0:
        return matrix
    return tuple(tuple(cell / total for cell in row) for row in raw)


def ranked_exact_scores(
    matrix: tuple[tuple[float, ...], ...],
    *,
    limit: int = 8,
) -> list[tuple[int, int, float]]:
    """Highest-probability scorelines first. Ties keep matrix scan order."""

    cells: list[tuple[int, int, float]] = [
        (home_goals, away_goals, probability)
        for home_goals, row in enumerate(matrix)
        for away_goals, probability in enumerate(row)
    ]
    cells.sort(key=lambda item: item[2], reverse=True)
    return cells[: max(0, limit)]


def _markets(
    probabilities: dict[str, float],
    *,
    draw_pick_threshold: float | None = None,
    draw_pick_margin: float | None = None,
) -> Markets:
    label = select_1x2_outcome(
        probabilities["home"],
        probabilities["draw"],
        probabilities["away"],
        draw_pick_threshold=draw_pick_threshold,
        draw_pick_margin=draw_pick_margin,
    )
    label_map = {"home": "Home", "draw": "Draw", "away": "Away"}
    result = label_map[label]
    result_probability = {
        "Home": probabilities["home"],
        "Draw": probabilities["draw"],
        "Away": probabilities["away"],
    }[result]
    btts = "Yes" if probabilities["btts_yes"] > 0.5 else "No"
    btts_probability = probabilities["btts_yes"] if btts == "Yes" else 1.0 - probabilities["btts_yes"]
    over_under = "Over" if probabilities["over_25"] > 0.5 else "Under"
    over_under_probability = (
        probabilities["over_25"] if over_under == "Over" else 1.0 - probabilities["over_25"]
    )
    return Markets(
        result_1x2=result,
        btts=btts,
        over_under_25=over_under,
        result_probability=_percent(result_probability),
        btts_probability=_percent(btts_probability),
        over_under_probability=_percent(over_under_probability),
    )


def _pick(options: tuple[tuple[str, float], ...]) -> tuple[str, float]:
    label, probability = options[0]
    for candidate, value in options[1:]:
        if value > probability:
            label, probability = candidate, value
    return label, probability


def _winner_label(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "Team A"
    if home_goals < away_goals:
        return "Team B"
    return "Draw"


def _percent(probability: float) -> int:
    return max(0, min(100, int(round(probability * 100))))


def _has_full_time(match: dict[str, Any]) -> bool:
    full_time = (match.get("score") or {}).get("fullTime") or {}
    return _is_int(full_time.get("home")) and _is_int(full_time.get("away"))


def _has_half_time(match: dict[str, Any]) -> bool:
    half_time = (match.get("score") or {}).get("halfTime") or {}
    return _is_int(half_time.get("home")) and _is_int(half_time.get("away"))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
