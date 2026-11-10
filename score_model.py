"""Multiplicative independent Poisson score distribution for one fixture.

Rates use same-competition season-to-date priors with shrinkage toward 1.
The grid grows until the raw tail is under the tolerance, then renormalizes.
Gemini does not choose the score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from models import Markets

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
            ],
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
    matrix, max_goals = _matrix(lambda_home, lambda_away, tail_tolerance)
    used_rho = None
    used_dependence = "independent"
    if dependence == "dixon_coles" and rho is not None:
        matrix = _apply_dixon_coles(matrix, lambda_home, lambda_away, rho)
        used_rho = float(rho)
        used_dependence = "dixon_coles"
    home_goals, away_goals, score_probability = _mode(matrix)
    probabilities = _market_probabilities(matrix)
    markets = _markets(probabilities)
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
                )
                means = _normalized_prior_means(
                    prior_means_for_fixture(
                        club_ratios,
                        home_id=str(home_team_id),
                        away_id=str(away_team_id),
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
            [(match.ft_home, weights[index]) for index, match in enumerate(priors)]
        )
        league_away = _weighted_mean(
            [(match.ft_away, weights[index]) for index, match in enumerate(priors)]
        )

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
        )
        attack_home = ratios["attack_home"].shrunk
        defence_away = ratios["defence_away"].shrunk
        attack_away = ratios["attack_away"].shrunk
        defence_home = ratios["defence_home"].shrunk
    else:
        attack_home, attack_home_detail = _ratio(
            goals=_weighted_per_game(
                [
                    (match.ft_home, weights[index])
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
                    (match.ft_home, weights[index])
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
                    (match.ft_away, weights[index])
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
                    (match.ft_away, weights[index])
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
    lambda_home = league_home * attack_home * defence_away
    lambda_away = league_away * attack_away * defence_home

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
) -> dict[str, RatioDetail]:
    """Iteratively solve venue attack/defence, then shrink the fixture's four ratios."""

    raw, counts = _joint_venue_strengths(
        priors,
        weights,
        league_home=league_home,
        league_away=league_away,
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
) -> tuple[dict[int, dict[str, float]], dict[int, dict[str, float]]]:
    """Return per-club venue strengths and effective sample sizes."""

    clubs = {match.home_id for match in priors} | {match.away_id for match in priors}
    attack_home = {club: 1.0 for club in clubs}
    defence_away = {club: 1.0 for club in clubs}
    attack_away = {club: 1.0 for club in clubs}
    defence_home = {club: 1.0 for club in clubs}
    counts = {
        club: {
            "attack_home": 0.0,
            "defence_away": 0.0,
            "attack_away": 0.0,
            "defence_home": 0.0,
        }
        for club in clubs
    }
    for match, weight in zip(priors, weights):
        counts[match.home_id]["attack_home"] += weight
        counts[match.home_id]["defence_home"] += weight
        counts[match.away_id]["attack_away"] += weight
        counts[match.away_id]["defence_away"] += weight

    league_home = max(league_home, 1e-9)
    league_away = max(league_away, 1e-9)

    for _ in range(max(1, iterations)):
        attack_home = _update_strength(
            clubs,
            numerator=lambda club: sum(
                weight * match.ft_home
                for match, weight in zip(priors, weights)
                if match.home_id == club
            ),
            denominator=lambda club: sum(
                weight * league_home * defence_away[match.away_id]
                for match, weight in zip(priors, weights)
                if match.home_id == club
            ),
        )
        defence_away = _update_strength(
            clubs,
            numerator=lambda club: sum(
                weight * match.ft_home
                for match, weight in zip(priors, weights)
                if match.away_id == club
            ),
            denominator=lambda club: sum(
                weight * league_home * attack_home[match.home_id]
                for match, weight in zip(priors, weights)
                if match.away_id == club
            ),
        )
        attack_away = _update_strength(
            clubs,
            numerator=lambda club: sum(
                weight * match.ft_away
                for match, weight in zip(priors, weights)
                if match.away_id == club
            ),
            denominator=lambda club: sum(
                weight * league_away * defence_home[match.home_id]
                for match, weight in zip(priors, weights)
                if match.away_id == club
            ),
        )
        defence_home = _update_strength(
            clubs,
            numerator=lambda club: sum(
                weight * match.ft_away
                for match, weight in zip(priors, weights)
                if match.home_id == club
            ),
            denominator=lambda club: sum(
                weight * league_away * attack_away[match.away_id]
                for match, weight in zip(priors, weights)
                if match.home_id == club
            ),
        )

    raw = {
        club: {
            "attack_home": attack_home[club],
            "defence_away": defence_away[club],
            "attack_away": attack_away[club],
            "defence_home": defence_home[club],
        }
        for club in clubs
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
) -> dict[str, dict[str, float]]:
    """Shrunk venue attack/defence ratios by club id from one finished-season sample."""

    finished = [
        match
        for match in matches
        if match.get("ft_home") is not None and match.get("ft_away") is not None
    ]
    if not finished:
        return {}
    league_home = sum(int(match["ft_home"]) for match in finished) / len(finished)
    league_away = sum(int(match["ft_away"]) for match in finished) / len(finished)
    clubs = {
        str(match["home_id"]) for match in finished
    } | {str(match["away_id"]) for match in finished}
    ratios: dict[str, dict[str, float]] = {}
    for club_id in clubs:
        attack_home, _ = _ratio(
            goals=_weighted_per_game(
                [
                    (int(match["ft_home"]), 1.0)
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
                    (int(match["ft_away"]), 1.0)
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
                    (int(match["ft_away"]), 1.0)
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
                    (int(match["ft_home"]), 1.0)
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
) -> dict[str, float]:
    """Map last-season club ratios onto the four fixture prior means."""

    home = club_ratios.get(str(home_id)) or {}
    away = club_ratios.get(str(away_id)) or {}
    return {
        "attack_home": float(home.get("attack_home", 1.0)),
        "defence_home": float(home.get("defence_home", 1.0)),
        "attack_away": float(away.get("attack_away", 1.0)),
        "defence_away": float(away.get("defence_away", 1.0)),
    }


def _weighted_per_game(items: list[tuple[int, float]]) -> tuple[float, float] | None:
    if not items:
        return None
    n_eff = sum(weight for _, weight in items)
    if n_eff <= 0:
        return None
    return sum(goals * weight for goals, weight in items) / n_eff, n_eff


def _weighted_mean(items: list[tuple[int, float]]) -> float:
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
    """Estimate ρ on earlier matches only. Returns None when the sample is too thin."""

    _ = tail_tolerance
    prepared: list[tuple[float, float, int, int, tuple[tuple[float, ...], ...]]] = []
    max_goals = 10
    for index, match in enumerate(priors):
        earlier = priors[:index]
        if len(earlier) < 6:
            continue
        lambdas = _one_pass_lambdas(
            earlier,
            home_team_id=match.home_id,
            away_team_id=match.away_id,
            k=k,
        )
        if lambdas is None:
            continue
        lambda_home, lambda_away = lambdas
        indep = tuple(
            tuple(
                _poisson(home_goals, lambda_home) * _poisson(away_goals, lambda_away)
                for away_goals in range(max_goals + 1)
            )
            for home_goals in range(max_goals + 1)
        )
        prepared.append((lambda_home, lambda_away, match.ft_home, match.ft_away, indep))
    if len(prepared) < MIN_RHO_TRAINING_MATCHES:
        return None

    best_rho = 0.0
    best_ll = float("-inf")
    for rho in RHO_GRID:
        if not _rho_is_feasible(prepared, rho):
            continue
        total = 0.0
        valid = True
        for lambda_home, lambda_away, home_goals, away_goals, indep in prepared:
            probability = _probability_from_indep(
                indep,
                home_goals,
                away_goals,
                lambda_home,
                lambda_away,
                rho,
            )
            if probability <= 0:
                valid = False
                break
            total += math.log(probability)
        if not valid:
            continue
        if total > best_ll:
            best_ll = total
            best_rho = rho
    if best_ll == float("-inf"):
        return None
    return best_rho


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
) -> dict[str, float | int | bool]:
    """Arithmetic check for D7: raw omitted mass and renormalized sums."""

    matrix, max_goals, raw_tail, hit_cap = _matrix_with_tail(
        lambda_home, lambda_away, tail_tolerance
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
) -> tuple[tuple[tuple[float, ...], ...], int]:
    matrix, max_goals, _raw_tail, _hit_cap = _matrix_with_tail(
        lambda_home, lambda_away, tail_tolerance
    )
    return matrix, max_goals


def _matrix_with_tail(
    lambda_home: float,
    lambda_away: float,
    tail_tolerance: float,
) -> tuple[tuple[tuple[float, ...], ...], int, float, bool]:
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
    hit_cap = max_goals >= MAX_GRID_GOALS and raw_tail > tail_tolerance
    raw: list[list[float]] = []
    total = 0.0
    for home_goals in range(max_goals + 1):
        row: list[float] = []
        for away_goals in range(max_goals + 1):
            probability = _poisson(home_goals, lambda_home) * _poisson(away_goals, lambda_away)
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


def _markets(probabilities: dict[str, float]) -> Markets:
    result, result_probability = _pick(
        (
            ("Home", probabilities["home"]),
            ("Draw", probabilities["draw"]),
            ("Away", probabilities["away"]),
        )
    )
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
