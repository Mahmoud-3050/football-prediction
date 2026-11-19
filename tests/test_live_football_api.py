"""Live Football API adapter for Saudi Pro League seasons."""

from __future__ import annotations

from live_football_api import (
    is_live_football_competition,
    lfa_league_id,
    season_label,
    season_to_fd,
)


def test_spl_live_football_league_id() -> None:
    assert is_live_football_competition("SPL") is True
    assert is_live_football_competition("PL") is False
    assert lfa_league_id("SPL") == 8454
    assert season_label(2024) == "2024/2025"


def test_season_to_fd_sets_league_emblem() -> None:
    payload = season_to_fd(
        {
            "league_id": 8454,
            "league_name": "Saudi Pro League",
            "season": "2024/2025",
            "weeks": [],
        },
        competition_code="SPL",
        season=2024,
    )
    assert payload["competition"]["emblem"] == (
        "https://live-football-api.com/leagues/8454.png"
    )


def test_season_to_fd_flattens_weeks() -> None:
    payload = season_to_fd(
        {
            "league_id": 8454,
            "league_name": "Saudi Pro League",
            "season": "2024/2025",
            "weeks": [
                {
                    "week": "1",
                    "matches": [
                        {
                            "id": 222340,
                            "league": {"id": 8454, "name": "Saudi Pro League"},
                            "date": "2024-08-13",
                            "kickoff": "16:15",
                            "status": {
                                "status": "finished",
                                "is_live": False,
                                "state": "postGame",
                            },
                            "home": {
                                "id": 123143,
                                "name": "Abha",
                                "logo": "https://example/abha.png",
                                "score": "1",
                            },
                            "away": {
                                "id": 134912,
                                "name": "Hazm",
                                "logo": "https://example/hazm.png",
                                "score": "2",
                            },
                            "halftime": {"home": 0, "away": 2},
                        }
                    ],
                }
            ],
        },
        competition_code="SPL",
        season=2024,
    )
    assert payload["competition"]["code"] == "SPL"
    assert len(payload["matches"]) == 1
    match = payload["matches"][0]
    assert match["id"] == 222340
    assert match["utcDate"] == "2024-08-13T16:15:00Z"
    assert match["status"] == "FINISHED"
    assert match["score"]["fullTime"] == {"home": 1, "away": 2}
    assert match["score"]["halfTime"] == {"home": 0, "away": 2}
    assert match["homeTeam"]["crest"] == "https://example/abha.png"
