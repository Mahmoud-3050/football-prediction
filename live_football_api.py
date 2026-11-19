"""Live Football API client for Saudi Pro League season history.

Uses `/league_fixtures` and normalizes into Football-Data-shaped payloads so the
laboratory importer can store seasons alongside Football-Data leagues.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from models import ProviderError

LFA_BASE = "https://live-football-api.com/api/v1"
# Internal code → Live Football league id.
# Note: 8485 is Serbia U19; Saudi Pro League is 8454.
LFA_LEAGUE_IDS: dict[str, int] = {
    "SPL": 8454,
}
LFA_COMPETITION_NAMES: dict[str, str] = {
    "SPL": "Saudi Pro League",
}
STATUS_MAP = {
    "finished": "FINISHED",
    "ft": "FINISHED",
    "live": "IN_PLAY",
    "inplay": "IN_PLAY",
    "scheduled": "SCHEDULED",
    "notstarted": "SCHEDULED",
    "ns": "SCHEDULED",
    "postponed": "POSTPONED",
    "canceled": "CANCELLED",
    "cancelled": "CANCELLED",
    "suspended": "SUSPENDED",
}


def is_live_football_competition(code: str) -> bool:
    return code.upper() in LFA_LEAGUE_IDS


def lfa_league_id(code: str) -> int:
    return LFA_LEAGUE_IDS[code.upper()]


def season_label(season: int) -> str:
    """Map laboratory start-year (2024) to Live Football season id (2024/2025)."""

    return f"{season}/{season + 1}"


def fetch_league_fixtures(
    api_key: str,
    *,
    competition_code: str = "SPL",
    season: int | str | None = None,
) -> dict[str, Any]:
    """Fetch `/league_fixtures` for one league/season (1 credit)."""

    key = api_key.strip()
    if not key:
        raise ProviderError("LIVE_FOOTBALL_API_KEY is missing.")
    code = competition_code.upper()
    params: dict[str, Any] = {
        "api_key": key,
        "league_id": lfa_league_id(code),
        "lang": "en",
    }
    if season is not None:
        params["season"] = season_label(int(season)) if isinstance(season, int) else str(season)
    query = urllib.parse.urlencode(params)
    url = f"{LFA_BASE}/league_fixtures?{query}"
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()[:200]
        suffix = f" {detail}" if detail else ""
        raise ProviderError(
            f"Live Football API returned HTTP {exc.code} for {code} season "
            f"{params.get('season', 'current')}.{suffix}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Live Football API could not be reached ({exc.reason}).") from exc
    if not isinstance(body, dict) or not body.get("success"):
        message = ""
        if isinstance(body, dict):
            message = str(body.get("message") or body.get("error") or "")
        raise ProviderError(
            f"Live Football API rejected {code} fixtures"
            + (f": {message}" if message else ".")
        )
    data = body.get("data")
    if not isinstance(data, dict):
        raise ProviderError(f"Live Football API returned no fixtures payload for {code}.")
    return data


def season_to_fd(
    data: dict[str, Any],
    *,
    competition_code: str,
    season: int,
) -> dict[str, Any]:
    """Flatten week-grouped fixtures into a Football-Data-shaped matches body."""

    code = competition_code.upper()
    matches: list[dict[str, Any]] = []
    for week in data.get("weeks") or []:
        if not isinstance(week, dict):
            continue
        for item in week.get("matches") or []:
            if not isinstance(item, dict):
                continue
            match = _to_fd_match(item, competition_code=code, season=season)
            if match is not None:
                matches.append(match)
    league_id = data.get("league_id") or LFA_LEAGUE_IDS.get(code)
    emblem = (
        f"https://live-football-api.com/leagues/{league_id}.png"
        if league_id is not None
        else None
    )
    return {
        "competition": {
            "code": code,
            "name": (
                str(data.get("league_name") or "")
                or LFA_COMPETITION_NAMES.get(code, code)
            ),
            "emblem": emblem,
        },
        "matches": matches,
        "resultSet": {"count": len(matches)},
        "filters": {"season": season_label(season)},
        "_live_football": {
            "league_id": data.get("league_id"),
            "season": data.get("season"),
            "available_seasons": data.get("available_seasons"),
        },
    }


def _to_fd_match(
    item: dict[str, Any],
    *,
    competition_code: str,
    season: int,
) -> dict[str, Any] | None:
    match_id = item.get("id")
    if isinstance(match_id, str) and match_id.isdigit():
        match_id = int(match_id)
    if not isinstance(match_id, int):
        return None
    home = item.get("home") or {}
    away = item.get("away") or {}
    home_id = _team_id(home)
    away_id = _team_id(away)
    home_name = home.get("name") if isinstance(home.get("name"), str) else None
    away_name = away.get("name") if isinstance(away.get("name"), str) else None
    if home_id is None or away_id is None or not home_name or not away_name:
        return None
    kickoff = _kickoff_utc(item.get("date"), item.get("kickoff"))
    if not kickoff:
        return None
    status_obj = item.get("status") if isinstance(item.get("status"), dict) else {}
    raw_status = str(status_obj.get("status") or status_obj.get("state") or "").casefold()
    status = STATUS_MAP.get(raw_status.replace(" ", ""), "SCHEDULED")
    if status_obj.get("is_live") is True:
        status = "IN_PLAY"
    ht = item.get("halftime") if isinstance(item.get("halftime"), dict) else {}
    home_ft, away_ft = _score_int(home.get("score")), _score_int(away.get("score"))
    home_ht, away_ht = _score_int(ht.get("home")), _score_int(ht.get("away"))
    league = item.get("league") if isinstance(item.get("league"), dict) else {}
    return {
        "id": match_id,
        "utcDate": kickoff,
        "status": status,
        "lastUpdated": None,
        "season": {"startDate": f"{season}-08-01", "currentMatchday": None},
        "competition": {
            "code": competition_code,
            "name": str(league.get("name") or LFA_COMPETITION_NAMES.get(competition_code, competition_code)),
        },
        "homeTeam": {
            "id": home_id,
            "name": home_name,
            "shortName": home_name,
            "tla": None,
            "crest": home.get("logo") if isinstance(home.get("logo"), str) else None,
        },
        "awayTeam": {
            "id": away_id,
            "name": away_name,
            "shortName": away_name,
            "tla": None,
            "crest": away.get("logo") if isinstance(away.get("logo"), str) else None,
        },
        "score": {
            "fullTime": {"home": home_ft, "away": away_ft},
            "halfTime": {"home": home_ht, "away": away_ht},
        },
        "_live_football_match_id": match_id,
    }


def _team_id(team: dict[str, Any]) -> int | None:
    value = team.get("id")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _kickoff_utc(date_value: Any, kickoff_value: Any) -> str | None:
    if not isinstance(date_value, str) or len(date_value) < 10:
        return None
    day = date_value[:10]
    time_part = "00:00"
    if isinstance(kickoff_value, str) and kickoff_value.strip():
        text = kickoff_value.strip()
        if len(text) >= 5 and text[2] == ":":
            time_part = text[:5]
    return f"{day}T{time_part}:00Z"


def _score_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None
