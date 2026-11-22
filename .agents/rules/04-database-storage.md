---
description: Rules for shared SQLite concurrency (WAL mode, timeouts, checkpoints), schema migrations, and atomic file persistence.
trigger: glob
globs: "**/database.py, **/storage.py, **/laboratory/store.py, **/db_paths.py, **/migrate_db.py"
---

# Shared SQLite Storage & Concurrency Rules

## 1. Shared Connection Contract (CRITICAL)
One shared SQLite file (`data/football.db`, WAL) is accessed concurrently by bot, jobs, dashboard, and lab:
- **REQUIRED:** Always open via `database.ensure_shared_schemas` / `laboratory.store.connect` / `storage.Storage`.
- **REQUIRED Pragmas:** `check_same_thread=False`, `timeout=60.0`, `PRAGMA foreign_keys=ON`, `busy_timeout=60000`, `journal_mode=WAL`, `synchronous=NORMAL`, `wal_autocheckpoint=1000`. **FORBIDDEN** weakening these or disabling WAL.

## 2. Write Contention & Dashboard Lock Prevention
- **Batch Commits:** Never hold multi-hour write transactions.
- **WAL Checkpointing:** After bulk `run_score_model` or grid runs, call `checkpoint_wal(..., mode="TRUNCATE")` (fallback `PASSIVE`).
- **Dashboard Lock Note:** `laboratory/dashboard.py` uses `ThreadingHTTPServer` with a long-lived connection. If concurrent writes trigger `database is locked`, restart the dashboard (`make dash-restart`).

## 3. Schema & Revision Integrity
- **Dual Schema Sync:** Any schema change must update BOTH `storage.BOT_SCHEMA` and `laboratory/store.SCHEMA` as applicable, plus any `ALTER TABLE` migration path, and initialize via `ensure_shared_schemas`.
- **Result Revisions:** Match results are revisioned (`result_revisions` PK `(match_id, revision)`); never overwrite revisions blindly.
- **DDL Check:** Before writing SQL against `raw_payloads`, `teams`, `competitions`, `standings_*`, or `job_runs`, read their exact DDL in `laboratory/store.py`.