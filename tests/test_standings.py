"""League standings fetch/parse/store and dashboard rendering."""

from __future__ import annotations

from pathlib import Path

from laboratory.dashboard import standings
from laboratory.standings import (
    ensure_standings,
    has_standings,
    list_standings_rows,
    parse_standings,
    replace_standings,
)
from laboratory.store import connect


def _sample_payload() -> dict:
    return {
        "lastUpdated": "2026-11-10T12:00:00Z",
        "competition": {
            "code": "PL",
            "name": "Premier League",
            "emblem": "https://crests.football-data.org/PL.png",
        },
        "season": {
            "startDate": "2025-08-15",
            "currentMatchday": 12,
        },
        "standings": [
            {
                "type": "TOTAL",
                "table": [
                    {
                        "position": 1,
                        "team": {
                            "id": 57,
                            "name": "Arsenal FC",
                            "crest": "https://crests.football-data.org/57.png",
                        },
                        "playedGames": 11,
                        "won": 8,
                        "draw": 2,
                        "lost": 1,
                        "points": 26,
                        "goalsFor": 22,
                        "goalsAgainst": 8,
                        "goalDifference": 14,
                        "form": "W,W,D,W,W",
                    },
                    {
                        "position": 2,
                        "team": {
                            "id": 65,
                            "name": "Manchester City FC",
                            "crest": "https://crests.football-data.org/65.png",
                        },
                        "playedGames": 11,
                        "won": 7,
                        "draw": 2,
                        "lost": 2,
                        "points": 23,
                        "goalsFor": 24,
                        "goalsAgainst": 10,
                        "goalDifference": 14,
                        "form": "W,L,W,W,D",
                    },
                ],
            }
        ],
    }


def test_parse_and_store_standings(tmp_path: Path) -> None:
    parsed = parse_standings(_sample_payload(), competition="PL")
    assert parsed["season"] == "2025"
    assert parsed["matchday"] == 12
    assert len(parsed["rows"]) == 2
    assert parsed["rows"][0]["team_name"] == "Arsenal FC"

    connection = connect(tmp_path / "football.db")
    count = replace_standings(connection, parsed)
    assert count == 2
    assert has_standings(connection, "PL") is True
    meta, rows = list_standings_rows(connection, "PL")
    assert meta is not None
    assert meta["season"] == "2025"
    assert [row["team_name"] for row in rows] == ["Arsenal FC", "Manchester City FC"]


def test_ensure_standings_uses_db_after_first_store(tmp_path: Path, monkeypatch) -> None:
    connection = connect(tmp_path / "football.db")
    calls = {"n": 0}

    def fake_fetch(api_key: str, competition: str, *, season: int | None = None):
        calls["n"] += 1
        return _sample_payload()

    monkeypatch.setattr("laboratory.standings.fetch_standings", fake_fetch)
    first = ensure_standings(connection, "key", "PL", force=False)
    second = ensure_standings(connection, "key", "PL", force=False)
    assert first["source"] == "api"
    assert second["source"] == "db"
    assert calls["n"] == 1


def test_dashboard_standings_reads_from_db(tmp_path: Path) -> None:
    connection = connect(tmp_path / "football.db")
    replace_standings(connection, parse_standings(_sample_payload(), competition="PL"))
    page = standings(connection, {"league": ["PL"]}, api_key=None)
    assert "Standings" in page
    assert "Arsenal" in page
    assert "Arsenal FC" not in page
    assert "Man City" in page
    assert "26" in page
    assert 'class="league-pills"' in page
    assert "form-pill win" in page
    assert "zone-europe" in page
    assert "10 Nov 2026" in page
    assert "T14:22:43" not in page
    assert "crests.football-data.org/57.png" in page
    assert 'class="crest"' in page
    assert 'class="emblem"' in page
    assert "/crests/PL.png" in page or "crests.football-data.org/PL.png" in page
    assert "href=\"/standings\"" in page or "Standings" in page
    team = connection.execute("SELECT crest_url FROM teams WHERE team_id = '57'").fetchone()
    assert team is not None
    assert "57.png" in team["crest_url"]
    league = connection.execute(
        "SELECT emblem_url FROM competitions WHERE code = 'PL'"
    ).fetchone()
    assert league is not None
    assert "PL.png" in league["emblem_url"] or "/crests/PL.png" in league["emblem_url"]
