"""Gemini reasoning for a score the Dixon-Coles model already chose.

The model is asked for tactical_reasoning only. Scores, confidence, and
markets come from the score distribution. Invalid JSON is retried once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from typing import Any, Awaitable, Callable

from models import (
    AnalysisSnapshot,
    Markets,
    ModelError,
    ProducedPrediction,
    ScorePrediction,
)
from score_model import ScoreEstimate

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v2"
SCHEMA_VERSION = "v2"
SCORE_RE = re.compile(r"^(\d+)-(\d+)$")
TRANSIENT_MARKERS = (
    "503",
    "429",
    "unavailable",
    "high demand",
    "resource_exhausted",
    "resource exhausted",
    "timed out",
    "timeout",
    "temporarily",
)
PROVIDER_RETRIES = 2
RETRY_BASE_SECONDS = 1.5
CALL_TIMEOUT_SECONDS = 25
HTTP_TIMEOUT_MS = 20_000

Generate = Callable[[str, dict[str, Any]], Awaitable[str]]


def statistical_reasoning(estimate: ScoreEstimate) -> str:
    markets = estimate.markets
    parts = [
        (
            f"Full-time score {estimate.full_time_score} comes from the Dixon-Coles "
            f"statistical model (λ_home={estimate.lambda_home:.2f}, "
            f"λ_away={estimate.lambda_away:.2f})."
        ),
        (
            f"1X2 {markets.result_1x2} ({markets.result_probability}%), "
            f"BTTS {markets.btts} ({markets.btts_probability}%), "
            f"O/U 2.5 {markets.over_under_25} ({markets.over_under_probability}%)."
        ),
    ]
    if estimate.half_time_score and estimate.half_time_prediction:
        parts.append(
            f"Half-time {estimate.half_time_score} ({estimate.half_time_prediction})."
        )
    parts.append("Gemini reasoning was unavailable for this request.")
    return " ".join(parts)


def derive_markets(full_time_score: str) -> Markets:
    home_goals, away_goals = require_score(full_time_score, "full_time_score")
    if home_goals > away_goals:
        result = "Home"
    elif home_goals < away_goals:
        result = "Away"
    else:
        result = "Draw"
    btts = "Yes" if home_goals > 0 and away_goals > 0 else "No"
    total = home_goals + away_goals
    over_under = "Over" if total > 2.5 else "Under"
    return Markets(result_1x2=result, btts=btts, over_under_25=over_under)


def require_score(value: Any, field_name: str) -> tuple[int, int]:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string like 2-1.")
    match = SCORE_RE.match(value.strip().replace(" ", ""))
    if match is None:
        raise ValueError(f"{field_name} must be a score like 2-1.")
    return int(match.group(1)), int(match.group(2))


def build_schema() -> dict[str, Any]:
    return {
        "type": "OBJECT",
        "properties": {"tactical_reasoning": {"type": "STRING"}},
        "required": ["tactical_reasoning"],
    }


_HALF_TIME_PREFIXES = ("half_time", "second_half")


def redact_half_time(value: Any) -> Any:
    """Drop half-time facts from the object the model is allowed to see."""

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key == "halfTime" or key.startswith(_HALF_TIME_PREFIXES):
                continue
            cleaned[key] = redact_half_time(item)
        return cleaned
    if isinstance(value, list):
        return [redact_half_time(item) for item in value]
    return value


def build_prompt(
    snapshot: AnalysisSnapshot,
    estimate: ScoreEstimate,
    *,
    previous_error: str | None = None,
) -> str:
    payload = snapshot.payload()
    if not snapshot.include_half_time:
        payload = redact_half_time(payload)
    home = snapshot.home_team_name or snapshot.request_home
    away = snapshot.away_team_name or snapshot.request_away
    if estimate.half_time_score and estimate.half_time_prediction:
        half_time_rules = (
            f"Half-time score: {estimate.half_time_score} ({estimate.half_time_prediction}). "
            "Team A is the home club and Team B is the away club.\n"
        )
    else:
        half_time_rules = (
            "Do not return a half-time prediction or a half-time score. "
            "Half-time is not part of this estimate.\n"
        )
    markets = estimate.markets
    correction = ""
    if previous_error:
        correction = (
            "\nThe previous response was rejected: "
            f"{previous_error}\nReturn a corrected JSON object.\n"
        )
    return (
        "Explain the score_model estimate. Do not invent a different score.\n"
        f"Team A is the home club {home}. Team B is the away club {away}.\n"
        "Use only the snapshot and this estimate. Do not invent results, table rows, injuries, or odds.\n"
        "record is wins-draws-losses. results are ordered most recent first.\n"
        f"Full-time score: {estimate.full_time_score}. "
        f"lambda_home {estimate.lambda_home:.4f}. lambda_away {estimate.lambda_away:.4f}. "
        f"1X2 {markets.result_1x2} ({markets.result_probability}%). "
        f"BTTS {markets.btts} ({markets.btts_probability}%). "
        f"Over/Under 2.5 {markets.over_under_25} ({markets.over_under_probability}%).\n"
        "Return JSON with tactical_reasoning citing only these facts.\n"
        f"{half_time_rules}"
        "Markets such as 1X2 are not part of your answer.\n"
        f"{correction}\n"
        "ESTIMATE:\n"
        f"{json.dumps(estimate.public(), ensure_ascii=False, sort_keys=True)}\n"
        "SNAPSHOT:\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )


def validate_reasoning(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("The model response was not a JSON object.")
    reasoning = payload.get("tactical_reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ValueError("tactical_reasoning must be a non-empty string.")
    return reasoning.strip()


class Predictor:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        fallback_models: Sequence[str] | None = None,
        generate: Generate | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.fallback_models = tuple(fallback_models or ())
        self._generate = generate
        self._sleep = sleep or asyncio.sleep
        self._client: Any = None
        self._last_model_used = model

    def model_candidates(self) -> tuple[str, ...]:
        ordered: list[str] = []
        seen: set[str] = set()
        for model in (self.model, *self.fallback_models):
            if model and model not in seen:
                seen.add(model)
                ordered.append(model)
        return tuple(ordered)

    async def predict(
        self,
        snapshot: AnalysisSnapshot,
        estimate: ScoreEstimate,
        *,
        limitations: list[str],
    ) -> ProducedPrediction:
        schema = build_schema()
        last_error = "The model returned an invalid prediction."
        previous_error: str | None = None
        notes = list(limitations)
        try:
            for attempt in (1, 2):
                prompt = build_prompt(snapshot, estimate, previous_error=previous_error)
                raw_text = await self._call(prompt, schema)
                try:
                    if not raw_text or not str(raw_text).strip():
                        raise ValueError("The model returned an empty response.")
                    reasoning = validate_reasoning(coerce_json(raw_text))
                except ValueError as exc:
                    last_error = str(exc)
                    previous_error = last_error
                    logger.info("model attempt %s rejected: %s", attempt, last_error)
                    continue
                if self._last_model_used != self.model:
                    note = (
                        f"Primary model {self.model} was unavailable; "
                        f"used {self._last_model_used}."
                    )
                    if note not in notes:
                        notes.append(note)
                return self._produced(
                    estimate,
                    reasoning=reasoning,
                    version_number=attempt,
                    model_id=self._last_model_used,
                    notes=notes,
                )
            raise ModelError(
                f"The model returned an invalid prediction twice. Last error: {last_error}"
            )
        except ModelError as exc:
            logger.warning("gemini unavailable; using statistical reasoning: %s", exc)
            note = (
                "Gemini reasoning unavailable; score comes from the statistical model only."
            )
            if note not in notes:
                notes.append(note)
            return self._produced(
                estimate,
                reasoning=statistical_reasoning(estimate),
                version_number=1,
                model_id="score-model",
                notes=notes,
            )

    def _produced(
        self,
        estimate: ScoreEstimate,
        *,
        reasoning: str,
        version_number: int,
        model_id: str,
        notes: list[str],
    ) -> ProducedPrediction:
        return ProducedPrediction(
            prediction=ScorePrediction(
                half_time_prediction=estimate.half_time_prediction,
                half_time_score=estimate.half_time_score,
                full_time_score=estimate.full_time_score,
                confidence_percentage=estimate.confidence_percentage,
                tactical_reasoning=reasoning,
            ),
            markets=estimate.markets,
            version_number=version_number,
            model_id=model_id,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            limitations=notes,
            lambda_home=estimate.lambda_home,
            lambda_away=estimate.lambda_away,
            probabilities=dict(estimate.probabilities),
        )

    async def _call(self, prompt: str, schema: dict[str, Any]) -> str:
        if self._generate is not None:
            self._last_model_used = self.model
            return await self._generate(prompt, schema)

        last_error: ModelError | None = None
        candidates = self.model_candidates()
        for model_index, model in enumerate(candidates):
            for attempt in range(1, PROVIDER_RETRIES + 1):
                try:
                    text = await asyncio.wait_for(
                        asyncio.to_thread(self._generate_sync, prompt, schema, model),
                        timeout=CALL_TIMEOUT_SECONDS,
                    )
                    self._last_model_used = model
                    return text
                except asyncio.TimeoutError:
                    last_error = ModelError(
                        f"The model request timed out after {CALL_TIMEOUT_SECONDS}s ({model})."
                    )
                    logger.warning("%s; trying next model", last_error)
                    break
                except ModelError as exc:
                    last_error = exc
                    if not is_transient_model_error(exc):
                        raise
                    if attempt < PROVIDER_RETRIES:
                        wait = RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                        logger.warning(
                            "model %s attempt %s failed transiently: %s; waiting %.1fs",
                            model,
                            attempt,
                            exc,
                            wait,
                        )
                        await self._sleep(wait)
                        continue
                    if model_index + 1 < len(candidates):
                        logger.warning(
                            "model %s exhausted transient retries; trying %s",
                            model,
                            candidates[model_index + 1],
                        )
        if last_error is not None:
            raise last_error
        raise ModelError("The model request failed with no candidates.")

    def _generate_sync(self, prompt: str, schema: dict[str, Any], model: str) -> str:
        from google import genai
        from google.genai import types

        if self._client is None:
            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(timeout=HTTP_TIMEOUT_MS),
            )
        try:
            response = self._client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type="application/json",
                    response_schema=schema,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                    # No tools are registered; disable AFC so the SDK does not
                    # enter its tool-calling loop or log "AFC is enabled".
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            )
        except Exception as exc:
            raise ModelError(f"The model request failed ({_redact(exc, self.api_key)}).") from exc
        text = getattr(response, "text", None)
        return text or ""


def is_transient_model_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in TRANSIENT_MARKERS)


def coerce_json(raw_text: str) -> Any:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("The model response was not valid JSON.") from exc


def _redact(exc: Exception, secret: str) -> str:
    message = str(exc)
    if secret:
        message = message.replace(secret, "[redacted]")
    return message[:400]

