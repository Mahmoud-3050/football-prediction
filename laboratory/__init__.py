"""Historical testing laboratory. Separate from the Telegram bot."""

EXPERIMENT_ID = "EXP-PL-001"
BOT_EXPERIMENT_ID = "BOT-PL-001"
COMPETITION = "PL"
K = 6.0
TAIL_TOLERANCE = 0.001
LOG_LOSS_FLOOR = 1e-6
SUM_TOLERANCE = 0.01
SCHEMA_VERSION = "gemini-schema-exp-pl-001"
PROMPT_VERSION = "prompt-exp-pl-001"
POISSON_CONFIG_ID = "poisson-exp-pl-001"
GEMINI_CONFIG_ID = "gemini-exp-pl-001"
SCORE_MODEL_CONFIG_ID = "bot-score-model-v1"
PREFERRED_MODEL = "gemini-3.5-flash"
FALLBACK_MODELS = ("gemini-3.8-flash", "gemini-3.5-flash-lite")
BOT_BACKTEST_SEASONS = (2024, 2025, 2026)
# Live bot pre-round job lead window (days before kickoff). Lab walk-forward still
# skips unfinished matches; on-demand /predict does not use this window.
PROSPECTIVE_LEAD_DAYS = 1

# Lab Experiment modules (lazy import to avoid circular deps)
def __getattr__(name: str):
    if name == "ExperimentConfig":
        from laboratory.lab_experiment import ExperimentConfig
        return ExperimentConfig
    if name == "make_model_id":
        from laboratory.lab_experiment import make_model_id
        return make_model_id
    if name == "run_experiment":
        from laboratory.lab_runner import run_experiment
        return run_experiment
    if name == "compare_to_baseline":
        from laboratory.lab_analysis import compare_to_baseline
        return compare_to_baseline
    if name == "calibration_analysis":
        from laboratory.lab_analysis import calibration_analysis
        return calibration_analysis
    if name == "score_distribution_analysis":
        from laboratory.lab_analysis import score_distribution_analysis
        return score_distribution_analysis
    if name == "promote_best_config":
        from laboratory.lab_promotion import promote_best_config
        return promote_best_config
    if name == "rollback_promotion":
        from laboratory.lab_promotion import rollback_promotion
        return rollback_promotion
    if name == "PromotionGuardrails":
        from laboratory.lab_promotion import PromotionGuardrails
        return PromotionGuardrails
    raise AttributeError(f"module 'laboratory' has no attribute '{name}'")
