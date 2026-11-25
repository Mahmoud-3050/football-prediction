"""One-shot idempotent daily pipeline: sync -> settle prospective -> lock tomorrow -> price finished -> WAL truncate."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from core.config import ConfigError, load_config
from db.prospective import settle_prospective_predictions
from jobs.tomorrow import run_tomorrow_once
from laboratory.job import run_score_model
from laboratory.store import (
    checkpoint_wal,
    connect,
    ensure_experiment,
    list_competitions,
    record_job_run,
)
from laboratory.sync import run_sync
from main import build_pipeline
from modeling.league_stacks import draw_calibration_kwargs, live_stack_for

logger = logging.getLogger(__name__)


def _price_finished_all(connection: Any, db_path: Path) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for code in list_competitions(connection):
        experiment_id = f"BOT-{code}-live"
        stack = live_stack_for(code, database_path=db_path)
        draw_kwargs = draw_calibration_kwargs(stack)
        ensure_experiment(
            connection,
            {
                "experiment_id": experiment_id,
                "competition": code,
                "k": stack["k"],
                "prior_mean": stack["prior_mean"],
                "dependence": stack["dependence"],
                "half_time_share_mode": stack["half_time_share_mode"],
                "draw_boost": draw_kwargs["draw_boost"],
                "draw_weights": draw_kwargs.get("draw_weights"),
                "draw_pick_threshold": draw_kwargs.get("draw_pick_threshold"),
                "draw_pick_margin": draw_kwargs.get("draw_pick_margin"),
                "draw_tightness_scale": draw_kwargs.get("draw_tightness_scale"),
                "tail_tolerance": 0.001,
                "engine": "score_model",
                "score_model_config_id": stack["model_id"],
                "gemini": False,
                "note": (
                    f"Telegram bot score_model walk-forward on finished matches "
                    f"({code} live stack: k={stack['k']}, {stack['prior_mean']}, "
                    f"{stack['dependence']}, {stack['half_time_share_mode']})."
                ),
            },
        )
        counts = run_score_model(
            connection,
            experiment_id=experiment_id,
            competition=code,
            k=float(stack["k"]),
            model_id=str(stack["model_id"]),
            prior_mean=str(stack["prior_mean"]),
            dependence=str(stack["dependence"]),
            half_time_share_mode=str(stack["half_time_share_mode"]),
            ratio_mode=str(stack.get("ratio_mode") or "one_pass"),
            decay_half_life_days=(
                float(stack["decay_half_life_days"])
                if stack.get("decay_half_life_days") is not None
                else None
            ),
            persistent_priors=bool(stack.get("persistent_priors")),
            elo_blend=float(stack.get("elo_blend") or 0.0),
            domestic_prior_weight=float(stack.get("domestic_prior_weight") or 0.0),
            **draw_kwargs,
        )
        summary.append(counts)
    return summary


def run_daily_sync() -> dict[str, Any]:
    load_dotenv(override=True)
    try:
        config = load_config()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    lock_path = config.database_path.parent / "daily_sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning("Another daily-sync run holds %s; exiting cleanly.", lock_path)
        lock_file.close()
        return {"status": "skipped", "reason": "lock_held"}

    started = datetime.now(timezone.utc).isoformat()
    connection = connect(config.database_path)
    try:
        logger.info("Step 1/4: Running laboratory.sync (fixtures, standings, PL Opta xG)")
        sync_summary = run_sync(
            connection,
            api_key=config.football_api_key,
            ninjas_api_key=config.ninjas_api_key or None,
        )

        logger.info("Step 2/4: Settling prospective predictions")
        settle_summary = settle_prospective_predictions(connection)

        logger.info("Step 3/4: Pricing upcoming tomorrow fixtures into prospective_predictions")
        pipeline = build_pipeline(config)
        tomorrow_summary = asyncio.run(run_tomorrow_once(pipeline))

        logger.info("Step 4/4: Pricing finished matches under active live stacks + WAL TRUNCATE")
        priced_summary = _price_finished_all(connection, config.database_path)
        checkpoint_wal(connection, mode="TRUNCATE")

        result = {
            "status": "ok",
            "sync_status": sync_summary.get("status"),
            "settle": settle_summary,
            "tomorrow": tomorrow_summary,
            "price_finished_leagues": len(priced_summary),
        }
        record_job_run(
            connection,
            name="daily_sync",
            started_at=started,
            finished_at=datetime.now(timezone.utc).isoformat(),
            ok=True,
            summary=result,
        )
        print(json.dumps(result, indent=2, default=str))
        return result
    except Exception as exc:
        logger.exception("daily-sync failed")
        record_job_run(
            connection,
            name="daily_sync",
            started_at=started,
            finished_at=datetime.now(timezone.utc).isoformat(),
            ok=False,
            summary={"error": str(exc)},
        )
        raise
    finally:
        connection.close()
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    sys.exit(0 if run_daily_sync().get("status") in {"ok", "skipped"} else 1)
