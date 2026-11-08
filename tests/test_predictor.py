import asyncio
import json

import pytest

from models import AnalysisSnapshot, ModelError
from predictor import (
    Predictor,
    build_prompt,
    build_schema,
    derive_markets,
    validate_prediction,
)


def test_markets_match_the_score(markets_table) -> None:
    for row in markets_table:
        markets = derive_markets(row["score"])
        assert markets.result_1x2 == row["result_1x2"]
        assert markets.btts == row["btts"]
        assert markets.over_under_25 == row["over_under_25"]


def test_half_time_label_must_agree_with_the_score() -> None:
    with pytest.raises(ValueError, match="disagrees"):
        validate_prediction(
            {
                "half_time_prediction": "Draw",
                "half_time_score": "1-0",
                "full_time_score": "2-1",
                "confidence_percentage": 40,
                "tactical_reasoning": "snapshot only",
            },
            include_half_time=True,
        )


def test_confidence_outside_0_to_100_is_rejected() -> None:
    with pytest.raises(ValueError, match="confidence"):
        validate_prediction(
            {
                "full_time_score": "1-0",
                "confidence_percentage": 150,
                "tactical_reasoning": "too sure",
            },
            include_half_time=False,
        )


def test_prompt_omits_half_time_when_that_market_is_ineligible() -> None:
    snapshot = _snapshot(include_half_time=False)
    prompt = build_prompt(snapshot)
    schema = build_schema(False)
    assert "Do not return a half-time" in prompt
    assert "half_time_prediction" not in schema["required"]
    assert "Arsenal" in prompt
    assert "Chelsea" in prompt
    assert "SNAPSHOT:" in prompt


def test_prompt_requires_an_agreeing_half_time_when_eligible() -> None:
    prompt = build_prompt(_snapshot(include_half_time=True))
    schema = build_schema(True)
    assert "half_time_prediction" in schema["required"]
    assert "must be the winner of the half-time score" in prompt


def test_retry_is_version_two_and_a_second_failure_stops() -> None:
    async def once_then_valid(prompt: str, schema: dict) -> str:
        once_then_valid.calls += 1
        if once_then_valid.calls == 1:
            return "not-json"
        return json.dumps(
            {
                "half_time_prediction": "Team A",
                "half_time_score": "1-0",
                "full_time_score": "2-1",
                "confidence_percentage": 55,
                "tactical_reasoning": "from the snapshot",
            }
        )

    once_then_valid.calls = 0
    predictor = Predictor("key", "gemini-2.5-flash", generate=once_then_valid)
    produced = asyncio.run(predictor.predict(_snapshot(True), limitations=["none found"]))
    assert produced.version_number == 2
    assert produced.markets.result_1x2 == "Home"
    assert produced.markets.btts == "Yes"
    assert produced.markets.over_under_25 == "Over"
    assert produced.model_id == "gemini-2.5-flash"

    async def always_bad(prompt: str, schema: dict) -> str:
        always_bad.calls += 1
        return "{}"

    always_bad.calls = 0
    failing = Predictor("key", "gemini-2.5-flash", generate=always_bad)
    with pytest.raises(ModelError, match="twice"):
        asyncio.run(failing.predict(_snapshot(False), limitations=[]))
    assert always_bad.calls == 2


def test_gemini_schema_uses_json_and_zero_thinking_budget() -> None:
    from google.genai import types

    config = types.GenerateContentConfig(
        temperature=0.2,
        response_mime_type="application/json",
        response_schema=build_schema(True),
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    assert config.response_mime_type == "application/json"


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
