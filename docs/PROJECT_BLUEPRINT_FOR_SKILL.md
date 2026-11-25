# docs/PROJECT_BLUEPRINT_FOR_SKILL.md

> Verified from source on 2026-11-22. Paths, signatures, table names, and CLI commands below were read from the repository—not inferred. Prefer this document over memory when editing.

---

## 1. Project Identity & Core Domain Purpose

### What this system does

**Football Predictor** is a Telegram bot + shared SQLite laboratory that estimates **one named football match at a time** and **abstains** when data quality is insufficient. It is explicitly **not** a fixture browser that auto-tips every game, and replies must state results are **statistical estimates, not betting tips**.

Primary production workflows:

1. **On-demand `/predict`** (`live/bot.py` → `live/pipeline.Pipeline.execute` → `modeling/score_model.estimate_score` → Gemini reasoning via `live/predictor.Predictor` → SQLite bot tables).
2. **Fixture listing** `/today` and `/matches DD-MM-YYYY` (list only; optional inline button `p:<match_id>` triggers the same predict path).
3. **Server jobs** (`python -m jobs`): Cairo-tomorrow pricing + standings refresh on a timer (`JOBS_INTERVAL_SECONDS`, default 3600).
4. **Laboratory / backtesting** (`python -m laboratory …`): import seasons, walk-forward price finished matches with the **same** `score_model` (no Gemini), evaluate markets, tune SPL stacks, optional lab experiment grid + promotion.

### Problem solved

Given home, away, and competition (or a provider match id), the system:

- Resolves the exact upcoming fixture (never swaps home/away to force a hit).
- Fetches standings, form, H2H, season finished matches.
- Validates eligibility (consistency, availability, quality, freshness).
- Builds expected goals via **shrunk multiplicative Poisson rates**, optionally **Dixon–Coles**, optional **draw calibration**.
- Derives markets from the normalized score grid (FT 1X2, exact score mode, O/U 2.5 & 3.5, optional HT).
- Asks Gemini **only for prose reasoning**—Gemini does **not** choose scores or markets.

### Domain concepts (as coded)

| Term | Meaning in this repo |
| --- | --- |
| **Leakage** | Snapshot must not contain the target FT score, later matches, or engine fields (`lambda_*`, `poisson`, `gemini`). Enforced in `laboratory/snapshot.leakage_status`. |
| **Walk-forward** | Lab prices matches in kickoff order; priors are only earlier same-competition (and for snapshots: same-season) finished games. Unfinished matches are skipped in `run_score_model` / `run_poisson`. |
| **Insert-only predictions** | `predictions` UNIQUE on `(experiment_id, snapshot_id, engine, model_id)`; re-price requires `delete_experiment_engine_outputs` first. |
| **Abstain** | Bot/lab refuses when clubs lack ≥3 finished scored matches (`eligibility.MINIMUM_SCORED_MATCHES` / adapter same), standings missing, fixture not predictable, etc. |
| **Shrinkage** | `r_shrunk = (n·r + k·prior) / (n + k)` with default `k=6` (`score_model.K`). |
| **λ rates** | `λ_home = league_home_avg × attack_home × defence_away` (and symmetric away); home advantage comes from league home vs away averages—not an extra constant. |
| **Grid** | Independent Poisson (or optional NB when `nb_alpha>0`) cells until raw tail ≤ `TAIL_TOLERANCE` (0.001), then renormalize; cap `MAX_GRID_GOALS=25`. |
| **Dixon–Coles** | Low-score τ on 0-0/1-0/0-1/1-1; ρ fit on earlier matches (`MIN_RHO_TRAINING_MATCHES=30`). |
| **Draw boost / weights / tightness** | Post-matrix diagonal scaling + optional pick rule (`select_1x2_outcome`) that can change `most_likely` **without** changing probability mass when using threshold/margin only. |
| **Calibration knobs** | Optional kwargs (`goal_cap`+`goal_cap_slope`, `nb_alpha`, `promoted_attack_prior`/`promoted_defence_prior`) with **neutral defaults** in `DEFAULT_LIVE_STACK`; per-league overrides via `configs/live/{code}_stack.json`. |
| **Live stack** | Per-competition score_model knobs via `league_stacks.live_stack_for` → `configs/live/{code}_stack.json` (lowercase path), with SPL legacy fallback `data/spl_stack.json`. |
| **Cairo time** | Fixture lists and job “tomorrow” use Africa/Cairo (`timeutil`). |
| **Engines** | Lab: `poisson` (lab-only), `score_model` (bot parity). Bot production path: `score_model` + Gemini text. |
| **Metrics** | FT/HT 1X2 log-loss & Brier, exact-score log-loss & hit, O/U binary log-loss & Brier (`laboratory/evaluate.py`). Floor: `LOG_LOSS_FLOOR=1e-6`. |

### Competition codes (verified)

Live list (`data_fetcher.LIVE_MATCH_COMPETITIONS` / `TODAY_MATCH_COMPETITIONS`): `PL`, `CL`, `PD`, `SA`, `BL1`, `FL1`, `PPL`, `DED`, `SPL`.

SPL history: Live Football API league_id **8454** (`providers/live_football_api.py`; **8485 is wrong**—Serbia U19). Live fixtures/standings may use API Ninjas when `NINJAS_API_KEY` is set.

---

## 2. High-Level Architecture & Data Flow

