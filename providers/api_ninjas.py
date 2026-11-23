"""API Ninjas soccer client for competitions Football-Data free tier omits.

Saudi Pro League (`SPL` / `saudiproleague`) is normalized into Football-Data-shaped
payloads so the existing pipeline, score model, and dashboard keep working.
"""

from __future__ import annotations

import json
import logging
import zlib
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from core.models import ProviderError

logger = logging.getLogger(__name__)

NINJAS_BASE = "https://api.api-ninjas.com"
# Internal code → Ninjas league slug.
NINJAS_LEAGUES: dict[str, str] = {
    "SPL": "saudiproleague",
}
NINJAS_COMPETITION_NAMES: dict[str, str] = {
    "SPL": "Saudi Pro League",
}
STATUS_MAP = {
    "scheduled": "SCHEDULED",
    "in_progress": "IN_PLAY",
    "final": "FINISHED",
    "postponed": "POSTPONED",
    "canceled": "CANCELLED",
    "cancelled": "CANCELLED",
    "suspended": "SUSPENDED",
    "delayed": "TIMED",
}


def is_ninjas_competition(code: str) -> bool:
    return code.upper() in NINJAS_LEAGUES


def ninjas_league(code: str) -> str:
    return NINJAS_LEAGUES[code.upper()]


def stable_int_id(value: str) -> int:
    """Stable positive int id (independent of PYTHONHASHSEED)."""

    return zlib.adler32(value.encode("utf-8")) % (10**9 - 1) + 1


