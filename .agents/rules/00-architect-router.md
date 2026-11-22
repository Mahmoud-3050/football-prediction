---
description: Core Architect Router & Anti-Hallucination Guardrails. Always active to route tasks to the right domain rule file and enforce source-of-truth discipline.
trigger: always_on
---

# Football Predictor — Core Architect Router

**Source of Truth:** `docs/PROJECT_BLUEPRINT_FOR_SKILL.md` and actual source files. If docs and source disagree, **source code wins**; report the discrepancy and do not "fix" it silently.

## 1. Task Routing Map (Load & Follow Matching Rule)
Before editing or answering, identify which subsystem(s) the task touches and consult the corresponding rule in `.agents/rules/`:
- **Math, Poisson/DC, Draw Controls, League Stacks, Eligibility:** `.agents/rules/01-score-math-stacks.md` (`score_model*.py`, `league_stacks.py`, `eligibility.py`, `configs/live/*`)
- **Telegram Bot, Live Pipeline, Providers, Cairo Jobs:** `.agents/rules/02-live-bot-jobs.md` (`bot.py`, `pipeline.py`, `predictor.py`, `jobs/*`, `data_fetcher.py`, `api_ninjas.py`, `live_football_api.py`)
- **Backtesting Lab, Snapshots, Experiments, Promotion:** `.agents/rules/03-laboratory-backtest.md` (`laboratory/*` excluding dashboard UI, `experiments/*`)
- **Shared SQLite, Schemas, WAL Concurrency:** `.agents/rules/04-database-storage.md` (`database.py`, `storage.py`, `laboratory/store.py`, `db_paths.py`, `migrate_db.py`)
- **Web Dashboard UI/UX, Routes, Auth & Visuals:** `.agents/rules/05-dashboard-ui-ux.md` (`laboratory/dashboard.py`, `laboratory/auth.py`, `laboratory/team_display.py`, `tests/test_dashboard_*.py`, `tests/test_match_predict_button.py`, `tests/test_product_ops.py`)

## 2. Universal Anti-Hallucination & Safety Invariants
1. **Read Before Edit:** Read the target file(s) in full (or relevant section) in the current session before editing. Never edit from memory alone.
2. **No Invented Identifiers:** Never invent config keys, stack keys, SQL columns, CLI flags, env vars, or module paths. Grep/read source (`__main__.py`, `league_stacks.py`, SQL DDL) first.
3. **Verify Unimplemented Plans:** Do NOT assume separate `lab.db`, `?mode=ro` URIs, `[lab]` extras, or enforced `code_version` gates exist unless verified in source; if absent, inform the user rather than coding against them.
4. **Domain Identity:** Estimates **one named football match at a time** and **abstains** when data is insufficient (`<3` scored matches). Not a fixture browser or tipster; bot replies must state results are **statistical estimates, not betting tips**.
5. **Tech Conventions:** Python 3.9+ (`from __future__ import annotations`), frozen dataclasses for configs/outcomes, `StageError`/`ProviderError`/`ModelError` with `stage`, stdlib `logging`. Never commit `.env` or call live external APIs in tests.

## 3. Workflow & Finishing Protocol
- **Pre-Edit:** Classify blast radius (`live bot` / `lab backtest` / `dashboard` / `SPL stack only` / `schema`), read target + boundary rules, and check `docs/` (`PROJECT_BLUEPRINT_FOR_SKILL.md`, `DASHBOARD_BLUEPRINT_FOR_SKILL.md`, `architecture.md`, `prediction/*.md`, `backtesting/BT-*.md`).
- **Post-Edit:** Run smallest relevant offline `pytest` suite first, then `.venv/bin/python -m pytest -q` (or `make test`).
- **Report:** (1) State which boundary was touched; (2) List exact `pytest` commands run and results; (3) Flag anything unverified; (4) Remind the user to update `docs/PROJECT_BLUEPRINT_FOR_SKILL.md` and `.agents/rules/` if schemas, CLI flags, stack contracts, or isolation boundaries changed.