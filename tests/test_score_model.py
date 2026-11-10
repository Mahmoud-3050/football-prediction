from conftest import ARSENAL, CHELSEA, MAN_CITY, SPURS, make_match
from score_model import K, TAIL_TOLERANCE, estimate_from_lambdas, estimate_score, inspect_grid


def test_grid_sums_to_one_under_independent_poisson() -> None:
    estimate = estimate_from_lambdas(1.2, 1.1)
    assert abs(sum(sum(row) for row in estimate.matrix) - 1) < 1e-9
    assert estimate.dependence == "independent"
    assert estimate.tail_tolerance == TAIL_TOLERANCE


def test_inspect_grid_reports_tail_and_renormalized_sums() -> None:
    report = inspect_grid(1.5, 0.9, tail_tolerance=TAIL_TOLERANCE)
    assert report["tail_within_tolerance"] is True
    assert report["hit_cap"] is False
    assert float(report["raw_tail"]) <= TAIL_TOLERANCE
    assert abs(float(report["renormalized_sum"]) - 1.0) < 1e-12
    assert abs(float(report["result_1x2_sum"]) - 1.0) < 1e-12
    assert int(report["max_goals"]) >= 6


def test_known_lambdas_lock_the_modal_score_and_can_disagree_with_1x2() -> None:
    estimate = estimate_from_lambdas(1.5, 1.1)
    assert estimate.full_time_score == "1-1"
    assert estimate.markets.result_1x2 == "Home"
    assert estimate.markets.btts == "Yes"
    assert estimate.markets.over_under_25 == "Under"
    assert estimate.confidence_percentage == 12
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
    assert estimate.half_time_probabilities is not None
    assert abs(sum(estimate.half_time_probabilities.values()) - 1.0) < 1e-12
    home_goals, away_goals = (int(part) for part in estimate.half_time_score.split("-"))
    if home_goals > away_goals:
        assert estimate.half_time_prediction == "Team A"
    elif home_goals < away_goals:
        assert estimate.half_time_prediction == "Team B"
    else:
        assert estimate.half_time_prediction == "Draw"


def test_shrinkage_pulls_a_short_attack_sample_toward_one() -> None:
    # Neutral 1-1 games between Spurs and a third club, plus one Arsenal home 4-0.
    # Chelsea has no prior games, so defence_away stays at the prior of 1.
    league = []
    for index in range(10):
        home, away = (SPURS, MAN_CITY) if index % 2 == 0 else (MAN_CITY, SPURS)
        league.append(
            make_match(
                1000 + index,
                home,
                away,
                home_goals=1,
                away_goals=1,
                half_home=0,
                half_away=0,
                when=f"2026-01-{10 + index:02d}T15:00:00Z",
            )
        )
    league.append(
        make_match(
            2000,
            ARSENAL,
            SPURS,
            home_goals=4,
            away_goals=0,
            half_home=2,
            half_away=0,
            when="2026-02-01T15:00:00Z",
        )
    )
    estimate = estimate_score(
        competition_finished_payload={"matches": league},
        home_finished_payload={"matches": [league[-1]]},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=False,
        kickoff="2026-03-01T15:00:00Z",
    )
    league_home = (10 + 4) / 11
    raw = 4 / league_home
    shrunk = (1 * raw + K * 1.0) / (1 + K)
    assert estimate.ratios["attack_home"].n == 1
    assert estimate.ratios["defence_away"].n == 0
    assert abs(estimate.ratios["attack_home"].shrunk - shrunk) < 1e-9
    assert abs(estimate.lambda_home - league_home * shrunk * 1.0) < 1e-9
    assert estimate.k == K
    assert any("k=6" in note for note in estimate.public()["assumptions"])


def test_last_season_mode_uses_previous_season_ratios_as_prior_means() -> None:
    previous = []
    for index in range(8):
        previous.append(
            make_match(
                3000 + index,
                ARSENAL if index % 2 == 0 else SPURS,
                SPURS if index % 2 == 0 else ARSENAL,
                home_goals=3 if index % 2 == 0 else 1,
                away_goals=0 if index % 2 == 0 else 1,
                half_home=1,
                half_away=0,
                when=f"2024-10-{10 + index:02d}T15:00:00Z",
            )
        )
    current = [
        make_match(
            4000,
            ARSENAL,
            MAN_CITY,
            home_goals=1,
            away_goals=0,
            half_home=0,
            half_away=0,
            when="2025-08-20T15:00:00Z",
        ),
        make_match(
            4001,
            SPURS,
            CHELSEA,
            home_goals=1,
            away_goals=1,
            half_home=0,
            half_away=0,
            when="2025-08-21T15:00:00Z",
        ),
        make_match(
            4002,
            MAN_CITY,
            CHELSEA,
            home_goals=2,
            away_goals=1,
            half_home=1,
            half_away=0,
            when="2025-08-22T15:00:00Z",
        ),
    ]
    estimate = estimate_score(
        competition_finished_payload={"matches": current},
        home_finished_payload={"matches": previous},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=False,
        kickoff="2025-09-01T15:00:00Z",
        prior_mean_mode="last_season",
    )
    assert estimate.prior_match_count == len(current)
    assert estimate.ratios["attack_home"].shrunk != 1.0
    assert any("last season" in note.lower() for note in estimate.public()["assumptions"])


