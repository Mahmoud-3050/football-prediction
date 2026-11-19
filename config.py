"""Environment configuration. Loading this module does not call external APIs."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

from db_paths import DEFAULT_DATABASE_PATH, resolve_database_path

REQUIRED_KEYS = ("BOT_TOKEN", "FOOTBALL_API_KEY", "GEMINI_API_KEY")
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_GEMINI_FALLBACKS = ("gemini-3.8-flash", "gemini-3.5-flash-lite")
_MODEL_ID = re.compile(r"^gemini-[a-z0-9]+(?:[.\-][a-z0-9]+)*$")
DEFAULT_LOG_LEVEL = "INFO"
# Pre-round inside the Telegram process is off by default; use `python -m jobs`.
DEFAULT_PRE_ROUND_ENABLED = False
DEFAULT_PRE_ROUND_INTERVAL_SECONDS = 3600


class ConfigError(Exception):
    """Raised at startup when a required key is missing."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    football_api_key: str
    ninjas_api_key: str
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_models: tuple[str, ...]
    log_level: str
    database_path: Path
    cache_dir: Path
    pre_round_enabled: bool
    pre_round_interval_seconds: float


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    """Read process configuration.

    A missing required key fails here. No football, Gemini, or Telegram call is made.
    """

    if environ is None:
        # Prefer .env over a stale shell export of GEMINI_MODEL or similar.
        load_dotenv(override=True)
        source: Mapping[str, str] = os.environ
    else:
        source = environ

    missing = [key for key in REQUIRED_KEYS if not _present(source, key)]
    if missing:
        raise ConfigError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    database_path = resolve_database_path(dict(source))
    gemini_model = normalize_gemini_model(source.get("GEMINI_MODEL") or "")
    return Config(
        bot_token=source["BOT_TOKEN"].strip(),
        football_api_key=source["FOOTBALL_API_KEY"].strip(),
        ninjas_api_key=(source.get("NINJAS_API_KEY") or "").strip(),
        gemini_api_key=source["GEMINI_API_KEY"].strip(),
        gemini_model=gemini_model,
        gemini_fallback_models=_fallback_models(
            source.get("GEMINI_FALLBACK_MODELS"),
            preferred=gemini_model,
        ),
        log_level=(source.get("LOG_LEVEL") or DEFAULT_LOG_LEVEL).strip() or DEFAULT_LOG_LEVEL,
        database_path=database_path,
        cache_dir=database_path.parent / "cache",
        pre_round_enabled=_bool(
            source.get("PRE_ROUND_ENABLED"),
            default=DEFAULT_PRE_ROUND_ENABLED,
        ),
        pre_round_interval_seconds=_positive_float(
            source.get("PRE_ROUND_INTERVAL_SECONDS"),
            default=float(DEFAULT_PRE_ROUND_INTERVAL_SECONDS),
        ),
    )


def normalize_gemini_model(value: str) -> str:
    """Turn a display name or models/ prefix into a Gemini API model id."""

    text = value.strip()
    if text.lower().startswith("models/"):
        text = text.split("/", 1)[1].strip()
    if _MODEL_ID.fullmatch(text):
        return text
    if re.match(r"(?i)^gemini[-\s]+", text):
        text = re.sub(r"(?i)^gemini[-\s]+", "", text).strip()
    slug = re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        return DEFAULT_GEMINI_MODEL
    if not slug.startswith("gemini-"):
        slug = f"gemini-{slug}"
    return slug


def _fallback_models(raw: str | None, *, preferred: str) -> tuple[str, ...]:
    if raw is None or not str(raw).strip():
        candidates = DEFAULT_GEMINI_FALLBACKS
    else:
        candidates = tuple(
            normalize_gemini_model(part)
            for part in str(raw).split(",")
            if part.strip()
        )
    ordered: list[str] = []
    seen: set[str] = set()
    for model in (preferred, *candidates):
        if model and model not in seen:
            seen.add(model)
            ordered.append(model)
    # Preferred is first; fallbacks are the rest.
    return tuple(ordered[1:])


def _present(source: Mapping[str, str], key: str) -> bool:
    value = source.get(key)
    return bool(value and str(value).strip())


def _bool(raw: str | None, *, default: bool) -> bool:
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


def _positive_float(raw: str | None, *, default: float) -> float:
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        return default
    return value if value > 0 else default
