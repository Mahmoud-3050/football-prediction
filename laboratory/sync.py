"""Background data sync for matches, results, standings, and finished pricing.

Syncs:
1. Match fixtures + FT/HT results for all supported competitions.
   Prefer an incremental Cairo date window (±14 days) when the competition
   already has stored season history; otherwise bootstrap the full season.
2. For finished matches in the sync window: price with the live score_model
   stack when no prediction exists; otherwise evaluate the existing prediction
   against the actual result (insert-only; no re-price). No Gemini.
3. League standings for all standings competitions.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from laboratory.football_data import (
    COMPETITION_NAMES,
    fetch_matches_window,
    fetch_season,
    import_bodies,
)
from laboratory.standings import STANDINGS_COMPETITIONS, import_all_standings
from laboratory.store import (
    checkpoint_wal,
    ensure_experiment,
)
from core.timeutil import cairo_today

logger = logging.getLogger(__name__)

JOB_NAME = "data_sync"
DEFAULT_SEASON = 2026
DEFAULT_MATCH_COMPETITIONS = tuple(COMPETITION_NAMES.keys())
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_LOOKAHEAD_DAYS = 14


def _dataset_config(competition: str, seasons: list[int]) -> dict[str, Any]:
    code = competition.upper()
    label = COMPETITION_NAMES.get(code, code)
    return {
        "experiment_id": f"DATA-{code}-001",
        "competition": code,
        "seasons": seasons,
        "purpose": f"store {label} history",
        "pricing": False,
    }


def create_sync_job_run(
    connection: sqlite3.Connection,
    *,
    started_at: str | None = None,
) -> int:
    """Insert a running sync job row into job_runs and return its rowid."""
    now_utc = started_at or datetime.now(timezone.utc).isoformat()
    initial_summary = {
        "status": "running",
        "started_at": now_utc,
        "detail": "Sync running in background… refreshing this page.",
        "leagues": {},
    }
    cursor = connection.execute(
        """
        INSERT INTO job_runs (name, started_at, finished_at, ok, summary_json)
        VALUES (?, ?, '', 0, ?)
        """,
        (
            JOB_NAME,
            now_utc,
            json.dumps(initial_summary, sort_keys=True, default=str),
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def complete_sync_job_run(
    connection: sqlite3.Connection,
    job_id: int,
    summary: dict[str, Any],
    *,
    finished_at: str | None = None,
) -> None:
    """Update an existing sync job row in job_runs with the final summary."""
    now_utc = finished_at or datetime.now(timezone.utc).isoformat()
    ok = 1 if summary.get("status") == "ok" else 0
    connection.execute(
        """
        UPDATE job_runs
        SET finished_at = ?, ok = ?, summary_json = ?
        WHERE id = ?
        """,
        (
            now_utc,
            ok,
            json.dumps(summary, sort_keys=True, default=str),
            job_id,
        ),
    )
    connection.commit()


def get_latest_sync_job(
    connection: sqlite3.Connection,
    *,
    stale_hours: float = 2.0,
) -> dict[str, Any] | None:
    """Return the summary dict of the latest sync job.

    If the job has been running for longer than ``stale_hours``, marks it failed
    in the database and returns the failed summary.
    """
    row = connection.execute(
        """
        SELECT id, name, started_at, finished_at, ok, summary_json
        FROM job_runs
        WHERE name = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (JOB_NAME,),
    ).fetchone()
    if row is None:
        return None
    try:
        summary = json.loads(row["summary_json"])
    except Exception:
        summary = {"status": "failed", "error": "Invalid summary JSON"}

    if summary.get("status") == "running":
        now_utc = datetime.now(timezone.utc)
        try:
            started_dt = datetime.fromisoformat(row["started_at"])
            if started_dt.tzinfo is None:
                started_dt = started_dt.replace(tzinfo=timezone.utc)
            age_hours = (now_utc - started_dt).total_seconds() / 3600.0
        except Exception:
            age_hours = 999.0
        if age_hours > stale_hours:
            summary["status"] = "failed"
            summary["error"] = "Execution timed out or aborted (stale running state)"
            summary["detail"] = "Sync failed: timed out or aborted."
            connection.execute(
                """
                UPDATE job_runs
                SET finished_at = ?, ok = ?, summary_json = ?
                WHERE id = ?
                """,
                (
                    now_utc.isoformat(),
                    0,
                    json.dumps(summary, sort_keys=True, default=str),
                    row["id"],
                ),
            )
            connection.commit()

    return summary


