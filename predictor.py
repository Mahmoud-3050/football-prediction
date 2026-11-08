"""Gemini score prediction and the markets derived from that score.

The model is asked for one structured score. 1X2, both teams to score, and
over/under 2.5 are computed afterwards. Invalid JSON or a half-time label that
disagrees with its score is retried once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable

from models import (
    AnalysisSnapshot,
    Markets,
    ModelError,
    ProducedPrediction,
    ScorePrediction,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
SCHEMA_VERSION = "v1"
SCORE_RE = re.compile(r"^(\d+)-(\d+)$")
HALF_TIME_LABELS = {"Team A": "home", "Team B": "away", "Draw": "draw"}

Generate = Callable[[str, dict[str, Any]], Awaitable[str]]


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


def winner_label(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "Team A"
    if home_goals < away_goals:
        return "Team B"
    return "Draw"


def build_schema(include_half_time: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "full_time_score": {"type": "STRING"},
        "confidence_percentage": {"type": "INTEGER"},
        "tactical_reasoning": {"type": "STRING"},
    }
    required = ["full_time_score", "confidence_percentage", "tactical_reasoning"]
    if include_half_time:
        properties["half_time_prediction"] = {
            "type": "STRING",
            "enum": ["Team A", "Team B", "Draw"],
        }
        properties["half_time_score"] = {"type": "STRING"}
        required = ["half_time_prediction", "half_time_score", *required]
    return {"type": "OBJECT", "properties": properties, "required": required}


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


def build_prompt(snapshot: AnalysisSnapshot, *, previous_error: str | None = None) -> str:
    payload = snapshot.payload()
    if not snapshot.include_half_time:
        payload = redact_half_time(payload)
    home = snapshot.home_team_name or snapshot.request_home
    away = snapshot.away_team_name or snapshot.request_away
    if snapshot.include_half_time:
        half_time_rules = (
            "Also return half_time_prediction as exactly Team A, Team B, or Draw, "
            "and half_time_score as N-N. Team A is the home club and Team B is the away club. "
            "The half-time prediction must be the winner of the half-time score, or Draw when the score is level.\n"
        )
    else:
        half_time_rules = (
            "Do not return a half-time prediction or a half-time score. "
            "Half-time data in this snapshot is not enough for that market.\n"
        )
    correction = ""
    if previous_error:
        correction = (
            "\nThe previous response was rejected: "
            f"{previous_error}\nReturn a corrected JSON object.\n"
        )
    return (
        "Estimate one football score from the snapshot JSON only.\n"
        f"Team A is the home club {home}. Team B is the away club {away}.\n"
        "Use only these facts. Do not invent results, table rows, injuries, or odds.\n"
        "record is wins-draws-losses. results are ordered most recent first.\n"
        "Return JSON with full_time_score as N-N (home goals-away goals), "
        "confidence_percentage as an integer from 0 to 100 for that scoreline, "
        "and tactical_reasoning citing only the snapshot.\n"
        f"{half_time_rules}"
        "Markets such as 1X2 are not part of your answer.\n"
        f"{correction}\n"
        "SNAPSHOT:\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )


def validate_prediction(payload: Any, *, include_half_time: bool) -> ScorePrediction:
    if not isinstance(payload, dict):
        raise ValueError("The model response was not a JSON object.")
    home_goals, away_goals = require_score(payload.get("full_time_score"), "full_time_score")
    confidence = _confidence(payload.get("confidence_percentage"))
    reasoning = payload.get("tactical_reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ValueError("tactical_reasoning must be a non-empty string.")

    half_label: str | None = None
    half_score: str | None = None
    if include_half_time:
        half_label = _canonical_label(payload.get("half_time_prediction"))
        half_home, half_away = require_score(payload.get("half_time_score"), "half_time_score")
        expected = winner_label(half_home, half_away)
        if half_label != expected:
            raise ValueError(
                f"half-time score {half_home}-{half_away} disagrees with half-time result {half_label}."
            )
        half_score = f"{half_home}-{half_away}"

    return ScorePrediction(
        half_time_prediction=half_label,
        half_time_score=half_score,
        full_time_score=f"{home_goals}-{away_goals}",
        confidence_percentage=confidence,
        tactical_reasoning=reasoning.strip(),
    )


class Predictor:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        generate: Generate | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self._generate = generate
        self._client: Any = None

    async def predict(
        self,
        snapshot: AnalysisSnapshot,
        *,
        limitations: list[str],
    ) -> ProducedPrediction:
        schema = build_schema(snapshot.include_half_time)
        last_error = "The model returned an invalid prediction."
        previous_error: str | None = None
        for attempt in (1, 2):
            prompt = build_prompt(snapshot, previous_error=previous_error)
            raw_text = await self._call(prompt, schema)
            try:
                if not raw_text or not str(raw_text).strip():
                    raise ValueError("The model returned an empty response.")
                prediction = validate_prediction(
                    coerce_json(raw_text),
                    include_half_time=snapshot.include_half_time,
                )
            except ValueError as exc:
                last_error = str(exc)
                previous_error = last_error
                logger.info("model attempt %s rejected: %s", attempt, last_error)
                continue
            return ProducedPrediction(
                prediction=prediction,
                markets=derive_markets(prediction.full_time_score),
                version_number=attempt,
                model_id=self.model,
                prompt_version=PROMPT_VERSION,
                schema_version=SCHEMA_VERSION,
                limitations=list(limitations),
            )
        raise ModelError(
            f"The model returned an invalid prediction twice. Last error: {last_error}"
        )

    async def _call(self, prompt: str, schema: dict[str, Any]) -> str:
        if self._generate is not None:
            return await self._generate(prompt, schema)
        return await asyncio.to_thread(self._generate_sync, prompt, schema)

    def _generate_sync(self, prompt: str, schema: dict[str, Any]) -> str:
        from google import genai
        from google.genai import types

        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type="application/json",
                    response_schema=schema,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
        except Exception as exc:
            raise ModelError(f"The model request failed ({_redact(exc, self.api_key)}).") from exc
        text = getattr(response, "text", None)
        return text or ""


def coerce_json(raw_text: str) -> Any:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("The model response was not valid JSON.") from exc


def _confidence(value: Any) -> int:
    if isinstance(value, bool) or isinstance(value, str):
        raise ValueError("confidence_percentage must be an integer from 0 to 100.")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("confidence_percentage must be an integer from 0 to 100.")
        value = int(value)
    if not isinstance(value, int) or not 0 <= value <= 100:
        raise ValueError("confidence_percentage must be an integer from 0 to 100.")
    return value


def _redact(exc: Exception, secret: str) -> str:
    message = str(exc)
    if secret:
        message = message.replace(secret, "[redacted]")
    return message[:400]


def _canonical_label(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("half_time_prediction must be Team A, Team B, or Draw.")
    for label in HALF_TIME_LABELS:
        if value.strip().casefold() == label.casefold():
            return label
    raise ValueError("half_time_prediction must be Team A, Team B, or Draw.")