def test_dixon_coles_fits_rho_from_earlier_matches_and_changes_low_scores() -> None:
    matches = []
    match_id = 6000
    # Build a season with many low-scoring draws so independence understates 1-1 / 0-0.
    clubs = [ARSENAL, CHELSEA, SPURS, MAN_CITY]
    day = 1
    for round_index in range(12):
        for home, away in (
            (clubs[0], clubs[1]),
            (clubs[2], clubs[3]),
            (clubs[1], clubs[2]),
            (clubs[3], clubs[0]),
        ):
            home_goals, away_goals = (1, 1) if round_index % 2 == 0 else (0, 0)
            matches.append(
                make_match(
                    match_id,
                    home,
                    away,
                    home_goals=home_goals,
                    away_goals=away_goals,
                    half_home=0,
                    half_away=0,
                    when=f"2025-09-{min(day, 28):02d}T15:00:00Z",
                )
            )
            match_id += 1
            day += 1
    independent = estimate_score(
        competition_finished_payload={"matches": matches},
        home_finished_payload={"matches": []},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=False,
        kickoff="2025-11-01T15:00:00Z",
        dependence="independent",
    )
    dixon = estimate_score(
        competition_finished_payload={"matches": matches},
        home_finished_payload={"matches": []},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=False,
        kickoff="2025-11-01T15:00:00Z",
        dependence="dixon_coles",
    )
    assert dixon.dependence == "dixon_coles"
    assert dixon.rho is not None
    assert dixon.probability(1, 1) != independent.probability(1, 1)
    assert any("Dixon-Coles" in note for note in dixon.public()["assumptions"])


def test_joint_ratios_adjust_for_weak_schedule_inflation() -> None:
    # Arsenal thrash weak Spurs/City at home; Chelsea are unused opponents in priors.
    # One-pass home attack is raw; joint should pull it after accounting for weak defence.
    weak_victims = []
    for index in range(6):
        victim = SPURS if index % 2 == 0 else MAN_CITY
        weak_victims.append(
            make_match(
                5000 + index,
                ARSENAL,
                victim,
                home_goals=4,
                away_goals=0,
                half_home=2,
                half_away=0,
                when=f"2026-01-{10 + index:02d}T15:00:00Z",
            )
        )
    # Give the victims no defensive reputation elsewhere: they also lose away to each other lightly.
    filler = [
        make_match(
            5100,
            SPURS,
            MAN_CITY,
            home_goals=1,
            away_goals=1,
            half_home=0,
            half_away=0,
            when="2026-01-20T15:00:00Z",
        ),
        make_match(
            5101,
            MAN_CITY,
            SPURS,
            home_goals=1,
            away_goals=1,
            half_home=0,
            half_away=0,
            when="2026-01-21T15:00:00Z",
        ),
        make_match(
            5102,
            CHELSEA,
            SPURS,
            home_goals=1,
            away_goals=0,
            half_home=0,
            half_away=0,
            when="2026-01-22T15:00:00Z",
        ),
        make_match(
            5103,
            CHELSEA,
            MAN_CITY,
            home_goals=1,
            away_goals=0,
            half_home=0,
            half_away=0,
            when="2026-01-23T15:00:00Z",
        ),
    ]
    payload = {"matches": [*weak_victims, *filler]}
    one_pass = estimate_score(
        competition_finished_payload=payload,
        home_finished_payload={"matches": []},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=False,
        kickoff="2026-03-01T15:00:00Z",
        ratio_mode="one_pass",
    )
    joint = estimate_score(
        competition_finished_payload=payload,
        home_finished_payload={"matches": []},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=False,
        kickoff="2026-03-01T15:00:00Z",
        ratio_mode="joint",
    )
    assert joint.ratios["attack_home"].raw != one_pass.ratios["attack_home"].raw
    assert any("jointly" in note.lower() for note in joint.public()["assumptions"])


