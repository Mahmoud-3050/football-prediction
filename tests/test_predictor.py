import asyncio
import json

import pytest

from models import AnalysisSnapshot, ModelError
from predictor import (
    Predictor,
    build_prompt,
    build_schema,
    derive_markets,
    is_transient_model_error,
)
from score_model import ScoreEstimate, estimate_from_lambdas


def test_markets_match_the_score(markets_table) -> None:
    for row in markets_table:
        markets = derive_markets(row["score"])
        assert markets.result_1x2 == row["result_1x2"]
        assert markets.btts == row["btts"]
        assert markets.over_under_25 == row["over_under_25"]


def test_prompt_omits_half_time_when_that_market_is_ineligible() -> None:
    snapshot = _snapshot(include_half_time=False)
    estimate = _estimate(include_half_time=False)
    prompt = build_prompt(snapshot, estimate)
    schema = build_schema()
    assert "Do not return a half-time" in prompt
    assert "Half-time is not part of this estimate" in prompt
    assert schema["required"] == ["tactical_reasoning"]
    assert "Arsenal" in prompt
    assert "Chelsea" in prompt
    assert "lambda_home" in prompt
    assert "Do not invent a different score" in prompt
    assert "SNAPSHOT:" in prompt


def test_prompt_cites_the_computed_half_time_when_eligible() -> None:
    estimate = _estimate(include_half_time=True)
    prompt = build_prompt(_snapshot(include_half_time=True), estimate)
    schema = build_schema()
    assert "half_time_prediction" not in schema["required"]
    assert estimate.half_time_score is not None
    assert estimate.half_time_score in prompt
    assert "Do not invent a different score" in prompt


def test_retry_is_version_two_and_a_second_failure_stops() -> None:
    estimate = _estimate(True)

    async def once_then_valid(prompt: str, schema: dict) -> str:
        once_then_valid.calls += 1
        if once_then_valid.calls == 1:
            return "not-json"
        return json.dumps({"tactical_reasoning": "from the snapshot"})

    once_then_valid.calls = 0
    predictor = Predictor("key", "gemini-2.5-flash", generate=once_then_valid)
    produced = asyncio.run(
        predictor.predict(_snapshot(True), estimate, limitations=["none found"])
    )
    assert produced.version_number == 2
    assert produced.prediction.full_time_score == estimate.full_time_score
    assert produced.markets.result_1x2 == estimate.markets.result_1x2
    assert produced.prediction.tactical_reasoning == "from the snapshot"
    assert produced.model_id == "gemini-2.5-flash"
    assert produced.lambda_home == estimate.lambda_home

    async def always_bad(prompt: str, schema: dict) -> str:
        always_bad.calls += 1
        return "{}"

    always_bad.calls = 0
    failing = Predictor("key", "gemini-2.5-flash", generate=always_bad)
    fallback = asyncio.run(
        failing.predict(_snapshot(False), _estimate(False), limitations=[])
    )
    assert always_bad.calls == 2
    assert fallback.model_id == "score-model"
    assert fallback.prediction.full_time_score == _estimate(False).full_time_score
    assert any("statistical model only" in note for note in fallback.limitations)


def test_gemini_schema_uses_json_and_zero_thinking_budget() -> None:
    from google.genai import types

    config = types.GenerateContentConfig(
        temperature=0.2,
        response_mime_type="application/json",
        response_schema=build_schema(),
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    assert config.response_mime_type == "application/json"
    assert config.automatic_function_calling is not None
    assert config.automatic_function_calling.disable is True


def test_provider_failure_returns_statistical_score(monkeypatch) -> None:
    async def boom(prompt: str, schema: dict) -> str:
        raise ModelError("The model request failed (503 UNAVAILABLE high demand)")

    predictor = Predictor("key", "gemini-3.5-flash", generate=boom)
    estimate = _estimate(False)
    produced = asyncio.run(predictor.predict(_snapshot(False), estimate, limitations=[]))
    assert produced.model_id == "score-model"
    assert produced.prediction.full_time_score == estimate.full_time_score
    assert "Dixon-Coles" in produced.prediction.tactical_reasoning
    assert any("statistical model only" in note for note in produced.limitations)


def test_transient_503_falls_back_to_the_next_model(monkeypatch) -> None:
    calls: list[str] = []
    waits: list[float] = []

    async def no_wait(seconds: float) -> None:
        waits.append(seconds)

    def fake_generate(self, prompt: str, schema: dict, model: str) -> str:
        calls.append(model)
        if model == "gemini-3.8-flash":
            raise ModelError(
                "The model request failed (503 UNAVAILABLE. This model is currently experiencing high demand.)"
            )
        return json.dumps({"tactical_reasoning": "fallback model answered"})

    monkeypatch.setattr(Predictor, "_generate_sync", fake_generate)
    predictor = Predictor(
        "key",
        "gemini-3.8-flash",
        fallback_models=("gemini-3.5-flash",),
        sleep=no_wait,
    )
    produced = asyncio.run(predictor.predict(_snapshot(False), _estimate(False), limitations=[]))
    assert calls.count("gemini-3.8-flash") == 2
    assert "gemini-3.5-flash" in calls
    assert produced.model_id == "gemini-3.5-flash"
    assert any("used gemini-3.5-flash" in note for note in produced.limitations)
    assert waits == [1.5]
    assert is_transient_model_error(ModelError("503 UNAVAILABLE high demand"))


def _estimate(include_half_time: bool) -> ScoreEstimate:
    return estimate_from_lambdas(
        1.5,
        1.1,
        include_half_time=include_half_time,
        half_time_share_home=0.5,
        half_time_share_away=0.4,
    )


def _snapshot(include_half_time: bool) -> AnalysisSnapshot:
    return AnalysisSnapshot(
        request_home="Arsenal",
        request_away="Chelsea",
        request_competition="Premier League",
        competition_code="PL",
        competition_name="Premier League",
        home_team_id=1,
        away_team_id=2,
        home_team_name="Arsenal",
        away_team_name="Chelsea",
        match_id=50,
        kickoff="2026-04-12T15:00:00Z",
        fixture_status="SCHEDULED",
        standings_rows={"home": {"points": 40}, "away": {"points": 30}},
        last_five={"home": [], "away": []},
        h2h=[],
        derived={"home": {"record": "3-1-1"}, "away": {"record": "1-2-2"}},
        checks=[],
        freshness={"standings": "2026-03-01T12:00:00Z"},
        fetched_at="2026-03-02T00:00:00Z",
        limitations=[] if include_half_time else ["Half-time result and half-time score are skipped."],
        include_half_time=include_half_time,
    )