```
                         ┌─────────────────────────────────────┐
                         │           .env / Config             │
                         │  BOT_TOKEN, FOOTBALL_API_KEY,       │
                         │  GEMINI_*, NINJAS_*, LIVE_FOOTBALL_ │
                         │  DATABASE_PATH → data/football.db   │
                         └─────────────────┬───────────────────┘
                                           │
     ┌─────────────────────────────────────┼─────────────────────────────────────┐
     │                                     │                                     │
     ▼                                     ▼                                     ▼
┌──────────────┐                   ┌──────────────┐                   ┌──────────────────┐
│  main.py     │                   │  jobs/       │                   │ laboratory/      │
│  Telegram    │                   │  __main__.py │                   │ __main__.py      │
│  long-poll   │                   │  tomorrow +  │                   │ CLI + dashboard  │
└──────┬───────┘                   │  standings   │                   └────────┬─────────┘
       │                           └──────┬───────┘                            │
       │                                  │                                    │
       ▼                                  ▼                                    ▼
┌──────────────┐                   ┌──────────────┐              ┌─────────────────────────┐
│ live/bot.py  │                   │ build_pipeline│              │ serve / import-* /      │
│ parse cmds   │◄──────────────────│ (same as bot) │              │ price-finished /        │
└──────┬───────┘                   └──────┬───────┘              │ tune-spl / lab-run …    │
       │                                  │                      └────────────┬────────────┘
       ▼                                  ▼                                   │
┌──────────────┐                   ┌──────────────┐                           │
│ live/pipeline│──────────────────►│ db/storage.py│◄──────────────────────────┤
│ identify →   │   bot tables      │ prediction_* │                           │
│ fetch →      │                   └──────┬───────┘                           │
│ eligibility  │                          │ shared file                       │
│ score_model  │                          │ data/football.db (WAL)            │
│ predictor    │                          │                                   │
└──────┬───────┘                          │                                   │
       │                                  │                                   ▼
       │                           ┌──────┴───────┐              ┌─────────────────────────┐
       │                           │ db/database  │              │ laboratory/store.py     │
       │                           │ ensure both  │◄─────────────│ matches, results,       │
       │                           │ schemas      │              │ snapshots, predictions, │
       │                           └──────────────┘              │ evaluations, lab_* …    │
       │                                                         └────────────┬────────────┘
       │                                                                      │
       ▼                                                                      ▼
┌──────────────┐                                                 ┌─────────────────────────┐
│ modeling/    │◄── modeling.league_stacks.live_stack_for(code)  │ job.py run_score_model  │
│ score_model  │    configs/live/* or data/spl_stack.json        │ score_model_adapter     │
└──────────────┘                                                 │ snapshot.build_snapshot │
                                                                 │ evaluate_prediction     │
                                                                 └─────────────────────────┘

Providers (read-only network):
  Football-Data.org  ← providers/data_fetcher / laboratory/football_data / standings
  Live Football API  ← providers/live_football_api (SPL seasons, league_id 8454)
  API Ninjas         ← providers/api_ninjas (SPL live fixtures/standings)
  Google GenAI       ← live/predictor.py only (reasoning text)
```

### Module boundaries (import / I/O rules)

| Boundary | Rule |
| --- | --- |
| `core/models.py` | Pure dataclasses/errors; **no** Telegram, HTTP, Gemini, SQLite. |
| `domain/*` | Pure immutable value objects (`ModelConfig`, `ScoringConstants`, `EncodedDataset`) & zero-copy NumPy feature engineering; **no** SQLite. |
| `modeling/*` | Pure math & vectorized NumPy/SciPy strategies (`modeling/score_model.py`, `base.py`, `shrinkage.py`, `dixon_coles.py`, `distributions.py`); **no** Gemini, Telegram, or SQLite. |
| `live/eligibility.py` | Validation only; gaps stay empty (never fill zeros). |
| `live/pipeline.py` | Orchestrates live predict; may write bot `Storage` tables. |
| `laboratory/*` | Must not start the Telegram bot; lab pricing must not call Gemini (`score_model_adapter` / `poisson_engine`). SQL isolated in `laboratory/repositories.py` & `laboratory/store.py`. |
| `live/predictor.py` | Gemini JSON reasoning only; scores/markets already computed. |
| Shared DB | Bot tables (`prediction_*`) and lab tables coexist; names do not overlap (`db.database.ensure_shared_schemas`). |
| Live vs lab configs | Prefer `configs/live/{league}_stack.json` via `modeling.league_stacks`; SPL may still read `data/spl_stack.json`. Lab promotions write atomically (`os.replace`). |

### Live vs laboratory execution

| Path | Writes predictions? | Gemini? | Typical experiment ids |
| --- | --- | --- | --- |
| Telegram `/predict` / button | Yes → `prediction_run/snapshot/version` | Yes (prose) | N/A (bot tables) |
| `python -m jobs` tomorrow | Yes → same bot tables via `Pipeline` | Yes | N/A |
| `price-finished` / `backtest-bot` / `tune-spl` | Yes → lab `predictions` + `evaluations` | **No** | `BOT-PL-001`, `BOT-{CODE}-live`, `BOT-SPL-chal-*` |
| `import-pl` + `run` | Lab Poisson `EXP-PL-001` | No | `EXP-PL-001` |
| `lab-run` | Lab experiment grid | No | YAML `experiment_id` |

---

## 3. Complete File & Module Map

### Entry points

