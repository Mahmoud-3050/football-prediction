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
