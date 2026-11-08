import asyncio
import json

from conftest import CHELSEA, SPURS, Harness, form_payload, standings_for, valid_model_json
from bot import render_report
from storage import loads


def test_completed_prediction_derives_markets_and_stores_the_trace(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path)
        result = await holder.execute()
        return result, holder

    result, holder = asyncio.run(run())
    assert result.status == "completed"
    assert result.full_time_score == "2-1"
    assert result.markets.result_1x2 == "Home"
    assert result.markets.btts == "Yes"
    assert result.markets.over_under_25 == "Over"
    assert result.include_half_time is True
    report = render_report(result)
    assert "1X2: Home" in report
    assert "BTTS: Yes" in report
    assert "Over/Under 2.5: Over" in report
    assert "Markets derived from the predicted scoreline" in report
    assert "Statistical estimate, not a betting tip." in report
    assert f"Run id: {result.run_id}" in report
    assert "Half-time:" in report
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
    assert output["markets"]["result_1x2"] == "Home"
    assert output["markets_derived_from_score"] is True
    payload = loads(snapshots[0]["payload"])
    assert payload["resolved"]["match_id"] == 50
    assert "SNAPSHOT:" in holder.prompts[0]
    echoed = json.loads(holder.prompts[0].split("SNAPSHOT:\n", 1)[1])
    assert echoed["resolved"]["home_team_id"] == 1
    assert echoed["resolved"]["away_team_id"] == 2


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
        holder.api.forms[2] = form_payload(CHELSEA, SPURS, start_id=200, with_half_time=False)
        result = await holder.execute()
        return result, holder

    result, holder = asyncio.run(run())
    assert result.status == "partial"
    assert result.include_half_time is False
    assert result.full_time_score == "2-1"
    assert "half_time_prediction" not in holder.schemas[0]["required"]
    assert "Do not return a half-time" in holder.prompts[0]
    report = render_report(result)
    assert "Half-time:" not in report
    assert "Full-time: 2-1" in report
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


def test_model_disagreement_twice_stores_no_version(tmp_path) -> None:
    bad = valid_model_json(half_time_prediction="Draw", half_time_score="1-0")

    async def run():
        holder = Harness(tmp_path, scripts=[bad, bad])
        result = await holder.execute()
        return result, holder

    result, holder = asyncio.run(run())
    assert result.status == "failed"
    assert result.stage == "model"
    assert "disagrees" in result.reason
    assert "twice" in result.reason
    assert holder.store.versions_for(result.run_id) == []
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


def test_standings_without_a_timestamp_is_partial(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path)
        holder.api.standings = standings_for(holder.api.teams, last_updated=None)
        result = await holder.execute()
        return result

    result = asyncio.run(run())
    assert result.status == "partial"
    assert any("Freshness unknown" in item for item in result.limitations)
