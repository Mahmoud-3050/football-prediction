"""Score a stored prediction against one result revision.

Metrics attach beside the prediction. This module does not change probabilities.
"""

from __future__ import annotations

import math
from typing import Any

from laboratory import LOG_LOSS_FLOOR


def _nested_engine(body: dict[str, Any]) -> bool:
    return body.get("engine") in ("poisson", "score_model")


def evaluate_prediction(body: dict[str, Any], result: dict[str, int | None]) -> dict[str, Any]:
    actual_ft = _result_label(int(result["ft_home"]), int(result["ft_away"]))
    actual_total = int(result["ft_home"]) + int(result["ft_away"])
    metrics: dict[str, Any] = {
        "full_time_1x2": _multiclass(
            body["full_time"]["result_1x2"] if _nested_engine(body) else body["full_time_1x2"],
            _selection(body, "full_time"),
            actual_ft,
        ),
        "exact_score": _exact(body, int(result["ft_home"]), int(result["ft_away"])),
        "over_under_2_5": _binary(
            _over_under(body, "2_5"),
            "over" if actual_total >= 3 else "under",
        ),
        "over_under_3_5": _binary(
            _over_under(body, "3_5"),
            "over" if actual_total >= 4 else "under",
        ),
    }
    half_time = _half_time_block(body)
    if result.get("ht_home") is None or result.get("ht_away") is None or half_time is None:
        metrics["half_time_1x2"] = {"status": "withheld"}
    else:
        metrics["half_time_1x2"] = _multiclass(
            half_time["probabilities"],
            half_time["selection"],
            _result_label(int(result["ht_home"]), int(result["ht_away"])),
        )
    return metrics


def _half_time_block(body: dict[str, Any]) -> dict[str, Any] | None:
    if _nested_engine(body):
        market = body["half_time"]
        if market.get("withheld"):
            return None
        return {"probabilities": market["result_1x2"], "selection": market["result_1x2"]["most_likely"]}
    if body.get("half_time_withheld"):
        return None
    return {
        "probabilities": body["half_time_1x2"],
        "selection": body["half_time_most_likely"],
    }


def _selection(body: dict[str, Any], market: str) -> str:
    if _nested_engine(body):
        return str(body["full_time"]["result_1x2"]["most_likely"])
    return str(body[f"{market}_most_likely"])


def _over_under(body: dict[str, Any], line: str) -> dict[str, Any]:
    if _nested_engine(body):
        return body["full_time"][f"over_under_{line}"]
    probabilities = body[f"over_under_{line}"]
    return {
        "over": probabilities["over"],
        "under": probabilities["under"],
        "most_likely": body[f"over_under_{line}_most_likely"],
    }


def _multiclass(probabilities: dict[str, Any], selection: str, actual: str) -> dict[str, Any]:
    classes = ("home", "draw", "away")
    probability = float(probabilities[actual])
    brier = sum(
        (float(probabilities[label]) - (1.0 if label == actual else 0.0)) ** 2 for label in classes
    )
    return {
        "status": "scored",
        "selection": selection,
        "actual": actual,
        "correct": selection == actual,
        "probability_of_actual": probability,
        "log_loss": _log_loss(probability),
        "brier": brier,
    }


def _binary(market: dict[str, Any], actual: str) -> dict[str, Any]:
    probability = float(market[actual])
    selection = str(market["most_likely"])
    return {
        "status": "scored",
        "selection": selection,
        "actual": actual,
        "correct": selection == actual,
        "probability_of_actual": probability,
        "log_loss": _log_loss(probability),
        "brier": (probability - 1.0) ** 2,
    }


def _exact(body: dict[str, Any], home_goals: int, away_goals: int) -> dict[str, Any]:
    if _nested_engine(body):
        cells = body["full_time"]["exact_scores"]
        mode = body["full_time"]["exact_score_most_likely"]
    else:
        cells = body["exact_scores"]
        mode = body["exact_score_most_likely"]
    probability = 0.0
    for cell in cells:
        if int(cell["home_goals"]) == home_goals and int(cell["away_goals"]) == away_goals:
            probability = float(cell["probability"])
            break
    hit = int(mode["home_goals"]) == home_goals and int(mode["away_goals"]) == away_goals
    return {
        "status": "scored",
        "correct": hit,
        "probability_of_actual": probability,
        "log_loss": _log_loss(probability),
    }


def _result_label(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home"
    if home_goals == away_goals:
        return "draw"
    return "away"


def _log_loss(probability: float) -> float:
    return -math.log(max(probability, LOG_LOSS_FLOOR))
