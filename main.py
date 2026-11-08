"""Long polling for Telegram commands.

Polling only waits for a message. It does not schedule predictions, prefetch
fixtures, or scan a competition.
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
    fetcher = DataFetcher(api_key=config.football_api_key, cache_dir=config.cache_dir)
    predictor = Predictor(api_key=config.gemini_api_key, model=config.gemini_model)
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
    application = build_application(config, build_pipeline(config))
    application.run_polling()


if __name__ == "__main__":
    main()
