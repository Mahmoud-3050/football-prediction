"""SQLite trace of a Prediction Run, its Snapshot, and any Prediction Version."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.models import AnalysisSnapshot, ProducedPrediction

BOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS prediction_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    user_id INTEGER,
    raw_home TEXT NOT NULL,
    raw_away TEXT NOT NULL,
    raw_competition TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'partial', 'abstained', 'failed')),
    stage TEXT,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS prediction_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES prediction_run(id),
    captured_at TEXT NOT NULL,
    competition_code TEXT,
    match_id INTEGER,
    home_team_id INTEGER,
    away_team_id INTEGER,
    kickoff TEXT,
    payload TEXT NOT NULL,
    freshness TEXT NOT NULL,
    validation_checks TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prediction_version (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES prediction_run(id),
    snapshot_id INTEGER NOT NULL REFERENCES prediction_snapshot(id),
    version_number INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    output_json TEXT NOT NULL,
    limitations TEXT NOT NULL,
    UNIQUE (run_id, version_number)
);

CREATE TABLE IF NOT EXISTS prospective_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id TEXT,
    match_key TEXT NOT NULL,
    competition TEXT NOT NULL,
    season INTEGER,
    home_team_id TEXT NOT NULL,
    away_team_id TEXT NOT NULL,
    home_team_name TEXT NOT NULL,
    away_team_name TEXT NOT NULL,
    kickoff_utc TEXT NOT NULL,
    predicted_at_utc TEXT NOT NULL,
    source TEXT NOT NULL,
    model_id TEXT NOT NULL,
    stack_json TEXT NOT NULL,
    prob_home REAL NOT NULL,
    prob_draw REAL NOT NULL,
    prob_away REAL NOT NULL,
    prob_over_25 REAL,
    prob_under_25 REAL,
    most_likely_score TEXT NOT NULL,
    most_likely_score_prob REAL,
    display_pick TEXT,
    lambda_home REAL,
    lambda_away REAL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    actual_home_goals INTEGER,
    actual_away_goals INTEGER,
    actual_outcome TEXT,
    ft_1x2_log_loss REAL,
    ft_1x2_brier REAL,
    exact_score_log_loss REAL,
    exact_score_hit INTEGER,
    settled_at_utc TEXT,
    UNIQUE(competition, home_team_id, away_team_id, kickoff_utc)
);

CREATE TABLE IF NOT EXISTS match_xg (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competition TEXT NOT NULL,
    season INTEGER NOT NULL,
    kickoff_utc TEXT NOT NULL,
    home_team_id TEXT NOT NULL REFERENCES canonical_teams(team_id),
    away_team_id TEXT NOT NULL REFERENCES canonical_teams(team_id),
    home_xg REAL NOT NULL,
    away_xg REAL NOT NULL,
    source TEXT NOT NULL,
    imported_at_utc TEXT NOT NULL,
    UNIQUE(competition, season, home_team_id, away_team_id, source)
);
"""

# Back-compat alias for older imports/tests.
SCHEMA = BOT_SCHEMA


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=60.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 60000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA wal_autocheckpoint = 1000")
        # Bot + laboratory tables live in the same file.
        from db.database import ensure_shared_schemas

        ensure_shared_schemas(self._conn)

    def close(self) -> None:
        self._conn.close()

    def create_run(
        self,
        *,
        chat_id: int | None,
        user_id: int | None,
        raw_home: str,
        raw_away: str,
        raw_competition: str,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO prediction_run (
                chat_id, user_id, raw_home, raw_away, raw_competition,
                created_at, status, stage, reason
            ) VALUES (?, ?, ?, ?, ?, ?, 'failed', 'input', 'in progress')
            """,
            (chat_id, user_id, raw_home, raw_away, raw_competition, utc_now()),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def update_run(
        self,
        run_id: int,
        *,
        status: str,
        stage: str | None,
        reason: str | None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE prediction_run
            SET status = ?, stage = ?, reason = ?
            WHERE id = ?
            """,
            (status, stage, reason, run_id),
        )
        self._conn.commit()

    def save_snapshot(self, run_id: int, snapshot: AnalysisSnapshot) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO prediction_snapshot (
                run_id, captured_at, competition_code, match_id,
                home_team_id, away_team_id, kickoff, payload,
                freshness, validation_checks
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                snapshot.fetched_at,
                snapshot.competition_code,
                snapshot.match_id,
                snapshot.home_team_id,
                snapshot.away_team_id,
                snapshot.kickoff,
                json.dumps(snapshot.payload(), ensure_ascii=False),
                json.dumps(snapshot.freshness, ensure_ascii=False),
                json.dumps([check.public() for check in snapshot.checks], ensure_ascii=False),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def save_version(self, run_id: int, snapshot_id: int, produced: ProducedPrediction) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO prediction_version (
                run_id, snapshot_id, version_number, model_id,
                prompt_version, schema_version, output_json, limitations
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                snapshot_id,
                produced.version_number,
                produced.model_id,
                produced.prompt_version,
                produced.schema_version,
                json.dumps(produced.output(), ensure_ascii=False),
                json.dumps(produced.limitations, ensure_ascii=False),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM prediction_run WHERE id = ?",
            (run_id,),
        ).fetchone()

    def snapshots_for(self, run_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM prediction_snapshot WHERE run_id = ? ORDER BY id",
                (run_id,),
            )
        )

    def versions_for(self, run_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM prediction_version WHERE run_id = ? ORDER BY version_number",
                (run_id,),
            )
        )

    def has_prediction_for_match(self, match_id: int) -> bool:
        """True when a completed or partial version already exists for this fixture."""

        row = self._conn.execute(
            """
            SELECT 1
            FROM prediction_version v
            JOIN prediction_snapshot s ON s.id = v.snapshot_id
            JOIN prediction_run r ON r.id = v.run_id
            WHERE s.match_id = ?
              AND r.status IN ('completed', 'partial')
            LIMIT 1
            """,
            (match_id,),
        ).fetchone()
        return row is not None


def loads(value: str) -> Any:
    return json.loads(value)
