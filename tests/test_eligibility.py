from conftest import ARSENAL, CHELSEA, SPURS, form_payload, make_match, standings_for
from providers.data_fetcher import parse_fixture
from live.eligibility import evaluate
from core.models import Competition, Team


def _team(raw: dict) -> Team:
    return Team(raw["id"], raw["name"], raw["shortName"], raw["tla"])


def _evaluate(**overrides):
    home = _team(ARSENAL)
    away = _team(CHELSEA)
    fixture = parse_fixture(
        make_match(50, ARSENAL, CHELSEA, status="SCHEDULED", home_goals=None, away_goals=None, half_home=None, half_away=None)
    )
    values = {
        "competition": Competition("PL", "Premier League"),
        "home": home,
        "away": away,
        "fixture": fixture,
        "standings_payload": standings_for([ARSENAL, CHELSEA]),
        "home_finished_payload": form_payload(ARSENAL, SPURS, first_opponent=CHELSEA),
        "away_finished_payload": form_payload(CHELSEA, SPURS, start_id=200),
        "h2h_matches": [{"id": 100}],
        "h2h_payload": {"matches": [{"id": 100, "lastUpdated": "2026-03-01T12:00:00Z"}]},
    }
    values.update(overrides)
    return evaluate(**values)


def test_missing_standings_abstains_without_filling_the_table(missing_standings) -> None:
    decision = _evaluate(standings_payload=missing_standings)
    assert decision.eligible is False
    assert decision.blocking_stage == "standings"
    assert "Chelsea FC" in decision.blocking_reason
    assert "not replaced" in decision.blocking_reason
    assert any(check.name == "consistency" and check.status == "fail" for check in decision.checks)
    assert any(check.name == "availability" and check.status == "fail" for check in decision.checks)


def test_thin_form_abstains_instead_of_inventing_results(thin_form) -> None:
    decision = _evaluate(home_finished_payload=thin_form)
    assert decision.eligible is False
    assert decision.blocking_stage == "form"
    assert "2 finished matches" in decision.blocking_reason
    assert "not filled with zeros" in decision.blocking_reason


def test_null_full_time_scores_are_not_turned_into_zero() -> None:
    payload = form_payload(ARSENAL, SPURS, count=4, null_full_time=1)
    decision = _evaluate(home_finished_payload=payload, h2h_matches=[], h2h_payload=None)
    assert decision.eligible is True
    assert decision.home_facts is not None
    assert decision.home_facts.window_size == 3
    assert all(
        match["score"]["fullTime"]["home"] != 0 or match["score"]["fullTime"]["away"] != 0
        for match in decision.home_window
    )
    assert any("not recorded as 0-0" in item for item in decision.limitations)
    quality = next(check for check in decision.checks if check.name == "quality")
    assert quality.status == "limitation"


def test_fewer_than_three_scored_matches_does_not_borrow_a_zero() -> None:
    payload = form_payload(ARSENAL, SPURS, count=3, null_full_time=1)
    decision = _evaluate(home_finished_payload=payload)
    assert decision.eligible is False
    assert decision.blocking_stage == "form"
    assert "2 finished matches" in decision.blocking_reason


def test_missing_half_time_skips_that_market_and_keeps_full_time(missing_half_time) -> None:
    away = form_payload(CHELSEA, SPURS, start_id=200, with_half_time=False)
    decision = _evaluate(
        home_finished_payload=missing_half_time,
        away_finished_payload=away,
    )
    assert decision.eligible is True
    assert decision.include_half_time is False
    assert decision.home_facts.half_time_goals_scored is None
    assert decision.home_facts.second_half_goals_scored is None
    assert any("same-league finished matches" in item for item in decision.limitations)


def test_half_time_uses_season_to_date_same_league_samples_not_only_last_five() -> None:
    # Last five lack half-time, but earlier same-league games supply enough samples.
    recent = form_payload(ARSENAL, SPURS, count=5, start_id=100, with_half_time=False)
    older = form_payload(ARSENAL, SPURS, count=3, start_id=300, with_half_time=True)
    for index, match in enumerate(older["matches"]):
        match["utcDate"] = f"2026-01-{20 - index:02d}T15:00:00Z"
    home = {
        "matches": recent["matches"] + older["matches"],
        "lastUpdated": recent["lastUpdated"],
    }
    away = form_payload(CHELSEA, SPURS, start_id=200, with_half_time=True)
    decision = _evaluate(home_finished_payload=home, away_finished_payload=away)
    assert decision.eligible is True
    assert decision.include_half_time is True
    assert decision.home_facts.window_size == 5
    assert decision.home_facts.half_time_samples == 0


def test_other_competitions_are_allowed_with_a_limitation() -> None:
    decision = _evaluate(
        home_finished_payload=form_payload(ARSENAL, SPURS, competition="CL", first_opponent=CHELSEA),
        away_finished_payload=form_payload(CHELSEA, SPURS, start_id=200, competition="CL"),
    )
    assert decision.eligible is True
    assert any("other competitions" in item for item in decision.limitations)


def test_no_head_to_head_is_a_limitation_not_a_stop() -> None:
    decision = _evaluate(h2h_matches=[], h2h_payload=None)
    assert decision.eligible is True
    assert any("No head-to-head" in item for item in decision.limitations)


def test_missing_timestamp_is_freshness_unknown() -> None:
    standings = standings_for([ARSENAL, CHELSEA], last_updated=None)
    home = form_payload(ARSENAL, SPURS, first_opponent=CHELSEA)
    home.pop("lastUpdated")
    for match in home["matches"]:
        match["lastUpdated"] = None
    decision = _evaluate(standings_payload=standings, home_finished_payload=home)
    freshness = next(check for check in decision.checks if check.name == "freshness")
    assert freshness.status == "limitation"
    assert "freshness unknown" in freshness.detail
    assert any(item.startswith("Freshness unknown") for item in decision.limitations)


def test_derived_record_uses_the_club_perspective() -> None:
    decision = _evaluate()
    assert decision.home_facts.record == "5-0-0"
    assert decision.home_facts.half_time_goals_scored == 5
    assert decision.home_facts.second_half_goals_scored == 10
    assert decision.home_facts.second_half_goals_conceded == 5
