"""Tests for Dashboard 'Sync matches + standings' feature.

Covers:
- UI View Builder: Overview Server Jobs panel, Sync button, CSRF, states, breakdown table
- Concurrency & Stale guard: get_latest_sync_job, can_start_sync, 2h stale timeout
- Summary Aggregation: success, partial (one league error / SPL missing key), empty, unchanged scores ("up to date")
- SQLite / WAL safety: OperationalError database locked handling, WAL checkpoint truncate/passive
- Sync post-step: price new finished matches; evaluate existing (no re-price, no Gemini)
- HTTP Server endpoints: GET / shows button + CSRF, POST /sync with auth & CSRF, busy guard, config guard, Popen mock
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener, urlopen
from http.cookiejar import CookieJar

import pytest

from db.db_paths import DEFAULT_DATABASE_PATH
from laboratory.__main__ import build_dashboard_server, seed_demo
from laboratory.auth import DashboardAuth
from laboratory.dashboard import overview
from laboratory.store import connect
from laboratory.sync import (
    JOB_NAME,
    can_start_sync,
    complete_sync_job_run,
    create_sync_job_run,
    get_latest_sync_job,
    run_sync,
)


# =====================================================================
# Unit & Builder Tests (No sockets required)
# =====================================================================


def test_overview_shows_sync_button_and_csrf(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)

    page = overview(conn, None, csrf_token="test-sync-csrf-123")
    assert "Server jobs" in page
    assert "Sync matches &amp; standings" in page or "Sync matches & standings" in page
    assert 'action="/sync"' in page
    assert 'value="test-sync-csrf-123"' in page
    assert "Fetches recent results, upcoming fixtures, and tables for all leagues (background)." in page
    assert "Incremental: recent results + upcoming fixtures" in page
    assert 'class="sync-btn"' in page


def test_overview_running_sync_disables_button_and_adds_refresh(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)

    job_id = create_sync_job_run(conn)
    assert job_id > 0

    page = overview(conn, None)
    assert '<meta http-equiv="refresh" content="5;url=/">' in page
    assert "Sync in progress…" in page
    assert "disabled" in page
    assert "Sync running in background… refreshing this page." in page


def test_overview_started_flash_refreshes_to_show_final_result(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)

    job_id = create_sync_job_run(conn)
    complete_sync_job_run(
        conn,
        job_id,
        {
            "status": "ok",
            "detail": "Synced 9 leagues. Results added: 2.",
            "leagues": {
                "PL": {
                    "code": "PL",
                    "name": "Premier League",
                    "matches": 10,
                    "finished": 2,
                    "results_added": 2,
                    "results_unchanged": 0,
                    "skipped": 0,
                    "matches_status": "ok",
                    "matches_error": None,
                    "standings_status": "ok",
                    "standings_error": None,
                    "standings_rows": 20,
                    "fetch_mode": "window",
                }
            },
        },
    )

    page = overview(
        conn,
        {"sync_status": ["started"], "sync_detail": ["Sync running in background…"]},
    )
    assert '<meta http-equiv="refresh" content="2;url=/">' in page
    assert "Sync completed" in page
    assert "Synced 9 leagues. Results added: 2." in page
    assert "sync-breakdown" in page


def test_overview_renders_flash_states(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)

    # Busy
    busy_page = overview(conn, {"sync_status": ["busy"], "sync_detail": ["A sync is running."]})
    assert "A sync is already in progress. Wait for it to finish." in busy_page

    # Config
    config_page = overview(conn, {"sync_status": ["config"], "sync_detail": ["Missing FOOTBALL_API_KEY in environment."]})
    assert "Missing configuration" in config_page
    assert "Missing FOOTBALL_API_KEY in environment." in config_page

    # Failed / Auth
    failed_page = overview(conn, {"sync_status": ["failed"], "sync_detail": ["Session expired. Try again."]})
    assert "Sync failed" in failed_page
    assert "Session expired. Try again." in failed_page

    # Empty
    empty_page = overview(conn, {"sync_status": ["empty"], "sync_detail": ["No fixtures returned."]})
    assert "No fixtures returned" in empty_page

    # Partial
    partial_page = overview(conn, {"sync_status": ["partial"], "sync_detail": ["Partial sync: 8/9 leagues OK."]})
    assert "Partial sync completed" in partial_page
    assert "Partial sync: 8/9 leagues OK." in partial_page

    # Ok / Completed
    ok_page = overview(conn, {"sync_status": ["ok"], "sync_detail": ["Synced 9 leagues. Results added: 5."]})
    assert "Sync completed" in ok_page
    assert "Synced 9 leagues. Results added: 5." in ok_page


def test_overview_shows_breakdown_table_from_stored_job(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)

    job_id = create_sync_job_run(conn)
    summary = {
        "status": "partial",
        "detail": "Partial sync: 1/2 leagues OK. Failed: PD (Rate limit).",
        "started_at": "2026-11-21T10:00:00Z",
        "finished_at": "2026-11-21T10:01:00Z",
        "duration_seconds": 60.0,
        "total_matches": 10,
        "total_finished": 8,
        "total_results_added": 2,
        "total_results_unchanged": 6,
        "total_standings_updated": 20,
        "leagues": {
            "PL": {
                "code": "PL",
                "name": "Premier League",
                "matches": 10,
                "finished": 8,
                "results_added": 2,
                "results_unchanged": 6,
                "matches_status": "ok",
                "matches_error": None,
                "standings_status": "ok",
                "standings_rows": 20,
                "standings_error": None,
            },
            "PD": {
                "code": "PD",
                "name": "La Liga",
                "matches": 0,
                "finished": 0,
                "results_added": 0,
                "results_unchanged": 0,
                "matches_status": "failed",
                "matches_error": "Rate limit exceeded (429)",
                "standings_status": "failed",
                "standings_rows": 0,
                "standings_error": "Rate limit exceeded (429)",
            },
        },
    }
    complete_sync_job_run(conn, job_id, summary)

    # Calling overview without query flash params renders from stored job summary
    page = overview(conn, None)
    assert "Partial sync completed" in page
    assert "sync-breakdown" in page
    assert "Premier League" in page
    assert "La Liga" in page
    assert "Rate limit exceeded" in page
    assert "job-ok" in page
    assert "job-failed" in page


def test_stale_running_job_marked_failed(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)

    # Start time 3 hours ago
    three_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    job_id = create_sync_job_run(conn, started_at=three_hours_ago)

    summary = get_latest_sync_job(conn, stale_hours=2.0)
    assert summary is not None
    assert summary["status"] == "failed"
    assert "timed out or aborted" in summary["error"]

    # Concurrency check now allows starting
    allowed, reason = can_start_sync(conn, stale_hours=2.0)
    assert allowed is True
    assert reason == ""


def test_can_start_sync_guard(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)

    # Empty DB allows starting
    allowed, _ = can_start_sync(conn)
    assert allowed is True

    # Running job blocks starting
    job_id = create_sync_job_run(conn)
    allowed, reason = can_start_sync(conn)
    assert allowed is False
    assert "already in progress" in reason

    # Completed job allows starting
    complete_sync_job_run(conn, job_id, {"status": "ok"})
    allowed, _ = can_start_sync(conn)
    assert allowed is True


# =====================================================================
# run_sync Logic & Edge Cases (Mocked APIs)
# =====================================================================


def test_run_sync_partial_when_one_league_errors(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)

    fake_pl_body = {
        "competition": {"code": "PL", "name": "Premier League"},
        "matches": [
            {
                "id": 1001,
                "utcDate": "2026-09-01T15:00:00Z",
                "status": "FINISHED",
                "homeTeam": {"id": 57, "name": "Arsenal", "shortName": "Arsenal"},
                "awayTeam": {"id": 61, "name": "Chelsea", "shortName": "Chelsea"},
                "score": {"fullTime": {"home": 2, "away": 1}, "halfTime": {"home": 1, "away": 0}},
            }
        ],
    }

    def fake_fetch_season(api_key, season, *, competition="PL", **kwargs):
        if competition == "PL":
            return fake_pl_body
        raise RuntimeError("Provider HTTP 429: Too Many Requests")

    def fake_import_all_standings(connection, api_key, *, competitions=(), **kwargs):
        results = []
        for c in competitions:
            if c == "PL":
                results.append({"competition": "PL", "rows": 20, "error": None})
            else:
                results.append({"competition": c, "rows": 0, "error": "HTTP 429"})
        return results

    def fake_fetch_for_sync(connection, *, competition="PL", **kwargs):
        return fake_fetch_season("k", 2026, competition=competition), "full_season"

    with patch("laboratory.sync.fetch_competition_for_sync", side_effect=fake_fetch_for_sync), \
         patch("laboratory.sync.import_all_standings", side_effect=fake_import_all_standings):
        summary = run_sync(
            conn,
            competitions=("PL", "PD"),
            standings_competitions=("PL", "PD"),
            api_key="valid-key",
            pause_seconds=0.0,
        )

    assert summary["status"] == "partial"
    assert "Partial sync: 1/2 leagues OK" in summary["detail"]
    assert "PD" in summary["detail"]
    assert summary["leagues"]["PL"]["matches_status"] == "ok"
    assert summary["leagues"]["PD"]["matches_status"] == "failed"
    assert "HTTP 429" in summary["leagues"]["PD"]["matches_error"]


def test_run_sync_scores_up_to_date_when_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)

    fake_body = {
        "competition": {"code": "PL", "name": "Premier League"},
        "matches": [
            {
                "id": 1001,
                "utcDate": "2026-09-01T15:00:00Z",
                "status": "FINISHED",
                "homeTeam": {"id": 57, "name": "Arsenal", "shortName": "Arsenal"},
                "awayTeam": {"id": 61, "name": "Chelsea", "shortName": "Chelsea"},
                "score": {"fullTime": {"home": 2, "away": 1}, "halfTime": {"home": 1, "away": 0}},
            }
        ],
    }

    with patch(
        "laboratory.sync.fetch_competition_for_sync",
        return_value=(fake_body, "full_season"),
    ), patch(
        "laboratory.sync.import_all_standings",
        return_value=[{"competition": "PL", "rows": 20}],
    ):
        # First sync adds the match and revision
        run_sync(conn, competitions=("PL",), standings_competitions=("PL",), api_key="valid-key", pause_seconds=0)

        # Second sync receives same scores -> 0 added, 1 unchanged
        summary = run_sync(
            conn,
            competitions=("PL",),
            standings_competitions=("PL",),
            api_key="valid-key",
            pause_seconds=0,
        )

    assert summary["status"] == "ok"
    assert summary["total_results_added"] == 0
    assert summary["total_results_unchanged"] == 1
    assert "Scores are up to date" in summary["detail"]
    assert "1 unchanged" in summary["detail"]


def test_run_sync_catches_operational_error_database_locked(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)

    with patch(
        "laboratory.sync.fetch_competition_for_sync",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        summary = run_sync(
            conn,
            competitions=("PL",),
            api_key="valid-key",
            pause_seconds=0,
        )

    assert summary["status"] == "failed"
    assert "Database locked / WAL contention" in summary["detail"]
    assert "make dash-restart" in summary["detail"]


def test_run_sync_skips_unsupported_competition(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)

    fake_body = {"competition": {"code": "PL", "name": "Premier League"}, "matches": []}
    with patch(
        "laboratory.sync.fetch_competition_for_sync",
        return_value=(fake_body, "full_season"),
    ), patch(
        "laboratory.sync.import_all_standings",
        return_value=[{"competition": "PL", "rows": 20}],
    ):
        summary = run_sync(
            conn,
            competitions=("PL", "NONEXISTENT_LEAGUE"),
            standings_competitions=("PL",),
            api_key="valid-key",
            pause_seconds=0,
        )

    assert summary["leagues"]["NONEXISTENT_LEAGUE"]["matches_status"] == "failed"
    assert "Unsupported competition code" in summary["leagues"]["NONEXISTENT_LEAGUE"]["matches_error"]


def test_run_sync_missing_spl_key_marks_partial(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)

    fake_body = {
        "competition": {"code": "PL", "name": "Premier League"},
        "matches": [
            {
                "id": 1001,
                "utcDate": "2026-09-01T15:00:00Z",
                "status": "FINISHED",
                "homeTeam": {"id": 57, "name": "Arsenal", "shortName": "Arsenal"},
                "awayTeam": {"id": 61, "name": "Chelsea", "shortName": "Chelsea"},
                "score": {"fullTime": {"home": 2, "away": 1}, "halfTime": {"home": 1, "away": 0}},
            }
        ],
    }

    with patch(
        "laboratory.sync.fetch_competition_for_sync",
        return_value=(fake_body, "full_season"),
    ), patch(
        "laboratory.sync.import_all_standings",
        return_value=[{"competition": "PL", "rows": 20}],
    ):
        summary = run_sync(
            conn,
            competitions=("PL", "SPL"),
            standings_competitions=("PL",),
            api_key="valid-key",
            live_football_api_key=None,
            ninjas_api_key=None,
            pause_seconds=0,
        )

    assert summary["status"] == "partial"
    assert "SPL" in summary["detail"]
    assert "LIVE_FOOTBALL_API_KEY" in summary["leagues"]["SPL"]["matches_error"]


def _score_model_body() -> dict:
    return {
        "engine": "score_model",
        "full_time": {
            "result_1x2": {
                "home": 0.5,
                "draw": 0.25,
                "away": 0.25,
                "most_likely": "home",
            },
            "exact_score_most_likely": {"home_goals": 2, "away_goals": 1},
            "exact_scores": [
                {"home_goals": 2, "away_goals": 1, "probability": 0.12},
                {"home_goals": 1, "away_goals": 0, "probability": 0.1},
            ],
            "over_under_2_5": {"over": 0.55, "under": 0.45, "most_likely": "over"},
            "over_under_3_5": {"over": 0.3, "under": 0.7, "most_likely": "under"},
        },
        "half_time": {"withheld": True},
    }


def test_price_or_evaluate_prices_missing_and_evaluates_existing(tmp_path: Path) -> None:
    from laboratory.job import price_or_evaluate_finished_in_window
    from laboratory.store import (
        append_result,
        ensure_experiment,
        insert_prediction,
        save_snapshot,
        upsert_match,
    )

    db = tmp_path / "football.db"
    conn = connect(db)
    ensure_experiment(conn, {"experiment_id": "BOT-PL-001", "competition": "PL"})

    upsert_match(
        conn,
        {
            "match_id": "sync-new",
            "dataset_version": "t",
            "competition": "PL",
            "season": "2026",
            "kickoff": "2026-11-15T15:00:00Z",
            "home_id": "57",
            "home_name": "Arsenal",
            "away_id": "61",
            "away_name": "Chelsea",
        },
    )
    append_result(
        conn,
        "sync-new",
        {"ft_home": 2, "ft_away": 1, "ht_home": 1, "ht_away": 0},
        "test",
    )
    upsert_match(
        conn,
        {
            "match_id": "sync-existing",
            "dataset_version": "t",
            "competition": "PL",
            "season": "2026",
            "kickoff": "2026-11-16T15:00:00Z",
            "home_id": "64",
            "home_name": "Liverpool",
            "away_id": "65",
            "away_name": "Man City",
        },
    )
    append_result(
        conn,
        "sync-existing",
        {"ft_home": 1, "ft_away": 1, "ht_home": 0, "ht_away": 0},
        "test",
    )
    payload = {
        "match_id": "sync-existing",
        "payload_hash": "sync-existing-hash",
        "competition": "PL",
        "kickoff": "2026-11-16T15:00:00Z",
        "home": {"id": "64", "name": "Liverpool"},
        "away": {"id": "65", "name": "Man City"},
        "prior_matches": [],
        "leakage_ok": True,
    }
    snapshot_id = save_snapshot(conn, payload, experiment_id="BOT-PL-001")
    prediction_id = insert_prediction(
        conn,
        match_id="sync-existing",
        snapshot_id=snapshot_id,
        engine="score_model",
        model_id="bot-score-model-live",
        body=_score_model_body(),
        experiment_id="BOT-PL-001",
    )
    assert prediction_id is not None
    conn.commit()

    priced_calls: list[str] = []

    def fake_price(connection, match_id, *, replace=True):
        priced_calls.append(match_id)
        return {
            "ok": True,
            "status": "priced",
            "match_id": match_id,
            "evaluated": True,
        }

    with patch("laboratory.job.price_one_score_model", side_effect=fake_price):
        counts = price_or_evaluate_finished_in_window(
            conn,
            competitions=("PL",),
            date_from="2026-11-07",
            date_to="2026-12-05",
        )

    assert priced_calls == ["sync-new"]
    assert counts["priced"] == 1
    assert counts["evaluated"] == 1
    assert counts["already_evaluated"] == 0
    eval_row = conn.execute(
        "SELECT metrics_json FROM evaluations WHERE prediction_id = ?",
        (prediction_id,),
    ).fetchone()
    assert eval_row is not None
    metrics = json.loads(eval_row["metrics_json"])
    assert metrics["full_time_1x2"]["actual"] == "draw"

    # Second pass: existing stays insert-only; evaluation already present.
    with patch("laboratory.job.price_one_score_model", side_effect=fake_price) as price_mock:
        again = price_or_evaluate_finished_in_window(
            conn,
            competitions=("PL",),
            date_from="2026-11-07",
            date_to="2026-12-05",
        )
    assert price_mock.call_count == 1  # only sync-new still missing
    assert again["priced"] == 1
    assert again["evaluated"] == 0
    assert again["already_evaluated"] == 1


def test_run_sync_wires_price_evaluate_counts(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)

    fake_body = {
        "competition": {"code": "PL", "name": "Premier League"},
        "matches": [
            {
                "id": 1001,
                "utcDate": "2026-11-15T15:00:00Z",
                "status": "FINISHED",
                "homeTeam": {"id": 57, "name": "Arsenal", "shortName": "Arsenal"},
                "awayTeam": {"id": 61, "name": "Chelsea", "shortName": "Chelsea"},
                "score": {"fullTime": {"home": 2, "away": 1}, "halfTime": {"home": 1, "away": 0}},
            }
        ],
    }

    with patch(
        "laboratory.sync.fetch_competition_for_sync",
        return_value=(fake_body, "window"),
    ), patch("laboratory.sync.import_all_standings", return_value=[]), patch(
        "laboratory.job.price_or_evaluate_finished_in_window",
        return_value={
            "priced": 1,
            "evaluated": 2,
            "already_evaluated": 3,
            "excluded": 0,
            "failed": 0,
            "by_competition": {
                "PL": {
                    "priced": 1,
                    "evaluated": 2,
                    "already_evaluated": 3,
                    "excluded": 0,
                    "failed": 0,
                }
            },
        },
    ):
        summary = run_sync(
            conn,
            competitions=("PL",),
            standings_competitions=(),
            api_key="valid-key",
            pause_seconds=0,
        )

    assert summary["status"] == "ok"
    assert summary["total_priced_new"] == 1
    assert summary["total_evaluated"] == 2
    assert summary["total_already_evaluated"] == 3
    assert summary["leagues"]["PL"]["priced_new"] == 1
    assert summary["leagues"]["PL"]["evaluated"] == 2
    assert "Priced 1 new, evaluated 2" in summary["detail"]


def test_overview_breakdown_shows_priced_evaluated_columns(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)

    job_id = create_sync_job_run(conn)
    complete_sync_job_run(
        conn,
        job_id,
        {
            "status": "ok",
            "detail": "Synced 1 leagues. Priced 1 new, evaluated 2 (0 already scored).",
            "leagues": {
                "PL": {
                    "code": "PL",
                    "name": "Premier League",
                    "matches": 5,
                    "finished": 3,
                    "results_added": 1,
                    "results_unchanged": 2,
                    "priced_new": 1,
                    "evaluated": 2,
                    "already_evaluated": 0,
                    "matches_status": "ok",
                    "matches_error": None,
                    "standings_status": "ok",
                    "standings_rows": 20,
                    "standings_error": None,
                }
            },
        },
    )
    page = overview(conn, {})
    assert ">Priced<" in page
    assert ">Evaluated<" in page
    assert "mono num\">1</td>" in page
    assert "mono num\">2</td>" in page


def test_sync_date_window_defaults() -> None:
    from datetime import date

    from laboratory.sync import sync_date_window

    date_from, date_to = sync_date_window(today=date(2026, 11, 21))
    assert date_from == "2026-11-07"
    assert date_to == "2026-12-05"


def test_filter_matches_by_utc_date_window() -> None:
    from laboratory.football_data import filter_matches_by_utc_date_window

    body = {
        "matches": [
            {"id": 1, "utcDate": "2026-11-01T15:00:00Z"},
            {"id": 2, "utcDate": "2026-11-10T15:00:00Z"},
            {"id": 3, "utcDate": "2026-12-01T15:00:00Z"},
        ]
    }
    filtered = filter_matches_by_utc_date_window(
        body, date_from="2026-11-07", date_to="2026-12-05"
    )
    assert [m["id"] for m in filtered["matches"]] == [2, 3]


def test_run_sync_uses_window_when_season_history_exists(tmp_path: Path) -> None:
    from laboratory.store import append_result, upsert_match

    db = tmp_path / "football.db"
    conn = connect(db)
    upsert_match(
        conn,
        {
            "match_id": "existing-pl",
            "dataset_version": "t",
            "competition": "PL",
            "season": "2026",
            "kickoff": "2026-08-15T15:00:00Z",
            "home_id": "57",
            "home_name": "Arsenal",
            "away_id": "61",
            "away_name": "Chelsea",
        },
    )
    append_result(
        conn,
        "existing-pl",
        {"ft_home": 1, "ft_away": 0, "ht_home": 0, "ht_away": 0},
        "test",
    )
    conn.commit()

    fake_body = {
        "competition": {"code": "PL", "name": "Premier League"},
        "matches": [
            {
                "id": 2002,
                "utcDate": "2026-11-15T15:00:00Z",
                "status": "FINISHED",
                "homeTeam": {"id": 57, "name": "Arsenal", "shortName": "Arsenal"},
                "awayTeam": {"id": 65, "name": "Man City", "shortName": "Man City"},
                "score": {
                    "fullTime": {"home": 2, "away": 2},
                    "halfTime": {"home": 1, "away": 1},
                },
            }
        ],
    }

    with patch("laboratory.sync.fetch_matches_window", return_value=fake_body) as window_mock, \
         patch("laboratory.sync.fetch_season") as season_mock, \
         patch("laboratory.sync.import_all_standings", return_value=[{"competition": "PL", "rows": 20}]):
        summary = run_sync(
            conn,
            competitions=("PL",),
            standings_competitions=("PL",),
            api_key="valid-key",
            pause_seconds=0,
        )

    assert summary["status"] == "ok"
    assert summary["leagues"]["PL"]["fetch_mode"] == "window"
    assert "window" in summary["detail"]
    assert window_mock.called
    assert not season_mock.called


def test_run_sync_bootstraps_full_season_without_history(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)

    fake_body = {
        "competition": {"code": "PL", "name": "Premier League"},
        "matches": [
            {
                "id": 3003,
                "utcDate": "2026-09-01T15:00:00Z",
                "status": "FINISHED",
                "homeTeam": {"id": 57, "name": "Arsenal", "shortName": "Arsenal"},
                "awayTeam": {"id": 61, "name": "Chelsea", "shortName": "Chelsea"},
                "score": {
                    "fullTime": {"home": 1, "away": 0},
                    "halfTime": {"home": 0, "away": 0},
                },
            }
        ],
    }

    with patch("laboratory.sync.fetch_season", return_value=fake_body) as season_mock, \
         patch("laboratory.sync.fetch_matches_window") as window_mock, \
         patch("laboratory.sync.import_all_standings", return_value=[]):
        summary = run_sync(
            conn,
            competitions=("PL",),
            standings_competitions=(),
            api_key="valid-key",
            pause_seconds=0,
        )

    assert summary["leagues"]["PL"]["fetch_mode"] == "full_season"
    assert season_mock.called
    assert not window_mock.called


# =====================================================================
# Real ThreadingHTTPServer HTTP Route Tests (Loopback Sockets)
# =====================================================================


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def test_http_post_sync_spawns_subprocess_with_absolute_path(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)
    conn.close()

    auth = DashboardAuth(username="ops", password="password", session_secret="test-secret")
    server = build_dashboard_server(db, "127.0.0.1", 0, auth=auth)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))

    try:
        # Wait for server
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with urlopen(f"{base}/healthz", timeout=0.2) as resp:
                    if resp.read() == b"ok\n":
                        break
            except OSError:
                time.sleep(0.05)

        # Login
        login_body = urlencode({
            "username": "ops",
            "password": "password",
            "csrf": auth.csrf_token(),
            "next": "/",
        }).encode()
        opener.open(Request(f"{base}/login", data=login_body, method="POST"), timeout=2)

        # POST /sync with mock Popen
        with patch("subprocess.Popen") as mock_popen, \
             patch.dict("os.environ", {"FOOTBALL_API_KEY": "test-key"}):
            post_body = urlencode({"csrf": auth.csrf_token()}).encode()
            req = Request(f"{base}/sync", data=post_body, method="POST")
            resp = opener.open(req, timeout=2)
            assert "sync_status=started" in resp.geturl()
            html = resp.read().decode()
            assert "Sync running in background… refreshing this page." in html

            mock_popen.assert_called_once()
            cmd_args = mock_popen.call_args[0][0]
            assert "sync-data" in cmd_args
            popen_kwargs = mock_popen.call_args.kwargs
            assert popen_kwargs.get("stdin") is subprocess.DEVNULL
            assert popen_kwargs.get("start_new_session") is True
            assert "--db" in cmd_args
            db_idx = cmd_args.index("--db")
            passed_db_path = cmd_args[db_idx + 1]
            assert Path(passed_db_path).is_absolute()
            assert Path(passed_db_path) == db.resolve()
            assert "--job-id" in cmd_args

    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_post_sync_auth_and_csrf_rejection(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)
    conn.close()

    auth = DashboardAuth(username="ops", password="password", session_secret="test-secret")
    server = build_dashboard_server(db, "127.0.0.1", 0, auth=auth)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    try:
        # Wait for server
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with urlopen(f"{base}/healthz", timeout=0.2) as resp:
                    if resp.read() == b"ok\n":
                        break
            except OSError:
                time.sleep(0.05)

        # 1. Unauthenticated POST -> redirect to login with next
        opener_unauth = build_opener(_NoRedirect)
        try:
            opener_unauth.open(Request(f"{base}/sync", data=b"csrf=foo", method="POST"), timeout=2)
            raise AssertionError("Expected 303 redirect to login")
        except Exception as exc:
            assert hasattr(exc, "code") and exc.code == 303
            assert "/login" in exc.headers.get("Location")
            assert "next=" in exc.headers.get("Location")

        # Login with cookie jar
        jar = CookieJar()
        authed_opener = build_opener(HTTPCookieProcessor(jar))
        login_body = urlencode({
            "username": "ops",
            "password": "password",
            "csrf": auth.csrf_token(),
            "next": "/",
        }).encode()
        authed_opener.open(Request(f"{base}/login", data=login_body, method="POST"), timeout=2)

        # 2. Authenticated POST with bad CSRF -> redirect with failed & Session expired
        bad_csrf_resp = authed_opener.open(
            Request(f"{base}/sync", data=b"csrf=bad-csrf-token", method="POST"),
            timeout=2,
        )
        assert "sync_status=failed" in bad_csrf_resp.geturl()
        html = bad_csrf_resp.read().decode()
        assert "Session expired" in html

    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_post_sync_busy_and_missing_key_guards(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    conn = connect(db)
    seed_demo(conn)
    conn.close()

    auth = DashboardAuth(username="ops", password="password", session_secret="test-secret")
    server = build_dashboard_server(db, "127.0.0.1", 0, auth=auth)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))

    try:
        # Wait for server
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with urlopen(f"{base}/healthz", timeout=0.2) as resp:
                    if resp.read() == b"ok\n":
                        break
            except OSError:
                time.sleep(0.05)

        # Login
        login_body = urlencode({
            "username": "ops",
            "password": "password",
            "csrf": auth.csrf_token(),
            "next": "/",
        }).encode()
        opener.open(Request(f"{base}/login", data=login_body, method="POST"), timeout=2)

        # 1. Missing FOOTBALL_API_KEY -> sync_status=config
        with patch.dict("os.environ", {"FOOTBALL_API_KEY": ""}):
            post_body = urlencode({"csrf": auth.csrf_token()}).encode()
            resp = opener.open(Request(f"{base}/sync", data=post_body, method="POST"), timeout=2)
            assert "sync_status=config" in resp.geturl()
            html = resp.read().decode()
            assert "Missing FOOTBALL_API_KEY in environment." in html

        # 2. Busy guard: existing running sync job
        conn = connect(db)
        create_sync_job_run(conn)
        conn.close()

        with patch.dict("os.environ", {"FOOTBALL_API_KEY": "test-key"}):
            post_body = urlencode({"csrf": auth.csrf_token()}).encode()
            resp = opener.open(Request(f"{base}/sync", data=post_body, method="POST"), timeout=2)
            assert "sync_status=busy" in resp.geturl()
            html = resp.read().decode()
            # Busy POST is rejected; overview still shows the active running job.
            assert (
                "A sync is already in progress" in html
                or "Sync running in background" in html
                or "Sync in progress" in html
            )

    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