| Path | Responsibility | Key API | I/O |
| --- | --- | --- | --- |
| `main.py` | Telegram long-poll | `build_pipeline(config)`, `main()` | Reads env; opens `db.storage.Storage`; runs polling |
| `live/bot.py` | Commands `/start`, `/today`, `/matches`, `/predict`, callbacks `p:` | `build_application`, `parse_*` | Uses `live.pipeline.Pipeline`; Cairo times |
| `jobs/__main__.py` | Background loop | `jobs_loop`, `main` | Tomorrow + standings; `record_job_run` |
| `jobs/tomorrow.py` | Price Cairo-tomorrow fixtures | `run_tomorrow_once` | Via `live.pipeline.Pipeline` |
| `jobs/standings_job.py` | Refresh standings after local rounds | `run_standings_once` | Lab standings tables |
| `laboratory/__main__.py` | Lab CLI + HTTP dashboard | `main`, `serve`, `_tune_spl`, … | Shared DB; Football-Data / LFA / Ninjas |
| `start.sh` / `Makefile` | Local process orchestration | `make bot\|jobs\|dash\|test\|start` | Logs under `data/logs/` |

### Core, Live, Providers, DB, Domain & Modeling

| Path | Responsibility | Key functions / classes | Inputs → Outputs |
| --- | --- | --- | --- |
| `core/config.py` | Env → `Config` | `load_config`, `normalize_gemini_model` | Requires BOT/FOOTBALL/GEMINI keys |
| `core/models.py` | Shared shapes | `Fixture`, `PipelineResult`, `Markets`, `StageError`, … | Pure data |
| `core/timeutil.py` | Cairo formatting | kickoff helpers | UTC ISO → Cairo display |
| `db/db_paths.py` | Shared DB path | `resolve_database_path`, `resolve_lab_database_path`, `DEFAULT_DATABASE_PATH` | env → `Path` |
| `db/database.py` | Dual schema bootstrap | `ensure_shared_schemas` | connection → bot+lab DDL |
| `db/storage.py` | Bot SQLite traces | `Storage`, `BOT_SCHEMA` | run/snapshot/version CRUD |
| `db/migrate_db.py` | Merge old DBs & compact DBs | `merge_into_shared`, `compact_database` | legacy → `football.db`; slim + `VACUUM` |
| `db/team_mapping.py` | Central team identity & strict resolution | `resolve_team_id`, `register_team_mapping`, `backfill_team_mappings`, `UnmappedTeamError` | `canonical_teams` + `team_provider_mappings` |
| `domain/entities.py` | Domain value objects & constants | `ScoringConstants`, `ModelConfig`, `EncodedDataset`, `FittedModelState` | Zero-copy `slice_view` |
| `domain/features.py` | Vectorized feature engineering | `encode_matches`, `compute_time_decay_weights`, `soft_cap_goals_vec`, `compute_standings_blend_means` | Records → contiguous NumPy arrays |
| `modeling/base.py` | Strategy interface | `BaseScoreModel` (`fit`, `predict_lambdas`) | Dataset + config → rates |
| `modeling/shrinkage.py` | Vectorized shrinkage models | `ShrinkageOnePassModel`, `ShrinkageJointModel`, `compute_joint_venue_strengths_vec` | `np.bincount` $O(M)$ |
| `modeling/dixon_coles.py` | DC ρ & analytical-gradient MLE | `fit_dixon_coles_rho_vec`, `JointDixonColesMLEModel`, `dixon_coles_joint_nll_and_grad` | 4-cell closed form; `L-BFGS-B` (`jac=True`) |
| `modeling/distributions.py` | Vectorized score matrices | `build_score_matrix_vec`, `market_1x2_from_matrix` | λ → normalized `(G+1, G+1)` grid |
| `modeling/score_model.py` | Facade & multiplicative Poisson (+ NB) + DC + draw knobs | `estimate_score`, `estimate_from_lambdas`, `select_1x2_outcome`, `_boost_draw_scorelines`, `ScoreEstimate` | Finished payloads + ids → grid, markets, λ |
| `modeling/league_stacks.py` | Per-league live knobs | `live_stack_for`, `draw_calibration_kwargs`, `save_live_stack`, `save_spl_stack` | code → stack dict |
| `live/pipeline.py` | One-match live analysis | `Pipeline.execute`, `_analyze` | home/away/comp → `PipelineResult`; may abstain |
| `live/predictor.py` | Gemini reasoning | `Predictor`, prompt builders | Snapshot + estimate → tactical text JSON |
| `live/eligibility.py` | Gate checks | `evaluate`, `MINIMUM_SCORED_MATCHES=3`, `FORM_WINDOW=5` | Payloads → `Evaluation` |
| `live/pre_round.py` | Legacy in-bot pre-round | Used only if `PRE_ROUND_ENABLED=1` | Prefer `python -m jobs` |
| `providers/data_fetcher.py` | Football-Data (+ Ninjas routing) | `DataFetcher`, `resolve_competition`, `fixture_is_predictable`, `LIVE_MATCH_COMPETITIONS` | API → fixtures/teams/standings caches |
| `providers/api_ninjas.py` | SPL live provider | Ninjas client helpers | Key → fixtures/standings |
| `providers/live_football_api.py` | SPL multi-season archive | `LFA_LEAGUE_IDS["SPL"]=8454`, season fetch | Key → season bodies |
| `archive/` | Superseded historical scripts | `evaluate_models*.py`, `enhanced.py` | Out of runtime import paths |

### Laboratory