def test_prior_mean_pulls_short_sample_away_from_one() -> None:
    league = []
    for index in range(10):
        home, away = (SPURS, MAN_CITY) if index % 2 == 0 else (MAN_CITY, SPURS)
        league.append(
            make_match(
                1000 + index,
                home,
                away,
                home_goals=1,
                away_goals=1,
                half_home=0,
                half_away=0,
                when=f"2026-01-{10 + index:02d}T15:00:00Z",
            )
        )
    league.append(
        make_match(
            2000,
            ARSENAL,
            SPURS,
            home_goals=4,
            away_goals=0,
            half_home=2,
            half_away=0,
            when="2026-02-01T15:00:00Z",
        )
    )
    plain = estimate_score(
        competition_finished_payload={"matches": league},
        home_finished_payload={"matches": [league[-1]]},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=False,
        kickoff="2026-03-01T15:00:00Z",
    )
    informed = estimate_score(
        competition_finished_payload={"matches": league},
        home_finished_payload={"matches": [league[-1]]},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=False,
        kickoff="2026-03-01T15:00:00Z",
        prior_means={"attack_home": 1.4, "defence_away": 1.0, "attack_away": 1.0, "defence_home": 1.0},
    )
    assert informed.ratios["attack_home"].shrunk > plain.ratios["attack_home"].shrunk


def test_decay_weights_recent_matches_more_than_old_ones() -> None:
    old = make_match(
        1,
        ARSENAL,
        SPURS,
        home_goals=0,
        away_goals=0,
        half_home=0,
        half_away=0,
        when="2025-09-01T15:00:00Z",
    )
    recent = make_match(
        2,
        ARSENAL,
        MAN_CITY,
        home_goals=4,
        away_goals=0,
        half_home=2,
        half_away=0,
        when="2026-02-20T15:00:00Z",
    )
    filler = [
        make_match(
            10 + index,
            SPURS,
            MAN_CITY if index % 2 == 0 else CHELSEA,
            home_goals=1,
            away_goals=1,
            half_home=0,
            half_away=0,
            when=f"2026-01-{10 + index:02d}T15:00:00Z",
        )
        for index in range(8)
    ]
    payload = {"matches": [old, recent, *filler]}
    plain = estimate_score(
        competition_finished_payload=payload,
        home_finished_payload={"matches": []},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=False,
        kickoff="2026-03-01T15:00:00Z",
    )
    decayed = estimate_score(
        competition_finished_payload=payload,
        home_finished_payload={"matches": []},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=False,
        kickoff="2026-03-01T15:00:00Z",
        decay_half_life_days=28,
    )
    # Recent 4-0 outweighs the old 0-0, so the raw home-attack ratio rises.
    assert decayed.ratios["attack_home"].raw > plain.ratios["attack_home"].raw
    assert decayed.ratios["attack_home"].n < plain.ratios["attack_home"].n
    assert any("half-life 28" in note for note in decayed.public()["assumptions"])


def test_multiplicative_rates_use_competition_priors() -> None:
    matches = _balanced_league()
    estimate = estimate_score(
        competition_finished_payload={"matches": matches},
        home_finished_payload={"matches": []},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=False,
        kickoff="2026-03-01T15:00:00Z",
    )
    assert estimate.prior_match_count == len(matches)
    assert estimate.league_home_avg > 0
    assert estimate.league_away_avg > 0
    assert estimate.dependence == "independent"
    assert not any("1.25" in note for note in estimate.limitations)
    assert not any("season table" in note for note in estimate.limitations)


def test_empty_competition_list_falls_back_to_club_histories() -> None:
    home = _games(ARSENAL, SPURS, venue="home", score=(2, 1), count=3, start_id=1)
    away = _games(CHELSEA, SPURS, venue="away", score=(1, 1), count=3, start_id=100)
    estimate = estimate_score(
        competition_finished_payload={"matches": []},
        home_finished_payload=home,
        away_finished_payload=away,
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=False,
        kickoff="2026-03-01T15:00:00Z",
    )
    assert any("two clubs' finished matches" in note for note in estimate.limitations)
    assert estimate.prior_match_count == 6


def test_half_time_uses_league_and_club_shares() -> None:
    matches = _balanced_league(with_half_time=True)
    estimate = estimate_score(
        competition_finished_payload={"matches": matches},
        home_finished_payload={"matches": []},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=True,
        kickoff="2026-03-01T15:00:00Z",
    )
    assert estimate.half_time_score is not None
    assert estimate.half_time_league_share is not None
    assert estimate.half_time_share_home is not None
    assert estimate.half_time_share_away is not None


