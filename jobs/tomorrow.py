"""Predict Cairo-tomorrow fixtures across all job leagues."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from data_fetcher import fixture_is_predictable, fixtures_from_payload
from jobs import JOB_COMPETITIONS
from models import Fixture, PipelineResult, ProviderError
from pipeline import Pipeline
from timeutil import CAIRO_TZ, cairo_today

logger = logging.getLogger(__name__)
SOURCE = "tomorrow-job"


def cairo_tomorrow(*, now: datetime | None = None) -> date:
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(CAIRO_TZ).date() + timedelta(days=1)


def select_tomorrow_fixtures(
    fixtures: list[Fixture],
    *,
    day: date,
    now: datetime | None = None,
) -> list[Fixture]:
    moment = now or datetime.now(timezone.utc)
    selected = [
        fixture
        for fixture in fixtures
        if fixture.competition_code in JOB_COMPETITIONS
        and fixture_is_predictable(fixture, now=moment)
    ]
    selected.sort(key=lambda fixture: (fixture.utc_date or "", fixture.id))
    _ = day
    return selected


async def collect_tomorrow_fixtures(
    pipeline: Pipeline,
    *,
    now: datetime | None = None,
) -> tuple[date, list[Fixture]]:
    day = cairo_tomorrow(now=now)
    async with pipeline.fetcher.request_scope():
        payload = await pipeline.fetcher.get_matches_for_cairo_day(
            day,
            competitions=JOB_COMPETITIONS,
        )
    fixtures = fixtures_from_payload(payload, cairo_day=day)
    return day, select_tomorrow_fixtures(fixtures, day=day, now=now)


async def run_tomorrow_once(
    pipeline: Pipeline,
    *,
    now: datetime | None = None,
) -> dict[str, int | str]:
    """Price each predictable fixture kicking off on Cairo tomorrow."""

    counts: dict[str, int | str] = {
        "day": "",
        "candidates": 0,
        "priced": 0,
        "abstained": 0,
        "failed": 0,
    }
    try:
        day, fixtures = await collect_tomorrow_fixtures(pipeline, now=now)
    except ProviderError as exc:
        logger.warning("tomorrow job fixture fetch failed: %s", exc.reason)
        counts["failed"] = 1
        return counts
    counts["day"] = day.isoformat()
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
            logger.exception("tomorrow job pricing failed for match %s", fixture.id)
            counts["failed"] = int(counts["failed"]) + 1
            continue
        _tally(result, counts)
        logger.info(
            "tomorrow job match #%s status=%s stage=%s",
            fixture.id,
            result.status,
            result.stage,
        )
    return counts


def _tally(result: PipelineResult, counts: dict[str, int | str]) -> None:
    if result.status in {"completed", "partial"}:
        counts["priced"] = int(counts["priced"]) + 1
    elif result.status == "abstained":
        counts["abstained"] = int(counts["abstained"]) + 1
    else:
        counts["failed"] = int(counts["failed"]) + 1


# Re-export for tests that may want "today" helpers nearby.
__all__ = [
    "cairo_tomorrow",
    "collect_tomorrow_fixtures",
    "run_tomorrow_once",
    "select_tomorrow_fixtures",
    "cairo_today",
]