| Path | Responsibility | Key API | Notes |
| --- | --- | --- | --- |
| `laboratory/__init__.py` | Constants + lazy lab API | `EXPERIMENT_ID`, `BOT_EXPERIMENT_ID`, `SCORE_MODEL_CONFIG_ID`, `PROSPECTIVE_LEAD_DAYS=1` | Lazy exports for experiment modules |
| `laboratory/store.py` | Lab schema + CRUD | `connect`, `checkpoint_wal`, `insert_prediction`, `ensure_experiment`, crest cache | `timeout=60`, WAL, busy_timeout 60s |
| `laboratory/repositories.py` | Bulk SQL repositories & transactions | `MatchRepository`, `ExperimentRepository`, `connect_managed`, `transaction` | Zero N+1 queries; `executemany` bulk I/O |
| `laboratory/metrics.py` | Streaming metric accumulators | `StreamingMetricsAccumulator`, `log_loss` | Computes Log-Loss/Brier without storing matrices |
| `laboratory/snapshot.py` | Pre-kickoff payload | `MatchRecord`, `build_snapshot`, `leakage_status` | Same season + kickoff &lt; target |
| `laboratory/job.py` | Walk-forward engines | `run_poisson`, `run_score_model`, `price_one_score_model` | Finished-only for batch; on-demand can replace |
| `laboratory/score_model_adapter.py` | Bot scorer ↔ lab body | `price_from_snapshot`, `to_evaluable_body`, `SCORE_MODEL_ENGINE="score_model"` | Abstain if &lt;3 priors/club |
| `laboratory/poisson_engine.py` | Lab-only Poisson | `price_match`, `_distribution` | Separate from bot path |
| `laboratory/evaluate.py` | Metrics | `evaluate_prediction` | Does not mutate probs |
| `laboratory/football_data.py` | Season import | `fetch_season`, `import_bodies`, `COMPETITION_NAMES` | Prefer LFA for SPL |
| `laboratory/standings.py` | Standings import | `import_all_standings`, `STANDINGS_COMPETITIONS` (`PL`, `PD`, `FL1`, `BL1`, `SA`, `PPL`, `DED`, `SPL`, `CL`) | |
| `laboratory/dashboard.py` | HTML UI | `overview`, `match_list`, `match_detail`, `experiments_list`, `experiment_detail`, JSON launch textarea, Exact/O/U leaderboard + AI prompt, Re-run | Auth via `auth.py`; no React |
| `laboratory/auth.py` | Dashboard login | session cookie, CSRF | Password required off-loopback |
| `laboratory/gemini_schema.py` | Validate Gemini JSON (lab) | schema checks | |
| `laboratory/lab_experiment.py` | Experiment config + JSON launch | `ExperimentConfig`, `parse_experiment_launch_json`, `merge_configs`, `resolve_primary_metric`, `make_model_id` | Explicit `None` in overrides wins; SHA-256 model ids |
| `laboratory/lab_runner.py` | Parallel grid runs | `run_experiment`, `shared_encoded_dataset`, `_aggregate_metrics` | `SharedMemory`/memmap, `OMP/MKL/OPENBLAS=1`, `persist_predictions` toggle |
| `laboratory/lab_analysis.py` | Compare / calibration | `compare_to_baseline`, bootstrap CIs | |
| `laboratory/lab_promotion.py` | Holdout + Bootstrap CI gates & rollback | `promote_experiment_run`, `promote_best_config`, `compute_paired_bootstrap_ci`, `get_promotion_audit_log`, `rollback_promotion` | Enforces Clean Holdout, 95% CI, SPL isolation, `lab_promotions` |
| `laboratory/team_display.py` | Display names | helpers for UI | |

### Tests & docs

| Path | Role |
| --- | --- |
| `tests/*.py` | Unit/integration; **no** live Football-Data/Gemini calls |
| `tests/fixtures/markets.json` | Market label fixtures |
| `docs/architecture.md` | Live bot architecture |
| `docs/prediction/*.md` | Math model, MVP philosophy, decision register |
| `docs/backtesting/BT-*.md` | Lab requirements & experiment specs |
| `docs/lab-experiment-form-filters.md` | Dashboard experiment form / JSON / primary metrics (AR) |
| `docs/DASHBOARD_BLUEPRINT_FOR_SKILL.md` | Dashboard UI contracts |
| `experiments/draw_boost_test.yaml` | Example `lab-run` config |

---

## 4. Data Models, Schemas & Configuration Contracts

### Shared SQLite: `data/football.db`

**Connection settings** (`laboratory/store.connect`, `laboratory/repositories.connect_managed`, and `db/storage.Storage`):

- `sqlite3.connect(..., check_same_thread=False, timeout=60.0)`
- `PRAGMA foreign_keys=ON`
- `PRAGMA busy_timeout=60000`
- `PRAGMA journal_mode=WAL`
- `PRAGMA synchronous=NORMAL`
- `PRAGMA wal_autocheckpoint=1000`
- After bulk `run_score_model`: prefer `checkpoint_wal(..., mode="TRUNCATE")` (fallback PASSIVE)

Verified tables (live DB):

#### Bot tables (`db.storage.BOT_SCHEMA`)

- **`prediction_run`**: `id PK`, `chat_id`, `user_id`, `raw_home/away/competition`, `created_at`, `status IN (completed,partial,abstained,failed)`, `stage`, `reason`
- **`prediction_snapshot`**: `id PK`, `run_id FK`, `captured_at`, `competition_code`, `match_id`, team ids, `kickoff`, `payload`, `freshness`, `validation_checks`
- **`prediction_version`**: `id PK`, `run_id FK`, `snapshot_id FK`, `version_number`, `model_id`, `prompt_version`, `schema_version`, `output_json`, `limitations`, UNIQUE `(run_id, version_number)`

#### Laboratory tables (`laboratory/store.SCHEMA` + migrations)

