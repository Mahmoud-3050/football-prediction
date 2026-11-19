"""Long polling for Telegram commands.

Send /start in a chat to enable the bot there. Background tomorrow predictions
and standings refresh run separately via `python -m jobs`.
"""

from __future__ import annotations

import logging

from bot import build_application
from config import ConfigError, load_config
from data_fetcher import DataFetcher
from pipeline import Pipeline
from predictor import Predictor
from storage import Storage


def build_pipeline(config: object) -> Pipeline:
    from config import Config

    if not isinstance(config, Config):
        raise TypeError("config must be a Config instance.")
    fetcher = DataFetcher(
        api_key=config.football_api_key,
        cache_dir=config.cache_dir,
        ninjas_api_key=config.ninjas_api_key,
    )
    predictor = Predictor(
        api_key=config.gemini_api_key,
        model=config.gemini_model,
        fallback_models=config.gemini_fallback_models,
    )
    store = Storage(config.database_path)
    return Pipeline(fetcher, predictor, store)


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger(__name__).info(
        "Using Gemini model %s (fallbacks: %s)",
        config.gemini_model,
        ", ".join(config.gemini_fallback_models) or "none",
    )
    logging.getLogger(__name__).info(
        "Telegram bot waits for /start in each chat before other commands"
    )
    if config.pre_round_enabled:
        logging.getLogger(__name__).info(
            "Legacy in-process pre-round enabled (prefer: python -m jobs)"
        )
    application = build_application(config, build_pipeline(config))
    application.run_polling()


if __name__ == "__main__":
    main()
