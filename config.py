"""Environment configuration. Loading this module does not call external APIs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

REQUIRED_KEYS = ("BOT_TOKEN", "FOOTBALL_API_KEY", "GEMINI_API_KEY")
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_DATABASE_PATH = "data/predictions.db"
DEFAULT_LOG_LEVEL = "INFO"


class ConfigError(Exception):
    """Raised at startup when a required key is missing."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    football_api_key: str
    gemini_api_key: str
    gemini_model: str
    log_level: str
    database_path: Path
    cache_dir: Path


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    """Read process configuration.

    A missing required key fails here. No football, Gemini, or Telegram call is made.
    """

    if environ is None:
        load_dotenv()
        source: Mapping[str, str] = os.environ
    else:
        source = environ

    missing = [key for key in REQUIRED_KEYS if not _present(source, key)]
    if missing:
        raise ConfigError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    database_path = Path(source.get("DATABASE_PATH") or DEFAULT_DATABASE_PATH)
    return Config(
        bot_token=source["BOT_TOKEN"].strip(),
        football_api_key=source["FOOTBALL_API_KEY"].strip(),
        gemini_api_key=source["GEMINI_API_KEY"].strip(),
        gemini_model=(source.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip()
        or DEFAULT_GEMINI_MODEL,
        log_level=(source.get("LOG_LEVEL") or DEFAULT_LOG_LEVEL).strip() or DEFAULT_LOG_LEVEL,
        database_path=database_path,
        cache_dir=database_path.parent / "cache",
    )


def _present(source: Mapping[str, str], key: str) -> bool:
    value = source.get(key)
    return bool(value and str(value).strip())
