"""Matches list: SQL filters, pagination, deferred prediction bodies."""

from __future__ import annotations

from pathlib import Path

from laboratory import BOT_EXPERIMENT_ID
from laboratory.dashboard import PAGE_SIZE, _count_matches, _load, _load_match_page
from laboratory.store import (
    append_result,
    connect,
    ensure_experiment,
    insert_prediction,
    save_snapshot,
    upsert_match,
)


def _seed_many(connection, *, n: int = 80) -> None:
    ensure_experiment(
        connection,
        {"experiment_id": BOT_EXPERIMENT_ID, "competition": "PL", "engine": "score_model"},
    )
    body = {
        "engine": "score_model",
        "full_time": {
            "result_1x2": {
                "home": 0.5,
                "draw": 0.25,
                "away": 0.25,
                "most_likely": "home",
            },
            "exact_score_most_likely": {"home_goals": 1, "away_goals": 0},
            "exact_scores": [{"home_goals": 1, "away_goals": 0, "probability": 0.2}],
            "over_under_2_5": {"over": 0.4, "under": 0.6, "most_likely": "under"},
            "over_under_3_5": {"over": 0.2, "under": 0.8, "most_likely": "under"},
        },
        "half_time": {"withheld": True},
    }
    for i in range(n):
        day = 1 + (i % 28)
        match_id = f"perf-{i:03d}"
        kickoff = f"2026-10-{day:02d}T15:00:00Z"
        upsert_match(
            connection,
            {
                "match_id": match_id,
                "dataset_version": "t",
                "competition": "PL",
                "season": "2026",
                "kickoff": kickoff,
                "home_id": f"H{i % 10}",
                "home_name": f"Home {i % 10}",
                "away_id": f"A{i % 10}",
                "away_name": f"Away {i % 10}",
            },
        )
        append_result(
            connection,
            match_id,
            {"ft_home": 1, "ft_away": 0, "ht_home": 0, "ht_away": 0},
            "test",
        )
        snap = save_snapshot(
            connection,
            {
                "match_id": match_id,
                "payload_hash": f"h-{i}",
                "competition": "PL",
                "kickoff": kickoff,
                "home": {"id": f"H{i % 10}", "name": f"Home {i % 10}"},
                "away": {"id": f"A{i % 10}", "name": f"Away {i % 10}"},
                "prior_matches": [],
                "leakage_ok": True,
            },
            experiment_id=BOT_EXPERIMENT_ID,
        )
        insert_prediction(
            connection,
            match_id=match_id,
            snapshot_id=snap,
            engine="score_model",
            model_id="bot-score-model-live",
            body=body,
            experiment_id=BOT_EXPERIMENT_ID,
        )
    connection.commit()


def test_dashboard_indexes_created(tmp_path: Path) -> None:
    conn = connect(tmp_path / "lab.db")
    names = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    assert "idx_matches_kickoff" in names
    assert "idx_predictions_match_engine_exp" in names
    assert "idx_snapshots_match_experiment" in names


def test_load_match_page_limits_and_attaches_body(tmp_path: Path) -> None:
    conn = connect(tmp_path / "lab.db")
    _seed_many(conn, n=PAGE_SIZE + 15)
    filters = {
        "engine": "score_model",
        "experiment": BOT_EXPERIMENT_ID,
        "league": "PL",
        "season": "2026",
        "team": "",
        "date_from": "2026-10-01",
        "date_to": "2026-10-31",
        "fixture": "all",
        "status": "all",
        "verdict": "all",
        "confidence": "all",
        "order": "newest",
        "page": "1",
    }
    total = _count_matches(conn, "score_model", BOT_EXPERIMENT_ID, filters)
    assert total == PAGE_SIZE + 15
    count, window = _load_match_page(conn, filters)
    assert count == total
    assert len(window) == PAGE_SIZE
    assert all(window_row.get("body") for window_row in window)
    # Limited SQL path must not pull body_json for the whole table.
    lean = _load(
        conn,
        "score_model",
        BOT_EXPERIMENT_ID,
        filters=filters,
        include_body=False,
        limit=PAGE_SIZE,
        offset=0,
    )
    assert len(lean) == PAGE_SIZE
    assert all(not item.get("body_json") for item in lean)


def test_load_match_page_second_page(tmp_path: Path) -> None:
    conn = connect(tmp_path / "lab.db")
    _seed_many(conn, n=PAGE_SIZE + 5)
    filters = {
        "engine": "score_model",
        "experiment": BOT_EXPERIMENT_ID,
        "league": "",
        "season": "",
        "team": "",
        "date_from": "2026-10-01",
        "date_to": "2026-10-31",
        "fixture": "finished",
        "status": "all",
        "verdict": "all",
        "confidence": "all",
        "order": "oldest",
        "page": "2",
    }
    total, window = _load_match_page(conn, filters)
    assert total == PAGE_SIZE + 5
    assert len(window) == 5
