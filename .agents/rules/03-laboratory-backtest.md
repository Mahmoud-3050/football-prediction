---
description: Rules for walk-forward backtesting, leakage prevention, insert-only predictions, experiment grids, and promotion/rollback.
trigger: glob
globs: "**/laboratory/**/*.py, !**/laboratory/dashboard.py, !**/laboratory/auth.py, !**/laboratory/team_display.py, **/experiments/**/*.yaml, **/evaluate_models*.py, **/tests/test_laboratory.py, **/tests/test_lab_*.py"
---

# Laboratory, Backtesting & Experiments Rules

## 1. Lab Isolation Boundaries
- **FORBIDDEN:** `laboratory/*` starting the Telegram bot or polling.
- **FORBIDDEN:** Lab pricing (`score_model_adapter.py`, `poisson_engine.py`, `job.run_score_model`, `job.run_poisson`) calling Gemini.
- **FORBIDDEN:** Merging bot `prediction_*` tables into lab `predictions` (or vice versa).
- **Promotion Safety:** **FORBIDDEN** running `tune-spl --promote` or `lab-promote` unless the user explicitly requested promotion. Default to compare-only / `--dry-run`.

## 2. Leakage Prevention & Walk-Forward Integrity (CRITICAL)
- **Snapshot Leakage:** Target match FT/HT score and target `match_id` are **FORBIDDEN** in snapshot payloads/priors. `laboratory/snapshot.leakage_status` must fail if `ft_home`/`score` appear on snapshot root or any prior has `kickoff >= cutoff`.
- **Strict Prior Window:** Priors = **same competition AND same season AND strictly earlier kickoff** (`build_snapshot`). Never add future or cross-league matches into `snapshot["prior_matches"]`.
- **CL-Only Prior Exception:** For `CL` only, `laboratory/cl_features.py` may compute pre-cutoff (`kickoff < target.kickoff`) Persistent Multi-Season CL + Elo and Domestic Prior `prior_means` without modifying `snapshot["prior_matches"]`.
- **Last-Season Ratio:** When `prior_mean="last_season"`, use the separate cache path in `job.py`; never leak current-match data into that fit.
- **Finished-Only Batch:** `run_score_model` / `run_poisson` skip unfinished matches.

## 3. Insert-Only Predictions & Evaluation Semantics
- **Insert-Only:** `predictions` has UNIQUE `(experiment_id, snapshot_id, engine, model_id)`. To re-price, call `delete_experiment_engine_outputs(experiment_id, engine)` first—never re-insert blindly.
- **Evaluation:** `evaluate_prediction` (`LOG_LOSS_FLOOR=1e-6`) only scores stored bodies against `result_revisions`; it never rewrites probabilities. Exact-score log-loss uses the probability of the **actual** score cell, not the mode cell.
- **Metric Reporting:** Never claim metrics without querying `evaluations` or running walk-forward; always cite `experiment_id` and sample count `n`.
- **CLI Verification:** Read `argparse` in `laboratory/__main__.py` before using flags for `lab-analyze`, `lab-promote`, or `lab-rollback`.

## 4. Verification
Run: `.venv/bin/python -m pytest tests/test_laboratory.py tests/test_lab_experiment.py -q`