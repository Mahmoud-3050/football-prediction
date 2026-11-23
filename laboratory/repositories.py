"""Repository layer for SQLite data access with context-managed transactions and bulk I/O.

Isolates all SQL queries behind clean, parameterized interfaces returning typed
domain records, eliminating N+1 queries and SQL leakage in modeling/evaluation code.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from laboratory.snapshot import MatchRecord
from laboratory.store import connect


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


@contextmanager
def connect_managed(
    path: Path,
    *,
    read_only: bool = False,
    attempts: int = 8,
    base_sleep: float = 0.05,
) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with retry on transient WAL lock and auto-close."""
    last: Exception | None = None
    conn: sqlite3.Connection | None = None
    for attempt in range(attempts):
        try:
            if read_only:
                conn = sqlite3.connect(
                    f"file:{Path(path).resolve()}?mode=ro",
                    uri=True,
                    check_same_thread=False,
                    timeout=60.0,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout = 60000")
            else:
                conn = connect(Path(path))
            break
        except sqlite3.OperationalError as exc:
            last = exc
            if not _is_lock_error(exc) or attempt == attempts - 1:
                raise
            time.sleep(base_sleep * (2**attempt))
    if conn is None:
        assert last is not None
        raise last
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(
    connection: sqlite3.Connection,
    *,
    attempts: int = 8,
    base_sleep: float = 0.05,
) -> Iterator[sqlite3.Connection]:
    """Commit with WAL lock retry on normal exit, rollback on exception."""
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                connection.commit()
                return
            except sqlite3.OperationalError as exc:
                last = exc
                if not _is_lock_error(exc) or attempt == attempts - 1:
                    raise
                time.sleep(base_sleep * (2**attempt))
        if last is not None:
            raise last


class MatchRepository:
    """Bulk read-only access to matches and latest result revisions."""

    @staticmethod
    def row_to_record(row: sqlite3.Row, *, default_competition: str = "PL") -> MatchRecord:
        return MatchRecord(
            match_id=str(row["match_id"]),
            season=str(row["season"]),
            kickoff=str(row["kickoff"]),
            home_id=str(row["home_id"]),
            home_name=str(row["home_name"]),
            away_id=str(row["away_id"]),
            away_name=str(row["away_name"]),
            ft_home=row["ft_home"],
            ft_away=row["ft_away"],
            ht_home=row["ht_home"],
            ht_away=row["ht_away"],
            competition=str(row["competition"] or default_competition),
        )

    @classmethod
    def fetch_matches(
        cls,
        connection: sqlite3.Connection,
        *,
        competition: str | None = None,
        seasons: Sequence[str] | None = None,
        finished_only: bool = False,
    ) -> list[MatchRecord]:
        """Bulk-fetch matches with their latest result revision in a single SQL query."""
        clauses: list[str] = []
        params: list[Any] = []
        if competition is not None:
            clauses.append("m.competition = ?")
            params.append(competition.upper())
        if seasons is not None:
            season_list = [str(s) for s in seasons]
            if not season_list:
                return []
            marks = ",".join("?" for _ in season_list)
            clauses.append(f"m.season IN ({marks})")
            params.extend(season_list)
        if finished_only:
            clauses.append("r.ft_home IS NOT NULL AND r.ft_away IS NOT NULL")

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT m.match_id, m.competition, m.season, m.kickoff,
                   m.home_id, m.home_name, m.away_id, m.away_name,
                   r.ft_home, r.ft_away, r.ht_home, r.ht_away, r.revision
            FROM matches m
            LEFT JOIN result_revisions r
              ON r.match_id = m.match_id
             AND r.revision = (
                SELECT MAX(r2.revision)
                FROM result_revisions r2
                WHERE r2.match_id = m.match_id
             )
            {where_sql}
            ORDER BY m.kickoff, m.match_id
        """
        rows = connection.execute(sql, params).fetchall()
        default_comp = competition.upper() if competition else "PL"
        return [cls.row_to_record(row, default_competition=default_comp) for row in rows]

    @classmethod
    def fetch_finished_before(
        cls,
        connection: sqlite3.Connection,
        *,
        before_kickoff: str | None = None,
    ) -> list[MatchRecord]:
        """Bulk-fetch all finished matches across competitions (optionally before kickoff)."""
        params: list[Any] = []
        extra = ""
        if before_kickoff is not None:
            extra = "AND m.kickoff < ?"
            params.append(before_kickoff)
        sql = f"""
            SELECT m.match_id, m.competition, m.season, m.kickoff,
                   m.home_id, m.home_name, m.away_id, m.away_name,
                   r.ft_home, r.ft_away, r.ht_home, r.ht_away, r.revision
            FROM matches m
            JOIN result_revisions r
              ON r.match_id = m.match_id
             AND r.revision = (
                SELECT MAX(r2.revision)
                FROM result_revisions r2
                WHERE r2.match_id = m.match_id
             )
            WHERE r.ft_home IS NOT NULL AND r.ft_away IS NOT NULL
            {extra}
            ORDER BY m.kickoff, m.match_id
        """
        rows = connection.execute(sql, params).fetchall()
        return [cls.row_to_record(row) for row in rows]


class ExperimentRepository:
    """Bulk read/write operations for snapshots, predictions, evaluations, and runs."""

    @staticmethod
    def bulk_save_snapshots(
        connection: sqlite3.Connection,
        items: Sequence[tuple[str, dict[str, Any]]],
    ) -> None:
        if not items:
            return
        from laboratory.store import _slim_snapshot_json

        rows = [
            (
                f"{exp_id}:{payload['payload_hash']}",
                exp_id,
                payload["match_id"],
                _slim_snapshot_json(payload),
                1 if payload["leakage_ok"] else 0,
            )
            for exp_id, payload in items
        ]
        connection.executemany(
            """
            INSERT INTO snapshots (snapshot_id, experiment_id, match_id, payload_json, leakage_ok)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_id) DO NOTHING
            """,
            rows,
        )

    @staticmethod
    def bulk_insert_predictions(
        connection: sqlite3.Connection,
        items: Sequence[dict[str, Any]],
    ) -> list[str]:
        """Insert predictions in bulk; returns list of prediction_ids."""
        if not items:
            return []
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        ids: list[str] = []
        for item in items:
            exp_id = item["experiment_id"]
            snap_id = item["snapshot_id"]
            engine = item["engine"]
            model_id = item["model_id"]
            pred_id = f"{exp_id}:{snap_id}:{engine}:{model_id}"
            ids.append(pred_id)
            rows.append(
                (
                    pred_id,
                    exp_id,
                    item["match_id"],
                    snap_id,
                    engine,
                    model_id,
                    json.dumps(item["body"], sort_keys=True),
                    now,
                )
            )
        connection.executemany(
            """
            INSERT INTO predictions (
                prediction_id, experiment_id, match_id, snapshot_id, engine,
                model_id, body_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(prediction_id) DO NOTHING
            """,
            rows,
        )
        return ids

    @staticmethod
    def bulk_insert_evaluations(
        connection: sqlite3.Connection,
        items: Sequence[tuple[str, int, dict[str, Any]]],
    ) -> None:
        if not items:
            return
        rows = [
            (f"{pred_id}:r{rev}", pred_id, rev, json.dumps(metrics, sort_keys=True))
            for pred_id, rev, metrics in items
        ]
        connection.executemany(
            """
            INSERT INTO evaluations (evaluation_id, prediction_id, revision, metrics_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(evaluation_id) DO NOTHING
            """,
            rows,
        )

    @staticmethod
    def bulk_record_runs(
        connection: sqlite3.Connection,
        items: Sequence[tuple[str, str, str, str, str, str]],
    ) -> None:
        """Bulk upsert engine_runs rows: (experiment_id, match_id, engine, status, detail)."""
        if not items:
            return
        rows = [
            (f"{exp_id}:{match_id}:{engine}", exp_id, match_id, engine, status, detail)
            for _run_id, exp_id, match_id, engine, status, detail in items
        ]
        connection.executemany(
            """
            INSERT INTO engine_runs (run_id, experiment_id, match_id, engine, status, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET status = excluded.status, detail = excluded.detail
            """,
            rows,
        )

    @staticmethod
    def fetch_evaluation_metrics_json(
        connection: sqlite3.Connection,
        *,
        experiment_id: str,
        model_id: str,
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT e.metrics_json
            FROM evaluations e
            JOIN predictions p ON p.prediction_id = e.prediction_id
            WHERE p.experiment_id = ? AND p.model_id = ?
            """,
            (experiment_id, model_id),
        ).fetchall()
        return [str(row["metrics_json"]) for row in rows]

    @staticmethod
    def fetch_completed_model_ids(
        connection: sqlite3.Connection,
        *,
        experiment_id: str,
    ) -> set[str]:
        rows = connection.execute(
            "SELECT model_id FROM lab_experiment_runs WHERE experiment_id = ? AND status = 'completed'",
            (experiment_id,),
        ).fetchall()
        return {str(row["model_id"]) for row in rows}

    @staticmethod
    def fetch_completed_runs(
        connection: sqlite3.Connection,
        *,
        experiment_id: str,
    ) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                """
                SELECT run_id, model_id, config_json, status, metrics_json,
                       matches_priced, matches_evaluated, error
                FROM lab_experiment_runs
                WHERE experiment_id = ? AND status = 'completed'
                ORDER BY matches_evaluated DESC
                """,
                (experiment_id,),
            ).fetchall()
        )