- **`experiments`**: `id PK`, `status`, `config_json`, `created_at`
- **`lab_experiments`**: `experiment_id PK`, `competition`, `config_json`, `status`, timestamps, `best_config_json`, `best_metrics_json`
- **`lab_experiment_runs`**: `run_id PK`, FK `experiment_id`, `model_id`, `config_json`, `status`, `metrics_json`, priced/evaluated counts, error, timestamps
- **`matches`**: `match_id PK`, `dataset_version`, `competition`, `season`, `kickoff`, `home_id/name`, `away_id/name`, optional `raw_payload_id`
- **`result_revisions`**: PK `(match_id, revision)`, `ft_home/away`, `ht_home/away`, `source` — results are revisioned, not overwritten blindly
- **`snapshots`**: `snapshot_id PK`, `experiment_id`, `match_id`, `payload_json`, `leakage_ok`
- **`predictions`**: `prediction_id PK`, UNIQUE `(experiment_id, snapshot_id, engine, model_id)`, `body_json`, `created_at`
- **`evaluations`**: UNIQUE `(prediction_id, revision)`, `metrics_json`
- **`engine_runs`**: per-match engine status/detail
- **`raw_payloads`**, **`teams`**, **`canonical_teams`**, **`team_provider_mappings`**, **`competitions`**, **`standings_meta`**, **`standings_rows`**, **`job_runs`**, **`lab_promotions`**

### Score / prediction body shapes

Nested engines (`engine` in `poisson` | `score_model`) — built by `to_evaluable_body` / poisson distribution:

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
    "exact_scores": [{"home_goals": 0, "away_goals": 0, "probability": 0.08}, "..."],
    "over_under_2_5": {"over": 0.48, "under": 0.52, "most_likely": "under"},
    "over_under_3_5": {"over": 0.25, "under": 0.75, "most_likely": "under"}
  },
  "half_time": {"withheld": true, "reason": "..."}
}
```

Metrics JSON (`evaluate_prediction`) keys: `full_time_1x2`, `half_time_1x2`, `exact_score`, `over_under_2_5`, `over_under_3_5` — each with `status`, and when scored: `selection`/`actual`/`correct`/`log_loss`/`brier` (exact_score omits selection; uses `correct` on mode hit). Exact-score log-loss uses **P(actual cell)**; hit uses mode equality.

Run-level aggregates (`lab_runner._aggregate_metrics` → `lab_experiment_runs.metrics_json`) flatten scored markets to `{market}_accuracy`, `{market}_log_loss`, `{market}_brier`, plus nested blocks for `exact_score` / `over_under_2_5` (and HT / 3.5). Exact also aliases `exact_score_hit_rate`. Older runs missing these keys must render as `—` in the dashboard.

### Live stack contract (`league_stacks.DEFAULT_LIVE_STACK`)

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
  "goal_cap": null,
  "goal_cap_slope": 0.5,
  "nb_alpha": 0.0,
  "promoted_attack_prior": null,
  "promoted_defence_prior": null,
  "code_version": "score_model_v1"
}
```

**Neutral calibration defaults** in `DEFAULT_LIVE_STACK` keep unknown competitions bit-identical to classic Poisson/`score_model_v1`. Per-league overrides live in `configs/live/{code}_stack.json` (path is **lowercase**, e.g. `pd_stack.json`). Do not change those defaults casually.

`draw_calibration_kwargs(stack)` → draw **and** calibration kwargs for `estimate_score` / `price_from_snapshot` / `run_score_model`. Promotions validate via `_merge_stack` / `lab_promotion._normalize_promoted_stack` / `save_live_stack`.

#### Promoted live stacks (verified 2026-11-22)

Shared pattern for PL + Euro domestic + CL (unless noted): `prior_mean=last_season`, `half_time_share_mode=club_shrunk`, `ratio_mode=one_pass`, `decay_half_life_days=150`, `goal_cap_slope=0.5`, `code_version=score_model_v1`. (`CL` uses `dependence=dixon_coles`, `nb_alpha=0.1`; other Euro leagues default to `dependence=independent`, `nb_alpha=0.0`).

| Code | File | Experiment | k | draw_boost | goal_cap | promoted atk/def | model_id |
| --- | --- | --- | --- | --- | --- | --- | --- |
| PL | `configs/live/pl_stack.json` | `EXP-PL-XG-HOLDOUT` | 12 | 1.06 | 4 | 0.80 / 1.25 | `bot-score-model-pl-v5` (`elo_weight=0.08`, `elo_k=24.0`, `elo_home_adv=55.0`, `xg_weight=0.25` via `fpl_opta_github` auto-sync; Holdout 2022–2023 `n=349`, `Exact LL Δ=-0.00754`, 95% CI `[-0.01223, -0.00264]`) |
| PD | `configs/live/pd_stack.json` | `EXP-EURO-DOMESTIC-01-PD` | 12 | 1.0 | 4 | 0.80 / 1.25 | `bot-score-model-pd-v2` |
| SA | `configs/live/sa_stack.json` | `EXP-EURO-DOMESTIC-01-SA` | 12 | 1.0 | 4 | 0.80 / 1.25 | `bot-score-model-sa-v2` |
| BL1 | `configs/live/bl1_stack.json` | `EXP-EURO-DOMESTIC-01-BL1` | 12 | 1.06 | 4 | 0.80 / 1.25 | `bot-score-model-bl1-v2` |
| FL1 | `configs/live/fl1_stack.json` | `EXP-EURO-DOMESTIC-01-FL1` | 12 | 1.0 | 4 | 0.80 / 1.25 | `bot-score-model-fl1-v2` |
| PPL | `configs/live/ppl_stack.json` | `EXP-EURO-DOMESTIC-01-PPL` | 8 | 1.06 | 4 | 0.80 / 1.25 | `bot-score-model-ppl-v2` |
| DED | `configs/live/ded_stack.json` | `EXP-EURO-DOMESTIC-01-DED` | 8 | 1.06 | 4 | 0.80 / 1.25 | `bot-score-model-ded-v2` |
| CL | `configs/live/cl_stack.json` | `EXP-EURO-ELO-HOLD-02-CL` | 8 | 0.85 | null | 0.85 / 1.15 | `bot-score-model-cl-v5` (`persistent_priors`, `elo_blend=0.5`, `domestic_prior_weight=0.4`, `elo_weight=0.08`, `elo_k=24.0`, `elo_home_adv=55.0`; Holdout 2023 `n=77`, `mean_diff=-0.0060`, 95% CI `[-0.0116, -0.0008]`) |
| SPL | `data/spl_stack.json` (legacy; no `configs/live/spl_stack.json`) | `BOT-SPL-chal-db1p0-pick` | (file) | 1.0 + pick thr 0.28 / margin 0.05 | null | null | `bot-score-model-live` + `dependence=dixon_coles` |

