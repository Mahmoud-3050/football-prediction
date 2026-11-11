"""Smoke tests for product/ops improvements (no lab science)."""

from __future__ import annotations

from pathlib import Path

from laboratory.dashboard import _comparison, overview
from laboratory.store import connect, latest_job_runs, record_job_run


def test_record_and_show_job_runs_on_overview(tmp_path: Path) -> None:
    connection = connect(tmp_path / "football.db")
    record_job_run(
        connection,
        name="tomorrow",
        started_at="2026-11-11T03:00:00+00:00",
        finished_at="2026-11-11T03:01:00+00:00",
        ok=True,
        summary={"day": "2026-11-12", "candidates": 4, "priced": 3, "abstained": 1, "failed": 0},
    )
    record_job_run(
        connection,
        name="standings",
        started_at="2026-11-11T03:01:00+00:00",
        finished_at="2026-11-11T03:02:00+00:00",
        ok=False,
        summary={"error": "quota"},
    )
    runs = latest_job_runs(connection)
    assert [row["name"] for row in runs] == ["tomorrow", "standings"]
    page = overview(connection)
    assert "Server jobs" in page
    assert "Tomorrow predictions" in page
    assert "Standings refresh" in page
    assert "job-ok" in page
    assert "job-failed" in page
    assert "priced 3" in page
    assert "quota" in page
    assert "2026-11-11 05:01 Cairo" in page
    assert " UTC" not in page.split("Server jobs", 1)[1]


def test_comparison_includes_log_loss_and_brier_columns() -> None:
    class _Row(dict):
        def __getitem__(self, key):  # noqa: ANN001
            return dict.__getitem__(self, key)

    result = _Row(ft_home=2, ft_away=1, ht_home=1, ht_away=0)
    evaluation = _Row(
        metrics_json={
            "full_time_1x2": {
                "status": "scored",
                "selection": "home",
                "correct": True,
                "log_loss": 0.512,
                "brier": 0.21,
            },
            "half_time_1x2": {"status": "withheld"},
            "exact_score": {
                "status": "scored",
                "correct": False,
                "log_loss": 2.1,
            },
            "over_under_2_5": {
                "status": "scored",
                "selection": "over",
                "correct": True,
                "log_loss": 0.4,
                "brier": 0.11,
            },
            "over_under_3_5": {
                "status": "scored",
                "selection": "under",
                "correct": False,
                "log_loss": 0.9,
                "brier": 0.33,
            },
        }
    )
    # evaluation metrics_json is read via _json which expects a string or dict-like
    import json

    evaluation = _Row(metrics_json=json.dumps(evaluation["metrics_json"]))
    html = _comparison(_Row({}), result, evaluation)
    assert "Log loss" in html
    assert "Brier" in html
    assert "0.512" in html
    assert "0.210" in html
    assert "2.100" in html
