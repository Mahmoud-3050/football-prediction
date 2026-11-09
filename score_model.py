"""Dixon-Coles score distribution for one fixture.

Rates come from finished matches at the relevant venue. The season table is
only a fallback, and a missing playedGames value is never treated as zero.
Gemini does not choose the score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from models import Markets

RHO = -0.13
HOME_FACTOR = 1.25
AWAY_FACTOR = 0.85
MIN_VENUE_MATCHES = 3
MAX_GOALS = 6
DEFAULT_HALF_TIME_SHARE = 0.45
MISSING_PLAYED_GAMES = (
    "Season games played was missing; rates use the finished-match list."
)
HOME_FACTOR_NOTE = (
    "Season rates are venue-neutral, so home expected goals were multiplied by 1.25 "
    "and away expected goals by 0.85."
)


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

    def probability(self, home_goals: int, away_goals: int) -> float:
        return self.matrix[home_goals][away_goals]

    def public(self) -> dict[str, Any]:
        return {
            "lambda_home": round(self.lambda_home, 4),
            "lambda_away": round(self.lambda_away, 4),
            "full_time_score": self.full_time_score,
            "half_time_score": self.half_time_score,
            "half_time_prediction": self.half_time_prediction,
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
        }


def estimate_from_lambdas(
    lambda_home: float,
    lambda_away: float,
    *,
    include_half_time: bool = False,
    half_time_share_home: float = DEFAULT_HALF_TIME_SHARE,
    half_time_share_away: float = DEFAULT_HALF_TIME_SHARE,
    limitations: list[str] | None = None,
    rho: float = RHO,
) -> ScoreEstimate:
    """Price a full-time grid, and a half-time grid when that market is eligible."""

    matrix = _matrix(lambda_home, lambda_away, rho=rho)
    home_goals, away_goals, score_probability = _mode(matrix)
    probabilities = _market_probabilities(matrix)
    markets = _markets(probabilities)
    half_score: str | None = None
    half_label: str | None = None
    if include_half_time:
        half_matrix = _matrix(
            lambda_home * half_time_share_home,
            lambda_away * half_time_share_away,
            rho=rho,
        )
        half_home, half_away, _half_probability = _mode(half_matrix)
        half_score = f"{half_home}-{half_away}"
        half_label = _winner_label(half_home, half_away)
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
    )


def estimate_score(
    *,
    standings_payload: dict[str, Any],
    home_finished_payload: dict[str, Any],
    away_finished_payload: dict[str, Any],
    competition_code: str,
    home_team_id: int,
    away_team_id: int,
    home_team_name: str,
    away_team_name: str,
    include_half_time: bool,
) -> ScoreEstimate:
    """Expected goals from venue rates, then the modal scores and market sums."""

    home_matches = _scored_matches(home_finished_payload)
    away_matches = _scored_matches(away_finished_payload)
    league_average, league_from_matches = _league_average(
        standings_payload,
        {home_team_id, away_team_id},
        home_matches,
        away_matches,
        home_team_id,
        away_team_id,
    )
    home_scored, home_conceded, home_notes = _venue_rates(
        home_matches,
        standings_payload,
        team_id=home_team_id,
        team_name=home_team_name,
        venue="home",
        competition_code=competition_code,
    )
    away_scored, away_conceded, away_notes = _venue_rates(
        away_matches,
        standings_payload,
        team_id=away_team_id,
        team_name=away_team_name,
        venue="away",
        competition_code=competition_code,
    )
    limitations: list[str] = []
    if league_from_matches or any(
        rate_source == "match_list"
        for rate_source in (
            home_scored.source,
            home_conceded.source,
            away_scored.source,
            away_conceded.source,
        )
    ):
        limitations.append(MISSING_PLAYED_GAMES)
    limitations.extend(home_notes)
    limitations.extend(away_notes)
    lambda_home = home_scored.per_game * away_conceded.per_game / league_average
    lambda_away = away_scored.per_game * home_conceded.per_game / league_average
    used_season = "season" in {
        home_scored.source,
        home_conceded.source,
        away_scored.source,
        away_conceded.source,
    }
    if used_season:
        lambda_home *= HOME_FACTOR
        lambda_away *= AWAY_FACTOR
        limitations.append(HOME_FACTOR_NOTE)
    share_home = DEFAULT_HALF_TIME_SHARE
    share_away = DEFAULT_HALF_TIME_SHARE
    if include_half_time:
        share_home = _half_time_share(home_matches, home_team_id)
        share_away = _half_time_share(away_matches, away_team_id)
    return estimate_from_lambdas(
        lambda_home,
        lambda_away,
        include_half_time=include_half_time,
        half_time_share_home=share_home,
        half_time_share_away=share_away,
        limitations=limitations,
    )


@dataclass(frozen=True)
class _Rate:
    per_game: float
    source: str


def _venue_rates(
    matches: list[dict[str, Any]],
    standings_payload: dict[str, Any],
    *,
    team_id: int,
    team_name: str,
    venue: str,
    competition_code: str,
) -> tuple[_Rate, _Rate, list[str]]:
    same = _at_venue(matches, team_id, venue, competition_code)
    any_competition = _at_venue(matches, team_id, venue, None)
    season_scored, season_conceded = _season_rates(standings_payload, team_id)
    notes: list[str] = []
    if len(same) >= MIN_VENUE_MATCHES:
        return _mean_rate(same, team_id, "competition_venue"), _mean_rate(same, team_id, "competition_venue", conceded=True), notes
    if len(any_competition) >= MIN_VENUE_MATCHES:
        notes.append(
            f"{team_name} has {len(same)} {venue} matches in {competition_code}. "
            "Rates use matches from other competitions."
        )
        pool = any_competition
        source = "any_competition_venue"
        return _mean_rate(pool, team_id, source), _mean_rate(pool, team_id, source, conceded=True), notes
    if season_scored is not None and season_conceded is not None:
        notes.append(
            f"{team_name} has {len(any_competition)} {venue} matches. Rates use the season table."
        )
        return _Rate(season_scored, "season"), _Rate(season_conceded, "season"), notes
    pool = any_competition or matches
    notes.append(
        f"{team_name} has {len(any_competition)} {venue} matches. Rates use the finished-match list."
    )
    if not pool:
        return _Rate(0.0, "match_list"), _Rate(0.0, "match_list"), notes
    return (
        _mean_rate(pool, team_id, "match_list"),
        _mean_rate(pool, team_id, "match_list", conceded=True),
        notes,
    )


def _mean_rate(
    matches: list[dict[str, Any]],
    team_id: int,
    source: str,
    *,
    conceded: bool = False,
) -> _Rate:
    total = 0
    for match in matches:
        scored, against = _team_goals(match, team_id)
        total += against if conceded else scored
    return _Rate(total / len(matches), source)


def _season_rates(payload: dict[str, Any], team_id: int) -> tuple[float | None, float | None]:
    for row in _table_rows(payload, {team_id}):
        team = row.get("team") or {}
        if team.get("id") != team_id:
            continue
        played = row.get("playedGames")
        goals_for = row.get("goalsFor")
        goals_against = row.get("goalsAgainst")
        if not _positive_int(played) or not _is_int(goals_for) or not _is_int(goals_against):
            return None, None
        return goals_for / played, goals_against / played
    return None, None


def _league_average(
    payload: dict[str, Any],
    team_ids: set[int],
    home_matches: list[dict[str, Any]],
    away_matches: list[dict[str, Any]],
    home_team_id: int,
    away_team_id: int,
) -> tuple[float, bool]:
    rows = _table_rows(payload, team_ids)
    goals = 0
    played = 0
    usable = bool(rows)
    for row in rows:
        row_played = row.get("playedGames")
        goals_for = row.get("goalsFor")
        if not _positive_int(row_played) or not _is_int(goals_for):
            usable = False
            break
        goals += goals_for
        played += row_played
    if usable and played > 0:
        average = goals / played
        return (average if average > 0 else 1.0), False
    scored = [
        _team_goals(match, home_team_id)[0]
        for match in home_matches
    ] + [
        _team_goals(match, away_team_id)[0]
        for match in away_matches
    ]
    if scored:
        average = sum(scored) / len(scored)
        if average > 0:
            return average, True
    return 1.0, True


def _table_rows(payload: dict[str, Any], team_ids: set[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in payload.get("standings") or []:
        if not isinstance(table, dict) or table.get("type") not in (None, "TOTAL"):
            continue
        table_rows = [row for row in table.get("table") or [] if isinstance(row, dict)]
        ids = {
            (row.get("team") or {}).get("id")
            for row in table_rows
        }
        if ids & team_ids:
            rows.extend(table_rows)
    return rows


def _at_venue(
    matches: list[dict[str, Any]],
    team_id: int,
    venue: str,
    competition_code: str | None,
) -> list[dict[str, Any]]:
    selected = []
    for match in matches:
        is_home = (match.get("homeTeam") or {}).get("id") == team_id
        if venue == "home" and not is_home:
            continue
        if venue == "away" and is_home:
            continue
        if competition_code is not None:
            code = (match.get("competition") or {}).get("code")
            if code != competition_code:
                continue
        selected.append(match)
    return selected


def _half_time_share(matches: list[dict[str, Any]], team_id: int) -> float:
    """First-half goals scored divided by full-time goals scored, on the last five."""

    scored = 0
    full = 0
    samples = 0
    for match in matches[:5]:
        pair = _consistent_half_time(match, team_id)
        if pair is None:
            continue
        half_scored, _half_conceded, full_scored, _full_conceded = pair
        samples += 1
        scored += half_scored
        full += full_scored
    if samples < MIN_VENUE_MATCHES or full <= 0:
        return DEFAULT_HALF_TIME_SHARE
    return scored / full


def _scored_matches(payload: dict[str, Any]) -> list[dict[str, Any]]:
    matches = [
        match
        for match in payload.get("matches") or []
        if isinstance(match, dict) and match.get("status") == "FINISHED" and _has_full_time(match)
    ]
    matches.sort(key=lambda match: match.get("utcDate") or "", reverse=True)
    return matches


def _consistent_half_time(
    match: dict[str, Any],
    team_id: int,
) -> tuple[int, int, int, int] | None:
    if not _has_half_time(match) or not _has_full_time(match):
        return None
    full_time = match["score"]["fullTime"]
    half_time = match["score"]["halfTime"]
    home_id = (match.get("homeTeam") or {}).get("id")
    if team_id == home_id:
        half = (half_time["home"], half_time["away"])
        full = (full_time["home"], full_time["away"])
    else:
        half = (half_time["away"], half_time["home"])
        full = (full_time["away"], full_time["home"])
    if half[0] > full[0] or half[1] > full[1]:
        return None
    return half[0], half[1], full[0], full[1]


def _has_full_time(match: dict[str, Any]) -> bool:
    full_time = (match.get("score") or {}).get("fullTime") or {}
    return _is_int(full_time.get("home")) and _is_int(full_time.get("away"))


def _has_half_time(match: dict[str, Any]) -> bool:
    half_time = (match.get("score") or {}).get("halfTime") or {}
    return _is_int(half_time.get("home")) and _is_int(half_time.get("away"))


def _team_goals(match: dict[str, Any], team_id: int) -> tuple[int, int]:
    full_time = match["score"]["fullTime"]
    if (match.get("homeTeam") or {}).get("id") == team_id:
        return full_time["home"], full_time["away"]
    return full_time["away"], full_time["home"]


def _matrix(lambda_home: float, lambda_away: float, *, rho: float) -> tuple[tuple[float, ...], ...]:
    raw: list[list[float]] = []
    total = 0.0
    for home_goals in range(MAX_GOALS + 1):
        row: list[float] = []
        for away_goals in range(MAX_GOALS + 1):
            weight = _tau(home_goals, away_goals, lambda_home, lambda_away, rho)
            probability = _poisson(home_goals, lambda_home) * _poisson(away_goals, lambda_away) * weight
            if probability < 0:
                probability = 0.0
            row.append(probability)
            total += probability
        raw.append(row)
    if total <= 0:
        share = 1.0 / ((MAX_GOALS + 1) ** 2)
        return tuple(tuple(share for _ in range(MAX_GOALS + 1)) for _ in range(MAX_GOALS + 1))
    return tuple(tuple(cell / total for cell in row) for row in raw)


def _poisson(goals: int, expected: float) -> float:
    expected = max(expected, 0.0)
    return math.exp(-expected) * (expected ** goals) / math.factorial(goals)


def _tau(home_goals: int, away_goals: int, lambda_home: float, lambda_away: float, rho: float) -> float:
    if home_goals == 0 and away_goals == 0:
        weight = 1.0 - (lambda_home * lambda_away * rho)
    elif home_goals == 0 and away_goals == 1:
        weight = 1.0 + (lambda_home * rho)
    elif home_goals == 1 and away_goals == 0:
        weight = 1.0 + (lambda_away * rho)
    elif home_goals == 1 and away_goals == 1:
        weight = 1.0 - rho
    else:
        weight = 1.0
    return weight if weight > 0 else 1e-6


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
    home = draw = away = btts_yes = over_25 = 0.0
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
            if home_goals + away_goals >= 3:
                over_25 += probability
    return {
        "home": home,
        "draw": draw,
        "away": away,
        "btts_yes": btts_yes,
        "over_25": over_25,
    }


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


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(value: Any) -> bool:
    return _is_int(value) and value > 0
