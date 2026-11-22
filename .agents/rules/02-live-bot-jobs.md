---
description: Rules for Telegram bot handlers, live Pipeline, Gemini prose predictor, Cairo background jobs, and Football-Data/Ninjas/LFA providers.
trigger: glob
globs: "**/main.py, **/bot.py, **/pipeline.py, **/predictor.py, **/models.py, **/data_fetcher.py, **/api_ninjas.py, **/live_football_api.py, **/timeutil.py, **/pre_round.py, **/jobs/**/*.py"
---

# Live Bot, Pipeline, Jobs & Providers Rules

## 1. Execution & Isolation Boundaries
- **`models.py`:** Pure dataclasses/errors (`Fixture`, `PipelineResult`, `Markets`, `StageError`, `ProviderError`, `ModelError`). **FORBIDDEN:** Telegram, HTTP, Gemini, or SQLite imports.
- **`predictor.py`:** Gemini returns **tactical prose reasoning JSON only**. **FORBIDDEN:** Gemini computing, choosing, or overriding λ, scorelines, or markets.
- **Async Boundary:** Bot, `pipeline.py`, and `jobs/` are `async`. Never block the event loop with synchronous network calls or long DB locks.
- **`pre_round.py`:** Legacy (active only if `PRE_ROUND_ENABLED=1`). All new scheduled behavior belongs in `python -m jobs` (`jobs/__main__.py`, `jobs/tomorrow.py`, `jobs/standings_job.py`).

## 2. Fixture & Time Rules
- **Predictability:** **FORBIDDEN** predicting `FINISHED`, `IN_PLAY`, or postponed fixtures as upcoming (`data_fetcher.fixture_is_predictable`).
- **Home/Away Integrity:** **FORBIDDEN** swapping home and away teams to force a fixture match.
- **Missing Data:** Missing HT samples (`<3`) → withhold HT market (`"half_time": {"withheld": true, ...}`), never invent `0-0`. Missing `lastUpdated` is NOT fresh; always preserve the free-tier delay warning.
- **Cairo Time:** `timeutil` governs fixture lists and jobs "tomorrow" (`PROSPECTIVE_LEAD_DAYS=1`) using `Africa/Cairo`.

## 3. Provider Routing & Secrets
- **SPL Providers (CRITICAL):** SPL Live Football API `league_id` is **8454** (`live_football_api.LFA_LEAGUE_IDS["SPL"]`). **8485 is WRONG** (Serbia U19).
- SPL **history** → Live Football API; SPL **live** fixtures/standings → API Ninjas when `NINJAS_API_KEY` is set. **FORBIDDEN:** assuming Football-Data has old SPL seasons.
- `config.load_config` uses `load_dotenv(override=True)` (`.env` overrides shell exports). Required keys: `BOT_TOKEN`, `FOOTBALL_API_KEY`, `GEMINI_API_KEY`.

## 4. Verification
Run relevant bot/pipeline/provider unit tests offline (using local fixtures, zero live HTTP/Gemini calls).