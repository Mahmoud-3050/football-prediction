"""Environment configuration. Loading this module does not call external APIs."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

REQUIRED_KEYS = ("BOT_TOKEN", "FOOTBALL_API_KEY", "GEMINI_API_KEY")
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_GEMINI_FALLBACKS = ("gemini-3.8-flash", "gemini-3.5-flash-lite")
_MODEL_ID = re.compile(r"^gemini-[a-z0-9]+(?:[.\-][a-z0-9]+)*$")
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
    gemini_fallback_models: tuple[str, ...]
    log_level: str
    database_path: Path
    cache_dir: Path


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

    database_path = Path(source.get("DATABASE_PATH") or DEFAULT_DATABASE_PATH)
    gemini_model = normalize_gemini_model(source.get("GEMINI_MODEL") or "")
    return Config(
        bot_token=source["BOT_TOKEN"].strip(),
        football_api_key=source["FOOTBALL_API_KEY"].strip(),
        gemini_api_key=source["GEMINI_API_KEY"].strip(),
        gemini_model=gemini_model,
        gemini_fallback_models=_fallback_models(
            source.get("GEMINI_FALLBACK_MODELS"),
            preferred=gemini_model,
        ),
        log_level=(source.get("LOG_LEVEL") or DEFAULT_LOG_LEVEL).strip() or DEFAULT_LOG_LEVEL,
        database_path=database_path,
        cache_dir=database_path.parent / "cache",
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
