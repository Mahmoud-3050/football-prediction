"""Quality, consistency, freshness, and eligibility for one fetched match.

Gaps are recorded and left empty. They are never filled with zeros.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models import (
    CheckResult,
    Competition,
    Fixture,
    StandingRow,
    Team,
    TeamFacts,
)

FREE_TIER_DELAY_WARNING = (
    "Football-Data.org free-tier data is delayed and is not a live feed."
)
MINIMUM_SCORED_MATCHES = 3
FORM_WINDOW = 5


@dataclass
class Evaluation:
    checks: list[CheckResult]
    eligible: bool
    blocking_stage: str | None
    blocking_reason: str | None
    limitations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    include_half_time: bool = False
    home_facts: TeamFacts | None = None
    away_facts: TeamFacts | None = None
    home_row: StandingRow | None = None
    away_row: StandingRow | None = None
    home_window: list[dict[str, Any]] = field(default_factory=list)
    away_window: list[dict[str, Any]] = field(default_factory=list)
    freshness: dict[str, str | None] = field(default_factory=dict)


def evaluate(
    *,
    competition: Competition,
    home: Team,
    away: Team,
    fixture: Fixture,
    standings_payload: dict[str, Any],
    home_finished_payload: dict[str, Any],
    away_finished_payload: dict[str, Any],
    h2h_matches: list[dict[str, Any]],
    h2h_payload: dict[str, Any] | None,
) -> Evaluation:
    home_row, home_row_problem = _standing_row(standings_payload, home)
    away_row, away_row_problem = _standing_row(standings_payload, away)
    home_scored = _scored_matches(home_finished_payload)
    away_scored = _scored_matches(away_finished_payload)
    home_same_league = _same_competition_matches(home_scored, competition.code)
    away_same_league = _same_competition_matches(away_scored, competition.code)
    home_window = home_scored[:FORM_WINDOW]
    away_window = away_scored[:FORM_WINDOW]
    home_facts = _facts(home, home_window, competition.code)
    away_facts = _facts(away, away_window, competition.code)
    home_ht_samples = _half_time_sample_count(home_same_league, home.id)
    away_ht_samples = _half_time_sample_count(away_same_league, away.id)

    fixture_matches = (
        fixture.home_team_id == home.id
        and fixture.away_team_id == away.id
        and fixture.competition_code == competition.code
    )
    standings_ok = home_row is not None and away_row is not None

    consistency_bits: list[str] = []
    if fixture_matches:
        consistency_bits.append("fixture home, away, and competition match the request")
    else:
        consistency_bits.append(
            "fixture home, away, or competition does not match the request"
        )
    if standings_ok:
        consistency_bits.append("both clubs appear in that competition's standings")
    else:
        consistency_bits.append("one or both clubs are missing from that competition's standings")

    standings_fail: list[str] = []
    form_fail: list[str] = []
    if home_row is None:
        standings_fail.append(home_row_problem or f"No standings row for {home.name}.")
    if away_row is None:
        standings_fail.append(away_row_problem or f"No standings row for {away.name}.")
    if len(home_scored) < MINIMUM_SCORED_MATCHES:
        form_fail.append(
            f"{home.name} has {len(home_scored)} finished matches with a full-time score."
        )
    if len(away_scored) < MINIMUM_SCORED_MATCHES:
        form_fail.append(
            f"{away.name} has {len(away_scored)} finished matches with a full-time score."
        )
    availability_fail = standings_fail + form_fail

    omitted = _omitted_full_time_count(home_finished_payload) + _omitted_full_time_count(
        away_finished_payload
    )
    inconsistent = _inconsistent_half_time_count(home_window, home.id) + _inconsistent_half_time_count(
        away_window, away.id
    )
    quality_notes: list[str] = []
    if omitted:
        quality_notes.append(
            "Finished matches without a full-time score were omitted and were not recorded as 0-0."
        )
    if inconsistent:
        quality_notes.append(
            "A half-time score that exceeded the full-time score was ignored and not replaced."
        )
    for team, row in ((home, home_row), (away, away_row)):
        if row is not None and not _is_int(row.raw.get("goalDifference")):
            quality_notes.append(
                f"Goal difference for {team.name} was calculated from goals for and against "
                "because the provider omitted it."
            )

    h2h_stamp = _stamp(h2h_payload)
    if h2h_stamp is None and h2h_matches:
        h2h_stamp = _stamp({"matches": h2h_matches})
    freshness = {
        "standings": _stamp(standings_payload),
        "home_form": _stamp(home_finished_payload),
        "away_form": _stamp(away_finished_payload),
        "fixture": fixture.last_updated,
        "head_to_head": h2h_stamp if h2h_matches else None,
    }
    unknown = [
        name
        for name in ("standings", "home_form", "away_form", "fixture")
        if not freshness.get(name)
    ]
    if h2h_matches and not freshness.get("head_to_head"):
        unknown.append("head_to_head")

    checks = [
        CheckResult(
            "consistency",
            "pass" if fixture_matches and standings_ok else "fail",
            "; ".join(consistency_bits) + ".",
        ),
        CheckResult(
            "availability",
            "fail" if availability_fail else "pass",
            " ".join(availability_fail)
            if availability_fail
            else "Standings rows are present and every match used has a full-time score.",
        ),
        CheckResult(
            "quality",
            "limitation" if quality_notes else "pass",
            " ".join(quality_notes)
            if quality_notes
            else "No missing scores or table values were replaced with zeros.",
        ),
        CheckResult(
            "freshness",
            "limitation" if unknown else "pass",
            "freshness unknown for " + ", ".join(unknown) + "."
            if unknown
            else "lastUpdated was stored. Free-tier delay still applies and was not treated as live.",
        ),
    ]

    warnings = [FREE_TIER_DELAY_WARNING]
    limitations: list[str] = []
    blocking_stage: str | None = None
    blocking_reason: str | None = None

    if not fixture_matches:
        blocking_stage = "fixture"
        blocking_reason = (
            f"The resolved fixture does not match {home.name} vs {away.name} "
            f"in {competition.name}."
        )
    elif standings_fail:
        blocking_stage = "standings"
        blocking_reason = " ".join(standings_fail) + " Missing table data is not replaced."
    elif form_fail:
        blocking_stage = "form"
        blocking_reason = (
            " ".join(form_fail)
            + " At least 3 are required, and missing scores are not filled with zeros."
        )
    else:
        limitations.extend(_competition_limitations(home, home_facts, competition.name))
        limitations.extend(_competition_limitations(away, away_facts, competition.name))
        short_half_time = []
        if home_ht_samples < MINIMUM_SCORED_MATCHES:
            short_half_time.append(f"{home.name} ({home_ht_samples})")
        if away_ht_samples < MINIMUM_SCORED_MATCHES:
            short_half_time.append(f"{away.name} ({away_ht_samples})")
        if short_half_time:
            limitations.append(
                "Half-time scores on fewer than 3 same-league finished matches for "
                + " and ".join(short_half_time)
                + ". Half-time result and half-time score are skipped."
            )
        if not h2h_matches:
            limitations.append("No head-to-head meetings were found.")
        if unknown:
            limitations.append("Freshness unknown for: " + ", ".join(unknown) + ".")
        limitations.extend(quality_notes)

    include_half_time = (
        blocking_stage is None
        and home_ht_samples >= MINIMUM_SCORED_MATCHES
        and away_ht_samples >= MINIMUM_SCORED_MATCHES
    )
    return Evaluation(
        checks=checks,
        eligible=blocking_stage is None,
        blocking_stage=blocking_stage,
        blocking_reason=blocking_reason,
        limitations=limitations,
        warnings=warnings,
        include_half_time=include_half_time,
        home_facts=home_facts,
        away_facts=away_facts,
        home_row=home_row,
        away_row=away_row,
        home_window=home_window,
        away_window=away_window,
        freshness=freshness,
    )


def _competition_limitations(team: Team, facts: TeamFacts, competition_name: str) -> list[str]:
    if facts.same_competition_count >= MINIMUM_SCORED_MATCHES:
        return []
    return [
        (
            f"{team.name} has {facts.same_competition_count} of the last {facts.window_size} "
            f"scored matches in {competition_name}. Matches from other competitions were used."
        )
    ]


def _standing_row(
    payload: dict[str, Any],
    team: Team,
) -> tuple[StandingRow | None, str | None]:
    raw_row = None
    for table in payload.get("standings") or []:
        if not isinstance(table, dict):
            continue
        if table.get("type") not in (None, "TOTAL"):
            continue
        for row in table.get("table") or []:
            if not isinstance(row, dict):
                continue
            if (row.get("team") or {}).get("id") == team.id:
                raw_row = row
                break
        if raw_row is not None:
            break
    if raw_row is None:
        return None, f"No standings row for {team.name}."
    goals_for = raw_row.get("goalsFor")
    goals_against = raw_row.get("goalsAgainst")
    points = raw_row.get("points")
    if not all(_is_int(value) for value in (goals_for, goals_against, points)):
        return None, (
            f"Standings row for {team.name} is missing goals or points, "
            "and those values were not filled in."
        )
    goal_difference = raw_row.get("goalDifference")
    if not _is_int(goal_difference):
        goal_difference = goals_for - goals_against
    position = raw_row.get("position") if _is_int(raw_row.get("position")) else None
    return (
        StandingRow(
            team_id=team.id,
            team_name=team.name,
            position=position,
            points=points,
            goals_for=goals_for,
            goals_against=goals_against,
            goal_difference=goal_difference,
            raw=raw_row,
        ),
        None,
    )


def _facts(team: Team, window: list[dict[str, Any]], competition_code: str) -> TeamFacts:
    wins = draws = losses = 0
    results: list[str] = []
    same_competition = 0
    half_time_samples = 0
    scored = conceded = 0
    second_scored = second_conceded = 0
    for match in window:
        outcome = _outcome(match, team.id)
        results.append(outcome)
        if outcome == "W":
            wins += 1
        elif outcome == "D":
            draws += 1
        else:
            losses += 1
        if (match.get("competition") or {}).get("code") == competition_code:
            same_competition += 1
        pair = _consistent_half_time(match, team.id)
        if pair is None:
            continue
        half_scored, half_conceded, full_scored, full_conceded = pair
        half_time_samples += 1
        scored += half_scored
        conceded += half_conceded
        second_scored += full_scored - half_scored
        second_conceded += full_conceded - half_conceded
    enough_half_time = half_time_samples >= MINIMUM_SCORED_MATCHES
    return TeamFacts(
        team_id=team.id,
        team_name=team.name,
        wins=wins,
        draws=draws,
        losses=losses,
        record=f"{wins}-{draws}-{losses}",
        results=results,
        same_competition_count=same_competition,
        window_size=len(window),
        finished_with_full_time=len(window),
        half_time_samples=half_time_samples,
        half_time_goals_scored=scored if enough_half_time else None,
        half_time_goals_conceded=conceded if enough_half_time else None,
        second_half_goals_scored=second_scored if enough_half_time else None,
        second_half_goals_conceded=second_conceded if enough_half_time else None,
    )


def _scored_matches(payload: dict[str, Any]) -> list[dict[str, Any]]:
    matches = [
        match
        for match in payload.get("matches") or []
        if isinstance(match, dict) and match.get("status") == "FINISHED" and _has_full_time(match)
    ]
    matches.sort(key=lambda match: match.get("utcDate") or "", reverse=True)
    return matches


def _same_competition_matches(
    matches: list[dict[str, Any]],
    competition_code: str,
) -> list[dict[str, Any]]:
    return [
        match
        for match in matches
        if (match.get("competition") or {}).get("code") == competition_code
    ]


def _half_time_sample_count(matches: list[dict[str, Any]], team_id: int) -> int:
    return sum(1 for match in matches if _consistent_half_time(match, team_id) is not None)


def _omitted_full_time_count(payload: dict[str, Any]) -> int:
    count = 0
    for match in payload.get("matches") or []:
        if isinstance(match, dict) and match.get("status") == "FINISHED" and not _has_full_time(match):
            count += 1
    return count


def _inconsistent_half_time_count(window: list[dict[str, Any]], team_id: int) -> int:
    count = 0
    for match in window:
        if _has_half_time(match) and _consistent_half_time(match, team_id) is None:
            count += 1
    return count


def _has_full_time(match: dict[str, Any]) -> bool:
    full_time = (match.get("score") or {}).get("fullTime") or {}
    return _is_int(full_time.get("home")) and _is_int(full_time.get("away"))


def _has_half_time(match: dict[str, Any]) -> bool:
    half_time = (match.get("score") or {}).get("halfTime") or {}
    return _is_int(half_time.get("home")) and _is_int(half_time.get("away"))


def _outcome(match: dict[str, Any], team_id: int) -> str:
    full_time = match["score"]["fullTime"]
    home_goals = full_time["home"]
    away_goals = full_time["away"]
    home_id = (match.get("homeTeam") or {}).get("id")
    if home_goals == away_goals:
        return "D"
    home_win = home_goals > away_goals
    if team_id == home_id:
        return "W" if home_win else "L"
    return "L" if home_win else "W"


def _consistent_half_time(
    match: dict[str, Any],
    team_id: int,
) -> tuple[int, int, int, int] | None:
    """Return scored, conceded at HT and FT, or None when HT is missing or impossible."""

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


def _stamp(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    stamp = payload.get("lastUpdated")
    if isinstance(stamp, str) and stamp:
        return stamp
    stamps = [
        match["lastUpdated"]
        for match in payload.get("matches") or []
        if isinstance(match, dict) and isinstance(match.get("lastUpdated"), str)
    ]
    if stamps:
        return max(stamps)
    return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
