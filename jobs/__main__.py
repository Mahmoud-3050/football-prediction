"""Run server jobs: tomorrow predictions + standings refresh after rounds.

Independent of the Telegram bot. Start with:

    python -m jobs
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from core.config import ConfigError, load_config
from jobs.standings_job import run_standings_once
from jobs.tomorrow import run_tomorrow_once
from laboratory.store import connect, record_job_run
from main import build_pipeline

DEFAULT_INTERVAL_SECONDS = 3600


async def jobs_loop(
    pipeline: Any,
    connection: Any,
    *,
    api_key: str,
    ninjas_api_key: str | None = None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    stop_event: asyncio.Event | None = None,
) -> None:
    stop = stop_event or asyncio.Event()
    standings_state: dict[str, Any] = {}
    logging.getLogger(__name__).info(
        "server jobs started (interval=%ss)", int(interval_seconds)
    )
    while not stop.is_set():
        await _run_tomorrow_tracked(pipeline, connection)
        await _run_standings_tracked(
            pipeline,
            connection,
            api_key=api_key,
            ninjas_api_key=ninjas_api_key,
            state=standings_state,
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(1.0, interval_seconds))
        except asyncio.TimeoutError:
            continue
    logging.getLogger(__name__).info("server jobs stopped")


async def _run_tomorrow_tracked(pipeline: Any, connection: Any) -> None:
    started = datetime.now(timezone.utc).isoformat()
    try:
        tomorrow = await run_tomorrow_once(pipeline)
        logging.getLogger(__name__).info("tomorrow job finished: %s", tomorrow)
        record_job_run(
            connection,
            name="tomorrow",
            started_at=started,
            finished_at=datetime.now(timezone.utc).isoformat(),
            ok=True,
            summary=dict(tomorrow),
        )
    except Exception as exc:
        logging.getLogger(__name__).exception("tomorrow job crashed")
        record_job_run(
            connection,
            name="tomorrow",
            started_at=started,
            finished_at=datetime.now(timezone.utc).isoformat(),
            ok=False,
            summary={"error": str(exc)},
        )


async def _run_standings_tracked(
    pipeline: Any,
    connection: Any,
    *,
    api_key: str,
    ninjas_api_key: str | None = None,
    state: dict[str, Any],
) -> None:
    started = datetime.now(timezone.utc).isoformat()
    try:
        standings = await run_standings_once(
            pipeline,
            connection,
            api_key=api_key,
            ninjas_api_key=ninjas_api_key,
            state=state,
        )
        logging.getLogger(__name__).info("standings job finished: %s", standings)
        record_job_run(
            connection,
            name="standings",
            started_at=started,
            finished_at=datetime.now(timezone.utc).isoformat(),
            ok=True,
            summary=dict(standings),
        )
    except Exception as exc:
        logging.getLogger(__name__).exception("standings job crashed")
        record_job_run(
            connection,
            name="standings",
            started_at=started,
            finished_at=datetime.now(timezone.utc).isoformat(),
            ok=False,
            summary={"error": str(exc)},
        )


def main() -> None:
    load_dotenv(override=True)
    try:
        config = load_config()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    interval = float(
        os.environ.get("JOBS_INTERVAL_SECONDS")
        or config.pre_round_interval_seconds
        or DEFAULT_INTERVAL_SECONDS
    )
    pipeline = build_pipeline(config)
    connection = connect(config.database_path)
    asyncio.run(
        jobs_loop(
            pipeline,
            connection,
            api_key=config.football_api_key,
            ninjas_api_key=config.ninjas_api_key or None,
            interval_seconds=interval,
        )
    )


if __name__ == "__main__":
    main()
