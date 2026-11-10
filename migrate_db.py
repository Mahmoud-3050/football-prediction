"""Merge legacy predictions.db + laboratory.db into the shared football.db."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from db_paths import DEFAULT_DATABASE_PATH
from database import ensure_shared_schemas

LEGACY_BOT = Path("data/predictions.db")
LEGACY_LAB = Path("data/laboratory.db")

BOT_TABLES = ("prediction_run", "prediction_snapshot", "prediction_version")
LAB_TABLES = (
    "experiments",
    "matches",
    "result_revisions",
    "snapshots",
    "predictions",
    "evaluations",
    "engine_runs",
    "raw_payloads",
)


def merge_into_shared(
    *,
    destination: Path | None = None,
    bot_path: Path | None = None,
    lab_path: Path | None = None,
) -> dict[str, Any]:
    """Create or update the shared database from legacy files.

    Prefer renaming/copying the large laboratory file when the destination is
    empty, then copy the small bot tables in. Existing destination rows win
    on primary-key conflict (INSERT OR IGNORE).
    """

    dest = Path(destination or DEFAULT_DATABASE_PATH)
    bot = Path(bot_path) if bot_path is not None else LEGACY_BOT
    lab = Path(lab_path) if lab_path is not None else LEGACY_LAB
    summary: dict[str, Any] = {
        "destination": str(dest),
        "bot_source": str(bot) if bot.exists() else None,
        "lab_source": str(lab) if lab.exists() else None,
        "seeded_from_lab": False,
        "copied": {},
    }

    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        if lab.exists() and lab.resolve() != dest.resolve():
            # Prefer rename so a multi-GB laboratory file is not duplicated.
            try:
                lab.rename(dest)
                summary["seeded_from_lab"] = True
                summary["moved_lab"] = True
            except OSError:
                shutil.copy2(lab, dest)
                summary["seeded_from_lab"] = True
                summary["moved_lab"] = False
        else:
            dest.touch()

    connection = sqlite3.connect(dest)
    connection.row_factory = sqlite3.Row
    ensure_shared_schemas(connection)

    if bot.exists() and bot.resolve() != dest.resolve():
        summary["copied"]["bot"] = _copy_tables(connection, bot, BOT_TABLES, alias="bot")
        _sync_sequences(connection, BOT_TABLES)

    if (
        lab.exists()
        and lab.resolve() != dest.resolve()
        and not summary["seeded_from_lab"]
    ):
        summary["copied"]["lab"] = _copy_tables(connection, lab, LAB_TABLES, alias="lab")

    connection.commit()
    connection.close()
    return summary


def _copy_tables(
    connection: sqlite3.Connection,
    source: Path,
    tables: tuple[str, ...],
    *,
    alias: str,
) -> dict[str, int]:
    connection.execute(f"ATTACH DATABASE ? AS {alias}", (str(source),))
    counts: dict[str, int] = {}
    try:
        for table in tables:
            present = connection.execute(
                f"SELECT 1 FROM {alias}.sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if present is None:
                counts[table] = 0
                continue
            before = connection.total_changes
            connection.execute(
                f"INSERT OR IGNORE INTO main.{table} SELECT * FROM {alias}.{table}"
            )
            counts[table] = connection.total_changes - before
        # DETACH fails while a write transaction is open.
        connection.commit()
    finally:
        connection.execute(f"DETACH DATABASE {alias}")
    return counts


def _sync_sequences(connection: sqlite3.Connection, tables: tuple[str, ...]) -> None:
    for table in tables:
        row = connection.execute(f"SELECT MAX(id) AS max_id FROM {table}").fetchone()
        max_id = row["max_id"] if row is not None else None
        if max_id is None:
            continue
        max_id = int(max_id)
        existing = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = ?",
            (table,),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                (table, max_id),
            )
        elif int(existing["seq"]) < max_id:
            connection.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                (max_id, table),
            )


def main() -> None:
    summary = merge_into_shared()
    print(summary)
    print(f"Shared database ready at {summary['destination']}")


if __name__ == "__main__":
    main()
