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


def test_compact_database_and_slim_snapshot(tmp_path: Path) -> None:
    import json
    from db.migrate_db import compact_database
    from laboratory.store import save_snapshot

    db_path = tmp_path / "compact.db"
    conn = connect(db_path)
    try:
        # Insert a legacy-style snapshot containing prior_matches
        legacy_payload = {
            "match_id": "m1",
            "payload_hash": "hash1",
            "leakage_ok": True,
            "coverage": {"prior_full_time_matches": 10, "prior_half_time_matches": 8},
            "prior_matches": [{"match_id": f"p{i}", "ft_home": 1, "ft_away": 1} for i in range(50)],
        }
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, experiment_id, match_id, payload_json, leakage_ok) VALUES (?, ?, ?, ?, ?)",
            ("EXP:hash1", "EXP", "m1", json.dumps(legacy_payload), 1),
        )
        # New save_snapshot should automatically omit prior_matches
        save_snapshot(
            conn,
            {
                "match_id": "m2",
                "payload_hash": "hash2",
                "leakage_ok": True,
                "coverage": {"prior_full_time_matches": 5, "prior_half_time_matches": 5},
                "prior_matches": [{"match_id": "p0"}],
            },
            experiment_id="EXP",
        )
        conn.commit()
    finally:
        conn.close()

    res = compact_database(db_path)
    assert res["snapshots_slimmed"] == 1
    assert res["integrity"] == "ok"

    verify = connect(db_path)
    try:
        for sid in ("EXP:hash1", "EXP:hash2"):
            row = verify.execute("SELECT payload_json FROM snapshots WHERE snapshot_id = ?", (sid,)).fetchone()
            parsed = json.loads(row["payload_json"])
            assert "prior_matches" not in parsed
            assert "coverage" in parsed
    finally:
        verify.close()