class NinjasIdMap:
    """Persist hashed ids → Ninjas string ids for /predict after restart."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._games: dict[str, str] = {}
        self._teams: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        games = payload.get("games") if "games" in payload else payload
        teams = payload.get("teams") if "teams" in payload else {}
        if isinstance(games, dict):
            self._games = {
                str(key): str(value) for key, value in games.items() if value is not None
            }
        if isinstance(teams, dict):
            self._teams = {
                str(key): str(value) for key, value in teams.items() if value is not None
            }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"games": self._games, "teams": self._teams}, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def remember_game(self, game_id: str) -> int:
        match_id = stable_int_id(game_id)
        key = str(match_id)
        if self._games.get(key) != game_id:
            self._games[key] = game_id
            self._save()
        return match_id

    def remember_team(self, team_key: str) -> int:
        team_id = stable_int_id(team_key)
        key = str(team_id)
        if self._teams.get(key) != team_key:
            self._teams[key] = team_key
            self._save()
        return team_id

    def game_id_for(self, match_id: int) -> str | None:
        return self._games.get(str(match_id))

    def team_key_for(self, team_id: int) -> str | None:
        return self._teams.get(str(team_id))


class NinjasClient:
    def __init__(
        self,
        api_key: str,
        *,
        id_map: NinjasIdMap | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.id_map = id_map
        self._client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=NINJAS_BASE, timeout=30.0)
        return self._client

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if not self.api_key:
            raise ProviderError("NINJAS_API_KEY is missing.")
        client = await self._ensure()
        try:
            response = await client.get(
                path,
                params=params,
                headers={"X-Api-Key": self.api_key},
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(f"API Ninjas timed out ({exc}).") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"API Ninjas could not be reached ({exc}).") from exc
        if response.status_code in (401, 403):
            raise ProviderError(
                f"API Ninjas rejected NINJAS_API_KEY (HTTP {response.status_code}) for {path}."
            )
        if response.status_code >= 400:
            detail = response.text.strip()[:200]
            suffix = f" {detail}" if detail else ""
            raise ProviderError(
                f"API Ninjas returned HTTP {response.status_code} for {path}.{suffix}"
            )
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError("API Ninjas returned a response that was not JSON.") from exc

    async def soccermatches(self, **params: Any) -> list[dict[str, Any]]:
        payload = await self.get("/v1/soccermatches", params=params or None)
        if not isinstance(payload, list):
            raise ProviderError("API Ninjas soccermatches did not return a list.")
        return [item for item in payload if isinstance(item, dict)]

    async def soccerstandings(self, **params: Any) -> list[dict[str, Any]]:
        payload = await self.get("/v1/soccerstandings", params=params or None)
        if not isinstance(payload, list):
            raise ProviderError("API Ninjas soccerstandings did not return a list.")
        return [item for item in payload if isinstance(item, dict)]

    async def soccerclubs(self, **params: Any) -> list[dict[str, Any]]:
        payload = await self.get("/v1/soccerclubs", params=params or None)
        if not isinstance(payload, list):
            raise ProviderError("API Ninjas soccerclubs did not return a list.")
        return [item for item in payload if isinstance(item, dict)]

    async def matches_for_cairo_day(
        self,
        competition_code: str,
        day: date,
    ) -> dict[str, Any]:
        """Fetch SPL fixtures that may fall on a Cairo calendar day."""

        league = ninjas_league(competition_code)
        raw: list[dict[str, Any]] = []
        for offset in (-1, 0, 1):
            local_day = day + timedelta(days=offset)
            raw.extend(
                await self.soccermatches(league=league, date=local_day.isoformat())
            )
        seen: set[str] = set()
        matches: list[dict[str, Any]] = []
        for item in raw:
            game_id = str(item.get("game_id") or "")
            if not game_id or game_id in seen:
                continue
            seen.add(game_id)
            matches.append(self.to_fd_match(item, competition_code=competition_code))
        return {"matches": matches}

    async def match_by_id(self, match_id: int) -> dict[str, Any]:
        game_id = self.id_map.game_id_for(match_id) if self.id_map else None
        if not game_id:
            raise ProviderError(
                f"Match #{match_id} is not a known API Ninjas fixture. "
                "Open /today or /matches first, then tap the match button."
            )
        rows = await self.soccermatches(game_id=game_id)
        if not rows:
            raise ProviderError(f"API Ninjas has no match for game_id={game_id}.")
        # Infer competition from league slug.
        league = str(rows[0].get("league") or "")
        code = next(
            (internal for internal, slug in NINJAS_LEAGUES.items() if slug == league),
            "SPL",
        )
        return self.to_fd_match(rows[0], competition_code=code)

    async def teams(self, competition_code: str) -> dict[str, Any]:
        league = ninjas_league(competition_code)
        clubs = await self.soccerclubs(league=league, active="true")
        teams = []
        for club in clubs:
            team_id = str(club.get("team_id") or "")
            name = str(club.get("name") or "")
            if not team_id or not name:
                continue
            hashed = (
                self.id_map.remember_team(team_id)
                if self.id_map is not None
                else stable_int_id(team_id)
            )
            teams.append(
                {
                    "id": hashed,
                    "name": name,
                    "shortName": club.get("short_name")
                    if isinstance(club.get("short_name"), str)
                    else name,
                    "tla": club.get("abbreviation")
                    if isinstance(club.get("abbreviation"), str)
                    else None,
                    "_ninjas_team_id": team_id,
                }
            )
        return {"teams": teams, "count": len(teams)}

    async def standings_fd(self, competition_code: str) -> dict[str, Any]:
        league = ninjas_league(competition_code)
        rows = await self.soccerstandings(league=league)
        return standings_to_fd(rows, competition_code=competition_code)

    async def competition_finished(self, competition_code: str) -> dict[str, Any]:
        league = ninjas_league(competition_code)
        raw = await self._paginate_matches(league=league, status="final")
        return {
            "matches": [
                self.to_fd_match(item, competition_code=competition_code) for item in raw
            ]
        }

    async def team_finished(
        self,
        team_id: int,
        competition_code: str,
        *,
        ninjas_team_id: str | None = None,
    ) -> dict[str, Any]:
        league = ninjas_league(competition_code)
        team_key = ninjas_team_id or (
            self.id_map.team_key_for(team_id) if self.id_map else None
        )
        if not team_key:
            return {"matches": []}
        raw = await self.soccermatches(league=league, team=team_key, status="final")
        return {
            "matches": [
                self.to_fd_match(item, competition_code=competition_code) for item in raw
            ]
        }

    async def team_matches(
        self,
        team_id: int,
        competition_code: str,
        *,
        ninjas_team_id: str | None = None,
    ) -> dict[str, Any]:
        league = ninjas_league(competition_code)
        team_key = ninjas_team_id or (
            self.id_map.team_key_for(team_id) if self.id_map else None
        )
        if not team_key:
            return {"matches": []}
        raw = await self.soccermatches(league=league, team=team_key)
        return {
            "matches": [
                self.to_fd_match(item, competition_code=competition_code) for item in raw
            ]
        }

    async def _paginate_matches(self, **params: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self.soccermatches(**params, offset=offset)
            if not page:
                break
            rows.extend(page)
            if len(page) < 50:
                break
            offset += len(page)
            if offset > 2000:
                break
        return rows

    def to_fd_match(
        self,
        item: dict[str, Any],
        *,
        competition_code: str,
    ) -> dict[str, Any]:
        game_id = str(item.get("game_id") or "")
        if not game_id:
            raise ProviderError("API Ninjas match is missing game_id.")
        match_id = (
            self.id_map.remember_game(game_id)
            if self.id_map is not None
            else stable_int_id(game_id)
        )
        home = item.get("home_team") or {}
        away = item.get("away_team") or {}
        home_key = str(home.get("team_id") or home.get("name") or "home")
        away_key = str(away.get("team_id") or away.get("name") or "away")
        status = STATUS_MAP.get(str(item.get("status") or "").casefold(), "SCHEDULED")
        home_ft, away_ft = _score_pair(item.get("home_score"), item.get("away_score"))
        home_ht, away_ht = _half_time_pair(item.get("period_scores"))
        code = competition_code.upper()
        return {
            "id": match_id,
            "utcDate": item.get("start_time"),
            "status": status,
            "lastUpdated": item.get("updated_at"),
            "competition": {
                "code": code,
                "name": NINJAS_COMPETITION_NAMES.get(code, code),
            },
            "homeTeam": {
                "id": (
                    self.id_map.remember_team(home_key)
                    if self.id_map is not None
                    else stable_int_id(home_key)
                ),
                "name": str(home.get("name") or home_key),
                "shortName": str(home.get("name") or home_key),
                "tla": home.get("abbreviation")
                if isinstance(home.get("abbreviation"), str)
                else None,
                "_ninjas_team_id": home.get("team_id"),
            },
            "awayTeam": {
                "id": (
                    self.id_map.remember_team(away_key)
                    if self.id_map is not None
                    else stable_int_id(away_key)
                ),
                "name": str(away.get("name") or away_key),
                "shortName": str(away.get("name") or away_key),
                "tla": away.get("abbreviation")
                if isinstance(away.get("abbreviation"), str)
                else None,
                "_ninjas_team_id": away.get("team_id"),
            },
            "score": {
                "fullTime": {"home": home_ft, "away": away_ft},
                "halfTime": {"home": home_ht, "away": away_ht},
            },
            "_ninjas_game_id": game_id,
        }


def fetch_season_matches(
    api_key: str,
    season: int,
    *,
    competition_code: str = "SPL",
) -> list[dict[str, Any]]:
    """Paginate one season of soccermatches (sync; for laboratory import)."""

    import urllib.error
    import urllib.parse
    import urllib.request

    league = ninjas_league(competition_code)
    key = api_key.strip()
    if not key:
        raise ProviderError("NINJAS_API_KEY is missing.")
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        query = urllib.parse.urlencode(
            {"league": league, "season": season, "offset": offset}
        )
        request = urllib.request.Request(
            f"{NINJAS_BASE}/v1/soccermatches?{query}",
            headers={"X-Api-Key": key},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                page = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()[:200]
            suffix = f" {detail}" if detail else ""
            raise ProviderError(
                f"API Ninjas returned HTTP {exc.code} for {competition_code} "
                f"season {season}.{suffix}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"API Ninjas could not be reached ({exc.reason}).") from exc
        if not isinstance(page, list):
            raise ProviderError(
                f"API Ninjas soccermatches did not return a list for {competition_code} "
                f"{season}."
            )
        chunk = [item for item in page if isinstance(item, dict)]
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < 50:
            break
        offset += len(chunk)
        if offset > 2000:
            break
    return rows


def season_to_fd(
    rows: list[dict[str, Any]],
    *,
    competition_code: str,
    season: int,
    id_map: NinjasIdMap | None = None,
) -> dict[str, Any]:
    """Normalize one Ninjas season into a Football-Data-shaped matches payload."""

    client = NinjasClient("unused", id_map=id_map)
    matches = [
        client.to_fd_match(item, competition_code=competition_code) for item in rows
    ]
    for match in matches:
        match["season"] = {
            "startDate": f"{season}-08-01",
            "currentMatchday": None,
        }
    code = competition_code.upper()
    return {
        "competition": {
            "code": code,
            "name": NINJAS_COMPETITION_NAMES.get(code, code),
            "emblem": None,
        },
        "matches": matches,
        "resultSet": {"count": len(matches)},
        "filters": {"season": str(season)},
    }


def standings_to_fd(
    rows: list[dict[str, Any]],
    *,
    competition_code: str,
) -> dict[str, Any]:
    code = competition_code.upper()
    season = ""
    table: list[dict[str, Any]] = []
    for row in rows:
        team = row.get("team") or {}
        team_key = str(team.get("team_id") or team.get("name") or "")
        name = str(team.get("name") or "")
        rank = row.get("league_rank")
        if not team_key or not name or not isinstance(rank, int):
            continue
        if not season:
            season = str(row.get("season") or "")
        table.append(
            {
                "position": rank,
                "team": {
                    "id": stable_int_id(team_key),
                    "name": name,
                    "shortName": team.get("short_name")
                    if isinstance(team.get("short_name"), str)
                    else name,
                    "tla": team.get("abbreviation")
                    if isinstance(team.get("abbreviation"), str)
                    else None,
                    "crest": None,
                },
                "playedGames": row.get("games_played"),
                "won": row.get("wins"),
                "draw": row.get("draws"),
                "lost": row.get("losses"),
                "points": row.get("points"),
                "goalsFor": row.get("goals_for"),
                "goalsAgainst": row.get("goals_against"),
                "goalDifference": row.get("goal_difference"),
                "form": None,
            }
        )
    start_year = season[:4] if season else "unknown"
    return {
        "competition": {
            "code": code,
            "name": NINJAS_COMPETITION_NAMES.get(code, code),
            "emblem": None,
        },
        "season": {
            "startDate": f"{start_year}-08-01" if start_year.isdigit() else "",
            "currentMatchday": None,
        },
        "standings": [{"type": "TOTAL", "table": table}],
        "lastUpdated": None,
    }


def _score_pair(home: Any, away: Any) -> tuple[int | None, int | None]:
    if isinstance(home, str) and "premium" in home.casefold():
        return None, None
    if isinstance(away, str) and "premium" in away.casefold():
        return None, None
    try:
        home_i = int(home) if home is not None else None
        away_i = int(away) if away is not None else None
    except (TypeError, ValueError):
        return None, None
    return home_i, away_i


def _half_time_pair(period_scores: Any) -> tuple[int | None, int | None]:
    if not isinstance(period_scores, dict):
        return None, None
    home_periods = period_scores.get("home")
    away_periods = period_scores.get("away")
    if not isinstance(home_periods, list) or not isinstance(away_periods, list):
        return None, None
    if not home_periods or not away_periods:
        return None, None
    try:
        return int(home_periods[0]), int(away_periods[0])
    except (TypeError, ValueError, IndexError):
        return None, None
