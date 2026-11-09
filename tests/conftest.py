"""Local API fixtures. Nothing here contacts Football-Data or Gemini."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from data_fetcher import DataFetcher
from pipeline import Pipeline
from predictor import Predictor
from storage import Storage

ARSENAL = {"id": 1, "name": "Arsenal FC", "shortName": "Arsenal", "tla": "ARS"}
CHELSEA = {"id": 2, "name": "Chelsea FC", "shortName": "Chelsea", "tla": "CHE"}
MAN_CITY = {"id": 3, "name": "Manchester City FC", "shortName": "Manchester City", "tla": "MCI"}
MAN_UNITED = {"id": 4, "name": "Manchester United FC", "shortName": "Manchester United", "tla": "MUN"}
SPURS = {"id": 5, "name": "Tottenham Hotspur FC", "shortName": "Tottenham", "tla": "TOT"}
BARCELONA = {"id": 10, "name": "FC Barcelona", "shortName": "Barcelona", "tla": "BAR"}
INTER = {"id": 20, "name": "FC Internazionale Milano", "shortName": "Internazionale", "tla": "INT"}
AC_MILAN = {"id": 21, "name": "AC Milan", "shortName": "Milan", "tla": "MIL"}

FIXTURES = Path(__file__).parent / "fixtures"
STAMP = "2026-03-01T12:00:00Z"


def make_match(
    match_id: int,
    home: dict[str, Any],
    away: dict[str, Any],
    status: str = "FINISHED",
    home_goals: int | None = 3,
    away_goals: int | None = 1,
    half_home: int | None = 1,
    half_away: int | None = 0,
    competition: str = "PL",
    when: str = "2026-04-12T15:00:00Z",
    last_updated: str | None = STAMP,
) -> dict[str, Any]:
    return {
        "id": match_id,
        "utcDate": when,
        "status": status,
        "competition": {"code": competition, "name": competition},
        "homeTeam": {"id": home["id"], "name": home["name"]},
        "awayTeam": {"id": away["id"], "name": away["name"]},
        "score": {
            "fullTime": {"home": home_goals, "away": away_goals},
            "halfTime": {"home": half_home, "away": half_away},
        },
        "lastUpdated": last_updated,
    }


def form_payload(
    team: dict[str, Any],
    opponent: dict[str, Any],
    *,
    count: int = 5,
    start_id: int = 100,
    competition: str = "PL",
    with_half_time: bool = True,
    first_opponent: dict[str, Any] | None = None,
    null_full_time: int = 0,
    venue: str = "home",
) -> dict[str, Any]:
    matches = []
    for index in range(count):
        other = first_opponent if index == 0 and first_opponent is not None else opponent
        scored = None if index < null_full_time else 3
        conceded = None if index < null_full_time else 1
        if venue == "away":
            match_home, match_away = other, team
            home_goals, away_goals = conceded, scored
            half_home, half_away = (0, 1) if with_half_time else (None, None)
        else:
            match_home, match_away = team, other
            home_goals, away_goals = scored, conceded
            half_home, half_away = (1, 0) if with_half_time else (None, None)
        matches.append(
            make_match(
                start_id + index,
                match_home,
                match_away,
                status="FINISHED",
                home_goals=home_goals,
                away_goals=away_goals,
                half_home=half_home,
                half_away=half_away,
                competition=competition,
                when=f"2026-02-{20 - index:02d}T15:00:00Z",
            )
        )
    return {"matches": matches, "lastUpdated": STAMP}


def standings_for(teams: list[dict[str, Any]], *, last_updated: str | None = STAMP) -> dict[str, Any]:
    table = []
    for index, team in enumerate(teams, start=1):
        table.append(
            {
                "position": index,
                "team": {"id": team["id"], "name": team["name"], "shortName": team["shortName"], "tla": team["tla"]},
                "playedGames": 10,
                "points": 40 - index,
                "goalsFor": 30 - index,
                "goalsAgainst": 10 + index,
                "goalDifference": 20 - (2 * index),
            }
        )
    payload: dict[str, Any] = {
        "competition": {"code": "PL", "name": "Premier League"},
        "standings": [{"type": "TOTAL", "table": table}],
    }
    if last_updated is not None:
        payload["lastUpdated"] = last_updated
    return payload


def valid_model_json(**overrides: Any) -> str:
    body = {"tactical_reasoning": "The home club's recent wins are in the snapshot."}
    body.update(overrides)
    return json.dumps(body)


class Api:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.sleeps: list[float] = []
        self.competition = "PL"
        self.teams = [ARSENAL, CHELSEA, MAN_CITY, MAN_UNITED, SPURS]
        self.fixtures = [
            make_match(50, ARSENAL, CHELSEA, status="SCHEDULED", home_goals=None, away_goals=None, half_home=None, half_away=None)
        ]
        self.standings = standings_for([ARSENAL, CHELSEA, MAN_CITY, MAN_UNITED, SPURS])
        self.forms = {
            1: form_payload(ARSENAL, SPURS, start_id=100, first_opponent=CHELSEA),
            2: form_payload(CHELSEA, SPURS, start_id=200, venue="away"),
        }
        self.h2h = {"matches": [], "lastUpdated": STAMP}
        self.status_overrides: dict[str, tuple[int, dict[str, Any], int]] = {}
        self._override_seen: dict[str, int] = {}

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(
            {
                "path": path,
                "params": dict(request.url.params),
                "token": request.headers.get("X-Auth-Token"),
            }
        )
        override = self.status_overrides.get(path)
        if override is not None:
            code, body, times = override
            seen = self._override_seen.get(path, 0)
            self._override_seen[path] = seen + 1
            if seen < times:
                headers = {"Retry-After": "0"} if code == 429 else None
                return httpx.Response(code, json=body, headers=headers)
        if path == f"/v4/competitions/{self.competition}/teams":
            return httpx.Response(200, json={"teams": self.teams, "competition": {"code": self.competition}})
        if path == f"/v4/competitions/{self.competition}/standings":
            return httpx.Response(200, json=self.standings)
        if path.startswith("/v4/teams/") and path.endswith("/matches"):
            team_id = int(path.split("/")[3])
            if request.url.params.get("status") == "FINISHED":
                return httpx.Response(200, json=self.forms[team_id])
            return httpx.Response(200, json={"matches": self.fixtures, "lastUpdated": STAMP})
        if path.endswith("/head2head"):
            return httpx.Response(200, json=self.h2h)
        return httpx.Response(404, json={"message": f"unexpected {path}"})


class Harness:
    def __init__(self, tmp_path: Path, api: Api | None = None, scripts: list[Any] | None = None) -> None:
        self.api = api or Api()
        self.prompts: list[str] = []
        self.schemas: list[dict[str, Any]] = []
        self.scripts = list(scripts or [valid_model_json()])
        self.tmp_path = tmp_path
        self.store: Storage | None = None
        self.pipeline: Pipeline | None = None

    async def execute(self, home: str = "Arsenal", away: str = "Chelsea", competition: str = "Premier League"):
        transport = httpx.MockTransport(self.api.handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://api.football-data.org") as client:
            fetcher = DataFetcher(
                "fd-test-key",
                self.tmp_path / "cache",
                client=client,
                sleep=self.api.sleep,
            )

            async def generate(prompt: str, schema: dict[str, Any]) -> str:
                self.prompts.append(prompt)
                self.schemas.append(schema)
                item = self.scripts.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

            predictor = Predictor("gemini-test-key", "gemini-2.5-flash", generate=generate)
            self.store = Storage(self.tmp_path / "predictions.db")
            self.pipeline = Pipeline(fetcher, predictor, self.store)
            return await self.pipeline.execute(
                chat_id=11,
                user_id=22,
                home=home,
                away=away,
                competition=competition,
            )


@pytest.fixture
def markets_table() -> list[dict[str, str]]:
    return json.loads((FIXTURES / "markets.json").read_text(encoding="utf-8"))


@pytest.fixture
def wrong_venue_matches() -> list[dict[str, Any]]:
    return [make_match(60, CHELSEA, ARSENAL, status="SCHEDULED", home_goals=None, away_goals=None, half_home=None, half_away=None)]


@pytest.fixture
def finished_match() -> dict[str, Any]:
    return make_match(70, ARSENAL, CHELSEA, status="FINISHED", home_goals=2, away_goals=1)


@pytest.fixture
def missing_standings() -> dict[str, Any]:
    return standings_for([ARSENAL])


@pytest.fixture
def thin_form() -> dict[str, Any]:
    return form_payload(ARSENAL, SPURS, count=2, start_id=300)


@pytest.fixture
def missing_half_time() -> dict[str, Any]:
    return form_payload(ARSENAL, SPURS, with_half_time=False, first_opponent=CHELSEA)
