"""Validation for gemini-schema-exp-pl-001.

The validator reads numbers from the structured fields. Analysis text cannot
fill a missing probability.
"""

from __future__ import annotations

from typing import Any

from laboratory import SUM_TOLERANCE

_RESULTS = ("home", "draw", "away")
_SIDES = ("over", "under")


class SchemaError(ValueError):
    """The Gemini body does not match the locked schema."""


def validate_gemini_body(body: dict[str, Any], *, half_time_required: bool) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise SchemaError("Gemini response is not an object.")
    full_time = _triple(body.get("full_time_1x2"), "full_time_1x2")
    _label(body.get("full_time_most_likely"), full_time, "full_time_most_likely", _RESULTS)
    scores = _exact_scores(body.get("exact_scores"))
    _exact_mode(body.get("exact_score_most_likely"), scores)
    _pair(body.get("over_under_2_5"), body.get("over_under_2_5_most_likely"), "over_under_2_5")
    _pair(body.get("over_under_3_5"), body.get("over_under_3_5_most_likely"), "over_under_3_5")
    analysis = body.get("analysis")
    if not isinstance(analysis, str) or not analysis.strip():
        raise SchemaError("analysis must be a non-empty string.")
    withheld = body.get("half_time_withheld")
    if not isinstance(withheld, bool):
        raise SchemaError("half_time_withheld must be a boolean.")
    if half_time_required and withheld:
        raise SchemaError("Half-time history is present, so half-time cannot be withheld.")
    if not half_time_required and not withheld:
        raise SchemaError("Half-time history is absent, so half-time must be withheld.")
    if withheld:
        reason = body.get("half_time_withheld_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise SchemaError("A withheld half-time market needs a reason.")
    else:
        half_time = _triple(body.get("half_time_1x2"), "half_time_1x2")
        _label(body.get("half_time_most_likely"), half_time, "half_time_most_likely", _RESULTS)
        if body.get("half_time_withheld_reason") not in ("", None):
            raise SchemaError("half_time_withheld_reason must be empty when half-time is priced.")
    return body


def _triple(value: Any, name: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise SchemaError(f"{name} must be an object.")
    probabilities = {key: _probability(value.get(key), f"{name}.{key}") for key in _RESULTS}
    _sum_to_one(probabilities.values(), name)
    return probabilities


def _pair(probabilities: Any, label: Any, name: str) -> None:
    if not isinstance(probabilities, dict):
        raise SchemaError(f"{name} must be an object.")
    values = {key: _probability(probabilities.get(key), f"{name}.{key}") for key in _SIDES}
    _sum_to_one(values.values(), name)
    if label not in _SIDES:
        raise SchemaError(f"{name}_most_likely must be over or under.")
    if label == "over" and values["over"] < values["under"]:
        raise SchemaError(f"{name}_most_likely does not match the probabilities.")
    if label == "under" and values["under"] < values["over"]:
        raise SchemaError(f"{name}_most_likely does not match the probabilities.")


def _exact_scores(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise SchemaError("exact_scores must be a non-empty list.")
    found: set[tuple[int, int]] = set()
    total = 0.0
    parsed: list[dict[str, Any]] = []
    for cell in value:
        if not isinstance(cell, dict):
            raise SchemaError("Each exact score must be an object.")
        home_goals = cell.get("home_goals")
        away_goals = cell.get("away_goals")
        if not isinstance(home_goals, int) or not isinstance(away_goals, int):
            raise SchemaError("Score goal counts must be integers.")
        if home_goals < 0 or away_goals < 0:
            raise SchemaError("Score goal counts must be non-negative.")
        probability = _probability(cell.get("probability"), "exact_scores.probability")
        key = (home_goals, away_goals)
        if key in found:
            raise SchemaError("exact_scores contains a duplicate score.")
        found.add(key)
        total += probability
        parsed.append({"home_goals": home_goals, "away_goals": away_goals, "probability": probability})
    for home_goals in range(11):
        for away_goals in range(11):
            if (home_goals, away_goals) not in found:
                raise SchemaError("exact_scores is missing a cell in the 0 through 10 rectangle.")
    _sum_to_one([total], "exact_scores")
    return parsed


def _exact_mode(value: Any, scores: list[dict[str, Any]]) -> None:
    if not isinstance(value, dict):
        raise SchemaError("exact_score_most_likely must be an object.")
    home_goals = value.get("home_goals")
    away_goals = value.get("away_goals")
    if not isinstance(home_goals, int) or not isinstance(away_goals, int):
        raise SchemaError("exact_score_most_likely goal counts must be integers.")
    best = sorted(
        scores,
        key=lambda cell: (
            -float(cell["probability"]),
            int(cell["home_goals"]) + int(cell["away_goals"]),
            int(cell["home_goals"]),
        ),
    )[0]
    if home_goals != best["home_goals"] or away_goals != best["away_goals"]:
        raise SchemaError("exact_score_most_likely is not the highest-probability cell.")


def _label(label: Any, probabilities: dict[str, float], name: str, allowed: tuple[str, ...]) -> None:
    if label not in allowed:
        raise SchemaError(f"{name} has an unknown label.")
    order = ("home", "draw", "away")
    best = max(order, key=lambda key: (probabilities[key], -order.index(key)))
    if label != best:
        raise SchemaError(f"{name} does not match the probabilities.")


def _probability(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{name} must be a number.")
    probability = float(value)
    if probability < 0 or probability > 1:
        raise SchemaError(f"{name} must be between 0 and 1.")
    return probability


def _sum_to_one(values, name: str) -> None:
    if abs(sum(values) - 1.0) > SUM_TOLERANCE:
        raise SchemaError(f"{name} probabilities must sum to 1 within 0.01.")
