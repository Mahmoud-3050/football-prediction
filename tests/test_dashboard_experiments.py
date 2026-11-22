"""Tests for the overhaul of the Laboratory Experiments experience.

Covers:
- HTML view builder states (empty, populated, batch filtered, flash banners)
- Experiment detail components (KPI strip, config inspector, leaderboard, comparison bars, traceback box, AI prompt)
- Meta-refresh for running experiments
- Cascading deletion with sqlite_master guards
- Real ThreadingHTTPServer HTTP route tests (GET /experiments, POST /experiments/run with Popen mock, POST /experiments/<id>/delete)
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

import pytest

from laboratory.__main__ import build_dashboard_server
from laboratory.auth import DashboardAuth
from laboratory.dashboard import experiment_detail, experiments_list
from laboratory.store import (
    connect,
    complete_lab_experiment,
    create_lab_experiment,
    delete_lab_experiment,
    fail_lab_experiment,
    get_lab_experiment,
    reset_lab_experiment_for_rerun,
    upsert_lab_experiment_run,
)


# =====================================================================
# Unit / Builder Tests
# =====================================================================

def test_experiments_list_empty(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    page = experiments_list(conn, None, lab_db=lab_db)
    assert "No experiments yet" in page
    assert "Run New Experiment" in page
    assert 'class="pill"' in page
    assert 'name="competitions"' in page
    assert 'name="seasons"' in page
    # Competition pills match Standings format with emblems and full league names
    assert 'class="emblem"' in page
    assert "Premier League" in page
    assert "La Liga" in page
    assert "Serie A" in page
    assert "Saudi Pro League" in page
    assert 'class="league-pills"' in page
    # Canonical order matching Standings: PL before PD, PD before SA
    pl_pos = page.find("Premier League")
    pd_pos = page.find("La Liga")
    sa_pos = page.find("Serie A")
    assert pl_pos < pd_pos < sa_pos


def test_experiments_list_with_data(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    create_lab_experiment(conn, "EXP-TEST-001", "PL", {"seasons": [2024, 2025]})
    upsert_lab_experiment_run(
        conn,
        "EXP-TEST-001:m1",
        "EXP-TEST-001",
        "model-a",
        {"k": 6},
        status="completed",
        metrics={"full_time_1x2_accuracy": 0.45, "full_time_1x2_log_loss": 1.02, "full_time_1x2_brier": 0.58},
        matches_priced=10,
        matches_evaluated=10,
    )
    page = experiments_list(conn, None, lab_db=lab_db)
    assert "EXP-TEST-001" in page
    assert "experiment-card" in page
    assert "PL" in page
    assert "Premier League" in page
    assert 'class="scope-badge league"' in page
    assert "2024" in page
    assert "2025" in page
    assert "1/1" in page  # 1/1 runs passed
    assert "View Full Analysis →" in page
    assert "Delete" in page
    assert 'class="confirm-dialog"' in page
    assert "Confirm Delete" in page
    assert "Cancel" in page


def test_experiments_list_batch_filter(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    create_lab_experiment(conn, "EXP-BATCH-PL", "PL", {"batch_id": "BATCH-999", "seasons": [2025]})
    create_lab_experiment(conn, "EXP-OTHER-SA", "SA", {"seasons": [2025]})

    # With batch filter
    page = experiments_list(conn, {"batch": ["BATCH-999"]}, lab_db=lab_db)
    assert "EXP-BATCH-PL" in page
    assert "EXP-OTHER-SA" not in page
    assert "Viewing Coordinated Batch" in page

    # Without batch filter
    page_all = experiments_list(conn, None, lab_db=lab_db)
    assert "EXP-BATCH-PL" in page_all
    assert "EXP-OTHER-SA" in page_all


def test_experiments_list_flash_banners(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    page = experiments_list(
        conn,
        {"run_status": ["started"], "run_detail": ["Background search queued."]},
        lab_db=lab_db,
    )
    assert "Experiment Started" in page
    assert "Background search queued." in page


def test_experiments_list_running_auto_refresh_and_planned_runs(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    create_lab_experiment(conn, "EXP-RUN-001", "PL", {
        "seasons": [2025],
        "param_grid": {"k": [5.0, 6.0], "draw_boost": [1.0, 1.2]},  # 4 planned models
    })
    page = experiments_list(conn, None, lab_db=lab_db)
    assert '<meta http-equiv="refresh" content="5">' in page
    assert "0/4" in page  # 0 passed out of 4 planned runs (no more 0/0)
    assert "pass-bar running" in page


def test_experiment_detail_populated(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    create_lab_experiment(conn, "EXP-DETAIL-001", "PL", {
        "seasons": [2025],
        "base_config": {"k": 6.0, "dependence": "independent"},
        "param_grid": {"k": [5.0, 6.0]},
        "primary_metric": "log_loss",
        "min_matches": 50,
    })
    upsert_lab_experiment_run(
        conn,
        "EXP-DETAIL-001:m1",
        "EXP-DETAIL-001",
        "model-base",
        {"k": 6.0, "dependence": "independent"},
        status="completed",
        metrics={
            "full_time_1x2_accuracy": 0.44,
            "full_time_1x2_log_loss": 1.05,
            "full_time_1x2_brier": 0.59,
            "exact_score": {"hit": 0.08, "log_loss": 3.21},
            "half_time_1x2": {"accuracy": 0.40, "log_loss": 1.12},
            "over_under_2_5": {"accuracy": 0.55, "log_loss": 0.70},
            "over_under_3_5": {"accuracy": 0.62, "log_loss": 0.65},
        },
        matches_priced=20,
        matches_evaluated=20,
    )
    upsert_lab_experiment_run(
        conn,
        "EXP-DETAIL-001:m2",
        "EXP-DETAIL-001",
        "model-chal",
        {"k": 5.0, "dependence": "independent"},
        status="completed",
        metrics={
            "full_time_1x2_accuracy": 0.48,
            "full_time_1x2_log_loss": 0.99,
            "full_time_1x2_brier": 0.55,
            "exact_score": {"hit": 0.12, "log_loss": 2.95},
            "half_time_1x2": {"accuracy": 0.43, "log_loss": 1.05},
            "over_under_2_5": {"accuracy": 0.58, "log_loss": 0.66},
            "over_under_3_5": {"accuracy": 0.64, "log_loss": 0.61},
        },
        matches_priced=20,
        matches_evaluated=20,
    )
    page = experiment_detail(conn, "EXP-DETAIL-001", None, lab_db=lab_db)
    assert page is not None
    assert "EXP-DETAIL-001" in page
    assert "Premier League" in page
    assert 'class="scope-badge league"' in page
    assert "Runs Leaderboard" in page
    assert "full_time_1x2_log_loss" in page
    assert "Exact Hit %" in page
    assert "Exact LL" in page
    assert "HT Acc %" in page and "HT Log-Loss" in page
    assert "O/U 2.5 Acc" in page and "O/U 2.5 LL" in page
    assert "Goals 3.5 Acc %" in page and "Goals 3.5 Log-Loss" in page
    assert "Best Exact Hit %" in page and "Best Exact LL" in page
    assert "Best O/U 2.5 Acc" in page
    assert "8.0%" in page and "12.0%" in page
    assert "3.2100" in page and "2.9500" in page
    assert "40.0%" in page and "55.0%" in page and "62.0%" in page
    assert "metric-win" in page
    # Lower log-loss (model-chal) must rank above model-base.
    assert page.index("model-chal") < page.index("model-base")
    assert "n&lt;50" in page or "n<50" in page
    assert "model-base" in page
    assert "model-chal" in page
    assert "Search Grid" in page
    assert "Base Configuration" in page
    assert "prompt-export-panel" in page
    assert "data-copy-target=\"ai-prompt-text\"" in page
    assert "Exact Hit %" in page  # AI diagnostic prompt table header too
    assert "Exact Score calibration" in page
    assert "goal-volume bias" in page or "O/U 2.5" in page


def test_experiments_form_includes_exact_score_log_loss_primary_metric(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    page = experiments_list(conn, None, lab_db=lab_db)
    assert 'value="exact_score_log_loss"' in page
    assert "Exact Score Log-Loss" in page


def test_exact_hit_reads_flat_hit_rate_key(tmp_path: Path) -> None:
    from laboratory.dashboard import _exact_score_from_metrics

    hit, ll = _exact_score_from_metrics(
        {"exact_score_hit_rate": 0.11, "exact_score_log_loss": 2.77}
    )
    assert hit == 0.11
    assert ll == 2.77
    hit2, ll2 = _exact_score_from_metrics({"exact_score": {"correct": 0.09, "log_loss": 3.0}})
    assert hit2 == 0.09
    assert ll2 == 3.0


def test_experiment_detail_legacy_metrics_render_dashes(tmp_path: Path) -> None:
    """Older runs without Exact / O/U keys must render — without crashing."""
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    create_lab_experiment(
        conn,
        "EXP-LEGACY",
        "PL",
        {"seasons": [2025], "primary_metric": "log_loss", "min_matches": 10},
    )
    upsert_lab_experiment_run(
        conn,
        "EXP-LEGACY:m1",
        "EXP-LEGACY",
        "legacy-model",
        {"k": 6.0},
        status="completed",
        metrics={"full_time_1x2_accuracy": 0.5, "full_time_1x2_log_loss": 1.0},
        matches_priced=12,
        matches_evaluated=12,
    )
    page = experiment_detail(conn, "EXP-LEGACY", None, lab_db=lab_db)
    assert page is not None
    assert "Exact Hit %" in page
    assert "O/U 2.5 Acc" in page
    assert "—" in page
    assert "legacy-model" in page


def test_experiment_detail_includes_rerun_button(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    create_lab_experiment(conn, "EXP-RERUN-UI", "PL", {"seasons": [2025]})
    complete_lab_experiment(conn, "EXP-RERUN-UI", best_config={"k": 6.0}, best_metrics={})
    page = experiment_detail(conn, "EXP-RERUN-UI", None, lab_db=lab_db)
    assert page is not None
    assert 'class="btn-rerun"' in page
    assert "/experiments/EXP-RERUN-UI/rerun" in page
    assert "Confirm Re-run" in page
    # Running experiments disable the trigger button.
    conn.execute(
        "UPDATE lab_experiments SET status = 'running' WHERE experiment_id = 'EXP-RERUN-UI'"
    )
    conn.commit()
    page_running = experiment_detail(conn, "EXP-RERUN-UI", None, lab_db=lab_db)
    assert page_running is not None
    assert 'class="btn-rerun" disabled' in page_running


def test_reset_lab_experiment_for_rerun_clears_runs(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    create_lab_experiment(conn, "EXP-RESET", "PL", {"seasons": [2025], "k": 6.0})
    upsert_lab_experiment_run(
        conn,
        "EXP-RESET:r1",
        "EXP-RESET",
        "m1",
        {"k": 6.0},
        status="completed",
        metrics={"full_time_1x2_log_loss": 1.0},
        matches_priced=10,
        matches_evaluated=10,
    )
    complete_lab_experiment(
        conn,
        "EXP-RESET",
        best_config={"k": 6.0},
        best_metrics={"full_time_1x2_log_loss": 1.0},
    )
    deleted = reset_lab_experiment_for_rerun(conn, "EXP-RESET")
    assert deleted == 1
    exp = get_lab_experiment(conn, "EXP-RESET")
    assert exp is not None
    assert exp["status"] == "running"
    assert exp["best_config"] is None
    assert exp["best_metrics"] is None
    assert exp["config"]["seasons"] == [2025]
    runs = conn.execute(
        "SELECT COUNT(*) AS n FROM lab_experiment_runs WHERE experiment_id = 'EXP-RESET'"
    ).fetchone()
    assert int(runs["n"]) == 0


def test_experiments_list_outer_card_shows_exact_metrics(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    create_lab_experiment(conn, "EXP-CARD-EXACT", "PL", {"seasons": [2025]})
    complete_lab_experiment(
        conn,
        "EXP-CARD-EXACT",
        best_config={"k": 6.0},
        best_metrics={
            "full_time_1x2_accuracy": 0.5,
            "full_time_1x2_log_loss": 1.01,
            "full_time_1x2_brier": 0.55,
            "exact_score_hit_rate": 0.09,
            "exact_score_log_loss": 2.88,
            "n_matches": 40,
        },
    )
    page = experiments_list(conn, None, lab_db=lab_db)
    assert "Exact Hit %" in page
    assert "Exact LL" in page
    assert "9.0%" in page
    assert "2.8800" in page


def test_experiment_detail_running_has_meta_refresh(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    create_lab_experiment(conn, "EXP-RUNNING", "PL", {"seasons": [2025]})
    # In running status:
    page = experiment_detail(conn, "EXP-RUNNING", None, lab_db=lab_db)
    assert page is not None
    assert '<meta http-equiv="refresh" content="5">' in page

    # Mark completed:
    conn.execute("UPDATE lab_experiments SET status = 'completed' WHERE experiment_id = 'EXP-RUNNING'")
    conn.commit()
    page_done = experiment_detail(conn, "EXP-RUNNING", None, lab_db=lab_db)
    assert page_done is not None
    assert '<meta http-equiv="refresh"' not in page_done


def test_experiment_detail_failed_shows_traceback(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    create_lab_experiment(conn, "EXP-FAIL", "PL", {})
    fail_lab_experiment(conn, "EXP-FAIL", "ZeroDivisionError: integer division by zero\n  File runner.py line 42")
    page = experiment_detail(conn, "EXP-FAIL", None, lab_db=lab_db)
    assert page is not None
    assert "error-box" in page
    assert "Failure Traceback" in page
    assert "ZeroDivisionError: integer division by zero" in page


def test_experiment_detail_has_ai_prompt(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    create_lab_experiment(conn, "EXP-AI", "SPL", {
        "seasons": [2025, 2026],
        "base_config": {"k": 6.0, "dependence": "dixon_coles"},
        "param_grid": {"draw_boost": [1.0, 1.15]},
        "primary_metric": "log_loss",
    })
    upsert_lab_experiment_run(
        conn,
        "EXP-AI:m1",
        "EXP-AI",
        "model-spl",
        {"k": 6.0, "dependence": "dixon_coles", "draw_boost": 1.15},
        status="completed",
        metrics={"full_time_1x2_accuracy": 0.41, "full_time_1x2_log_loss": 1.01},
        matches_priced=30,
        matches_evaluated=30,
    )
    page = experiment_detail(conn, "EXP-AI", None, lab_db=lab_db)
    assert page is not None
    assert "prompt-export-panel" in page
    assert "data-copy-target=\"ai-prompt-text\"" in page
    assert '<textarea id="ai-prompt-text" readonly>' in page
    assert "AI Diagnostic Analysis Request: Experiment EXP-AI" in page
    assert "Strengths" in page and "Winning Patterns" in page
    assert "Weaknesses" in page and "Overfitting Risks" in page


def test_experiment_detail_404(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    assert experiment_detail(conn, "NONEXISTENT", None, lab_db=lab_db) is None


def test_experiments_delete_removes_data(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    create_lab_experiment(conn, "EXP-TO-DEL", "PL", {})
    upsert_lab_experiment_run(conn, "EXP-TO-DEL:r1", "EXP-TO-DEL", "m1", {}, status="completed", metrics={})

    deleted = delete_lab_experiment(conn, "EXP-TO-DEL", cascade_predictions=True)
    assert deleted >= 2
    assert get_lab_experiment(conn, "EXP-TO-DEL") is None


# =====================================================================
# Real HTTP Route Tests (ThreadingHTTPServer)
# =====================================================================

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_303(self, req, fp, code, msg, headers):
        return fp

    def http_error_302(self, req, fp, code, msg, headers):
        return fp


def _start_server(tmp_path: Path, auth: DashboardAuth | None = None) -> tuple[ThreadingHTTPServer, str, Path, Path]:
    live_db = tmp_path / "football.db"
    lab_db = tmp_path / "lab.db"
    conn_live = connect(live_db)
    conn_live.close()
    conn_lab = connect(lab_db)
    conn_lab.close()

    default_auth = auth or DashboardAuth(
        username="admin",
        password="",  # disabled
        session_secret="test-secret-key",
    )
    server = build_dashboard_server(live_db, "127.0.0.1", 0, auth=default_auth, lab_db=lab_db)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"

    # Wait for server ready
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            with urlopen(f"{base_url}/healthz", timeout=0.2) as resp:
                if resp.read() == b"ok\n":
                    break
        except OSError:
            time.sleep(0.05)
    else:
        raise AssertionError("Dashboard server did not start")

    return server, base_url, lab_db, live_db


def test_route_experiments_get(tmp_path: Path) -> None:
    server, base_url, lab_db, _ = _start_server(tmp_path)
    try:
        with urlopen(f"{base_url}/experiments", timeout=2) as resp:
            assert resp.status == 200
            html_text = resp.read().decode("utf-8")
            assert "Lab Experiments" in html_text
            assert "Run New Experiment" in html_text
    finally:
        server.shutdown()


def test_route_run_experiment_csrf_rejection(tmp_path: Path) -> None:
    auth = DashboardAuth(
        username="admin",
        password="secretpassword",
        session_secret="test-secret-key",
    )
    server, base_url, lab_db, _ = _start_server(tmp_path, auth=auth)
    opener = build_opener(_NoRedirect)
    try:
        # Issue valid session
        token = auth.issue_session("admin")
        from laboratory.auth import COOKIE_NAME
        cookie_header = f"{COOKIE_NAME}={token}"

        # POST without CSRF token
        data = urlencode({"competitions": "PL", "seasons": "2025"}).encode()
        req = Request(f"{base_url}/experiments/run", data=data, headers={"Cookie": cookie_header})
        try:
            resp = opener.open(req)
            assert resp.status in (303, 400)
            loc = resp.headers.get("Location") or ""
            assert "Session+expired" in loc
        except HTTPError as e:
            assert e.code in (303, 400)
    finally:
        server.shutdown()


def test_experiments_form_includes_json_config_textarea(tmp_path: Path) -> None:
    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    page = experiments_list(conn, None, lab_db=lab_db)
    assert 'name="config_json"' in page
    assert "JSON config" in page
    assert "overrides form" in page
    assert 'class="json-config-row"' in page
    assert 'name="launch_source"' in page and 'value="json"' in page
    assert ">Run</button>" in page


def test_route_run_experiment_from_json_config(tmp_path: Path) -> None:
    server, base_url, lab_db, _ = _start_server(tmp_path)
    opener = build_opener(_NoRedirect)
    try:
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            config = {
                "experiment_id": "EXP-JSON-RUN",
                "competitions": ["PL"],
                "seasons": [2024, 2025],
                "primary_metric": "exact_score_log_loss",
                "min_matches": 40,
                "base_config": {"k": 5.5, "prior_mean": "one"},
                "param_grid": {"k": [5.5, 6.5], "draw_boost": [1.0]},
            }
            payload = {"config_json": json.dumps(config)}
            data = urlencode(payload).encode()
            resp = opener.open(Request(f"{base_url}/experiments/run", data=data))
            assert resp.status == 303
            assert "/experiments/EXP-JSON-RUN" in (resp.headers.get("Location") or "")
            assert mock_popen.called
            conn = connect(lab_db)
            exp = get_lab_experiment(conn, "EXP-JSON-RUN")
            conn.close()
            assert exp is not None
            assert exp["config"]["base_config"]["k"] == 5.5
            assert exp["config"]["base_config"]["prior_mean"] == "one"
            assert exp["config"]["param_grid"]["k"] == [5.5, 6.5]
            assert exp["config"]["min_matches"] == 40
            assert exp["config"]["primary_metric"] == "exact_score_log_loss"
    finally:
        server.shutdown()


def test_route_run_experiment_form_selects_exact_score_log_loss(tmp_path: Path) -> None:
    server, base_url, lab_db, _ = _start_server(tmp_path)
    opener = build_opener(_NoRedirect)
    try:
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            payload = {
                "competitions": "PL",
                "seasons": "2025",
                "experiment_id": "EXP-FORM-EXACT",
                "primary_metric": "exact_score_log_loss",
                "k": "6.0",
                "min_matches": "50",
            }
            data = urlencode(payload).encode()
            resp = opener.open(Request(f"{base_url}/experiments/run", data=data))
            assert resp.status == 303
            assert mock_popen.called
            conn = connect(lab_db)
            exp = get_lab_experiment(conn, "EXP-FORM-EXACT")
            conn.close()
            assert exp is not None
            assert exp["config"]["primary_metric"] == "exact_score_log_loss"
    finally:
        server.shutdown()


def test_route_run_experiment_rejects_invalid_json(tmp_path: Path) -> None:
    server, base_url, lab_db, _ = _start_server(tmp_path)
    opener = build_opener(_NoRedirect)
    try:
        with patch("subprocess.Popen") as mock_popen:
            data = urlencode({"config_json": "{not-json"}).encode()
            resp = opener.open(Request(f"{base_url}/experiments/run", data=data))
            assert resp.status == 303
            loc = resp.headers.get("Location") or ""
            assert "run_status=failed" in loc
            assert "Invalid+JSON" in loc or "Invalid%20JSON" in loc or "JSON" in loc
            assert not mock_popen.called
            conn = connect(lab_db)
            rows = conn.execute("SELECT COUNT(*) AS n FROM lab_experiments").fetchone()
            conn.close()
            assert int(rows["n"]) == 0
    finally:
        server.shutdown()


def test_route_run_experiment_single_league_spawns_process(tmp_path: Path) -> None:
    server, base_url, lab_db, _ = _start_server(tmp_path)
    opener = build_opener(_NoRedirect)
    try:
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_popen.return_value = mock_proc

            payload = {
                "competitions": "PL",
                "seasons": "2024,2025",
                "experiment_id": "EXP-UNIT-PL",
                "primary_metric": "log_loss",
                "k": "6.0",
                "min_matches": "30",
                "draw_boost": "1.0",
                "draw_pick_threshold": "0.28",
                "draw_pick_margin": "0.05",
                "grid_k": "5.0, 6.0",
            }
            data = urlencode(payload).encode()
            req = Request(f"{base_url}/experiments/run", data=data)
            resp = opener.open(req)

            assert resp.status == 303
            location = resp.headers.get("Location") or ""
            assert "/experiments/EXP-UNIT-PL" in location
            assert "run_status=started" in location

            # Verify Popen called with isolated CLI
            assert mock_popen.called
            call_args = mock_popen.call_args[0][0]
            assert "lab-run" in call_args
            assert "--experiment-id" in call_args
            assert "EXP-UNIT-PL" in call_args
            assert "--lab-db" in call_args

            # Verify DB recorded running status
            conn = connect(lab_db)
            exp = get_lab_experiment(conn, "EXP-UNIT-PL")
            conn.close()
            assert exp is not None
            assert exp["status"] == "running"
            assert exp["competition"] == "PL"
            assert tuple(exp["config"]["seasons"]) == (2024, 2025)
            assert exp["config"]["min_matches"] == 30
            # Single advanced knobs live in base_config; only k/draw_boost are grid dims.
            assert exp["config"]["base_config"]["draw_pick_threshold"] == 0.28
            assert exp["config"]["base_config"]["draw_pick_margin"] == 0.05
            assert "draw_pick_threshold" not in exp["config"]["param_grid"]
            assert exp["config"]["param_grid"]["k"] == [5.0, 6.0]
    finally:
        server.shutdown()


def test_route_run_experiment_multi_league_batch(tmp_path: Path) -> None:
    server, base_url, lab_db, _ = _start_server(tmp_path)
    opener = build_opener(_NoRedirect)
    try:
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_popen.return_value = mock_proc

            # Send multiple competitions and seasons checkboxes
            payload_tuples = [
                ("competitions", "PL"),
                ("competitions", "SPL"),
                ("seasons", "2024"),
                ("seasons", "2025"),
                ("primary_metric", "log_loss"),
            ]
            data = urlencode(payload_tuples).encode()
            req = Request(f"{base_url}/experiments/run", data=data)
            resp = opener.open(req)

            assert resp.status == 303
            location = resp.headers.get("Location") or ""
            assert "/experiments?batch=BATCH-" in location
            assert "run_status=started" in location

            assert mock_popen.called
            call_args = mock_popen.call_args[0][0]
            assert "lab-run" in call_args
            assert "--batch-id" in call_args

            # Verify both experiments created with batch_id
            conn = connect(lab_db)
            rows = conn.execute("SELECT experiment_id, competition, config_json FROM lab_experiments").fetchall()
            conn.close()
            comps = {r["competition"] for r in rows}
            assert "PL" in comps
            assert "SPL" in comps
            for r in rows:
                c = json.loads(r["config_json"])
                assert "batch_id" in c
                assert c["batch_id"].startswith("BATCH-")
    finally:
        server.shutdown()


def test_route_run_experiment_slug_validation(tmp_path: Path) -> None:
    server, base_url, lab_db, _ = _start_server(tmp_path)
    opener = build_opener(_NoRedirect)
    try:
        # Invalid slug with spaces and special chars
        payload = {
            "competitions": "PL",
            "seasons": "2025",
            "experiment_id": "bad slug; DROP TABLE matches;",
        }
        data = urlencode(payload).encode()
        req = Request(f"{base_url}/experiments/run", data=data)
        resp = opener.open(req)
        assert resp.status == 303
        loc = resp.headers.get("Location") or ""
        assert "Invalid+Experiment+ID" in loc
    finally:
        server.shutdown()


def test_route_run_experiment_invalid_grid_redirects(tmp_path: Path) -> None:
    server, base_url, lab_db, _ = _start_server(tmp_path)
    opener = build_opener(_NoRedirect)
    try:
        payload = {
            "competitions": "PL",
            "seasons": "2025",
            "grid_k": "5.0, not-a-number",
        }
        data = urlencode(payload).encode()
        req = Request(f"{base_url}/experiments/run", data=data)
        resp = opener.open(req)
        assert resp.status == 303
        loc = resp.headers.get("Location") or ""
        assert "run_status=failed" in loc
        assert "Invalid" in loc and "numeric" in loc
    finally:
        server.shutdown()


def test_route_run_experiment_uses_absolute_db_paths(tmp_path: Path) -> None:
    server, base_url, lab_db, live_db = _start_server(tmp_path)
    opener = build_opener(_NoRedirect)
    try:
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            data = urlencode(
                {
                    "competitions": "PL",
                    "seasons": "2025",
                    "experiment_id": "EXP-ABS-PATH",
                }
            ).encode()
            req = Request(f"{base_url}/experiments/run", data=data)
            resp = opener.open(req)
            assert resp.status == 303
            call_args = mock_popen.call_args[0][0]
            lab_idx = call_args.index("--lab-db") + 1
            live_idx = call_args.index("--live-db") + 1
            assert Path(call_args[lab_idx]).is_absolute()
            assert Path(call_args[live_idx]).is_absolute()
            assert Path(call_args[lab_idx]) == lab_db.resolve()
            assert Path(call_args[live_idx]) == live_db.resolve()
    finally:
        server.shutdown()


def test_route_delete_experiment_success(tmp_path: Path) -> None:
    server, base_url, lab_db, _ = _start_server(tmp_path)
    opener = build_opener(_NoRedirect)
    try:
        conn = connect(lab_db)
        create_lab_experiment(conn, "EXP-ROUTE-DEL", "PL", {})
        upsert_lab_experiment_run(conn, "EXP-ROUTE-DEL:r1", "EXP-ROUTE-DEL", "m1", {}, status="completed", metrics={})
        conn.close()

        req = Request(f"{base_url}/experiments/EXP-ROUTE-DEL/delete", data=b"")
        resp = opener.open(req)
        assert resp.status == 303
        loc = resp.headers.get("Location") or ""
        assert "deleted" in loc

        conn = connect(lab_db)
        assert get_lab_experiment(conn, "EXP-ROUTE-DEL") is None
        conn.close()
    finally:
        server.shutdown()


def test_route_rerun_experiment_resets_and_spawns(tmp_path: Path) -> None:
    server, base_url, lab_db, live_db = _start_server(tmp_path)
    opener = build_opener(_NoRedirect)
    try:
        conn = connect(lab_db)
        create_lab_experiment(conn, "EXP-ROUTE-RERUN", "PL", {"seasons": [2025]})
        upsert_lab_experiment_run(
            conn,
            "EXP-ROUTE-RERUN:r1",
            "EXP-ROUTE-RERUN",
            "m1",
            {"k": 6.0},
            status="completed",
            metrics={"full_time_1x2_log_loss": 1.1},
        )
        complete_lab_experiment(
            conn,
            "EXP-ROUTE-RERUN",
            best_config={"k": 6.0},
            best_metrics={"full_time_1x2_log_loss": 1.1},
        )
        conn.close()

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            req = Request(f"{base_url}/experiments/EXP-ROUTE-RERUN/rerun", data=b"")
            resp = opener.open(req)
            assert resp.status == 303
            loc = resp.headers.get("Location") or ""
            assert "/experiments/EXP-ROUTE-RERUN" in loc
            assert "run_status=started" in loc
            assert mock_popen.called
            call_args = mock_popen.call_args[0][0]
            assert "lab-run" in call_args
            assert "--experiment-id" in call_args
            assert "EXP-ROUTE-RERUN" in call_args
            assert Path(call_args[call_args.index("--lab-db") + 1]) == lab_db.resolve()
            assert Path(call_args[call_args.index("--live-db") + 1]) == live_db.resolve()

        conn = connect(lab_db)
        exp = get_lab_experiment(conn, "EXP-ROUTE-RERUN")
        runs_n = conn.execute(
            "SELECT COUNT(*) AS n FROM lab_experiment_runs WHERE experiment_id = 'EXP-ROUTE-RERUN'"
        ).fetchone()
        conn.close()
        assert exp is not None
        assert exp["status"] == "running"
        assert exp["best_metrics"] is None
        assert int(runs_n["n"]) == 0
    finally:
        server.shutdown()