def test_league_only_half_time_share_uses_one_value_for_both_clubs() -> None:
    matches = _balanced_league(with_half_time=True)
    club = estimate_score(
        competition_finished_payload={"matches": matches},
        home_finished_payload={"matches": []},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=True,
        kickoff="2026-03-01T15:00:00Z",
        half_time_share_mode="club_shrunk",
    )
    league = estimate_score(
        competition_finished_payload={"matches": matches},
        home_finished_payload={"matches": []},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=True,
        kickoff="2026-03-01T15:00:00Z",
        half_time_share_mode="league_only",
    )
    assert league.half_time_league_share is not None
    assert league.half_time_share_home == league.half_time_share_away == league.half_time_league_share
    assert any("one league first-half share" in note for note in league.public()["assumptions"])
    assert club.half_time_share_home != league.half_time_share_home or (
        club.half_time_share_away != league.half_time_share_away
    )


def test_fixed_half_time_share_modes() -> None:
    matches = _balanced_league(with_half_time=True)
    fixed_45 = estimate_score(
        competition_finished_payload={"matches": matches},
        home_finished_payload={"matches": []},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=True,
        kickoff="2026-03-01T15:00:00Z",
        half_time_share_mode="fixed_0_45",
    )
    fixed_50 = estimate_score(
        competition_finished_payload={"matches": matches},
        home_finished_payload={"matches": []},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=True,
        kickoff="2026-03-01T15:00:00Z",
        half_time_share_mode="fixed_0_50",
    )
    assert fixed_45.half_time_share_home == fixed_45.half_time_share_away == 0.45
    assert fixed_50.half_time_share_home == fixed_50.half_time_share_away == 0.50
    assert any("fixed first-half share of 0.45" in note for note in fixed_45.public()["assumptions"])
    assert any("fixed first-half share of 0.50" in note for note in fixed_50.public()["assumptions"])


def test_half_time_is_withheld_without_usable_samples() -> None:
    matches = _balanced_league(with_half_time=False)
    estimate = estimate_score(
        competition_finished_payload={"matches": matches},
        home_finished_payload={"matches": []},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=True,
        kickoff="2026-03-01T15:00:00Z",
    )
    assert estimate.half_time_score is None
    assert any("half-time" in note.lower() for note in estimate.limitations)


def test_kickoff_excludes_later_matches() -> None:
    early = make_match(
        1,
        ARSENAL,
        SPURS,
        home_goals=2,
        away_goals=1,
        when="2026-02-01T15:00:00Z",
    )
    late = make_match(
        2,
        CHELSEA,
        SPURS,
        home_goals=5,
        away_goals=0,
        when="2026-04-01T15:00:00Z",
    )
    estimate = estimate_score(
        competition_finished_payload={"matches": [early, late]},
        home_finished_payload={"matches": []},
        away_finished_payload={"matches": []},
        home_team_id=ARSENAL["id"],
        away_team_id=CHELSEA["id"],
        include_half_time=False,
        kickoff="2026-03-01T15:00:00Z",
    )
    assert estimate.prior_match_count == 1


def _balanced_league(*, with_half_time: bool = True) -> list[dict]:
    pairs = [
        (ARSENAL, SPURS, (2, 1)),
        (SPURS, ARSENAL, (1, 1)),
        (ARSENAL, CHELSEA, (2, 0)),
        (CHELSEA, ARSENAL, (1, 2)),
        (CHELSEA, SPURS, (1, 1)),
        (SPURS, CHELSEA, (0, 2)),
        (ARSENAL, SPURS, (1, 0)),
        (CHELSEA, SPURS, (2, 1)),
        (SPURS, ARSENAL, (0, 1)),
        (SPURS, CHELSEA, (1, 1)),
        (CHELSEA, ARSENAL, (0, 0)),
        (ARSENAL, CHELSEA, (3, 1)),
    ]
    matches = []
    for index, (home, away, score) in enumerate(pairs):
        half = (score[0] // 2, score[1] // 2) if with_half_time else (None, None)
        matches.append(
            make_match(
                100 + index,
                home,
                away,
                home_goals=score[0],
                away_goals=score[1],
                half_home=half[0],
                half_away=half[1],
                when=f"2026-02-{(index % 27) + 1:02d}T15:00:00Z",
            )
        )
    return matches


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
                when=f"2026-02-{20 - index:02d}T15:00:00Z",
            )
        )
    return {"matches": matches, "lastUpdated": "2026-03-01T12:00:00Z"}
