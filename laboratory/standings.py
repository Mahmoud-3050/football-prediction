"""Fetch league standings from Football-Data.org and cache them in SQLite."""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from laboratory.football_data import COMPETITION_NAMES

STANDINGS_URL = "https://api.football-data.org/v4/competitions/{competition}/standings"
# Cups do not expose a single TOTAL table on this endpoint.
STANDINGS_COMPETITIONS = ("PL", "PD", "FL1", "BL1", "SA", "PPL", "DED", "SPL")


def fetch_standings(
    api_key: str,
    competition: str,
    *,
    season: int | None = None,
    ninjas_api_key: str | None = None,
) -> dict[str, Any]:
    code = competition.upper()
    label = COMPETITION_NAMES.get(code, code)
    from api_ninjas import NINJAS_BASE, is_ninjas_competition, ninjas_league, standings_to_fd

    if is_ninjas_competition(code):
        key = (ninjas_api_key or "").strip()
        if not key:
            raise RuntimeError(
                f"{label} standings need NINJAS_API_KEY (API Ninjas), not Football-Data."
            )
        url = f"{NINJAS_BASE}/v1/soccerstandings?league={ninjas_league(code)}"
        if season is not None:
            url = f"{url}&season={season}"
        request = urllib.request.Request(url, headers={"X-Api-Key": key})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                rows = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"API Ninjas rejected the {label} standings request with HTTP {exc.code}."
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"API Ninjas could not be reached ({exc.reason}).") from exc
        if not isinstance(rows, list):
            raise RuntimeError(f"API Ninjas returned no standings for {label}.")
        return standings_to_fd(
            [row for row in rows if isinstance(row, dict)],
            competition_code=code,
        )

    url = STANDINGS_URL.format(competition=code)
    if season is not None:
        url = f"{url}?season={season}"
    request = urllib.request.Request(url, headers={"X-Auth-Token": api_key})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Football-Data.org rejected the {label} standings request with HTTP {exc.code}."
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Football-Data.org could not be reached ({exc.reason}).") from exc
    if not isinstance(body, dict) or not isinstance(body.get("standings"), list):
        raise RuntimeError(f"Football-Data.org returned no standings for {label}.")
    return body


def parse_standings(body: dict[str, Any], *, competition: str) -> dict[str, Any]:
    """Normalize a TOTAL table into rows ready for SQLite."""

    code = competition.upper()
    season_info = body.get("season") or {}
    start = str(season_info.get("startDate") or "")
    season = start[:4] if len(start) >= 4 and start[:4].isdigit() else ""
    if not season:
        filters = body.get("filters") or {}
        season = str(filters.get("season") or "").strip()
    table = _total_table(body)
    rows: list[dict[str, Any]] = []
    for entry in table:
        team = entry.get("team") or {}
        team_id = team.get("id")
        team_name = team.get("name")
        position = entry.get("position")
        if not isinstance(team_id, int) or not isinstance(team_name, str):
            continue
        if not isinstance(position, int):
            continue
        crest = team.get("crest") if isinstance(team.get("crest"), str) else None
        short_name = team.get("shortName") if isinstance(team.get("shortName"), str) else None
        rows.append(
            {
                "competition": code,
                "season": season or "unknown",
                "position": position,
                "team_id": str(team_id),
                "team_name": team_name,
                "short_name": short_name,
                "crest_url": crest,
                "played": _int_or_none(entry.get("playedGames")),
                "won": _int_or_none(entry.get("won")),
                "draw": _int_or_none(entry.get("draw")),
                "lost": _int_or_none(entry.get("lost")),
                "goals_for": _int_or_none(entry.get("goalsFor")),
                "goals_against": _int_or_none(entry.get("goalsAgainst")),
                "goal_difference": _int_or_none(entry.get("goalDifference")),
                "points": _int_or_none(entry.get("points")),
                "form": _normalize_form(entry.get("form")),
            }
        )
    matchday = season_info.get("currentMatchday")
    competition_info = body.get("competition") if isinstance(body.get("competition"), dict) else {}
    emblem = (
        competition_info.get("emblem")
        if isinstance(competition_info.get("emblem"), str)
        else None
    )
    competition_name = (
        competition_info.get("name")
        if isinstance(competition_info.get("name"), str)
        else COMPETITION_NAMES.get(code, code)
    )
    return {
        "competition": code,
        "competition_name": competition_name,
        "emblem_url": emblem,
        "season": season or "unknown",
        "matchday": matchday if isinstance(matchday, int) else None,
        "last_updated": body.get("lastUpdated") if isinstance(body.get("lastUpdated"), str) else None,
        "rows": rows,
        "payload": body,
    }


