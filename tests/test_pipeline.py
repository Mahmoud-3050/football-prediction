import asyncio
import json

from conftest import CHELSEA, SPURS, Harness, form_payload, standings_for, valid_model_json
from live.bot import render_report
from modeling.score_model import estimate_score
from db.storage import loads


def test_completed_prediction_derives_markets_and_stores_the_trace(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path)
        result = await holder.execute()
        return result, holder

    result, holder = asyncio.run(run())
    estimate = _estimate_for(holder, include_half_time=True)
    assert result.status == "completed"
    assert result.full_time_score == estimate.full_time_score
    assert result.half_time_score == estimate.half_time_score
    assert result.markets.result_1x2 == estimate.markets.result_1x2
    assert result.markets.btts == estimate.markets.btts
    assert result.markets.over_under_25 == estimate.markets.over_under_25
    assert result.include_half_time is True
    report = render_report(result, markdown=False)
    assert "Most likely score" in report
    assert "Interpretation" in report
    assert "Over 2.5" in report
    assert "Over 3.5" in report
    assert "Statistical estimate, not a betting tip." in report
    assert f"Run id: {result.run_id}" in report
    assert "Half time" in report
    assert result.probabilities is not None
    assert "home" in result.probabilities
    assert "over_35" in result.probabilities
    assert result.top_scores
    assert result.top_scores[0][2] >= result.top_scores[-1][2]
    score_label = estimate.full_time_score.replace("-", "–")
    assert f"Most likely score {score_label}" in report
    assert result.half_time_probabilities is not None
    assert abs(sum(result.half_time_probabilities.values()) - 1.0) < 1e-9
    assert holder.api.calls[0]["token"] == "fd-test-key"
    assert not any(call["path"].endswith("/head2head") for call in holder.api.calls)

    store = holder.store
    run_row = store.get_run(result.run_id)
    assert run_row["status"] == "completed"
    assert run_row["raw_home"] == "Arsenal"
    assert run_row["raw_away"] == "Chelsea"
    snapshots = store.snapshots_for(result.run_id)
    versions = store.versions_for(result.run_id)
    assert len(snapshots) == 1
    assert len(versions) == 1
    assert versions[0]["version_number"] == 1
    assert versions[0]["model_id"] == "gemini-2.5-flash"
    output = loads(versions[0]["output_json"])
    assert output["markets"]["result_1x2"] == estimate.markets.result_1x2
    assert output["markets_derived_from_distribution"] is True
    assert output["lambda_home"] == round(estimate.lambda_home, 4)
    assert output["lambda_away"] == round(estimate.lambda_away, 4)
    payload = loads(snapshots[0]["payload"])
    assert payload["resolved"]["match_id"] == 50
    assert "SNAPSHOT:" in holder.prompts[0]
    echoed = json.loads(holder.prompts[0].split("SNAPSHOT:\n", 1)[1])
    assert echoed["resolved"]["home_team_id"] == 1
    assert echoed["resolved"]["away_team_id"] == 2
    assert holder.schemas[0]["required"] == ["tactical_reasoning", "tactical_reasoning_ar"]
    assert "lambda_home" in holder.prompts[0]
    assert "Do not invent a different score" in holder.prompts[0]
    stored = loads(snapshots[0]["payload"])
    assert stored["derived"]["score_model"]["full_time_score"] == estimate.full_time_score


def test_missing_standings_does_not_call_the_model(tmp_path, missing_standings) -> None:
    async def run():
        holder = Harness(tmp_path)
        holder.api.standings = missing_standings
        result = await holder.execute()
        return result, holder

    result, holder = asyncio.run(run())
    assert result.status == "abstained"
    assert result.stage == "standings"
    assert holder.prompts == []
    assert holder.store.versions_for(result.run_id) == []
    assert holder.store.snapshots_for(result.run_id)


def test_thin_form_abstains_at_the_form_stage(tmp_path, thin_form) -> None:
    async def run():
        holder = Harness(tmp_path)
        holder.api.forms[1] = thin_form
        result = await holder.execute()
        return result, holder

    result, holder = asyncio.run(run())
    assert result.stage == "form"
    assert "not filled with zeros" in result.reason
    assert holder.prompts == []


