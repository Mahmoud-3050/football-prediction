import asyncio

from conftest import (
    AC_MILAN,
    ARSENAL,
    BARCELONA,
    CHELSEA,
    INTER,
    MAN_CITY,
    MAN_UNITED,
    SPURS,
    Harness,
    make_match,
)
from data_fetcher import (
    classify_fixtures,
    resolve_competition,
    resolve_team,
    unknown_competition_reason,
)
from models import Team


def _team(raw: dict) -> Team:
    return Team(raw["id"], raw["name"], raw["shortName"], raw["tla"])


PL = [_team(team) for team in (ARSENAL, CHELSEA, MAN_CITY, MAN_UNITED, SPURS)]


def test_competition_aliases_stay_on_the_named_league() -> None:
    assert resolve_competition("Premier League").code == "PL"
    assert resolve_competition("La Liga").code == "PD"
    assert resolve_competition("UCL").code == "CL"
    assert resolve_competition("Brasileirão").code == "BSA"
    assert resolve_competition("Serie A").code == "SA"
    assert resolve_competition("Championship").code == "ELC"
    assert resolve_competition("Champions League").code == "CL"
    assert resolve_competition("Premiership") is None
    assert "Premier League (PL)" in unknown_competition_reason("Premiership")


def test_exact_name_alias_and_typo_rules() -> None:
    arsenal = resolve_team("Arsenal", PL, "Premier League")
    assert arsenal.team is not None and arsenal.team.id == 1

    city = resolve_team("Man City", PL, "Premier League")
    assert city.team is not None and city.team.id == 3

    spain = [_team(BARCELONA)]
    barca = resolve_team("Barca", spain, "Primera Division")
    assert barca.team is not None and barca.team.id == 10

    italy = [_team(INTER), _team(AC_MILAN)]
    inter = resolve_team("Inter", italy, "Serie A")
    assert inter.team is not None and inter.team.id == 20

    ambiguous = resolve_team("Man", PL, "Premier League")
    assert ambiguous.team is None
    assert "more specific" in ambiguous.reason
    assert "Manchester City FC" in ambiguous.reason
    assert "Manchester United FC" in ambiguous.reason

    typo = resolve_team("Arsenl", PL, "Premier League")
    assert typo.team is None
    assert typo.reason.startswith('"Arsenl" did not match')
    assert "Closest names: Arsenal FC" in typo.reason


def test_earliest_scheduled_fixture_beats_a_later_one_and_a_finished_one() -> None:
    matches = [
        make_match(2, ARSENAL, CHELSEA, status="SCHEDULED", home_goals=None, away_goals=None, half_home=None, half_away=None, when="2026-05-01T15:00:00Z"),
        make_match(1, ARSENAL, CHELSEA, status="TIMED", home_goals=None, away_goals=None, half_home=None, half_away=None, when="2026-04-01T15:00:00Z"),
        make_match(3, ARSENAL, CHELSEA, status="FINISHED", home_goals=1, away_goals=0, when="2026-01-01T15:00:00Z"),
    ]
    found = classify_fixtures(matches, competition_code="PL", home_id=1, away_id=2)
    assert found.kind == "upcoming"
    assert found.fixture is not None and found.fixture.id == 1


def test_wrong_venue_is_not_used_as_the_requested_home(wrong_venue_matches) -> None:
    found = classify_fixtures(wrong_venue_matches, competition_code="PL", home_id=1, away_id=2)
    assert found.kind == "opposite"
    assert found.fixture.home_team_id == 2
    assert found.fixture.away_team_id == 1


def test_finished_fixture_is_classified_separately(finished_match) -> None:
    found = classify_fixtures([finished_match], competition_code="PL", home_id=1, away_id=2)
    assert found.kind == "finished"
    assert found.fixture.score_text() == "2-1"


def test_pipeline_does_not_swap_home_and_away(tmp_path, wrong_venue_matches) -> None:
    async def run():
        api_holder = Harness(tmp_path, _api_with(wrong_venue_matches))
        result = await api_holder.execute()
        return result, api_holder

    result, holder = asyncio.run(run())
    assert result.status == "abstained"
    assert result.stage == "fixture"
    assert "not swapped" in result.reason
    assert "Chelsea FC vs Arsenal FC" in result.reason
    assert holder.prompts == []
    assert holder.store.versions_for(result.run_id) == []
    assert len(holder.store.snapshots_for(result.run_id)) == 1


def test_finished_match_reports_the_score_and_abstains(tmp_path, finished_match) -> None:
    async def run():
        holder = Harness(tmp_path, _api_with([finished_match]))
        result = await holder.execute()
        return result, holder

    result, holder = asyncio.run(run())
    assert result.status == "abstained"
    assert result.stage == "fixture"
    assert "finished" in result.reason
    assert "2-1" in result.reason
    assert "already been played" in result.reason
    assert holder.prompts == []


def test_missing_fixture_names_both_clubs(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path, _api_with([]))
        return await holder.execute()

    result = asyncio.run(run())
    assert result.stage == "fixture"
    assert "Arsenal FC" in result.reason
    assert "Chelsea FC" in result.reason
    assert "No fixture" in result.reason


def test_unknown_competition_makes_no_provider_call(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path, _api_with([]))
        result = await holder.execute(competition="Premiership")
        return result, holder

    result, holder = asyncio.run(run())
    assert result.stage == "competition"
    assert holder.api.calls == []
    assert "Premier League (PL)" in result.reason


def test_ambiguous_club_stops_before_the_fixture_search(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path, _api_with([]))
        result = await holder.execute(home="Man", away="Chelsea")
        return result, holder

    result, holder = asyncio.run(run())
    assert result.stage == "teams"
    assert "more specific" in result.reason
    assert all(not call["path"].endswith("/matches") for call in holder.api.calls)


def test_typo_returns_closest_names_without_choosing_one(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path, _api_with([]))
        result = await holder.execute(home="Arsenl")
        return result, holder

    result, holder = asyncio.run(run())
    assert result.stage == "teams"
    assert "Closest names: Arsenal FC" in result.reason
    assert holder.prompts == []


def _api_with(fixtures: list[dict]):
    from conftest import Api

    api = Api()
    api.fixtures = fixtures
    return api
