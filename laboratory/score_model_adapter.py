"""Bridge laboratory snapshots to the Telegram bot score_model.

Gemini is never called. Predictions use the same multiplicative Poisson path as
`/predict`, then are reshaped for laboratory evaluation.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Any, Sequence

from laboratory import SCORE_MODEL_CONFIG_ID
from modeling.score_model import (
    K,
    MINIMUM_HALF_TIME_SAMPLES,
    ScoreEstimate,
    estimate_score,
    select_1x2_outcome,
)

MINIMUM_SCORED_MATCHES = 3
SCORE_MODEL_ENGINE = "score_model"


@dataclass(frozen=True)
class ScoreModelOutcome:
    excluded: bool
    reason: str | None
    body: dict[str, Any] | None


def price_from_snapshot(
    payload: dict[str, Any],
    *,
    k: float = K,
    config_id: str = SCORE_MODEL_CONFIG_ID,
    decay_half_life_days: float | None = None,
    prior_means: dict[str, float] | None = None,
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
    last_kickoff_by_team: dict | None = None,
    standings_blend: float = 0.0,
    standings_min_games: int = 6,
    standings_full_games: float = 19.0,
    neutral_venue: bool = False,
    min_scored_matches: int | None = None,
) -> ScoreModelOutcome:
    """Price one pre-kickoff snapshot with the bot scorer."""

    home_id = str(payload["home"]["id"])
    away_id = str(payload["away"]["id"])
    home_int = team_int_id(home_id)
    away_int = team_int_id(away_id)
    priors = list(payload.get("prior_matches") or [])
    home_n = _club_prior_count(priors, home_id)
    away_n = _club_prior_count(priors, away_id)
    min_req = MINIMUM_SCORED_MATCHES if min_scored_matches is None else int(min_scored_matches)
    if home_n < min_req or away_n < min_req:
        return ScoreModelOutcome(
            excluded=True,
            reason=(
                f"Bot abstain: need at least {min_req} prior finished matches "
                f"per club (home={home_n}, away={away_n})."
            ),
            body=None,
        )

    competition_code = str(payload.get("competition") or "PL")
    competition_payload = {
        "matches": [
            _to_fd_match(prior, competition_code=competition_code) for prior in priors
        ]
    }
    home_payload = {
        "matches": [
            _to_fd_match(prior, competition_code=competition_code)
            for prior in priors
            if prior["home_id"] == home_id or prior["away_id"] == home_id
        ]
    }
    away_payload = {
        "matches": [
            _to_fd_match(prior, competition_code=competition_code)
            for prior in priors
            if prior["home_id"] == away_id or prior["away_id"] == away_id
        ]
    }
    include_half_time = (
        _club_half_time_samples(priors, home_id) >= MINIMUM_HALF_TIME_SAMPLES
        and _club_half_time_samples(priors, away_id) >= MINIMUM_HALF_TIME_SAMPLES
    )
    estimate = estimate_score(
        competition_finished_payload=competition_payload,
        home_finished_payload=home_payload,
        away_finished_payload=away_payload,
        home_team_id=home_int,
        away_team_id=away_int,
        include_half_time=include_half_time,
        kickoff=str(payload["kickoff"]),
        k=k,
        decay_half_life_days=decay_half_life_days,
        prior_means=prior_means,
        ratio_mode=ratio_mode,
        dependence=dependence,
        half_time_share_mode=half_time_share_mode,
        draw_boost=draw_boost,
        draw_weights=draw_weights,
        draw_pick_threshold=draw_pick_threshold,
        draw_pick_margin=draw_pick_margin,
        draw_tightness_scale=draw_tightness_scale,
        draw_tightness_ref=draw_tightness_ref,
        goal_cap=goal_cap,
        goal_cap_slope=goal_cap_slope,
        nb_alpha=nb_alpha,
        promoted_attack_prior=promoted_attack_prior,
        promoted_defence_prior=promoted_defence_prior,
        venue_k=venue_k,
        short_rest_days=short_rest_days,
        rest_attack_factor=rest_attack_factor,
        rest_defence_factor=rest_defence_factor,
        target_kickoff=target_kickoff if target_kickoff is not None else str(payload.get('kickoff')),
        last_kickoff_by_team=last_kickoff_by_team,
        standings_blend=standings_blend,
        standings_min_games=standings_min_games,
        standings_full_games=standings_full_games,
        neutral_venue=neutral_venue,
    )
    return ScoreModelOutcome(
        excluded=False,
        reason=None,
        body=to_evaluable_body(
            estimate,
            include_half_time=include_half_time,
            config_id=config_id,
        ),
    )


def to_evaluable_body(
    estimate: ScoreEstimate,
    *,
    include_half_time: bool,
    config_id: str = SCORE_MODEL_CONFIG_ID,
) -> dict[str, Any]:
    """Nested market body compatible with evaluate_prediction (poisson-shaped)."""

    full_time = _distribution_block(
        estimate.matrix,
        estimate.lambda_home,
        estimate.lambda_away,
        draw_pick_threshold=estimate.draw_pick_threshold,
        draw_pick_margin=estimate.draw_pick_margin,
    )
    full_time["max_goals"] = estimate.max_goals
    if include_half_time and estimate.half_time_share_home is not None and estimate.half_time_share_away is not None:
        half_matrix = _half_matrix(estimate)
        half_time = _distribution_block(
            half_matrix,
            estimate.lambda_home * estimate.half_time_share_home,
            estimate.lambda_away * estimate.half_time_share_away,
        )
        half_time["withheld"] = False
        half_time["share_home"] = estimate.half_time_share_home
        half_time["share_away"] = estimate.half_time_share_away
        half_time["league_share"] = estimate.half_time_league_share
    else:
        half_time = {
            "withheld": True,
            "reason": "Half-time market withheld by bot coverage rules.",
        }
    public = estimate.public()
    return {
        "engine": SCORE_MODEL_ENGINE,
        "config_id": config_id,
        "k": estimate.k,
        "tail_tolerance": estimate.tail_tolerance,
        "dependence": estimate.dependence,
        "lambda_home": estimate.lambda_home,
        "lambda_away": estimate.lambda_away,
        "league_home_avg": estimate.league_home_avg,
        "league_away_avg": estimate.league_away_avg,
        "ratios": public["ratios"],
        "prior_match_count": estimate.prior_match_count,
        "full_time": full_time,
        "half_time": half_time,
        "limitations": list(estimate.limitations),
        "assumptions": public.get("assumptions") or [],
        "bot_public": public,
    }


def team_int_id(team_id: str) -> int:
    """Stable positive int id for score_model (independent of PYTHONHASHSEED)."""

    return zlib.adler32(team_id.encode("utf-8")) % (10**9 - 1) + 1


def _to_fd_match(
    prior: dict[str, Any],
    *,
    competition_code: str = "PL",
    competition_name: str | None = None,
) -> dict[str, Any]:
    home_id = team_int_id(str(prior["home_id"]))
    away_id = team_int_id(str(prior["away_id"]))
    code = competition_code or "PL"
    match: dict[str, Any] = {
        "id": prior.get("match_id"),
        "utcDate": prior["kickoff"],
        "status": "FINISHED",
        "competition": {
            "code": code,
            "name": competition_name or code,
        },
        "homeTeam": {"id": home_id, "name": prior.get("home_name") or prior["home_id"]},
        "awayTeam": {"id": away_id, "name": prior.get("away_name") or prior["away_id"]},
        "score": {
            "fullTime": {"home": int(prior["ft_home"]), "away": int(prior["ft_away"])},
            "halfTime": {
                "home": None if prior.get("ht_home") is None else int(prior["ht_home"]),
                "away": None if prior.get("ht_away") is None else int(prior["ht_away"]),
            },
        },
    }
    return match


def _club_prior_count(priors: list[dict[str, Any]], club_id: str) -> int:
    return sum(1 for prior in priors if prior["home_id"] == club_id or prior["away_id"] == club_id)


def _club_half_time_samples(priors: list[dict[str, Any]], club_id: str) -> int:
    count = 0
    for prior in priors:
        if prior["home_id"] != club_id and prior["away_id"] != club_id:
            continue
        if prior.get("ht_home") is None or prior.get("ht_away") is None:
            continue
        if prior["ht_home"] > prior["ft_home"] or prior["ht_away"] > prior["ft_away"]:
            continue
        count += 1
    return count


def _half_matrix(estimate: ScoreEstimate) -> tuple[tuple[float, ...], ...]:
    from modeling.score_model import estimate_from_lambdas

    half = estimate_from_lambdas(
        estimate.lambda_home * float(estimate.half_time_share_home or 0.0),
        estimate.lambda_away * float(estimate.half_time_share_away or 0.0),
        include_half_time=False,
        tail_tolerance=estimate.tail_tolerance,
        k=estimate.k,
        nb_alpha=estimate.nb_alpha,
    )
    return half.matrix


def _distribution_block(
    matrix: tuple[tuple[float, ...], ...],
    lambda_home: float,
    lambda_away: float,
    *,
    draw_pick_threshold: float | None = None,
    draw_pick_margin: float | None = None,
) -> dict[str, Any]:
    cells: list[dict[str, float | int]] = []
    ranked: list[tuple[float, int, int]] = []
    home = draw = away = over_25 = over_35 = 0.0
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            cells.append(
                {
                    "home_goals": home_goals,
                    "away_goals": away_goals,
                    "probability": float(probability),
                }
            )
            ranked.append((float(probability), home_goals, away_goals))
            if home_goals > away_goals:
                home += float(probability)
            elif home_goals == away_goals:
                draw += float(probability)
            else:
                away += float(probability)
            total = home_goals + away_goals
            if total >= 3:
                over_25 += float(probability)
            if total >= 4:
                over_35 += float(probability)
    ranked.sort(key=lambda item: (-item[0], item[1] + item[2], item[1]))
    mode = ranked[0]
    return {
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "max_goals": len(matrix) - 1,
        "exact_scores": cells,
        "exact_score_most_likely": {"home_goals": mode[1], "away_goals": mode[2]},
        "result_1x2": {
            "home": home,
            "draw": draw,
            "away": away,
            "most_likely": select_1x2_outcome(
                home,
                draw,
                away,
                draw_pick_threshold=draw_pick_threshold,
                draw_pick_margin=draw_pick_margin,
            ),
        },
        "over_under_2_5": {
            "over": over_25,
            "under": 1.0 - over_25,
            "most_likely": "over" if over_25 > 0.5 else "under",
        },
        "over_under_3_5": {
            "over": over_35,
            "under": 1.0 - over_35,
            "most_likely": "over" if over_35 > 0.5 else "under",
        },
    }