def can_start_sync(
    connection: sqlite3.Connection,
    *,
    stale_hours: float = 2.0,
) -> tuple[bool, str]:
    """Check if a new sync job may be started.

    Returns ``(True, "")`` if permitted, or ``(False, reason)`` if busy.
    """
    latest = get_latest_sync_job(connection, stale_hours=stale_hours)
    if latest and latest.get("status") == "running":
        return False, "A sync is already in progress. Wait for it to finish."
    return True, ""


def competition_has_season_history(
    connection: sqlite3.Connection,
    competition: str,
    season: int,
) -> bool:
    """True when the shared DB already stores fixtures for this competition season."""
    row = connection.execute(
        """
        SELECT 1
        FROM matches
        WHERE competition = ? AND season = ?
        LIMIT 1
        """,
        (competition.upper(), str(int(season))),
    ).fetchone()
    return row is not None


def sync_date_window(
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    today=None,
) -> tuple[str, str]:
    """Inclusive Cairo YYYY-MM-DD window around today for incremental sync."""
    day = today if today is not None else cairo_today()
    date_from = (day - timedelta(days=max(0, int(lookback_days)))).isoformat()
    date_to = (day + timedelta(days=max(0, int(lookahead_days)))).isoformat()
    return date_from, date_to


def fetch_competition_for_sync(
    connection: sqlite3.Connection,
    *,
    api_key: str,
    season: int,
    competition: str,
    date_from: str,
    date_to: str,
    force_full_season: bool = False,
    ninjas_api_key: str | None = None,
    live_football_api_key: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Return ``(payload, mode)`` where mode is ``full_season`` or ``window``.

    Bootstrap (no stored season rows, or ``force_full_season``) fetches the whole
    season. Otherwise fetch only the date window around today.
    """
    code = competition.upper()
    use_full = force_full_season or not competition_has_season_history(
        connection, code, season
    )
    if use_full:
        body = fetch_season(
            api_key,
            season,
            competition=code,
            ninjas_api_key=ninjas_api_key,
            live_football_api_key=live_football_api_key,
        )
        return body, "full_season"

    body = fetch_matches_window(
        api_key,
        season,
        competition=code,
        date_from=date_from,
        date_to=date_to,
        ninjas_api_key=ninjas_api_key,
        live_football_api_key=live_football_api_key,
    )
    return body, "window"


def _refresh_pl_xg_best_effort(connection: sqlite3.Connection, *, season: int) -> None:
    """Best-effort non-blocking refresh of current-season PL xG from fpl_opta_github."""
    try:
        from db.xg_store import import_xg_records
        from scripts.fetch_pl_xg import _fetch_fpl_opta_github

        records, status = _fetch_fpl_opta_github(int(season))
        if not records:
            logger.warning("PL xG sync skipped for season=%s: %s", season, status)
            return
        res = import_xg_records(
            connection,
            records,
            source="fpl_opta_github",
            competition="PL",
            season=int(season),
        )
        checkpoint_wal(connection, mode="TRUNCATE")
        logger.info("PL xG sync season=%s imported=%s", season, res.get("imported", 0))
    except Exception as exc:
        logger.warning("PL xG sync failed (non-blocking): %s", exc)


def run_sync(
    connection: sqlite3.Connection,
    *,
    seasons: list[int] | None = None,
    competitions: tuple[str, ...] | None = None,
    standings_competitions: tuple[str, ...] | None = None,
    pause_seconds: float = 7.0,
    api_key: str | None = None,
    ninjas_api_key: str | None = None,
    live_football_api_key: str | None = None,
    job_id: int | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    force_full_season: bool = False,
) -> dict[str, Any]:
    """Execute background data sync for matches and standings.

    Catches errors per competition, respects rate limits, handles WAL checkpoint,
    and returns a structured summary.
    """
    start_time = time.time()
    started_at = datetime.now(timezone.utc).isoformat()

    seasons = seasons or [DEFAULT_SEASON]
    match_comps = tuple(c.upper() for c in (competitions or DEFAULT_MATCH_COMPETITIONS))
    if standings_competitions is None:
        if competitions is not None:
            st_comps = tuple(c for c in match_comps if c in STANDINGS_COMPETITIONS)
        else:
            st_comps = tuple(c.upper() for c in STANDINGS_COMPETITIONS)
    else:
        st_comps = tuple(c.upper() for c in standings_competitions)

    all_codes = tuple(dict.fromkeys(match_comps + st_comps))
    date_from, date_to = sync_date_window(
        lookback_days=lookback_days,
        lookahead_days=lookahead_days,
    )

    resolved_api_key = (api_key or os.environ.get("FOOTBALL_API_KEY", "")).strip()
    resolved_ninjas_key = (
        ninjas_api_key if ninjas_api_key is not None else os.environ.get("NINJAS_API_KEY", "").strip()
    ) or None
    resolved_lfa_key = (
        live_football_api_key
        if live_football_api_key is not None
        else os.environ.get("LIVE_FOOTBALL_API_KEY", "").strip()
    ) or None

    leagues: dict[str, dict[str, Any]] = {}
    for code in all_codes:
        leagues[code] = {
            "code": code,
            "name": COMPETITION_NAMES.get(code, code),
            "matches": 0,
            "finished": 0,
            "results_added": 0,
            "results_unchanged": 0,
            "skipped": 0,
            "matches_status": "pending" if code in match_comps else "n/a",
            "matches_error": None,
            "standings_status": "pending" if code in st_comps else "n/a",
            "standings_error": None,
            "standings_rows": 0,
            "fetch_mode": None,
            "priced_new": 0,
            "evaluated": 0,
            "already_evaluated": 0,
            "price_excluded": 0,
            "price_failed": 0,
            "price_status": "pending" if code in match_comps else "n/a",
            "price_error": None,
        }

    # 1. Match fixtures + results for each competition
    try:
        for idx, code in enumerate(match_comps):
            if idx > 0 and pause_seconds > 0:
                time.sleep(pause_seconds)

            league_entry = leagues[code]
            if code not in COMPETITION_NAMES:
                league_entry["matches_status"] = "failed"
                league_entry["matches_error"] = f"Unsupported competition code: {code}"
                league_entry["price_status"] = "skipped"
                logger.warning("Unsupported competition code %s; skipping", code)
                continue

            # Key checks
            if code == "SPL":
                if not resolved_lfa_key and not resolved_ninjas_key:
                    league_entry["matches_status"] = "failed"
                    league_entry["matches_error"] = (
                        "SPL history needs LIVE_FOOTBALL_API_KEY (or NINJAS_API_KEY as a fallback)."
                    )
                    league_entry["price_status"] = "skipped"
                    continue
            elif not resolved_api_key:
                league_entry["matches_status"] = "failed"
                league_entry["matches_error"] = "Missing FOOTBALL_API_KEY."
                league_entry["price_status"] = "skipped"
                continue

            try:
                bodies: list[tuple[int, dict[str, Any]]] = []
                modes: list[str] = []
                for s_idx, season in enumerate(seasons):
                    if s_idx > 0 and pause_seconds > 0:
                        time.sleep(pause_seconds)
                    body, mode = fetch_competition_for_sync(
                        connection,
                        api_key=resolved_api_key or "",
                        season=season,
                        competition=code,
                        date_from=date_from,
                        date_to=date_to,
                        force_full_season=force_full_season,
                        ninjas_api_key=resolved_ninjas_key,
                        live_football_api_key=resolved_lfa_key,
                    )
                    bodies.append((season, body))
                    modes.append(mode)

                league_entry["fetch_mode"] = (
                    "full_season"
                    if "full_season" in modes
                    else (modes[0] if modes else None)
                )

                if not bodies:
                    league_entry["matches_status"] = "empty"
                    league_entry["matches_error"] = "No fixtures returned."
                    league_entry["price_status"] = "skipped"
                else:
                    cfg = _dataset_config(code, [s for s, _ in bodies])
                    ensure_experiment(connection, cfg)
                    counts = import_bodies(
                        connection, bodies, competition=code, experiment_config=cfg
                    )
                    league_entry["matches"] = counts.get("matches", 0)
                    league_entry["finished"] = counts.get("finished", 0)
                    league_entry["results_added"] = counts.get("results_added", 0)
                    league_entry["results_unchanged"] = counts.get("results_unchanged", 0)
                    league_entry["skipped"] = counts.get("skipped", 0)
                    league_entry["matches_status"] = (
                        "empty" if counts.get("matches", 0) == 0 else "ok"
                    )
            except sqlite3.OperationalError:
                raise
            except Exception as exc:
                league_entry["matches_status"] = "failed"
                league_entry["matches_error"] = str(exc)
                league_entry["price_status"] = "skipped"

        # 1a-bis. Best-effort non-blocking current-season PL xG refresh
        if "PL" in match_comps and not os.environ.get("PYTEST_CURRENT_TEST"):
            _refresh_pl_xg_best_effort(connection, season=max(seasons))

        # 1b. Price new finished matches / evaluate existing predictions (no Gemini)
        from laboratory.job import price_or_evaluate_finished_in_window

        priceable = tuple(
            code
            for code in match_comps
            if leagues[code].get("matches_status") in ("ok", "empty")
        )
        if priceable:
            try:
                pe = price_or_evaluate_finished_in_window(
                    connection,
                    competitions=priceable,
                    date_from=date_from,
                    date_to=date_to,
                )
                by_comp = pe.get("by_competition") or {}
                for code in priceable:
                    bucket = by_comp.get(code) or {}
                    leagues[code]["priced_new"] = int(bucket.get("priced", 0))
                    leagues[code]["evaluated"] = int(bucket.get("evaluated", 0))
                    leagues[code]["already_evaluated"] = int(
                        bucket.get("already_evaluated", 0)
                    )
                    leagues[code]["price_excluded"] = int(bucket.get("excluded", 0))
                    leagues[code]["price_failed"] = int(bucket.get("failed", 0))
                    leagues[code]["price_status"] = (
                        "failed" if int(bucket.get("failed", 0)) > 0 else "ok"
                    )
            except sqlite3.OperationalError:
                raise
            except Exception as exc:
                logger.exception("Sync price/evaluate step failed: %s", exc)
                for code in priceable:
                    leagues[code]["price_status"] = "failed"
                    leagues[code]["price_error"] = str(exc)

        # 2. League standings
        try:
            st_results = import_all_standings(
                connection,
                resolved_api_key,
                competitions=st_comps,
                force=True,
                pause_seconds=pause_seconds,
                ninjas_api_key=resolved_ninjas_key,
            )
            for st in st_results:
                st_code = str(st.get("competition") or "").upper()
                if st_code in leagues:
                    if st.get("error"):
                        leagues[st_code]["standings_status"] = "failed"
                        leagues[st_code]["standings_error"] = str(st["error"])
                    else:
                        leagues[st_code]["standings_status"] = "ok"
                        leagues[st_code]["standings_rows"] = int(st.get("rows", 0))
        except sqlite3.OperationalError:
            raise
        except Exception as exc:
            for st_code in st_comps:
                if st_code in leagues and leagues[st_code]["standings_status"] == "pending":
                    leagues[st_code]["standings_status"] = "failed"
                    leagues[st_code]["standings_error"] = str(exc)

        # 3. WAL checkpoint
        try:
            checkpoint_wal(connection, mode="TRUNCATE")
        except Exception:
            try:
                checkpoint_wal(connection, mode="PASSIVE")
            except Exception:
                pass

    except sqlite3.OperationalError as exc:
        duration = round(time.time() - start_time, 2)
        finished_at = datetime.now(timezone.utc).isoformat()
        summary = {
            "status": "failed",
            "detail": f"Database locked / WAL contention: {exc}. Try 'make dash-restart' or checkpoint WAL.",
            "error": str(exc),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": duration,
            "date_from": date_from,
            "date_to": date_to,
            "total_matches": 0,
            "total_finished": 0,
            "total_results_added": 0,
            "total_results_unchanged": 0,
            "total_standings_updated": 0,
            "total_priced_new": 0,
            "total_evaluated": 0,
            "total_already_evaluated": 0,
            "leagues": leagues,
        }
        if job_id is not None:
            complete_sync_job_run(connection, job_id, summary, finished_at=finished_at)
        return summary

    duration = round(time.time() - start_time, 2)
    finished_at = datetime.now(timezone.utc).isoformat()

    total_matches = sum(l["matches"] for l in leagues.values())
    total_finished = sum(l["finished"] for l in leagues.values())
    total_results_added = sum(l["results_added"] for l in leagues.values())
    total_results_unchanged = sum(l["results_unchanged"] for l in leagues.values())
    total_standings_updated = sum(l["standings_rows"] for l in leagues.values())
    total_priced_new = sum(int(l.get("priced_new", 0)) for l in leagues.values())
    total_evaluated = sum(int(l.get("evaluated", 0)) for l in leagues.values())
    total_already_evaluated = sum(
        int(l.get("already_evaluated", 0)) for l in leagues.values()
    )
    window_count = sum(1 for l in leagues.values() if l.get("fetch_mode") == "window")
    full_count = sum(1 for l in leagues.values() if l.get("fetch_mode") == "full_season")

    match_failed = [
        f"{l['code']} ({l['matches_error']})"
        for l in leagues.values()
        if l["matches_status"] == "failed"
    ]
    standings_failed = [
        f"{l['code']} standings ({l['standings_error']})"
        for l in leagues.values()
        if l["standings_status"] == "failed"
    ]
    all_failed = match_failed + standings_failed

    all_match_failed = all(l["matches_status"] == "failed" for l in leagues.values())
    all_st_failed = all(
        l["standings_status"] == "failed"
        for l in leagues.values()
        if l["code"] in st_comps
    )

    successful_leagues_count = sum(
        1
        for l in leagues.values()
        if l["matches_status"] in ("ok", "empty") and l["standings_status"] in ("ok", "n/a")
    )

    mode_note = ""
    if window_count or full_count:
        mode_note = f" Fetch: {window_count} window, {full_count} full-season ({date_from}→{date_to})."

    price_note = (
        f" Priced {total_priced_new} new, evaluated {total_evaluated}"
        f" ({total_already_evaluated} already scored)."
    )

    if all_match_failed and (all_st_failed or not st_comps):
        status = "failed"
        detail = f"Sync failed: {all_failed[0] if all_failed else 'Zero leagues imported.'}"
    elif all_failed:
        status = "partial"
        failed_desc = ", ".join(all_failed)
        detail = (
            f"Partial sync: {successful_leagues_count}/{len(leagues)} leagues OK. "
            f"Failed: {failed_desc}.{mode_note}{price_note}"
        )
    elif total_matches == 0:
        status = "empty"
        detail = (
            f"No fixtures returned for {len(leagues)} leagues "
            f"(check season year / provider / window {date_from}→{date_to})."
        )
    else:
        status = "ok"
        if total_results_added == 0 and total_results_unchanged > 0:
            detail = (
                f"Synced {len(leagues)} leagues. Scores are up to date "
                f"({total_results_unchanged} unchanged). Standings updated: {total_standings_updated}."
                f"{mode_note}{price_note}"
            )
        else:
            detail = (
                f"Synced {len(leagues)} leagues. Results added: {total_results_added}. "
                f"Standings updated: {total_standings_updated}.{mode_note}{price_note}"
            )

    summary = {
        "status": status,
        "detail": detail,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration,
        "date_from": date_from,
        "date_to": date_to,
        "total_matches": total_matches,
        "total_finished": total_finished,
        "total_results_added": total_results_added,
        "total_results_unchanged": total_results_unchanged,
        "total_standings_updated": total_standings_updated,
        "total_priced_new": total_priced_new,
        "total_evaluated": total_evaluated,
        "total_already_evaluated": total_already_evaluated,
        "leagues": leagues,
    }

    if job_id is not None:
        complete_sync_job_run(connection, job_id, summary, finished_at=finished_at)

    return summary
