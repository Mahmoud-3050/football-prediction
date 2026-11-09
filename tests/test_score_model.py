from conftest import ARSENAL, CHELSEA, SPURS, make_match, standings_for
from score_model import (
    HOME_FACTOR,
    AWAY_FACTOR,
    MISSING_PLAYED_GAMES,
    estimate_from_lambdas,
    estimate_score,
)


def test_grid_sums_to_one_and_negative_rho_raises_nil_nil() -> None:
    adjusted = estimate_from_lambdas(1.2, 1.1)
    independent = estimate_from_lambdas(1.2, 1.1, rho=0.0)
    assert abs(sum(sum(row) for row in adjusted.matrix) - 1) < 1e-9
    assert adjusted.probability(0, 0) > independent.probability(0, 0)


def test_known_lambdas_lock_the_modal_score_and_can_disagree_with_1x2() -> None:
    estimate = estimate_from_lambdas(1.5, 1.1)
    assert estimate.full_time_score == "1-1"
    assert estimate.markets.result_1x2 == "Home"
    assert estimate.markets.btts == "Yes"
    assert estimate.markets.over_under_25 == "Under"
    assert estimate.confidence_percentage == 14
    home_goals, away_goals = (int(part) for part in estimate.full_time_score.split("-"))
    assert home_goals == away_goals


def test_half_time_is_omitted_unless_requested_and_its_label_matches() -> None:
    full_time_only = estimate_from_lambdas(1.6, 0.4, include_half_time=False)
    assert full_time_only.half_time_score is None
    assert full_time_only.half_time_prediction is None

    estimate = estimate_from_lambdas(
        1.6,
        0.4,
        include_half_time=True,
        half_time_share_home=0.6,
        half_time_share_away=0.2,
    )
    assert estimate.half_time_score is not None
    home_goals, away_goals = (int(part) for part in estimate.half_time_score.split("-"))
    if home_goals > away_goals:
        assert estimate.half_time_prediction == "Team A"
    elif home_goals < away_goals:
        assert estimate.half_time_prediction == "Team B"
    else:
        assert estimate.half_time_prediction == "Draw"


def test_missing_played_games_uses_the_match_list_instead_of_zero() -> None:
    standings = standings_for([ARSENAL, CHELSEA])
    for row in standings["standings"][0]["table"]:
        row["goalsFor"] = 100
        del row["playedGames"]
    estimate = estimate_score(
        standings_payload=standings,
        home_finished_payload=_games(ARSENAL, SPURS, venue="home", score=(2, 1)),
        away_finished_payload=_games(CHELSEA, SPURS, venue="away", score=(1, 1)),
        competition_code="PL",
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        home_team_name=ARSENAL["name"],
        away_team_name=CHELSEA["name"],
        include_half_time=False,
    )
    assert MISSING_PLAYED_GAMES in estimate.limitations
    assert estimate.lambda_home == (2 * 1) / 1.5
    assert estimate.lambda_away == (1 * 1) / 1.5
    assert estimate.full_time_score != "6-6"


def test_thin_venue_sample_falls_back_to_other_competitions() -> None:
    estimate = estimate_score(
        standings_payload=standings_for([ARSENAL, CHELSEA]),
        home_finished_payload=_games(ARSENAL, SPURS, venue="home", competition="CL"),
        away_finished_payload=_games(CHELSEA, SPURS, venue="away"),
        competition_code="PL",
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        home_team_name="Arsenal",
        away_team_name="Chelsea",
        include_half_time=False,
    )
    assert any("other competitions" in note for note in estimate.limitations)
    assert not any("1.25" in note for note in estimate.limitations)


def test_season_table_fallback_applies_the_home_factor() -> None:
    standings = standings_for([ARSENAL, CHELSEA])
    table = standings["standings"][0]["table"]
    table[0]["goalsFor"] = 20
    table[0]["goalsAgainst"] = 10
    table[0]["playedGames"] = 10
    table[1]["goalsFor"] = 10
    table[1]["goalsAgainst"] = 20
    table[1]["playedGames"] = 10
    estimate = estimate_score(
        standings_payload=standings,
        home_finished_payload={"matches": []},
        away_finished_payload={"matches": []},
        competition_code="PL",
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        home_team_name="Arsenal",
        away_team_name="Chelsea",
        include_half_time=False,
    )
    league = 30 / 20
    assert estimate.lambda_home == ((2 * 2) / league) * HOME_FACTOR
    assert estimate.lambda_away == ((1 * 1) / league) * AWAY_FACTOR
    assert any("season table" in note for note in estimate.limitations)
    assert any("1.25" in note and "0.85" in note for note in estimate.limitations)


def test_a_scoreless_half_time_sample_uses_the_default_share() -> None:
    standings = standings_for([ARSENAL, CHELSEA])
    for row in standings["standings"][0]["table"]:
        row["goalsFor"] = 10
        row["playedGames"] = 10
    recent = _games(ARSENAL, SPURS, venue="home", score=(0, 0), half=(0, 0), count=5, start_id=1)
    older = _games(
        ARSENAL,
        SPURS,
        venue="home",
        score=(5, 0),
        half=(0, 0),
        count=5,
        start_id=20,
        day_offset=10,
    )
    home_matches = {"matches": recent["matches"] + older["matches"], "lastUpdated": recent["lastUpdated"]}
    away = _games(CHELSEA, SPURS, venue="away", score=(0, 1), half=(0, 0))
    estimate = estimate_score(
        standings_payload=standings,
        home_finished_payload=home_matches,
        away_finished_payload=away,
        competition_code="PL",
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        home_team_name="Arsenal",
        away_team_name="Chelsea",
        include_half_time=True,
    )
    priced = estimate_from_lambdas(
        estimate.lambda_home,
        estimate.lambda_away,
        include_half_time=True,
        half_time_share_home=0.45,
        half_time_share_away=0.45,
    )
    assert estimate.half_time_score == priced.half_time_score
    assert estimate.half_time_score != "0-0"


def _games(
    team: dict,
    opponent: dict,
    *,
    venue: str,
    score: tuple[int, int] = (2, 1),
    half: tuple[int, int] = (1, 0),
    count: int = 3,
    start_id: int = 1,
    competition: str = "PL",
    day_offset: int = 0,
) -> dict:
    matches = []
    for index in range(count):
        scored, conceded = score
        if venue == "away":
            match_home, match_away = opponent, team
            home_goals, away_goals = conceded, scored
            half_home, half_away = half[1], half[0]
        else:
            match_home, match_away = team, opponent
            home_goals, away_goals = scored, conceded
            half_home, half_away = half
        matches.append(
            make_match(
                start_id + index,
                match_home,
                match_away,
                home_goals=home_goals,
                away_goals=away_goals,
                half_home=half_home,
                half_away=half_away,
                competition=competition,
                when=f"2026-02-{20 - index - day_offset:02d}T15:00:00Z",
            )
        )
    return {"matches": matches, "lastUpdated": "2026-03-01T12:00:00Z"}
