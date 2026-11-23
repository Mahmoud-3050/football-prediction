---
description: Rules for score_model math, Poisson/Dixon-Coles, draw calibration, eligibility abstinence, and per-league stack isolation.
trigger: glob
globs: "**/modeling/**/*.py, **/domain/**/*.py, **/live/eligibility.py, **/configs/**/*.json, **/data/spl_stack.json, **/tests/test_score_model.py, **/tests/test_league_stacks.py, **/tests/test_golden_master.py, **/tests/test_modeling_and_gradients.py"
---

# Math, Draw Calibration & League Stacks Rules

## 1. Pure Math & Validation Boundaries
- **`modeling/score_model.py` & `modeling/*` / `domain/*`:** Pure math & vectorized NumPy feature/model logic (`estimate_score`, `estimate_from_lambdas`, `select_1x2_outcome`, `_boost_draw_scorelines`, `BaseScoreModel`, `ShrinkageOnePassModel`, `ShrinkageJointModel`, `JointDixonColesMLEModel`). **FORBIDDEN:** Gemini, Telegram, network, or `sqlite3` / DB access.
- **`live/eligibility.py`:** Gate checks only (`MINIMUM_SCORED_MATCHES=3`, `FORM_WINDOW=5`). **FORBIDDEN:** filling missing stats/standings with zeros; gaps stay empty and trigger abstain.
- **Formulas & Constants:** Shrinkage `r_shrunk = (n·r + k·prior) / (n + k)` (`K=6`), `TAIL_TOLERANCE=0.001`, `MAX_GRID_GOALS=25`, `MINIMUM_HALF_TIME_SAMPLES=3`, `MIN_RHO_TRAINING_MATCHES=30`. Home advantage comes from league home vs. away averages, not an extra constant.

## 2. Draw Calibration vs. Pick Rules (CRITICAL)
- `draw_boost` / `draw_weights` / `draw_tightness_*` alter **diagonal probability mass** (affects log-loss, Brier, exact score, O/U).
- `draw_pick_threshold` / `draw_pick_margin` (`select_1x2_outcome`) change **`most_likely` selection ONLY**, not the probability vector.
- **FORBIDDEN:** Boosting diagonal matrix mass to "fix" draws when the promoted SPL stack uses pick rules at `draw_boost=1.0`.
- **FORBIDDEN:** Promoting a uniform high `draw_boost` across all diagonal cells (including 4-4) for SPL without walk-forward comparison.
- **Confidence Semantics:** FT 1X2 confidence = `P(selected 1X2)`; score confidence = `P(mode cell)`. Never swap them.

## 3. Per-League Isolation & Stack Contract
- Peer leagues (`PL`, `CL`, `PD`, `SA`, `BL1`, `FL1`, `PPL`, `DED`) must keep `DEFAULT_LIVE_STACK` unless `configs/live/{code}_stack.json` exists.
- **FORBIDDEN:** Applying SPL draw-pick rules globally or editing PL stack while changing SPL. Touch only `live_stack_for("SPL")` / SPL stack path.
- `league_stacks.live_stack_for` must keep working when `configs/live/` is absent (fallback to `DEFAULT_LIVE_STACK` / `data/spl_stack.json`).
- Pass stack knobs exclusively via `league_stacks.draw_calibration_kwargs(stack)` → `estimate_score` / `price_from_snapshot` / `run_score_model`.
- `stack["code_version"]` (default `"score_model_v1"`) travels with the stack. Do not add keys to `DEFAULT_LIVE_STACK` without reading `league_stacks.py` and updating tests.
- Atomic stack writes only (`save_live_stack` via temp file + `os.replace`).

## 4. Verification
Run: `.venv/bin/python -m pytest tests/test_score_model.py tests/test_league_stacks.py -q`