Unknown competition codes → `DEFAULT_LIVE_STACK`. **Do not** overwrite PL/SPL when promoting other leagues.

### Exact Score / goal-volume knobs (`modeling/score_model.py`)

Defaults leave `score_model_v1` behavior unchanged (`goal_cap=None`, `nb_alpha=0.0`, promoted priors `None` → prior mean 1.0):

| Knob | Default | Effect when set |
| --- | --- | --- |
| `goal_cap`, `goal_cap_slope` | `None`, `0.5` | Soft-cap prior-match FT goals in rate estimation / league averages / `club_venue_ratios` only—never rewrite snapshot or target scores |
| `xg_weight` | `0.0` | Secondary xG blend weight in `[0.0, 1.0]` (`eff = (1 - w)*capped_goals + w*capped_xg`; missing xG falls back to 100% actual goals, never `0.0`). Imported into `match_xg` via `python -m laboratory import-xg --file <csv|json> --source <understat|fbref> --competition <COMP> --season <YEAR>` using strict `resolve_team_id` canonical mapping. |
| `nb_alpha` | `0.0` | `>0` → Negative Binomial marginals (mean λ, Var=λ+αλ²) via stdlib recurrence; same tail/renorm/DC/draw pipeline |
| `promoted_attack_prior`, `promoted_defence_prior` | `None` | When `prior_mean=last_season` and club missing from previous-season ratios, use these instead of 1.0 |

### Stage 3 — Calibration & Market Benchmark (Items 8 & 9 Completed)

- **Item 8 (`laboratory/calibration.py` + `EXP-PL-CALIB-01`, PL 2023–2025, `n=1049`):** Manual `draw_boost=1.06` (`calibration_mode="none"`) outperformed out-of-sample `platt_fit_prior_season` (`Exact Score LL=3.0074` vs `3.0109`, `FT 1X2 LL=1.0056` vs `1.0090`, `Brier=0.6011` vs `0.6034`, `FT Draw ECE=0.0071` vs `0.0152`). Live `PL` keeps `draw_boost=1.06`.
- **Item 9 (`db/market_odds.py` + `match_odds`, PL 2022–2025 `football_data_co_uk` closing odds):** Imported `1520` matches (`380` × 4 seasons) via `scripts/fetch_pl_odds.py` & `import-odds`. Matched `n=1049` backtest fixtures: `Model LL=1.0056` vs `Vig-Free Closing Market LL=0.9664` (`Δ=+0.0391`), `Model Brier=0.6011` vs `Market Brier=0.5745`, `Mean CLV=-2.43%` (`41.94%` positive CLV), `Simulated Flat-Stake ROI=-12.20%` (`839` bets, `205` hits, `-102.35` units) → Honest Verdict: **`NO_EDGE`**. Scientific diagnostic only; never a betting tip.

### Experiment grid merge (`merge_configs` / `_merge_configs`)

**CRITICAL:** An explicit `None` in `param_grid` **must override** a non-null `base_config` value (e.g. `"decay_half_life_days": [null, 150]`). Do **not** skip `None` with truthy/`continue` merges—that made EXP-PL-EXACT-K-14 combos identical. Keys present in the override win, including null.

### Environment (`.env.example`)

Required for bot: `BOT_TOKEN`, `FOOTBALL_API_KEY`, `GEMINI_API_KEY`.  
Optional: `LIVE_FOOTBALL_API_KEY`, `NINJAS_API_KEY`, `GEMINI_MODEL` (default `gemini-3.5-flash`), `GEMINI_FALLBACK_MODELS`, `DATABASE_PATH`, `LAB_DATABASE_PATH`, `JOBS_INTERVAL_SECONDS`, `PRE_ROUND_*`, `DASHBOARD_*`, `LOG_LEVEL`.  
**Never commit `.env`.**

### Lab experiment YAML / dashboard JSON

Requires `experiment_id`, `competition`(s), seasons or tuning/holdout, `base_config`, optional `param_grid`, `primary_metric`, `min_matches`.

`primary_metric` UI/JSON values (`resolve_primary_metric` / `ALLOWED_PRIMARY_METRIC`):

| UI / JSON | Aggregate key | Direction |
| --- | --- | --- |
| `log_loss` | `full_time_1x2_log_loss` | lower better |
| `brier` | `full_time_1x2_brier` | lower better |
| `full_time_1x2_accuracy` / `accuracy` | `full_time_1x2_accuracy` | higher better |
| `exact_score_log_loss` | `exact_score_log_loss` | lower better |

Dashboard: `/experiments` form + optional `config_json` textarea (`parse_experiment_launch_json`); detail page leaderboard/KPI/AI prompt surface Exact Hit % / Exact LL and O/U 2.5 Acc/LL; `POST /experiments/<id>/rerun` resets runs via `reset_lab_experiment_for_rerun` and re-spawns `lab-run`.

---

## 5. Coding Conventions, Patterns & Tech Stack

