---
name: football-predictor-architect
description: Mandatory context for ANY edit, debug, review, or extension of the Football Predictor repo (Telegram bot + shared SQLite laboratory). Activate when touching pipeline.py, score_model.py, eligibility.py, predictor.py, data_fetcher.py, league_stacks.py, storage.py, database.py, laboratory/*, jobs/*, bot.py, main.py, configs/live/*, data/spl_stack.json, experiments/*.yaml, DB schemas or migrations, SPL/Live Football API/API Ninjas provider code, backtesting, walk-forward pricing, draw-boost/pick-rule tuning, stack promotion/rollback, dashboard, or any pytest/CLI run for this repo. Also activate when the user mentions /predict, /today, /matches, price-finished, tune-spl, lab-run, leakage, or abstain.
---

# Football Predictor — Architect Skill

Source of truth: `docs/PROJECT_BLUEPRINT_FOR_SKILL.md` (verified from source 2026-11-20). If source code and this skill disagree, **the source code wins**; report the discrepancy to the user and do not "fix" it silently.

## 1. Core Domain & Mental Model

**What it is:** a Telegram bot + shared SQLite laboratory that estimates **one named football match at a time** and **abstains** when data quality is insufficient. It is NOT a fixture browser and NOT a tipster. Replies must state results are **statistical estimates, not betting tips**.

**Math:** shrunk multiplicative Poisson rates → optional Dixon–Coles → optional draw calibration → normalized score grid → markets.
- Shrinkage: `r_shrunk = (n·r + k·prior) / (n + k)`, default `k=6` (`score_model.K`).
- `λ_home = league_home_avg × attack_home × defence_away` (symmetric for away). Home advantage comes from league home vs away averages, not an extra constant.
- Grid: Poisson (or DC-adjusted) cells until raw tail ≤ `TAIL_TOLERANCE` (0.001), renormalize, cap `MAX_GRID_GOALS=25`.
- Dixon–Coles: τ on 0-0/1-0/0-1/1-1; ρ fit on earlier matches (`MIN_RHO_TRAINING_MATCHES=30`).
- Markets from the grid: FT 1X2, exact-score mode, O/U 2.5 and 3.5, optional HT (needs `MINIMUM_HALF_TIME_SAMPLES=3`).
- Gemini produces **prose reasoning only**. It never chooses λ, scorelines, or markets.

**Two execution paths**

| Path | Entry | Writes | Gemini | Notes |
| --- | --- | --- | --- | --- |
| Live: `/predict`, `p:<match_id>` button | `bot.py` → `pipeline.Pipeline.execute` → `score_model.estimate_score` → `predictor.Predictor` | bot tables `prediction_run/snapshot/version` | Yes (prose) | Upcoming fixtures only |
| Live: server jobs | `python -m jobs` (`jobs/__main__.py`: `jobs_loop`, `main`) | bot tables via `Pipeline`; standings via lab standings tables; `record_job_run` | Yes | Cairo-tomorrow pricing + standings refresh, interval `JOBS_INTERVAL_SECONDS` (default 3600) |
| Lab: walk-forward | `python -m laboratory price-finished / backtest-bot / tune-spl` | lab `predictions` + `evaluations` | **No** | Same `score_model` as the bot. Experiment ids `BOT-PL-001`, `BOT-{CODE}-live`, `BOT-SPL-chal-*` |
| Lab: Poisson baseline | `python -m laboratory import-pl` + `run` | lab `predictions` | No | Lab-only engine `poisson`, `EXP-PL-001` |
| Lab: experiment grid | `python -m laboratory lab-run` | `lab_experiments`, `lab_experiment_runs`, `predictions` | No | YAML-driven; promotion via `lab-promote` / `lab-rollback` |

**Key concepts**
- **Leakage:** a snapshot must not contain the target FT score, later matches, or engine fields (`lambda_*`, `poisson`, `gemini`). Enforced by `laboratory/snapshot.leakage_status`.
- **Walk-forward:** matches priced in kickoff order; priors are only strictly-earlier **same-competition AND same-season** finished games (`build_snapshot`).
- **Insert-only predictions:** UNIQUE `(experiment_id, snapshot_id, engine, model_id)`.
- **Abstain:** fewer than 3 finished scored matches per club (`eligibility.MINIMUM_SCORED_MATCHES=3`, `FORM_WINDOW=5`), missing standings, fixture not predictable, etc.
- **Live stack:** per-competition `score_model` knobs via `league_stacks.live_stack_for(code)` → `configs/live/{code}_stack.json`, SPL legacy fallback `data/spl_stack.json`, else `DEFAULT_LIVE_STACK`.
- **Draw controls:** `draw_boost` / `draw_weights` / `draw_tightness_*` act on diagonal matrix mass. `draw_pick_threshold` / `draw_pick_margin` (`select_1x2_outcome`) change **`most_likely` only**, not the probability vector.
- **Metrics (`laboratory/evaluate.py`):** FT/HT 1X2 log-loss & Brier; exact-score log-loss & hit; O/U 2.5/3.5 binary log-loss & Brier. `LOG_LOSS_FLOOR=1e-6`.
- **Cairo time:** `timeutil`; fixture lists and the jobs "tomorrow" use Africa/Cairo.
- **Competition codes:** `PL, CL, PD, SA, BL1, FL1, PPL, DED, SPL` (`data_fetcher.LIVE_MATCH_COMPETITIONS` / `TODAY_MATCH_COMPETITIONS`).

## 2. Strict Invariants & "NEVER DO THIS" Guardrails (HIGHEST PRIORITY)

### 2.1 Isolation Boundaries

- **CRITICAL** `models.py` is pure dataclasses/errors. **FORBIDDEN:** Telegram, HTTP, Gemini, or SQLite imports/calls in it.
- **CRITICAL** `score_model.py` is pure math on payloads. **FORBIDDEN:** Gemini, Telegram, network, or DB access in it.
- **CRITICAL** `eligibility.py` is validation only. **FORBIDDEN:** filling gaps with zeros; gaps stay empty.
- **FORBIDDEN** `laboratory/*` starting the Telegram bot. The lab CLI must never start polling.
- **FORBIDDEN** lab pricing (`score_model_adapter`, `poisson_engine`, `job.run_score_model`, `job.run_poisson`) calling Gemini.
- **FORBIDDEN** `predictor.py` computing or overriding scores/markets. It only returns tactical reasoning text as JSON.
- **FORBIDDEN** merging bot `prediction_*` rows into lab `predictions` (or the reverse). Table names do not overlap (`database.ensure_shared_schemas`); lifecycles differ.
- **REQUIRED** open the shared DB through `database.ensure_shared_schemas` / `laboratory.store.connect` / `storage.Storage`. Do not hand-roll `sqlite3.connect` with different settings.
- **REQUIRED** per-league isolation: peer leagues keep `DEFAULT_LIVE_STACK` unless `configs/live/{code}_stack.json` exists. **FORBIDDEN:** applying SPL draw-pick rules globally; editing the PL stack while changing SPL. Touch only the SPL stack path / `live_stack_for("SPL")`.
- **REQUIRED** `league_stacks.live_stack_for` must keep working when `configs/live/` does not exist (fall back to `DEFAULT_LIVE_STACK` / `data/spl_stack.json`).
- **REQUIRED** stack knobs reach pricing via `league_stacks.draw_calibration_kwargs(stack)` → `estimate_score` / `price_from_snapshot` / `run_score_model`. Do not duplicate or re-derive these kwargs.
- **REQUIRED** `stack["code_version"]` (default `"score_model_v1"`) travels with the stack. If you change `score_model` behavior in a way that makes saved stacks mean something different, you must say so to the user and check how `league_stacks` / `lab_promotion` treat `code_version` in source before assuming any gate exists.
- **REQUIRED** minimize blast radius: decide whether a change touches **live bot**, **lab backtest**, **dashboard**, or **SPL stack only** before editing.
- **REQUIRED** `pre_round.py` is legacy (only with `PRE_ROUND_ENABLED=1`). New scheduled behavior goes in `python -m jobs`.

### 2.2 Data Integrity & Leakage Prevention

1. **FORBIDDEN** target match FT/HT score in a lab snapshot payload. `leakage_status` fails if `ft_home` / `score` appear on the snapshot root or if any prior has `kickoff >= cutoff`.
2. **FORBIDDEN** the target `match_id` inside `prior_matches`.
3. **FORBIDDEN** pricing unfinished matches in batch walk-forward. `run_score_model` / `run_poisson` skip them. Upcoming fixtures belong to `/predict` or the 1-day jobs path (`PROSPECTIVE_LEAD_DAYS=1`).
4. **FORBIDDEN** adding future games or other-league games to snapshot priors ("helpfully"). Priors = same competition AND same season AND strictly earlier kickoff.
5. With `prior_mean=last_season`, last-season ratio lookup is a separate cache path in `job.py`. **FORBIDDEN:** leaking the current match into that fit.
6. **FORBIDDEN** predicting FINISHED / IN_PLAY / postponed fixtures as upcoming (`data_fetcher.fixture_is_predictable`).
7. **FORBIDDEN** swapping home/away to force a fixture match.
8. **FORBIDDEN** inventing scores, standings zeros, or HT values. Missing HT → withhold the HT market (`"half_time": {"withheld": true, ...}`), never 0-0.
9. **REQUIRED** minimum 3 finished scored matches per club (bot eligibility and lab adapter both); HT needs ≥3 valid HT samples per club.
10. **REQUIRED** free-tier Football-Data delay warning is always recorded; a missing `lastUpdated` is NOT "fresh".
11. **REQUIRED** results are revisioned (`result_revisions` PK `(match_id, revision)`); never overwrite blindly.

### 2.3 Prediction & Evaluation Integrity

6. **CRITICAL** lab predictions are **insert-only**. A UNIQUE conflict means "already exists". To re-price: call `delete_experiment_engine_outputs(experiment_id, engine)` first, then re-run.
7. **FORBIDDEN** `evaluate_prediction` rewriting probabilities; it only scores stored bodies against result revisions.
8. **CRITICAL** `draw_pick_threshold` / `draw_pick_margin` change selection (`most_likely`), not the probability vector used for log-loss/Brier. **FORBIDDEN:** "fixing" calibration by boosting matrix mass when the promoted SPL stack uses pick rules at `draw_boost=1.0`.
9. **FORBIDDEN** promoting a uniform high `draw_boost` on all diagonal cells (including 4-4) for SPL without walk-forward comparison; it distorts exact score and O/U.
10. **REQUIRED** exact-score hit = mode cell equals FT score; exact-score log-loss uses the probability of the **actual** cell, not the mode.
11. **REQUIRED** confidence semantics: FT 1X2 confidence = P(selected 1X2); score confidence = P(mode cell). Never swap them.
12. **FORBIDDEN** tuning SPL by maximizing draw count alone. Use walk-forward metrics (log-loss / season mean) with the accuracy floor in `_select_spl_winner`.
13. **FORBIDDEN** claiming metrics without querying `evaluations` or re-running walk-forward. Cite `experiment_id` and n.
14. **FORBIDDEN** running `tune-spl --promote` or `lab-promote` unless the user explicitly asked.

### 2.4 Concurrency & Storage Safety

- **CRITICAL** one shared SQLite file (`data/football.db`, WAL) is used concurrently by the bot, jobs, dashboard, and lab. Connection contract: `check_same_thread=False`, `timeout=60.0`, `PRAGMA foreign_keys=ON`, `busy_timeout=60000`, `journal_mode=WAL`, `synchronous=NORMAL`, `wal_autocheckpoint=1000`. Do not weaken these.
- **FORBIDDEN** disabling WAL without a concurrency plan.
- **FORBIDDEN** holding multi-hour write transactions. Commit in batches.
- **REQUIRED** after bulk `run_score_model`, call `checkpoint_wal(..., mode="TRUNCATE")` (fallback PASSIVE). A long WAL causes `database is locked`.
- **REQUIRED** be careful with the dashboard: it historically uses a long-lived connection with `ThreadingHTTPServer`; concurrent tune/reprice writes can contend. If predictions fail with `database is locked`, restart the dashboard (`make dash-restart`).
- **REQUIRED** atomic file writes for live configs: temp file + `os.replace` (`league_stacks.save_live_stack`, `lab_promotion._write_live_config_atomic`). **FORBIDDEN:** hand-editing a half-written stack JSON while serving; use `lab-rollback` / rollback files.
- **REQUIRED** schema changes: update BOTH `storage.BOT_SCHEMA` and `laboratory/store.SCHEMA` as applicable, plus any migration `ALTER TABLE` path, and open via `ensure_shared_schemas`.

### 2.5 Providers, Config & Secrets

- **CRITICAL** SPL Live Football API `league_id` is **8454**. **8485 is wrong** (Serbia U19). Constant: `live_football_api.LFA_LEAGUE_IDS["SPL"]`.
- **REQUIRED** SPL **history** → Live Football API; SPL **live** fixtures/standings → API Ninjas when `NINJAS_API_KEY` is set. **FORBIDDEN:** assuming Football-Data has old SPL seasons.
- **REQUIRED** `config.load_config` uses `load_dotenv(override=True)`; `.env` beats shell exports. Bot requires `BOT_TOKEN`, `FOOTBALL_API_KEY`, `GEMINI_API_KEY`.
- **FORBIDDEN** committing `.env` or any API key. **REQUIRED:** a non-loopback dashboard bind needs `DASHBOARD_PASSWORD`.
- **REQUIRED** tests never call live Football-Data/Gemini; use local fixtures.

### 2.6 Anti-Hallucination Rules

- **FORBIDDEN** inventing config keys, stack keys, table names, columns, CLI subcommands/flags, env vars, function signatures, or module paths that are not listed in this skill. If a name is not in section 3, **Read/Grep the actual source** before using it.
- **REQUIRED** before editing any file, Read it in full (or the relevant part) in this session. Do not edit from memory of this skill alone.
- **FORBIDDEN** assuming these exist; the blueprint does not document them. Verify in source before asserting or relying on them:
  - a separate `lab.db` file or `?mode=ro` read-only DB URIs (the documented design is one shared `data/football.db`);
  - `[lab]` / core dependency extras (documented `requirements.txt` is a single list);
  - an enforced `code_version` compatibility gate (only the `code_version` key in the stack is documented);
  - a "Live never imports Laboratory" rule (documented rules are narrower: lab must not start the bot or call Gemini; `jobs/standings_job.py` already writes lab standings tables and `database.py` bootstraps both schemas).
- **REQUIRED** when the user's request conflicts with a rule here, state the conflict and ask; do not bypass it.
- **REQUIRED** if the DB can't be queried in your environment, say so rather than guessing metric values.

## 3. Authoritative File & Schema Reference

### 3.1 Entry points & core domain

| Path | Responsibility | Key API |
| --- | --- | --- |
| `main.py` | Telegram long-poll | `build_pipeline(config)`, `main()` |
| `bot.py` | `/start`, `/today`, `/matches DD-MM-YYYY`, `/predict`, callback `p:<match_id>` | `build_application`, `parse_*` |
| `jobs/__main__.py` | Background loop | `jobs_loop`, `main` |
| `jobs/tomorrow.py` | Price Cairo-tomorrow fixtures | `run_tomorrow_once` |
| `jobs/standings_job.py` | Refresh standings after local rounds | `run_standings_once` |
| `pipeline.py` | One-match live analysis | `Pipeline.execute`, `_analyze` → `PipelineResult` |
| `score_model.py` | Poisson + DC + draw controls | `estimate_score`, `estimate_from_lambdas`, `select_1x2_outcome`, `_boost_draw_scorelines`, `ScoreEstimate`; consts `K`, `TAIL_TOLERANCE`, `MAX_GRID_GOALS`, `MINIMUM_HALF_TIME_SAMPLES`, `MIN_RHO_TRAINING_MATCHES` |
| `score_model_enhanced.py` | Experimental ensembles/bivariate/copula (NOT default live) | `estimate_score_ensemble`, `estimate_score_special` |
| `predictor.py` | Gemini reasoning JSON | `Predictor`, prompt builders |
| `eligibility.py` | Gate checks | `evaluate`, `MINIMUM_SCORED_MATCHES=3`, `FORM_WINDOW=5` |
| `data_fetcher.py` | Football-Data (+ Ninjas routing) | `DataFetcher`, `resolve_competition`, `fixture_is_predictable`, `LIVE_MATCH_COMPETITIONS` |
| `api_ninjas.py` | SPL live provider | client helpers |
| `live_football_api.py` | SPL multi-season archive | `LFA_LEAGUE_IDS["SPL"]=8454` |
| `league_stacks.py` | Per-league live knobs | `live_stack_for`, `draw_calibration_kwargs`, `save_live_stack`, `save_spl_stack`, `DEFAULT_LIVE_STACK` |
| `models.py` | Shared shapes | `Fixture`, `PipelineResult`, `Markets`, `StageError`, `ProviderError`, `ModelError` |
| `config.py` | Env → `Config` | `load_config`, `normalize_gemini_model` |
| `db_paths.py` | DB path | `resolve_database_path`, `DEFAULT_DATABASE_PATH` |
| `database.py` | Dual schema bootstrap | `ensure_shared_schemas` |
| `storage.py` | Bot SQLite traces | `Storage`, `BOT_SCHEMA` |
| `timeutil.py` | Cairo formatting | kickoff helpers |
| `pre_round.py` | Legacy pre-round | only if `PRE_ROUND_ENABLED=1` |
| `migrate_db.py` | Merge old DBs into shared file | CLI |
| `evaluate_models.py`, `evaluate_models_v2.py` | Ad-hoc research evaluation | utilities |
| `start.sh`, `Makefile` | Process orchestration | `make install\|bot\|jobs\|dash\|dash-restart\|test`, `./start.sh` |

### 3.2 Laboratory

| Path | Responsibility | Key API |
| --- | --- | --- |
| `laboratory/__init__.py` | Constants + lazy exports | `EXPERIMENT_ID`, `BOT_EXPERIMENT_ID`, `SCORE_MODEL_CONFIG_ID`, `PROSPECTIVE_LEAD_DAYS` |
| `laboratory/__main__.py` | CLI + HTTP dashboard | `main`, `serve`, `_tune_spl` |
| `laboratory/store.py` | Lab schema + CRUD | `connect`, `checkpoint_wal`, `insert_prediction`, `ensure_experiment`, `delete_experiment_engine_outputs`, crest cache |
| `laboratory/snapshot.py` | Pre-kickoff payload | `MatchRecord`, `build_snapshot`, `leakage_status` |
| `laboratory/job.py` | Walk-forward engines | `run_poisson`, `run_score_model`, `price_one_score_model` |
| `laboratory/score_model_adapter.py` | Bot scorer ↔ lab body | `price_from_snapshot`, `to_evaluable_body`, `SCORE_MODEL_ENGINE="score_model"` |
| `laboratory/poisson_engine.py` | Lab-only Poisson | `price_match`, `_distribution` |
| `laboratory/evaluate.py` | Metrics | `evaluate_prediction`, `LOG_LOSS_FLOOR` |
| `laboratory/football_data.py` | Season import | `fetch_season`, `import_bodies`, `COMPETITION_NAMES` |
| `laboratory/standings.py` | Standings import | `import_all_standings`, `STANDINGS_COMPETITIONS` |
| `laboratory/dashboard.py` | Server-rendered HTML (no SPA) | `overview`, `match_list`, `match_detail`; class names `match-card` / `market-strip` are load-bearing for tests |
| `laboratory/auth.py` | Dashboard login | session cookie, CSRF |
| `laboratory/gemini_schema.py` | Validate Gemini JSON | schema checks |
| `laboratory/lab_experiment.py` | Experiment config | `ExperimentConfig`, `make_model_id` (SHA-256), grid expand |
| `laboratory/lab_runner.py` | Parallel grid runs | `run_experiment` |
| `laboratory/lab_analysis.py` | Compare / calibration | `compare_to_baseline`, bootstrap CIs |
| `laboratory/lab_promotion.py` | Promote/rollback live stacks | `promote_best_config`, `PromotionGuardrails`, `_write_live_config_atomic` |
| `laboratory/team_display.py` | Display names | UI helpers |

Docs: `docs/architecture.md`, `docs/prediction/*.md` (math, MVP philosophy, decision register), `docs/backtesting/BT-*.md`. Example config: `experiments/draw_boost_test.yaml`. Fixtures: `tests/fixtures/markets.json`.

### 3.3 Database: `data/football.db` (shared, WAL)

**Bot tables (`storage.BOT_SCHEMA`)**

| Table | Columns / constraints |
| --- | --- |
| `prediction_run` | `id` PK, `chat_id`, `user_id`, `raw_home`, `raw_away`, `raw_competition`, `created_at`, `status` IN (`completed`,`partial`,`abstained`,`failed`), `stage`, `reason` |
| `prediction_snapshot` | `id` PK, `run_id` FK, `captured_at`, `competition_code`, `match_id`, team ids, `kickoff`, `payload`, `freshness`, `validation_checks` |
| `prediction_version` | `id` PK, `run_id` FK, `snapshot_id` FK, `version_number`, `model_id`, `prompt_version`, `schema_version`, `output_json`, `limitations`; UNIQUE `(run_id, version_number)` |

**Lab tables (`laboratory/store.SCHEMA` + migrations)**

| Table | Columns / constraints |
| --- | --- |
| `experiments` | `id` PK, `status`, `config_json`, `created_at` |
| `lab_experiments` | `experiment_id` PK, `competition`, `config_json`, `status`, timestamps, `best_config_json`, `best_metrics_json` |
| `lab_experiment_runs` | `run_id` PK, FK `experiment_id`, `model_id`, `config_json`, `status`, `metrics_json`, priced/evaluated counts, error, timestamps |
| `matches` | `match_id` PK, `dataset_version`, `competition`, `season`, `kickoff`, `home_id`, `home_name`, `away_id`, `away_name`, optional `raw_payload_id` |
| `result_revisions` | PK `(match_id, revision)`, `ft_home`, `ft_away`, `ht_home`, `ht_away`, `source` |
| `snapshots` | `snapshot_id` PK, `experiment_id`, `match_id`, `payload_json`, `leakage_ok` |
| `predictions` | `prediction_id` PK, UNIQUE `(experiment_id, snapshot_id, engine, model_id)`, `body_json`, `created_at` |
| `evaluations` | UNIQUE `(prediction_id, revision)`, `metrics_json` |
| `engine_runs` | per-match engine status/detail |
| `raw_payloads`, `teams`, `competitions`, `standings_meta`, `standings_rows`, `job_runs` | see `laboratory/store.py` for exact columns before touching |

For tables where only a summary is listed, **Read the DDL in source** before writing SQL.

### 3.4 Prediction body (`to_evaluable_body` / poisson distribution)

```json
{
  "engine": "score_model",
  "config_id": "bot-score-model-v1",
  "k": 6.0,
  "dependence": "dixon_coles",
  "lambda_home": 1.4,
  "lambda_away": 1.1,
  "full_time": {
    "result_1x2": {"home": 0.45, "draw": 0.28, "away": 0.27, "most_likely": "home"},
    "exact_score_most_likely": {"home_goals": 1, "away_goals": 1},
    "exact_scores": [{"home_goals": 0, "away_goals": 0, "probability": 0.08}],
    "over_under_2_5": {"over": 0.48, "under": 0.52, "most_likely": "under"},
    "over_under_3_5": {"over": 0.25, "under": 0.75, "most_likely": "under"}
  },
  "half_time": {"withheld": true, "reason": "..."}
}
```

`engine` ∈ {`poisson`, `score_model`}. Metrics JSON (`evaluate_prediction`) keys: `full_time_1x2`, `half_time_1x2`, `exact_score`, `over_under_2_5`, `over_under_3_5`; each has `status`, and when scored: `selection`/`actual`/`correct`/`log_loss`/`brier`. `exact_score` omits `selection` and uses `correct` on mode hit.

### 3.5 Live stack contract (`league_stacks.DEFAULT_LIVE_STACK`)

```json
{
  "k": 6.0,
  "prior_mean": "last_season",
  "dependence": "independent",
  "half_time_share_mode": "fixed_0_45",
  "ratio_mode": "one_pass",
  "model_id": "bot-score-model-live",
  "decay_half_life_days": null,
  "draw_boost": 1.0,
  "draw_weights": null,
  "draw_pick_threshold": null,
  "draw_pick_margin": null,
  "draw_tightness_scale": null,
  "draw_tightness_ref": 1.0,
  "code_version": "score_model_v1"
}
```

Paths: `configs/live/{league}_stack.json` (preferred), SPL legacy `data/spl_stack.json`. Current promoted SPL (verified): `draw_boost=1.0`, `draw_pick_threshold=0.28`, `draw_pick_margin=0.05`, `dependence=dixon_coles`, `selected_experiment_id=BOT-SPL-chal-db1p0-pick`. **Do not add keys to this contract** without reading `league_stacks.py` and updating its tests.

### 3.6 Environment & constants

- Env (`.env.example`): required `BOT_TOKEN`, `FOOTBALL_API_KEY`, `GEMINI_API_KEY`. Optional `LIVE_FOOTBALL_API_KEY`, `NINJAS_API_KEY`, `GEMINI_MODEL` (default `gemini-3.5-flash`), `GEMINI_FALLBACK_MODELS`, `DATABASE_PATH`, `LAB_DATABASE_PATH`, `JOBS_INTERVAL_SECONDS`, `PRE_ROUND_*`, `DASHBOARD_*`, `LOG_LEVEL`.
- Constants: `laboratory.EXPERIMENT_ID=EXP-PL-001`, `laboratory.BOT_EXPERIMENT_ID=BOT-PL-001`, per-league live `BOT-{CODE}-live`, `SCORE_MODEL_CONFIG_ID=bot-score-model-v1`, `SCORE_MODEL_ENGINE=score_model`, `BOT_BACKTEST_SEASONS=(2024, 2025, 2026)`, `PROSPECTIVE_LEAD_DAYS=1`.
- Lab experiment YAML: requires `experiment_id`, `competition`, seasons or tuning/holdout, `base_config`; optional `param_grid`, `primary_metric` (default `log_loss`), `min_matches`.

## 4. Mandatory Implementation & Modification Workflow

### Step 1 — Pre-Edit Check
1. Classify the change: **live bot** / **lab backtest** / **dashboard** / **SPL stack only** / **schema**. State it to the user in one line.
2. Read, in this order: the target file(s); the module boundary rule in §2.1 for that file; the relevant `docs/` file (`docs/architecture.md` for live, `docs/prediction/*.md` for math, `docs/backtesting/BT-*.md` for lab).
3. For stack/draw/score changes also Read `score_model.py`, `league_stacks.py`, `laboratory/score_model_adapter.py`, `laboratory/job.py`.
4. For schema changes Read `storage.py`, `laboratory/store.py`, `database.py`.
5. Grep for any identifier you plan to use that is not in §3. If it does not exist, do not invent it.

### Step 2 — Implementation Standards
- Python **3.9+** compatible (README recommends 3.10+). Use `from __future__ import annotations`; dataclasses (frozen for configs/outcomes); `Mapping`/`Sequence` in signatures.
- Async in bot/jobs/pipeline; the lab CLI is synchronous SQLite. Do not mix.
- Errors: raise `StageError` / `ProviderError` / `ModelError` with `stage` so users get clean abstain/fail messages.
- Logging: stdlib `logging`, format `%(asctime)s %(levelname)s %(name)s %(message)s`.
- Dependencies: only `python-telegram-bot>=20.7`, `httpx>=0.27`, `google-genai>=1.0`, `python-dotenv>=1.0`, `pytest>=8.0` are documented. Do not add a dependency without asking.
- Caching: disk cache under `Config.cache_dir` (`database_path.parent / "cache"`); crest cache `data/crests`.
- Dashboard stays server-rendered HTML strings (no React/SPA).
- Preserve identifiers verbatim (functions, tables, experiment ids, CLI choices).
- Bot replies keep the "statistical estimate, not betting tip" framing.

### Step 3 — Post-Edit Verification (run, or instruct the user to run)
Smallest relevant tests first, then the full suite:

```bash
.venv/bin/python -m pytest tests/test_score_model.py tests/test_league_stacks.py -q
.venv/bin/python -m pytest tests/test_laboratory.py tests/test_dashboard_states.py -q
.venv/bin/python -m pytest tests/test_lab_experiment.py -q
.venv/bin/python -m pytest -q        # or: make test
```

Checklist:
1. Score/draw/stack change → run `test_score_model`, `test_league_stacks`, `test_laboratory`, `test_dashboard_states`.
2. Pricing change → confirm `leakage_status` still fails when the target score is in the snapshot, and unfinished matches are still skipped in batch jobs.
3. SPL change → compare against baseline experiment metrics (`python -m laboratory tune-spl` compare-only); verify `live_stack_for("SPL")` and that `live_stack_for("PL")` still returns independence with `draw_boost=1.0`. Do NOT `--promote` unless asked.
4. Schema change → both schema constants + migration `ALTER TABLE` paths updated; DB opens via `ensure_shared_schemas`.
5. Dashboard markup change → keep `match-card` / `market-strip` classes unless tests are updated together.
6. After bulk DB writes → checkpoint WAL; restart dashboard if `database is locked`.
7. Tests must pass without network.
8. Report metrics only with `experiment_id` + n from `evaluations` or a fresh walk-forward run.

Useful commands (verified CLI choices; do not invent others):

```bash
python -m laboratory migrate-db | import-pl | import-pd | import-standings
python -m laboratory import-leagues [--competition FL1] [--season 2025]
python -m laboratory cache-logos [--competition SPL]
python -m laboratory backtest-bot [--season 2024 ...]
python -m laboratory price-finished [--competition SPL] [--exclude-competition PL]
python -m laboratory tune-spl [--promote]
python -m laboratory lab-run --config experiments/draw_boost_test.yaml [--workers 4]
python -m laboratory lab-list | lab-analyze ... | lab-promote ... | lab-rollback ...
python -m laboratory demo | run
python -m laboratory serve --host 127.0.0.1 --port 8765     # GET /healthz, no auth
make install | bot | jobs | dash | dash-restart | test ; ./start.sh
```

For `lab-analyze`, `lab-promote`, `lab-rollback`, read the argparse definitions in `laboratory/__main__.py` for exact flags before using them.

## 5. Common Pitfalls & Safe Alternatives

| Mistake an AI Might Make | Why It Fails | Required Safe Pattern |
| --- | --- | --- |
| Re-price by calling INSERT again | `predictions` UNIQUE `(experiment_id, snapshot_id, engine, model_id)` | `delete_experiment_engine_outputs(experiment_id, engine)` then re-run |
| Add target FT score or later matches to a snapshot | Look-ahead bias; `leakage_status` fails | Priors = same competition + same season + strictly earlier kickoff |
| Price upcoming fixtures in batch lab jobs | Batch walk-forward is finished-only | Use `/predict` or the jobs 1-day path |
| Tune SPL by maximizing draw count | Hurts calibration | Walk-forward log-loss / season mean with the `_select_spl_winner` accuracy floor |
| Boost diagonal mass to "fix" draws when pick rules are promoted | Pick rules change `most_likely` only; boosting distorts exact score and O/U | Keep `draw_boost=1.0` with `draw_pick_threshold`/`draw_pick_margin` |
| Edit PL stack while changing SPL (or apply SPL pick rules globally) | Peers must keep `DEFAULT_LIVE_STACK` | Touch only `live_stack_for("SPL")` / SPL stack path |
| Assume `configs/live/` exists | Fresh checkout lacks it | Fall back to `DEFAULT_LIVE_STACK` / `data/spl_stack.json` |
| Use exact-score probability as FT 1X2 confidence | Different markets | FT confidence = P(selected 1X2); score confidence = P(mode cell) |
| Fill missing HT with 0-0 | Fabricates data | Withhold HT market |
| Fill missing standings/scores with zeros | Violates eligibility invariant | Leave empty and abstain |
| Fetch old SPL seasons from Football-Data | No such history | Live Football API, `league_id` 8454 (never 8485) |
| Ask Gemini to pick scoreline/market | Violates domain boundary | Compute in `score_model`; Gemini writes prose only |
| Call Gemini or start Telegram from lab code | Breaks lab isolation | `score_model_adapter` / `poisson_engine` only; lab CLI never starts polling |
| Open ad-hoc `sqlite3.connect` with defaults | Lock errors under WAL contention | `laboratory.store.connect` / `Storage` (timeout 60, busy_timeout 60000, WAL) |
| Long write transaction during tune/reprice with dashboard running | `database is locked` | Batch commits; `checkpoint_wal(..., mode="TRUNCATE")`; restart dashboard if needed |
| Hand-edit a stack JSON while the bot is serving | Partial write | `save_live_stack` / `lab-promote` (temp + `os.replace`); `lab-rollback` to revert |
| Run `tune-spl --promote` or `lab-promote` unprompted | Changes live behavior | Compare-only by default; promote only on explicit request |
| Invent a CLI flag, stack key, or column | Not in the verified contract | Read the source (`__main__.py`, `league_stacks.py`, SQL DDL) first |
| Assume `lab.db`, `?mode=ro`, `[lab]` extras, or a `code_version` gate | Not documented in the blueprint | Verify in source; if absent, tell the user instead of coding against it |
| Rename `match-card` / `market-strip` in dashboard HTML | Tests depend on them | Change tests and markup together |
| Call external APIs in tests | Tests must be offline | Local fixtures (`tests/fixtures/markets.json` etc.) |
| Commit `.env` / bind dashboard off-loopback without password | Secret leak / open dashboard | `.env` stays untracked; set `DASHBOARD_PASSWORD` for non-loopback |
| Cite metrics from memory | Unverifiable | Query `evaluations` or re-run walk-forward; cite `experiment_id` and n |

## 6. Finishing Protocol

1. Summarize which boundary your change touched and confirm it stayed inside it.
2. List exactly which pytest modules/commands you ran and their result; list any you could not run.
3. Flag anything you could not verify from source rather than asserting it.