"""Server-side background jobs (independent of the Telegram bot)."""

from __future__ import annotations

from providers.data_fetcher import LIVE_MATCH_COMPETITIONS

# Same leagues as /today and /predict so jobs never price a gated-out fixture.
JOB_COMPETITIONS: tuple[str, ...] = LIVE_MATCH_COMPETITIONS