def replace_standings(connection: sqlite3.Connection, parsed: dict[str, Any]) -> int:
    from laboratory.store import upsert_competition, upsert_team

    competition = str(parsed["competition"])
    season = str(parsed["season"])
    connection.execute(
        "DELETE FROM standings_rows WHERE competition = ? AND season = ?",
        (competition, season),
    )
    connection.execute(
        "DELETE FROM standings_meta WHERE competition = ? AND season = ?",
        (competition, season),
    )
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO standings_meta (
            competition, season, fetched_at, matchday, last_updated, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            competition,
            season,
            now,
            parsed.get("matchday"),
            parsed.get("last_updated"),
            json.dumps(parsed["payload"], sort_keys=True),
        ),
    )
    upsert_competition(
        connection,
        code=competition,
        name=str(parsed.get("competition_name") or COMPETITION_NAMES.get(competition, competition)),
        emblem_url=parsed.get("emblem_url") if isinstance(parsed.get("emblem_url"), str) else None,
    )
    for row in parsed["rows"]:
        upsert_team(
            connection,
            team_id=str(row["team_id"]),
            name=str(row["team_name"]),
            crest_url=row.get("crest_url") if isinstance(row.get("crest_url"), str) else None,
            short_name=row.get("short_name") if isinstance(row.get("short_name"), str) else None,
        )
        connection.execute(
            """
            INSERT INTO standings_rows (
                competition, season, position, team_id, team_name,
                played, won, draw, lost, goals_for, goals_against,
                goal_difference, points, form
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["competition"],
                row["season"],
                row["position"],
                row["team_id"],
                row["team_name"],
                row["played"],
                row["won"],
                row["draw"],
                row["lost"],
                row["goals_for"],
                row["goals_against"],
                row["goal_difference"],
                row["points"],
                row["form"],
            ),
        )
    connection.commit()
    backfill_form_from_matches(connection, competition, season)
    return len(parsed["rows"])


def _normalize_form(value: Any) -> str | None:
    """Accept API form as 'W,W,D' or ['W','W','D']."""

    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, list):
        marks = []
        for item in value:
            token = str(item).strip().upper()[:1]
            if token in {"W", "D", "L"}:
                marks.append(token)
        return ",".join(marks) if marks else None
    return None


def backfill_form_from_matches(
    connection: sqlite3.Connection,
    competition: str,
    season: str,
    *,
    limit: int = 5,
) -> int:
    """Fill empty form cells from recent finished matches in the shared DB.

    Football-Data free-tier standings often omit `form`. When league history is
    already imported, derive last-N W/D/L so the dashboard Form column can show.
    """

    code = competition.upper()
    rows = list(
        connection.execute(
            """
            SELECT team_id FROM standings_rows
            WHERE competition = ? AND season = ?
              AND (form IS NULL OR TRIM(form) = '')
            """,
            (code, season),
        )
    )
    updated = 0
    for row in rows:
        team_id = str(row["team_id"])
        form = _form_from_matches(connection, code, team_id, limit=limit)
        if not form:
            continue
        connection.execute(
            """
            UPDATE standings_rows
            SET form = ?
            WHERE competition = ? AND season = ? AND team_id = ?
            """,
            (form, code, season, team_id),
        )
        updated += 1
    if updated:
        connection.commit()
    return updated


def _form_from_matches(
    connection: sqlite3.Connection,
    competition: str,
    team_id: str,
    *,
    limit: int = 5,
) -> str | None:
    matches = list(
        connection.execute(
            """
            SELECT m.home_id, m.away_id, r.ft_home, r.ft_away
            FROM matches m
            JOIN result_revisions r
              ON r.match_id = m.match_id
             AND r.revision = (
                 SELECT MAX(revision) FROM result_revisions WHERE match_id = m.match_id
             )
            WHERE m.competition = ?
              AND (m.home_id = ? OR m.away_id = ?)
              AND r.ft_home IS NOT NULL
              AND r.ft_away IS NOT NULL
            ORDER BY m.kickoff DESC
            LIMIT ?
            """,
            (competition, team_id, team_id, limit),
        )
    )
    if not matches:
        return None
    # Chronological form (oldest → newest) matches Football-Data style.
    marks: list[str] = []
    for match in reversed(matches):
        home_goals = int(match["ft_home"])
        away_goals = int(match["ft_away"])
        is_home = str(match["home_id"]) == team_id
        for_goals = home_goals if is_home else away_goals
        against = away_goals if is_home else home_goals
        if for_goals > against:
            marks.append("W")
        elif for_goals < against:
            marks.append("L")
        else:
            marks.append("D")
    return ",".join(marks)


def has_standings(connection: sqlite3.Connection, competition: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM standings_rows WHERE competition = ? LIMIT 1",
        (competition.upper(),),
    ).fetchone()
    return row is not None


def latest_season(connection: sqlite3.Connection, competition: str) -> str | None:
    row = connection.execute(
        """
        SELECT season FROM standings_meta
        WHERE competition = ?
        ORDER BY season DESC
        LIMIT 1
        """,
        (competition.upper(),),
    ).fetchone()
    return None if row is None else str(row["season"])


def list_standings_rows(
    connection: sqlite3.Connection,
    competition: str,
    *,
    season: str | None = None,
) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    code = competition.upper()
    chosen = season or latest_season(connection, code)
    if chosen is None:
        return None, []
    meta = connection.execute(
        """
        SELECT * FROM standings_meta
        WHERE competition = ? AND season = ?
        """,
        (code, chosen),
    ).fetchone()
    rows = list(
        connection.execute(
            """
            SELECT sr.*,
                   t.crest_url AS crest_url,
                   COALESCE(NULLIF(t.display_name, ''), sr.team_name) AS display_name
            FROM standings_rows sr
            LEFT JOIN teams t ON t.team_id = sr.team_id
            WHERE sr.competition = ? AND sr.season = ?
            ORDER BY sr.position ASC, sr.team_name ASC
            """,
            (code, chosen),
        )
    )
    return meta, rows


def ensure_standings(
    connection: sqlite3.Connection,
    api_key: str,
    competition: str,
    *,
    force: bool = False,
    season: int | None = None,
    ninjas_api_key: str | None = None,
) -> dict[str, Any]:
    """Fetch from the API only when the DB has no rows (or force refresh)."""

    code = competition.upper()
    if code not in STANDINGS_COMPETITIONS:
        raise ValueError(f"Standings are not supported for {code}.")
    if not force and has_standings(connection, code):
        meta, rows = list_standings_rows(connection, code)
        if meta is not None and any(
            not row["form"] or not str(row["form"]).strip() for row in rows
        ):
            backfill_form_from_matches(connection, code, str(meta["season"]))
            meta, rows = list_standings_rows(connection, code)
        return {
            "competition": code,
            "season": None if meta is None else meta["season"],
            "rows": len(rows),
            "source": "db",
        }
    body = fetch_standings(
        api_key,
        code,
        season=season,
        ninjas_api_key=ninjas_api_key,
    )
    parsed = parse_standings(body, competition=code)
    count = replace_standings(connection, parsed)
    return {
        "competition": code,
        "season": parsed["season"],
        "rows": count,
        "source": "api",
    }


def import_all_standings(
    connection: sqlite3.Connection,
    api_key: str,
    *,
    competitions: tuple[str, ...] = STANDINGS_COMPETITIONS,
    force: bool = True,
    pause_seconds: float = 7.0,
    ninjas_api_key: str | None = None,
) -> list[dict[str, Any]]:
    import time

    summary: list[dict[str, Any]] = []
    for index, code in enumerate(competitions):
        if index:
            time.sleep(pause_seconds)
        try:
            summary.append(
                ensure_standings(
                    connection,
                    api_key,
                    code,
                    force=force,
                    ninjas_api_key=ninjas_api_key,
                )
            )
        except Exception as exc:  # noqa: BLE001 — collect per-league failures
            summary.append(
                {
                    "competition": code.upper(),
                    "season": None,
                    "rows": 0,
                    "source": "error",
                    "error": str(exc),
                }
            )
    return summary


def _total_table(body: dict[str, Any]) -> list[dict[str, Any]]:
    for block in body.get("standings") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "TOTAL":
            continue
        table = block.get("table")
        if isinstance(table, list):
            return [row for row in table if isinstance(row, dict)]
    # Fall back to the first table when TOTAL is absent.
    for block in body.get("standings") or []:
        if not isinstance(block, dict):
            continue
        table = block.get("table")
        if isinstance(table, list) and table:
            return [row for row in table if isinstance(row, dict)]
    return []


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
