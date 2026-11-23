"""Bot and laboratory share one SQLite file."""

from __future__ import annotations

from pathlib import Path

from laboratory.store import connect, ensure_experiment, upsert_match
from db.migrate_db import merge_into_shared
from db.storage import Storage


def test_storage_creates_laboratory_tables(tmp_path: Path) -> None:
    path = tmp_path / "football.db"
    store = Storage(path)
    try:
        row = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='matches'"
        ).fetchone()
        assert row is not None
    finally:
        store.close()


def test_laboratory_connect_creates_bot_tables(tmp_path: Path) -> None:
    path = tmp_path / "football.db"
    connection = connect(path)
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='prediction_run'"
        ).fetchone()
        assert row is not None
    finally:
        connection.close()


def test_migrate_merges_legacy_files(tmp_path: Path) -> None:
    bot = tmp_path / "predictions.db"
    lab = tmp_path / "laboratory.db"
    dest = tmp_path / "football.db"

    store = Storage(bot)
    run_id = store.create_run(
        chat_id=1,
        user_id=2,
        raw_home="A",
        raw_away="B",
        raw_competition="PL",
    )
    store.update_run(run_id, status="failed", stage="input", reason="test")
    store.close()

    connection = connect(lab)
    ensure_experiment(connection, {"experiment_id": "TEST-001"})
    upsert_match(
        connection,
        {
            "match_id": "m1",
            "dataset_version": "test",
            "competition": "PL",
            "season": "2024",
            "kickoff": "2024-08-16T19:00:00Z",
            "home_id": "1",
            "home_name": "A",
            "away_id": "2",
            "away_name": "B",
        },
    )
    connection.commit()
    connection.close()

    summary = merge_into_shared(destination=dest, bot_path=bot, lab_path=lab)
    assert summary["destination"] == str(dest)

    merged = connect(dest)
    try:
        assert merged.execute("SELECT COUNT(*) AS n FROM prediction_run").fetchone()["n"] == 1
        assert merged.execute("SELECT COUNT(*) AS n FROM matches").fetchone()["n"] == 1
        assert merged.execute("SELECT COUNT(*) AS n FROM experiments").fetchone()["n"] >= 1
    finally:
        merged.close()
