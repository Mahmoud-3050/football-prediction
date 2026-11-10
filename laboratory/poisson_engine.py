"""Independent Poisson engine for EXP-PL-001.

Rates follow the multiplicative baseline. k, the tail tolerance, and the
half-time estimator are the values locked on that experiment. They are not
a claim that those values have won a walk-forward test.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from laboratory import K, TAIL_TOLERANCE


@dataclass(frozen=True)
class FinishedMatch:
    match_id: str
    season: str
    kickoff: str
    home_id: str
    away_id: str
    ft_home: int
    ft_away: int
    ht_home: int | None = None
    ht_away: int | None = None


@dataclass(frozen=True)
class PoissonOutcome:
    """A priced match, or an exclusion when league averages do not exist."""

    excluded: bool
    reason: str | None
    body: dict[str, Any] | None


def price_match(
    *,
    season: str,
    home_id: str,
    away_id: str,
    priors: list[FinishedMatch],
    k: float = K,
    tail_tolerance: float = TAIL_TOLERANCE,
) -> PoissonOutcome:
    sample = [match for match in priors if match.season == season]
    if not sample:
        return PoissonOutcome(
            excluded=True,
            reason="No prior league matches in this season, so league averages do not exist.",
            body=None,
        )
    if k < 0 or not math.isfinite(k):
        raise ValueError("k must be a finite non-negative number.")

    league_home = _mean([match.ft_home for match in sample])
    league_away = _mean([match.ft_away for match in sample])
    attack_home, attack_home_detail = _ratio(
        goals=_per_game([match.ft_home for match in sample if match.home_id == home_id]),
        baseline=league_home,
        k=k,
    )
    defence_away, defence_away_detail = _ratio(
        goals=_per_game([match.ft_home for match in sample if match.away_id == away_id]),
        baseline=league_home,
        k=k,
    )
    attack_away, attack_away_detail = _ratio(
        goals=_per_game([match.ft_away for match in sample if match.away_id == away_id]),
        baseline=league_away,
        k=k,
    )
    defence_home, defence_home_detail = _ratio(
        goals=_per_game([match.ft_away for match in sample if match.home_id == home_id]),
        baseline=league_away,
        k=k,
    )
    lambda_home = league_home * attack_home * defence_away
    lambda_away = league_away * attack_away * defence_home
    full_time = _distribution(lambda_home, lambda_away, tail_tolerance)
    half_time, half_detail = _half_time(
        sample,
        home_id=home_id,
        away_id=away_id,
        lambda_home=lambda_home,
        lambda_away=lambda_away,
        k=k,
        tail_tolerance=tail_tolerance,
    )
    body = {
        "engine": "poisson",
        "config_id": "poisson-exp-pl-001",
        "k": k,
        "tail_tolerance": tail_tolerance,
        "dependence": "independent",
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "league_home_avg": league_home,
        "league_away_avg": league_away,
        "ratios": {
            "attack_home": attack_home_detail,
            "defence_away": defence_away_detail,
            "attack_away": attack_away_detail,
            "defence_home": defence_home_detail,
        },
        "full_time": full_time,
        "half_time": half_time,
        "half_time_detail": half_detail,
        "limitations": [
            "One-pass ratios keep schedule bias.",
            "Home and away goals are independent given the rates.",
            f"Grid tail tolerance is {tail_tolerance}.",
        ],
    }
    _require_sums(body)
    return PoissonOutcome(excluded=False, reason=None, body=body)


def _half_time(
    sample: list[FinishedMatch],
    *,
    home_id: str,
    away_id: str,
    lambda_home: float,
    lambda_away: float,
    k: float,
    tail_tolerance: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    usable = [match for match in sample if _usable_half_time(match)]
    full_time_goals = sum(match.ft_home + match.ft_away for match in usable)
    if not usable or full_time_goals == 0:
        return (
            {"withheld": True, "reason": "No usable prior half-time sample."},
            {"estimator": "HT-SHARE-1", "withheld": True},
        )
    league_share = sum(match.ht_home + match.ht_away for match in usable) / full_time_goals
    share_home, home_detail = _club_share(usable, home_id, league_share, k)
    share_away, away_detail = _club_share(usable, away_id, league_share, k)
    distribution = _distribution(
        lambda_home * share_home,
        lambda_away * share_away,
        tail_tolerance,
    )
    distribution["withheld"] = False
    detail = {
        "estimator": "HT-SHARE-1",
        "withheld": False,
        "league_share": league_share,
        "share_home": home_detail,
        "share_away": away_detail,
    }
    return distribution, detail


def _club_share(
    usable: list[FinishedMatch],
    club_id: str,
    league_share: float,
    k: float,
) -> tuple[float, dict[str, float]]:
    scored_ht = 0
    scored_ft = 0
    played = 0
    for match in usable:
        if match.home_id == club_id:
            scored_ht += match.ht_home or 0
            scored_ft += match.ft_home
            played += 1
        elif match.away_id == club_id:
            scored_ht += match.ht_away or 0
            scored_ft += match.ft_away
            played += 1
    if scored_ft == 0:
        raw = league_share
        shrunk = league_share
    else:
        raw = scored_ht / scored_ft
        shrunk = (played * raw + k * league_share) / (played + k)
    return shrunk, {"n": played, "raw": raw, "shrunk": shrunk, "prior": league_share}


def _distribution(lambda_home: float, lambda_away: float, tail_tolerance: float) -> dict[str, Any]:
    if lambda_home < 0 or lambda_away < 0:
        raise ValueError("Expected goals must be non-negative.")
    if not math.isfinite(lambda_home) or not math.isfinite(lambda_away):
        raise ValueError("Expected goals must be finite.")
    max_goals = 0
    while True:
        home_mass = sum(_pmf(lambda_home, goals) for goals in range(max_goals + 1))
        away_mass = sum(_pmf(lambda_away, goals) for goals in range(max_goals + 1))
        tail = 1.0 - (home_mass * away_mass)
        if tail <= tail_tolerance or max_goals >= 25:
            break
        max_goals += 1
    cells: list[dict[str, float | int]] = []
    raw_total = 0.0
    raw: list[list[float]] = []
    for home_goals in range(max_goals + 1):
        row: list[float] = []
        for away_goals in range(max_goals + 1):
            probability = _pmf(lambda_home, home_goals) * _pmf(lambda_away, away_goals)
            row.append(probability)
            raw_total += probability
        raw.append(row)
    if raw_total <= 0:
        raise ValueError("Score grid has no probability mass.")
    ranked: list[tuple[float, int, int]] = []
    for home_goals, row in enumerate(raw):
        for away_goals, probability in enumerate(row):
            normalized = probability / raw_total
            cells.append(
                {
                    "home_goals": home_goals,
                    "away_goals": away_goals,
                    "probability": normalized,
                }
            )
            ranked.append((normalized, home_goals, away_goals))
    ranked.sort(key=lambda item: (-item[0], item[1] + item[2], item[1]))
    mode = ranked[0]
    one_x_two = _result_probabilities(cells)
    totals_2_5 = _total_probabilities(cells, line=3)
    totals_3_5 = _total_probabilities(cells, line=4)
    return {
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "max_goals": max_goals,
        "tail_before_normalization": 1.0 - raw_total,
        "exact_scores": cells,
        "exact_score_most_likely": {"home_goals": mode[1], "away_goals": mode[2]},
        "result_1x2": one_x_two,
        "over_under_2_5": totals_2_5,
        "over_under_3_5": totals_3_5,
    }


def _result_probabilities(cells: list[dict[str, float | int]]) -> dict[str, float | str]:
    home = draw = away = 0.0
    for cell in cells:
        probability = float(cell["probability"])
        home_goals = int(cell["home_goals"])
        away_goals = int(cell["away_goals"])
        if home_goals > away_goals:
            home += probability
        elif home_goals == away_goals:
            draw += probability
        else:
            away += probability
    return {
        "home": home,
        "draw": draw,
        "away": away,
        "most_likely": _argmax_three(home, draw, away),
    }


def _total_probabilities(cells: list[dict[str, float | int]], *, line: int) -> dict[str, float | str]:
    over = 0.0
    for cell in cells:
        if int(cell["home_goals"]) + int(cell["away_goals"]) >= line:
            over += float(cell["probability"])
    under = 1.0 - over
    if over > under:
        selection = "over"
    else:
        selection = "under"
    return {"over": over, "under": under, "most_likely": selection}


def _argmax_three(home: float, draw: float, away: float) -> str:
    if home >= draw and home >= away:
        return "home"
    if draw >= away:
        return "draw"
    return "away"


def _ratio(*, goals: tuple[float, int] | None, baseline: float, k: float) -> tuple[float, dict[str, float]]:
    if goals is None:
        return 1.0, {"n": 0, "raw": 1.0, "shrunk": 1.0}
    per_game, played = goals
    if played == 0 or baseline == 0:
        return 1.0, {"n": float(played), "raw": 1.0, "shrunk": 1.0}
    raw = per_game / baseline
    shrunk = (played * raw + k * 1.0) / (played + k)
    return shrunk, {"n": float(played), "raw": raw, "shrunk": shrunk}


def _per_game(goals: list[int]) -> tuple[float, int] | None:
    if not goals:
        return None
    return sum(goals) / len(goals), len(goals)


def _mean(values: list[int]) -> float:
    return sum(values) / len(values)


def _usable_half_time(match: FinishedMatch) -> bool:
    if match.ht_home is None or match.ht_away is None:
        return False
    return match.ht_home <= match.ft_home and match.ht_away <= match.ft_away


def _pmf(expected: float, goals: int) -> float:
    if expected == 0:
        return 1.0 if goals == 0 else 0.0
    probability = math.exp(-expected)
    for count in range(1, goals + 1):
        probability *= expected / count
    return probability


def _require_sums(body: dict[str, Any]) -> None:
    for market in (body["full_time"], body["half_time"]):
        if market.get("withheld"):
            continue
        total = sum(float(cell["probability"]) for cell in market["exact_scores"])
        if abs(total - 1.0) > 1e-9:
            raise ValueError("Renormalized score grid does not sum to 1.")
        result = market["result_1x2"]
        result_total = float(result["home"]) + float(result["draw"]) + float(result["away"])
        if abs(result_total - 1.0) > 1e-9:
            raise ValueError("1X2 probabilities do not sum to 1.")