def test_missing_half_time_limits_the_prompt_and_the_report(tmp_path, missing_half_time) -> None:
    async def run():
        holder = Harness(tmp_path)
        holder.api.forms[1] = missing_half_time
        holder.api.forms[2] = form_payload(
            CHELSEA, SPURS, start_id=200, with_half_time=False, venue="away"
        )
        result = await holder.execute()
        return result, holder

    result, holder = asyncio.run(run())
    estimate = _estimate_for(holder, include_half_time=False)
    assert result.status == "partial"
    assert result.include_half_time is False
    assert result.full_time_score == estimate.full_time_score
    assert result.half_time_score is None
    assert "half_time_prediction" not in holder.schemas[0]["required"]
    assert "Do not return a half-time" in holder.prompts[0]
    report = render_report(result, markdown=False)
    assert "Half time" not in report
    assert f"Most likely score {estimate.full_time_score.replace('-', '–')}" in report
    assert any("Half-time" in item for item in result.limitations)


def test_accepted_retry_is_the_next_version(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path, scripts=["nope", valid_model_json()])
        result = await holder.execute()
        return result, holder

    result, holder = asyncio.run(run())
    assert result.status == "completed"
    versions = holder.store.versions_for(result.run_id)
    assert len(versions) == 1
    assert versions[0]["version_number"] == 2


def test_invalid_reasoning_twice_falls_back_to_score_model(tmp_path) -> None:
    bad = "{}"

    async def run():
        holder = Harness(tmp_path, scripts=[bad, bad])
        result = await holder.execute()
        return result, holder

    result, holder = asyncio.run(run())
    assert result.status == "partial"
    assert result.full_time_score
    versions = holder.store.versions_for(result.run_id)
    assert len(versions) == 1
    assert versions[0]["model_id"] == "score-model"
    assert any("statistical model only" in note for note in result.limitations)
    assert len(holder.prompts) == 2


def test_rate_limit_after_one_wait_stops_the_request(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path)
        holder.api.status_overrides["/v4/competitions/PL/teams"] = (
            429,
            {"message": "limit"},
            2,
        )
        result = await holder.execute()
        return result, holder

    result, holder = asyncio.run(run())
    assert result.stage == "provider"
    assert "free-tier limit was hit" in result.reason
    assert holder.api.sleeps == [0.0]
    assert holder.prompts == []


def test_rejected_key_names_the_provider_failure(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path)
        holder.api.status_overrides["/v4/competitions/PL/teams"] = (
            403,
            {"message": "competition not permitted"},
            1,
        )
        result = await holder.execute()
        return result, holder

    result, holder = asyncio.run(run())
    assert result.stage == "provider"
    assert "API key or competition" in result.reason
    assert "403" in result.reason
    assert holder.prompts == []


def test_same_club_string_never_calls_the_provider(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path)
        result = await holder.execute(home="Arsenal", away="arsenal")
        return result, holder

    result, holder = asyncio.run(run())
    assert result.stage == "input"
    assert holder.api.calls == []


def test_no_head_to_head_calls_the_endpoint_once(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path)
        holder.api.forms[1] = form_payload(holder.api.teams[0], SPURS, start_id=100)
        result = await holder.execute()
        return result, holder

    result, holder = asyncio.run(run())
    assert result.status == "partial"
    assert any("No head-to-head" in item for item in result.limitations)
    assert sum(1 for call in holder.api.calls if call["path"].endswith("/head2head")) == 1


def _estimate_for(holder: Harness, *, include_half_time: bool):
    from modeling.league_stacks import draw_calibration_kwargs, live_stack_for

    stack = live_stack_for(holder.api.competition)
    return estimate_score(
        competition_finished_payload=holder.api._competition_finished_payload(),
        home_finished_payload=holder.api.forms[1],
        away_finished_payload=holder.api.forms[2],
        home_team_id=1,
        away_team_id=2,
        include_half_time=include_half_time,
        kickoff="2026-12-12T15:00:00Z",
        k=float(stack["k"]),
        prior_mean_mode=str(stack["prior_mean"]),
        half_time_share_mode=str(stack["half_time_share_mode"]),
        dependence=str(stack["dependence"]),
        ratio_mode=str(stack.get("ratio_mode") or "one_pass"),
        decay_half_life_days=stack.get("decay_half_life_days"),
        **draw_calibration_kwargs(stack),
    )


def test_standings_without_a_timestamp_is_partial(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path)
        holder.api.standings = standings_for(holder.api.teams, last_updated=None)
        result = await holder.execute()
        return result

    result = asyncio.run(run())
    assert result.status == "partial"
    assert any("Freshness unknown" in item for item in result.limitations)
