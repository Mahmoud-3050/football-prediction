"""Server jobs: tomorrow predictions and standings refresh."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from jobs.standings_job import competitions_needing_standings_refresh
from jobs.tomorrow import cairo_tomorrow, select_tomorrow_fixtures
from laboratory.standings import parse_standings, replace_standings
from laboratory.store import connect
from core.models import Fixture


def _fixture(
    match_id: int,
    *,
    competition: str,
    kickoff: str,
    status: str = "TIMED",
) -> Fixture:
    return Fixture(
        id=match_id,
        status=status,
        utc_date=kickoff,
        competition_code=competition,
        competition_name=competition,
        home_team_id=1,
        home_team_name="Home",
        away_team_id=2,
        away_team_name="Away",
        full_time_home=None,
        full_time_away=None,
        half_time_home=None,
        half_time_away=None,
        last_updated=None,
    )


def test_cairo_tomorrow_is_next_cairo_calendar_day() -> None:
    now = datetime(2026, 11, 10, 21, 0, tzinfo=timezone.utc)
    assert cairo_tomorrow(now=now) == date(2026, 11, 11)


def test_select_tomorrow_keeps_predictable_job_leagues_only() -> None:
    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    day = date(2026, 11, 11)
    keep = _fixture(1, competition="PL", kickoff="2026-11-11T18:00:00Z")
    finished = _fixture(
        2, competition="PL", kickoff="2026-11-11T15:00:00Z", status="FINISHED"
    )
    other = _fixture(3, competition="WC", kickoff="2026-11-11T18:00:00Z")
    selected = select_tomorrow_fixtures(
        [keep, finished, other], day=day, now=now
    )
    assert [item.id for item in selected] == [1]


def test_standings_refresh_when_finished_count_rises(tmp_path: Path) -> None:
    connection = connect(tmp_path / "football.db")
    replace_standings(
        connection,
        parse_standings(
            {
                "season": {"startDate": "2026-08-15", "currentMatchday": 10},
                "standings": [
                    {
                        "type": "TOTAL",
                        "table": [
                            {
                                "position": 1,
                                "team": {"id": 1, "name": "A"},
                                "playedGames": 10,
                                "won": 5,
                                "draw": 3,
                                "lost": 2,
                                "points": 18,
                                "goalsFor": 12,
                                "goalsAgainst": 8,
                                "goalDifference": 4,
                            }
                        ],
                    }
                ],
            },
            competition="PL",
        ),
    )
    assert competitions_needing_standings_refresh(
        connection, {"PL": 2}, stored_finished={"PL": 2}
    ) == []
    assert competitions_needing_standings_refresh(
        connection, {"PL": 3}, stored_finished={"PL": 2}
    ) == ["PL"]