| Item | Fact |
| --- | --- |
| Python | **3.9+** (README recommends 3.10+; 3.9 EOL) |
| Dependencies (`requirements.txt`) | `python-telegram-bot>=20.7`, `httpx>=0.27`, `google-genai>=1.0`, `python-dotenv>=1.0`, `pytest>=8.0`, `numpy>=1.24,<2.0`, `scipy>=1.10,<1.14` |
| Typing | `from __future__ import annotations`; dataclasses; `Mapping`/`Sequence`; frozen dataclasses for configs/outcomes |
| Config | `python-dotenv` with `load_dotenv(override=True)` so `.env` beats stale shell exports |
| Async | Bot/jobs/pipeline use asyncio; lab CLI is sync SQLite |
| Errors | `StageError` / `ProviderError` / `ModelError` with `stage` for user-facing abstain/fail |
| Logging | stdlib `logging`; format `%(asctime)s %(levelname)s %(name)s %(message)s` |
| Atomic writes | Live stack / promotion: temp file + `os.replace` (`league_stacks.save_live_stack`, `lab_promotion._write_live_config_atomic`) |
| Caching | Disk cache under `Config.cache_dir` (`database_path.parent / "cache"`); crest cache under `data/crests` |
| Tests | Local fixtures only; do not call external APIs |
| UI | Dashboard is server-rendered HTML strings in `dashboard.py` (no React SPA) |

Score-model constants (authoritative in `modeling/score_model.py` & `domain/entities.py`): `K=6.0`, `TAIL_TOLERANCE=0.001`, `MAX_GRID_GOALS=25`, `MINIMUM_HALF_TIME_SAMPLES=3`, `MIN_RHO_TRAINING_MATCHES=30`.

---

## 6. Strict Invariants & "NEVER DO THIS" Guardrails

### Leakage & walk-forward

1. **NEVER** put the target match FT/HT score into a lab snapshot payload. `leakage_status` fails if `ft_home`/`score` appear on the snapshot root or if a prior has `kickoff >= cutoff`.
2. **NEVER** include the target `match_id` in `prior_matches`.
3. **NEVER** price unfinished matches in batch walk-forward (`run_score_model` / `run_poisson` skip them). Upcoming fixtures are for `/predict` or the 1-day jobs path only.
4. Lab snapshot priors are **same competition AND same season** and strictly earlier kickoff (`build_snapshot`). Do not “helpfully” add future or other-league games.
5. When `prior_mean=last_season`, last-season ratio lookup is a separate cache path in `job.py`—do not leak current match into that fit.

### Predictions & evaluation

6. Lab predictions are **insert-only**. Duplicate UNIQUE → treat as existing. To reprice: `delete_experiment_engine_outputs(experiment_id, engine)` then re-run.
7. `evaluate_prediction` **must not** rewrite probabilities; it only scores stored bodies against result revisions.
8. Decoupled draw pick (`draw_pick_threshold` / `draw_pick_margin`) changes **selection / most_likely**, not the probability vector used for log-loss/Brier. Do not “fix” calibration by boosting matrix mass when the promoted SPL stack uses pick rules at `draw_boost=1.0`.
9. Uniform high `draw_boost` on all diagonal cells (including 4-4) distorts exact score and O/U—avoid promoting that for SPL without walk-forward comparison.
10. Exact-score “hit” = mode cell matches FT score; log-loss uses **probability of the actual cell**, not the mode.
10a. Lab grid merges: **explicit `None` overrides** non-null base knobs; skipping nulls is a regression (identical grid cells / model_ids).
10b. Calibration knobs may live in `DEFAULT_LIVE_STACK` only as **neutral no-ops**; per-league values belong in `configs/live/{code}_stack.json`. Never copy one league’s champion stack onto another; leave PL/SPL untouched unless explicitly promoting that league.

### Bot product rules

11. **NEVER** predict FINISHED / IN_PLAY / postponed etc. as if upcoming (`fixture_is_predictable` / architecture docs).
12. **NEVER** swap home/away to force a fixture match.
13. **NEVER** invent missing scores or standings zeros (`eligibility`: gaps stay empty).
14. Gemini must not choose λ, scoreline, or markets—only reasoning text.
15. Minimum **3** finished scored matches per club (bot eligibility + lab adapter). HT needs ≥3 valid HT samples per club (`MINIMUM_HALF_TIME_SAMPLES`).
16. Free-tier Football-Data delay warning is always recorded; missing `lastUpdated` ≠ “fresh”.

### SQLite / concurrency

17. Bot + dashboard + lab jobs share one file. Long WAL after bulk pricing causes **`database is locked`**. After heavy writes: checkpoint; keep `busy_timeout` high; avoid holding multi-hour write transactions.
18. Dashboard historically used a long-lived connection with `ThreadingHTTPServer`—be careful with concurrent writes from tune/reprice while serving.
19. Do not disable WAL without a concurrency plan.

### SPL / providers

20. SPL Live Football league_id is **8454**, never **8485**.
21. Prefer Live Football for SPL **history**; Ninjas for live list/standings when keyed. Do not assume Football-Data has SPL seasons.
22. Unknown codes keep `DEFAULT_LIVE_STACK`; promoted leagues read their own `configs/live/{code}_stack.json`—**do not** apply SPL draw-pick rules globally or mutate PL/SPL while promoting PD/SA/BL1/FL1/PPL/DED/CL.

### Imports / process isolation

23. Laboratory CLI must not start Telegram polling.
24. `score_model_adapter` / walk-forward must not call Gemini.
25. Do not merge bot `prediction_*` rows into lab `predictions` or vice versa—different schemas and lifecycles.

### Config / secrets

