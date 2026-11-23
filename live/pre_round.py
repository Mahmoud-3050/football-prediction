"""One-day pre-round prediction job.

Prices upcoming fixtures whose kickoff falls within PROSPECTIVE_LEAD_DAYS,
using the same pipeline as /predict. Re-prices even when a completed/partial
prediction already exists. Runs as an asyncio background task.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from providers.data_fetcher import (
    PROSPECTIVE_LEAD_DAYS,
    TODAY_MATCH_COMPETITIONS,
    fixture_in_prospective_window,
    fixtures_from_payload,
)
from core.models import Fixture, PipelineResult, ProviderError
from live.pipeline import Pipeline
from core.timeutil import CAIRO_TZ

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 3600
SOURCE = "pre-round"


def cairo_days_covering(start: datetime, end: datetime) -> list[date]:
    """Cairo calendar days that overlap the closed UTC window [start, end]."""

    if end < start:
        return []
    cursor = start.astimezone(CAIRO_TZ).date()
    last = end.astimezone(CAIRO_TZ).date()
    days: list[date] = []
    while cursor <= last:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def select_prospective_fixtures(
    fixtures: list[Fixture],
    *,
    now: datetime | None = None,
    lead_days: float = PROSPECTIVE_LEAD_DAYS,
) -> list[Fixture]:
    moment = now or datetime.now(timezone.utc)
    selected = [
        fixture
        for fixture in fixtures
        if fixture.competition_code in TODAY_MATCH_COMPETITIONS
        and fixture_in_prospective_window(fixture, now=moment, lead_days=lead_days)
    ]
    selected.sort(key=lambda fixture: (fixture.utc_date or "", fixture.id))
    return selected


async def collect_window_fixtures(
    pipeline: Pipeline,
    *,
    now: datetime | None = None,
    lead_days: float = PROSPECTIVE_LEAD_DAYS,
) -> list[Fixture]:
    moment = now or datetime.now(timezone.utc)
    end = moment + timedelta(days=lead_days)
    seen: set[int] = set()
    collected: list[Fixture] = []
    async with pipeline.fetcher.request_scope():
        for day in cairo_days_covering(moment, end):
            payload = await pipeline.fetcher.get_matches_for_cairo_day(day)
            for fixture in fixtures_from_payload(payload, cairo_day=day):
                if fixture.id in seen:
                    continue
                seen.add(fixture.id)
                collected.append(fixture)
    return select_prospective_fixtures(collected, now=moment, lead_days=lead_days)


async def run_pre_round_once(
    pipeline: Pipeline,
    *,
    now: datetime | None = None,
    lead_days: float = PROSPECTIVE_LEAD_DAYS,
) -> dict[str, int]:
    """Price each eligible fixture in the lead window (including re-prices)."""

    counts = {
        "candidates": 0,
        "priced": 0,
        "abstained": 0,
        "failed": 0,
    }
    try:
        fixtures = await collect_window_fixtures(
            pipeline, now=now, lead_days=lead_days
        )
    except ProviderError as exc:
        logger.warning("pre-round fixture fetch failed: %s", exc.reason)
        counts["failed"] += 1
        return counts
    counts["candidates"] = len(fixtures)
    for fixture in fixtures:
        try:
            result = await pipeline.execute_by_match_id(
                chat_id=None,
                user_id=None,
                match_id=fixture.id,
                source=SOURCE,
            )
        except Exception:
            logger.exception("pre-round pricing failed for match %s", fixture.id)
            counts["failed"] += 1
            continue
        _tally(result, counts)
        logger.info(
            "pre-round match #%s status=%s stage=%s",
            fixture.id,
            result.status,
            result.stage,
        )
    return counts


def _tally(result: PipelineResult, counts: dict[str, int]) -> None:
    if result.status in {"completed", "partial"}:
        counts["priced"] += 1
    elif result.status == "abstained":
        counts["abstained"] += 1
    else:
        counts["failed"] += 1


async def pre_round_loop(
    pipeline: Pipeline,
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    lead_days: float = PROSPECTIVE_LEAD_DAYS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Periodic scan. First pass runs immediately after startup."""

    stop = stop_event or asyncio.Event()
    logger.info(
        "pre-round job started (lead_days=%s, interval_seconds=%s)",
        lead_days,
        interval_seconds,
    )
    while not stop.is_set():
        try:
            counts = await run_pre_round_once(pipeline, lead_days=lead_days)
            logger.info("pre-round pass finished: %s", counts)
        except Exception:
            logger.exception("pre-round pass crashed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(1.0, interval_seconds))
        except asyncio.TimeoutError:
            continue
    logger.info("pre-round job stopped")
