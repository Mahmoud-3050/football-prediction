"""Canonical SQLite path shared by the Telegram bot and the laboratory."""

from __future__ import annotations

import os
from pathlib import Path

# One file holds bot runs and laboratory league/history/experiment tables.
DEFAULT_DATABASE_PATH = "data/football.db"


def resolve_database_path(
    environ: dict[str, str] | None = None,
    *,
    prefer_lab_override: bool = False,
) -> Path:
    """Return the shared database path.

    Resolution order:
    1. ``LAB_DATABASE_PATH`` when ``prefer_lab_override`` is true (laboratory CLI)
    2. ``DATABASE_PATH``
    3. ``LAB_DATABASE_PATH`` (legacy lab-only override)
    4. ``data/football.db``
    """

    source = environ if environ is not None else os.environ
    if prefer_lab_override:
        lab = (source.get("LAB_DATABASE_PATH") or "").strip()
        if lab:
            return Path(lab)
    shared = (source.get("DATABASE_PATH") or "").strip()
    if shared:
        return Path(shared)
    lab = (source.get("LAB_DATABASE_PATH") or "").strip()
    if lab:
        return Path(lab)
    return Path(DEFAULT_DATABASE_PATH)
