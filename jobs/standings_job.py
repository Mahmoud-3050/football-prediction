"""Refresh league standings after a round's fixtures finish."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from typing import Any

from data_fetcher import FINISHED_STATUSES, fixtures_from_payload
from jobs import JOB_COMPETITIONS
from laboratory.standings import (
    STANDINGS_COMPETITIONS,
    ensure_standings,
    latest_season,
    list_standings_rows,
)
from models import ProviderError
from pipeline import Pipeline
from timeutil import cairo_today

logger = logging.getLogger(__name__)


def competitions_needing_standings_refresh(
    connection: sqlite3.Connection,
    finished_by_competition: dict[str, int],
    *,
    stored_finished: dict[str, int] | None = None,
) -> list[str]:
    """Return league codes whose finished-today count rose since the last check."""

    previous = stored_finished or {}
    refresh: list[str] = []
    for code in STANDINGS_COMPETITIONS:
        if code not in JOB_COMPETITIONS:
            continue
        current = int(finished_by_competition.get(code, 0))
        prior = int(previous.get(code, 0))
        if current > prior:
            refresh.append(code)
            continue
        # First server pass with finished matches and no standings yet.
        if current > 0 and not _has_rows(connection, code):
            refresh.append(code)
    return refresh


def _has_rows(connection: sqlite3.Connection, competition: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM standings_rows WHERE competition = ? LIMIT 1",
        (competition.upper(),),
    ).fetchone()
    return row is not None


async def count_finished_today(
    pipeline: Pipeline,
    *,
    day: date | None = None,
) -> dict[str, int]:
    target = day or cairo_today()
    async with pipeline.fetcher.request_scope():
        payload = await pipeline.fetcher.get_matches_for_cairo_day(
            target,
            competitions=tuple(
                code for code in JOB_COMPETITIONS if code in STANDINGS_COMPETITIONS
            ),
        )
    fixtures = fixtures_from_payload(payload, cairo_day=target)
    counts: dict[str, int] = {}
    for fixture in fixtures:
        if fixture.status not in FINISHED_STATUSES:
            continue
        code = fixture.competition_code
        counts[code] = counts.get(code, 0) + 1
    return counts


async def run_standings_once(
    pipeline: Pipeline,
    connection: sqlite3.Connection,
    *,
    api_key: str,
    state: dict[str, Any] | None = None,
    day: date | None = None,
) -> dict[str, Any]:
    """Update standings for leagues that completed fixtures today."""

    memory = state if state is not None else {}
    prior = dict(memory.get("finished_today") or {})
    day_key = (day or cairo_today()).isoformat()
    if memory.get("day") != day_key:
        prior = {}
        memory["day"] = day_key

    summary: dict[str, Any] = {
        "day": day_key,
        "finished": {},
        "refreshed": [],
        "skipped": [],
        "errors": [],
    }
    try:
        finished = await count_finished_today(pipeline, day=day)
    except ProviderError as exc:
        logger.warning("standings job fixture fetch failed: %s", exc.reason)
        summary["errors"].append(str(exc.reason))
        return summary

    summary["finished"] = finished
    targets = competitions_needing_standings_refresh(
        connection, finished, stored_finished=prior
    )
    for code in STANDINGS_COMPETITIONS:
        if code not in targets:
            if code in finished:
                summary["skipped"].append(code)
            continue
        try:
            result = ensure_standings(connection, api_key, code, force=True)
            summary["refreshed"].append(result)
            logger.info(
                "standings refreshed for %s season=%s rows=%s",
                code,
                result.get("season"),
                result.get("rows"),
            )
        except Exception as exc:  # noqa: BLE001 — continue other leagues
            logger.exception("standings refresh failed for %s", code)
            summary["errors"].append(f"{code}: {exc}")

    memory["finished_today"] = finished
    # Touch latest season metadata so dashboard readers see a fresh fetched_at.
    for code in summary["refreshed"]:
        season = latest_season(connection, str(code["competition"]))
        if season:
            list_standings_rows(connection, str(code["competition"]), season=season)
    return summary
