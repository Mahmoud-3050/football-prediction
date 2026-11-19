"""API Ninjas adapter for Saudi Pro League."""

from __future__ import annotations

from pathlib import Path

from api_ninjas import (
    NinjasClient,
    NinjasIdMap,
    is_ninjas_competition,
    season_to_fd,
    stable_int_id,
    standings_to_fd,
)
from data_fetcher import FREE_COMPETITIONS, resolve_competition


def test_spl_is_ninjas_competition() -> None:
    assert is_ninjas_competition("SPL") is True
    assert is_ninjas_competition("PL") is False
    resolved = resolve_competition("Saudi Pro League")
    assert resolved is not None
    assert resolved.code == "SPL"
    assert FREE_COMPETITIONS["SPL"]["provider"] == "api-ninjas"


def test_id_map_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ninjas_ids.json"
    store = NinjasIdMap(path)
    match_id = store.remember_game("saudiproleague-20261118-hil-nas")
    team_id = store.remember_team("soccer-al-hilal")
    assert store.game_id_for(match_id) == "saudiproleague-20261118-hil-nas"
    assert store.team_key_for(team_id) == "soccer-al-hilal"
    again = NinjasIdMap(path)
    assert again.game_id_for(match_id) == "saudiproleague-20261118-hil-nas"
    assert again.team_key_for(team_id) == "soccer-al-hilal"
    assert match_id == stable_int_id("saudiproleague-20261118-hil-nas")


def test_to_fd_match_and_standings_shape(tmp_path: Path) -> None:
    client = NinjasClient("test-key", id_map=NinjasIdMap(tmp_path / "ids.json"))
    raw = {
        "game_id": "saudiproleague-20261118-hil-nas",
        "league": "saudiproleague",
        "start_time": "2026-11-18T18:00:00Z",
        "status": "scheduled",
        "updated_at": "2026-11-18T10:00:00Z",
        "home_team": {
            "team_id": "soccer-al-hilal",
            "name": "Al Hilal",
            "abbreviation": "HIL",
        },
        "away_team": {
            "team_id": "soccer-al-nassr",
            "name": "Al Nassr",
            "abbreviation": "NAS",
        },
        "home_score": None,
        "away_score": None,
    }
    match = client.to_fd_match(raw, competition_code="SPL")
    assert match["competition"]["code"] == "SPL"
    assert match["status"] == "SCHEDULED"
    assert match["homeTeam"]["name"] == "Al Hilal"
    assert isinstance(match["id"], int)
    assert client.id_map.game_id_for(match["id"]) == raw["game_id"]

    standings = standings_to_fd(
        [
            {
                "season": 2025,
                "league_rank": 1,
                "games_played": 10,
                "wins": 8,
                "draws": 1,
                "losses": 1,
                "points": 25,
                "goals_for": 20,
                "goals_against": 5,
                "goal_difference": 15,
                "team": {
                    "team_id": "soccer-al-hilal",
                    "name": "Al Hilal",
                    "abbreviation": "HIL",
                },
            }
        ],
        competition_code="SPL",
    )
    assert standings["competition"]["code"] == "SPL"
    assert standings["standings"][0]["table"][0]["position"] == 1
    assert standings["standings"][0]["table"][0]["team"]["name"] == "Al Hilal"


def test_season_to_fd_includes_season_start(tmp_path: Path) -> None:
    payload = season_to_fd(
        [
            {
                "game_id": "saudiproleague-20260813-haz-abh",
                "start_time": "2026-08-13T16:15:00Z",
                "status": "final",
                "home_team": {
                    "team_id": "soccer-abha",
                    "name": "Abha",
                    "abbreviation": "ABH",
                },
                "away_team": {
                    "team_id": "soccer-al-hazem",
                    "name": "Al Hazem",
                    "abbreviation": "HAZ",
                },
                "home_score": 1,
                "away_score": 2,
                "period_scores": {"home": [0, 1], "away": [2, 0]},
            }
        ],
        competition_code="SPL",
        season=2026,
        id_map=NinjasIdMap(tmp_path / "ids.json"),
    )
    assert payload["competition"]["code"] == "SPL"
    assert len(payload["matches"]) == 1
    match = payload["matches"][0]
    assert match["season"]["startDate"] == "2026-08-01"
    assert match["score"]["fullTime"] == {"home": 1, "away": 2}
