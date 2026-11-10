"""Server-side background jobs (independent of the Telegram bot)."""

from __future__ import annotations

# Leagues priced/refreshed by server jobs (includes cups that expose fixtures).
JOB_COMPETITIONS: tuple[str, ...] = (
    "PL",
    "CL",
    "PD",
    "SA",
    "BL1",
    "FL1",
    "PPL",
    "DED",
)
