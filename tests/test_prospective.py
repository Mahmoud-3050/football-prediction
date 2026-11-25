"""Offline tests for prospective live prediction logging, kickoff lock, settlement, and dashboard scorecard."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from db.prospective import (
    compute_prospective_summary,
    lock_started_predictions,
    record_prospective_prediction,
    settle_prospective_predictions,
)
from db.storage import Storage
from laboratory.dashboard import overview
from laboratory.store import connect


def test_prospective_pre_kickoff_save_and_post_kickoff_lock(tmp_path: Path) -> None:
    db_path = tmp_path / "football.db"
    store = Storage(db_path)
    try:
        conn = store._conn
        kickoff = "2026-11-30T15:00:00+00:00"
        pre_time = "2026-11-29T18:00:00+00:00"
        post_time = "2026-11-30T15:05:00+00:00"

        row_id = record_prospective_prediction(
            conn,
            competition="PL",
            fixture_id="9001",
            home_team_id="57",
            away_team_id="65",
            home_team_name="Arsenal",
            away_team_name="Manchester City",
            kickoff_utc=kickoff,
            predicted_at_utc=pre_time,
            source="telegram_predict",
            model_id="pl-live-v1",
            stack_json=json.dumps({"k": 12.0}),
            prob_home=0.50,
            prob_draw=0.25,
            prob_away=0.25,
            prob_over_25=0.58,
            prob_under_25=0.42,
            most_likely_score="2-1",
            most_likely_score_prob=0.12,
            display_pick="1",
            lambda_home=1.65,
            lambda_away=1.10,
        )
        assert row_id is not None

        row = conn.execute("SELECT * FROM prospective_predictions WHERE id = ?", (row_id,)).fetchone()
        assert row is not None
        assert row["status"] == "OPEN"
        assert row["prob_home"] == pytest.approx(0.50)

        # Attempting to record/overwrite at or after kickoff must lock OPEN row and refuse overwrite.
        rejected = record_prospective_prediction(
            conn,
            competition="PL",
            fixture_id="9001",
            home_team_id="57",
            away_team_id="65",
            home_team_name="Arsenal",
            away_team_name="Manchester City",
            kickoff_utc=kickoff,
            predicted_at_utc=post_time,
            source="telegram_predict",
            model_id="pl-live-v1",
            stack_json=json.dumps({"k": 99.0}),
            prob_home=0.90,
            prob_draw=0.05,
            prob_away=0.05,
            most_likely_score="4-0",
        )
        assert rejected is None

        locked_row = conn.execute("SELECT * FROM prospective_predictions WHERE id = ?", (row_id,)).fetchone()
        assert locked_row is not None
        assert locked_row["status"] == "LOCKED"
        assert locked_row["prob_home"] == pytest.approx(0.50)
        assert locked_row["most_likely_score"] == "2-1"

        # Brand new post-kickoff prediction is also rejected.
        rejected_new = record_prospective_prediction(
            conn,
            competition="PL",
            fixture_id="9002",
            home_team_id="61",
            away_team_id="64",
            home_team_name="Chelsea",
            away_team_name="Liverpool",
            kickoff_utc=kickoff,
            predicted_at_utc=kickoff,
            source="scheduled_job",
            model_id="pl-live-v1",
            stack_json=json.dumps({"k": 12.0}),
            prob_home=0.33,
            prob_draw=0.34,
            prob_away=0.33,
            most_likely_score="1-1",
        )
        assert rejected_new is None
    finally:
        store.close()


def test_prospective_settlement_and_summary(tmp_path: Path) -> None:
    db_path = tmp_path / "football.db"
    conn = connect(db_path)
    try:
        kickoff = "2026-11-20T20:00:00+00:00"
        pre_time = "2026-11-19T12:00:00+00:00"

        record_prospective_prediction(
            conn,
            competition="PL",
            fixture_id="fd:777",
            home_team_id="57",
            away_team_id="66",
            home_team_name="Arsenal",
            away_team_name="Manchester United",
            kickoff_utc=kickoff,
            predicted_at_utc=pre_time,
            source="scheduled_job",
            model_id="pl-live-v1",
            stack_json=json.dumps({"k": 12.0}),
            prob_home=0.50,
            prob_draw=0.25,
            prob_away=0.25,
            most_likely_score="2-1",
            most_likely_score_prob=0.11,
            display_pick="1",
            lambda_home=1.6,
            lambda_away=1.0,
        )

        # Lock before settlement
        locked = lock_started_predictions(conn, now_utc="2026-11-20T20:05:00+00:00")
        assert locked == 1

        # Insert finished match + result_revisions
        conn.execute(
            """
            INSERT INTO matches (match_id, dataset_version, competition, season, kickoff, home_id, home_name, away_id, away_name)
            VALUES ('fd:777', 'v1', 'PL', '2026', ?, '57', 'Arsenal', '66', 'Manchester United')
            """,
            (kickoff,),
        )
        conn.execute(
            """
            INSERT INTO result_revisions (match_id, revision, ft_home, ft_away, source)
            VALUES ('fd:777', 1, 2, 1, 'test')
            """
        )
        conn.commit()

        res = settle_prospective_predictions(conn, now_utc="2026-11-20T22:15:00+00:00")
        assert res["settled"] == 1

        settled = conn.execute("SELECT * FROM prospective_predictions").fetchone()
        assert settled is not None
        assert settled["status"] == "SETTLED"
        assert settled["actual_home_goals"] == 2
        assert settled["actual_away_goals"] == 1
        assert settled["actual_outcome"] == "H"
        assert settled["exact_score_hit"] == 1
        assert settled["ft_1x2_log_loss"] == pytest.approx(-math.log(0.50), rel=1e-6)
        assert settled["ft_1x2_brier"] == pytest.approx((0.50 - 1.0) ** 2 + 0.25 ** 2 + 0.25 ** 2, rel=1e-6)
        assert settled["exact_score_log_loss"] is not None

        summary = compute_prospective_summary(conn, competition="PL")
        assert summary["settled_count"] == 1
        assert summary["pending_count"] == 0
        assert summary["ft_1x2_accuracy"] == pytest.approx(1.0)
        assert summary["exact_score_hit_rate"] == pytest.approx(1.0)
        assert summary["naive_uniform_log_loss"] == pytest.approx(math.log(3.0))
    finally:
        conn.close()


def test_dashboard_overview_renders_separate_prospective_scorecard(tmp_path: Path) -> None:
    db_path = tmp_path / "football.db"
    conn = connect(db_path)
    try:
        # Empty state (0 settled predictions)
        html_empty = overview(conn, None)
        assert "Live Prospective Performance" in html_empty
        assert "Strictly Separate from Backtest" in html_empty
        assert "0 settled live predictions yet — waiting for upcoming locked fixtures to finish" in html_empty

        # Add a pre-kickoff OPEN prediction and verify it renders without touching backtest tables
        record_prospective_prediction(
            conn,
            competition="PL",
            fixture_id="fd:888",
            home_team_id="57",
            away_team_id="61",
            home_team_name="Arsenal",
            away_team_name="Chelsea",
            kickoff_utc="2099-05-01T15:00:00+00:00",
            predicted_at_utc="2026-11-25T10:00:00+00:00",
            source="telegram_predict",
            model_id="pl-live-v1",
            stack_json=json.dumps({"k": 12.0}),
            prob_home=0.55,
            prob_draw=0.25,
            prob_away=0.20,
            most_likely_score="2-1",
            display_pick="1",
        )
        html_with_open = overview(conn, None)
        assert "Arsenal vs Chelsea" in html_with_open
        assert "OPEN" in html_with_open
        assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 0
    finally:
        conn.close()
