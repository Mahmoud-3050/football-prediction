"""Ensure bot and laboratory tables coexist in one SQLite file."""

from __future__ import annotations

import sqlite3


def ensure_shared_schemas(connection: sqlite3.Connection) -> None:
    """Create every table used by the bot and the laboratory.

    Table names do not overlap. Either process may open the file first.
    """

    from laboratory.store import ensure_laboratory_schema
    from db.storage import BOT_SCHEMA

    connection.executescript(BOT_SCHEMA)
    ensure_laboratory_schema(connection)
    connection.commit()