26. Never commit `.env` or API keys.
27. Non-loopback dashboard bind **requires** `DASHBOARD_PASSWORD`.
28. Promotion to live configs should be atomic; use rollback files / `lab-rollback` rather than hand-editing half a JSON file mid-serve.

### Common AI pitfalls → required alternative

| Pitfall | Required alternative |
| --- | --- |
| Reprice by INSERT without delete | Call `delete_experiment_engine_outputs` first |
| Tune SPL by maximizing draw count only | Use walk-forward metrics (log-loss / season mean) with accuracy floor (`_select_spl_winner`) |
| Change PL stack while editing SPL | Touch only SPL stack path / `live_stack_for("SPL")` |
| Use exact-score confidence as FT 1X2 confidence | FT confidence = P(selected 1X2); score confidence = P(mode cell) |
| Fill missing HT with 0-0 | Withhold HT market |
| Call Football-Data for old SPL seasons | Use Live Football API |
| Assume `configs/live/` always exists | Fall back to `DEFAULT_LIVE_STACK` / SPL `data/spl_stack.json` |
| Skip `None` in `param_grid` merge | Assign override keys even when value is `None` |
| Soft-cap or rewrite target FT in snapshots | Cap only prior goals inside rate estimation |
| Change calibration defaults in `DEFAULT_LIVE_STACK` casually | Keep neutral no-ops; override only via per-league stack files |

---

## 7. Verification, Testing & CLI Commands

### Tests

```bash
# From repo root with venv
.venv/bin/python -m pytest -q
# or
make test
```

Targeted (examples):

```bash
.venv/bin/python -m pytest tests/test_score_model.py tests/test_league_stacks.py -q
.venv/bin/python -m pytest tests/test_laboratory.py tests/test_dashboard_states.py -q
.venv/bin/python -m pytest tests/test_lab_experiment.py -q
```

Tests must pass without network. If dashboard HTML assertions fail, inspect `laboratory/dashboard.py` markup—class names like `match-card` / `market-strip` are load-bearing for tests.

### Local processes

```bash
make install          # venv + requirements
make bot              # python main.py
make jobs             # python -m jobs
make dash             # laboratory serve :8765
make dash-restart     # kill :8765 + restart
./start.sh            # bot + jobs + dash together
```

Health: `GET http://127.0.0.1:8765/healthz` (no auth).

### Laboratory CLI (verified choices in `__main__.py`)

```bash
python -m laboratory migrate-db
python -m laboratory import-pl
python -m laboratory import-pd
python -m laboratory import-leagues [--competition FL1] [--season 2025]
python -m laboratory import-standings
python -m laboratory cache-logos [--competition SPL]
python -m laboratory backtest-bot [--season 2024 ...]
python -m laboratory price-finished [--competition SPL] [--exclude-competition PL]
python -m laboratory tune-spl              # compare only
python -m laboratory tune-spl --promote    # write SPL stack + rebuild BOT-SPL-live
python -m laboratory lab-run --config experiments/draw_boost_test.yaml [--workers 4]
python -m laboratory lab-run --experiment-id EXP-… --lab-db data/lab.db --live-db data/football.db [--workers 1]
python -m laboratory lab-list
python -m laboratory lab-analyze ...
python -m laboratory lab-promote ...
python -m laboratory lab-rollback ...
python -m laboratory demo
python -m laboratory run                   # Poisson walk-forward for EXP-PL-001
python -m laboratory serve --host 127.0.0.1 --port 8765
```

Dashboard experiment routes (CSRF when auth on): `POST /experiments/run`, `POST /experiments/<id>/delete`, `POST /experiments/<id>/rerun`.

### Agent verification checklist (before finishing a task)

1. Identify whether the change affects **live bot**, **lab backtest**, **dashboard**, or **SPL stack only**—keep blast radius minimal.
2. Run the smallest relevant pytest modules; for score/draw/stack/lab-knob changes: `test_score_model`, `test_league_stacks`, `test_lab_experiment`, `test_laboratory`, `test_dashboard_states` / `test_dashboard_experiments`.
3. If changing schemas: update both `BOT_SCHEMA` / `laboratory/store.SCHEMA` and any migration `ALTER TABLE` paths; open DB via `ensure_shared_schemas`.
4. If changing pricing: confirm leakage still fails on target score in snapshot; confirm unfinished matches remain skipped in batch jobs; confirm default kwargs still match live Poisson path.
5. If changing SPL: compare against baseline experiment metrics; do not `--promote` unless asked; verify `live_stack_for("SPL")` and that PL still returns default independence/`draw_boost=1.0`.
6. After bulk DB writes: check WAL size / checkpoint; restart dashboard if predictions fail with `database is locked`.
7. Do not claim metrics without querying `evaluations` or re-running walk-forward—cite experiment_id and n.
8. If changing experiment grids: assert `None` overrides and distinct `make_model_id` for null vs non-null cells.

### Key experiment / model id constants

| Constant | Value |
| --- | --- |
| `laboratory.EXPERIMENT_ID` | `EXP-PL-001` |
| `laboratory.BOT_EXPERIMENT_ID` | `BOT-PL-001` |
| Live per league | `BOT-{CODE}-live` (e.g. `BOT-SPL-live`) |
| `SCORE_MODEL_CONFIG_ID` | `bot-score-model-v1` |
| `SCORE_MODEL_ENGINE` | `score_model` |
| `BOT_BACKTEST_SEASONS` | `(2024, 2025, 2026)` |
| `PROSPECTIVE_LEAD_DAYS` | `1` |

---

*End of blueprint. When authoring `SKILL.md`, encode section 6 as hard rules and section 7 as the mandatory verification protocol.*
