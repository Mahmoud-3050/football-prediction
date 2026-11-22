from __future__ import annotations

import json
from pathlib import Path

from laboratory.evaluate import evaluate_prediction
from laboratory.gemini_schema import SchemaError, validate_gemini_body
from laboratory.job import gemini_prompt, run_poisson
from laboratory.poisson_engine import FinishedMatch, _distribution, price_match
from laboratory.snapshot import MatchRecord, build_snapshot
from laboratory.store import append_result, connect, ensure_experiment, prediction_body, upsert_match
from laboratory.__main__ import seed_demo


def test_grid_sums_to_one_and_names_the_mode() -> None:
    distribution = _distribution(1.5, 1.1, 0.001)
    total = sum(cell["probability"] for cell in distribution["exact_scores"])
    assert abs(total - 1) < 1e-9
    assert distribution["tail_before_normalization"] <= 0.001
    assert distribution["exact_score_most_likely"] == {"home_goals": 1, "away_goals": 1}
    result = distribution["result_1x2"]
    assert result["most_likely"] == "home"
    assert abs(result["home"] + result["draw"] + result["away"] - 1) < 1e-9


def test_shrinkage_pulls_a_short_sample_toward_one() -> None:
    priors = [
        FinishedMatch("a", "2024", "2024-08-01T15:00:00Z", "H", "X", 2, 0, 1, 0),
        FinishedMatch("b", "2024", "2024-08-02T15:00:00Z", "Z", "Y", 0, 0, 0, 0),
    ]
    outcome = price_match(season="2024", home_id="H", away_id="Y", priors=priors, k=6)
    assert outcome.body is not None
    attack = outcome.body["ratios"]["attack_home"]
    assert attack["n"] == 1
    assert abs(attack["raw"] - 2.0) < 1e-9
    assert abs(attack["shrunk"] - (2 + 6) / 7) < 1e-9


def test_first_match_is_excluded() -> None:
    outcome = price_match(season="2024", home_id="H", away_id="A", priors=[])
    assert outcome.excluded is True
    assert outcome.body is None


def test_snapshot_hides_the_target_score_and_later_matches() -> None:
    target = MatchRecord("m2", "2024", "2024-08-17T14:00:00Z", "CHE", "Chelsea", "LIV", "Liverpool", 8, 7, 4, 3)
    earlier = MatchRecord("m1", "2024", "2024-08-16T19:00:00Z", "ARS", "Arsenal", "WOL", "Wolves", 2, 0, 1, 0)
    later = MatchRecord("m3", "2024", "2024-08-24T14:00:00Z", "ARS", "Arsenal", "CHE", "Chelsea", 3, 1, 2, 0)
    payload = build_snapshot(target, [target, earlier, later])
    assert payload["leakage_ok"] is True
    assert [item["match_id"] for item in payload["prior_matches"]] == ["m1"]
    assert "ft_home" not in payload["home"]
    prompt = gemini_prompt(payload, half_time_required=True)
    assert "lambda_home" not in prompt
    assert "poisson" not in prompt
    assert '"ft_home": 8' not in json.dumps(payload["prior_matches"])
    assert '"ft_away": 7' not in json.dumps(payload["prior_matches"])


def test_gemini_schema_accepts_a_complete_rectangle() -> None:
    cells = [
        {"home_goals": home, "away_goals": away, "probability": 0.0}
        for home in range(11)
        for away in range(11)
    ]
    cells[0]["probability"] = 1.0
    body = {
        "full_time_1x2": {"home": 0.0, "draw": 1.0, "away": 0.0},
        "full_time_most_likely": "draw",
        "exact_scores": cells,
        "exact_score_most_likely": {"home_goals": 0, "away_goals": 0},
        "half_time_withheld": True,
        "half_time_withheld_reason": "No prior half-time sample.",
        "over_under_2_5": {"over": 0.0, "under": 1.0},
        "over_under_2_5_most_likely": "under",
        "over_under_3_5": {"over": 0.0, "under": 1.0},
        "over_under_3_5_most_likely": "under",
        "analysis": "The snapshot has one prior match.",
    }
    validate_gemini_body(body, half_time_required=False)
    cells.pop()
    try:
        validate_gemini_body(body, half_time_required=False)
    except SchemaError as exc:
        assert "missing a cell" in str(exc)
    else:
        raise AssertionError("incomplete score list was accepted")


def test_predictions_are_not_replaced(tmp_path: Path) -> None:
    connection = connect(tmp_path / "lab.db")
    seed_demo(connection)
    first = run_poisson(connection)
    assert first["priced"] == 3
    assert first["excluded"] == 1
    stored = prediction_body(connection, _one_prediction(connection))
    second = run_poisson(connection)
    assert second["priced"] == 0
    assert second["existing"] == 3
    assert prediction_body(connection, _one_prediction(connection)) == stored


def test_evaluation_uses_the_played_score(tmp_path: Path) -> None:
    connection = connect(tmp_path / "lab.db")
    ensure_experiment(connection, {"experiment_id": "EXP-PL-001"})
    upsert_match(
        connection,
        {
            "match_id": "only",
            "dataset_version": "t",
            "season": "2024",
            "kickoff": "2024-09-01T15:00:00Z",
            "home_id": "H",
            "home_name": "Home",
            "away_id": "A",
            "away_name": "Away",
        },
    )
    append_result(connection, "only", {"ft_home": 1, "ft_away": 0, "ht_home": 1, "ht_away": 0}, "test")
    # A prior match so the target can be priced.
    upsert_match(
        connection,
        {
            "match_id": "prior",
            "dataset_version": "t",
            "season": "2024",
            "kickoff": "2024-08-01T15:00:00Z",
            "home_id": "H",
            "home_name": "Home",
            "away_id": "B",
            "away_name": "Other",
        },
    )
    append_result(connection, "prior", {"ft_home": 1, "ft_away": 0, "ht_home": 0, "ht_away": 0}, "test")
    connection.commit()
    run_poisson(connection)
    row = connection.execute(
        "SELECT metrics_json FROM evaluations e JOIN predictions p ON p.prediction_id = e.prediction_id WHERE p.match_id = 'only'"
    ).fetchone()
    metrics = json.loads(row["metrics_json"])
    assert metrics["full_time_1x2"]["actual"] == "home"
    assert "log_loss" in metrics["full_time_1x2"]
    body = json.loads(
        connection.execute(
            "SELECT body_json FROM predictions WHERE match_id = 'only'"
        ).fetchone()["body_json"]
    )
    again = evaluate_prediction(body, {"ft_home": 1, "ft_away": 0, "ht_home": 0, "ht_away": 0})
    assert again["half_time_1x2"]["actual"] == "draw"


def test_walk_forward_skips_unfinished_matches(tmp_path: Path) -> None:
    connection = connect(tmp_path / "lab.db")
    ensure_experiment(connection, {"experiment_id": "EXP-PL-001", "competition": "PL"})
    upsert_match(
        connection,
        {
            "match_id": "prior",
            "dataset_version": "t",
            "competition": "PL",
            "season": "2024",
            "kickoff": "2024-08-01T15:00:00Z",
            "home_id": "H",
            "home_name": "Home",
            "away_id": "B",
            "away_name": "Other",
        },
    )
    append_result(connection, "prior", {"ft_home": 1, "ft_away": 0, "ht_home": 0, "ht_away": 0}, "test")
    upsert_match(
        connection,
        {
            "match_id": "finished",
            "dataset_version": "t",
            "competition": "PL",
            "season": "2024",
            "kickoff": "2024-09-01T15:00:00Z",
            "home_id": "H",
            "home_name": "Home",
            "away_id": "A",
            "away_name": "Away",
        },
    )
    append_result(
        connection,
        "finished",
        {"ft_home": 2, "ft_away": 1, "ht_home": 1, "ht_away": 0},
        "test",
    )
    upsert_match(
        connection,
        {
            "match_id": "upcoming",
            "dataset_version": "t",
            "competition": "PL",
            "season": "2024",
            "kickoff": "2024-12-01T15:00:00Z",
            "home_id": "H",
            "home_name": "Home",
            "away_id": "C",
            "away_name": "Later",
        },
    )
    connection.commit()
    counts = run_poisson(connection)
    assert counts["skipped_unfinished"] >= 1
    assert connection.execute(
        "SELECT COUNT(*) AS n FROM predictions WHERE match_id = 'upcoming'"
    ).fetchone()["n"] == 0
    assert connection.execute(
        "SELECT COUNT(*) AS n FROM predictions WHERE match_id = 'finished'"
    ).fetchone()["n"] == 1


def test_import_keeps_the_raw_payload_and_does_not_duplicate_results(tmp_path: Path) -> None:
    from laboratory.football_data import import_bodies
    from laboratory.__main__ import seed_demo

    connection = connect(tmp_path / "lab.db")
    seed_demo(connection)
    body = {
        "matches": [
            {
                "id": 50,
                "utcDate": "2024-08-16T19:00:00Z",
                "status": "FINISHED",
                "season": {"startDate": "2024-08-16"},
                "homeTeam": {"id": 57, "name": "Arsenal"},
                "awayTeam": {"id": 76, "name": "Wolves"},
                "score": {"fullTime": {"home": 2, "away": 0}, "halfTime": {"home": 1, "away": 0}},
            },
            {
                "id": 51,
                "utcDate": "2026-11-21T15:00:00Z",
                "status": "TIMED",
                "season": {"startDate": "2026-08-21"},
                "homeTeam": {"id": 57, "name": "Arsenal"},
                "awayTeam": {"id": 61, "name": "Chelsea"},
                "score": {"fullTime": {}, "halfTime": {}},
            },
        ]
    }
    first = import_bodies(connection, [(2024, body)])
    assert first["demo_removed"] == 4
    assert first["matches"] == 2
    assert first["finished"] == 1
    assert first["results_added"] == 1
    assert connection.execute("SELECT COUNT(*) AS n FROM raw_payloads").fetchone()["n"] == 1
    second = import_bodies(connection, [(2024, body)])
    assert second["results_unchanged"] == 1
    assert second["results_added"] == 0
    revisions = connection.execute(
        "SELECT COUNT(*) AS n FROM result_revisions WHERE match_id = '50'"
    ).fetchone()["n"]
    assert revisions == 1
    stored = connection.execute(
        "SELECT season, ft_home FROM matches m JOIN result_revisions r ON r.match_id = m.match_id WHERE m.match_id = '50'"
    ).fetchone()
    assert stored["season"] == "2024"
    assert stored["ft_home"] == 2
    scheduled = connection.execute(
        "SELECT COUNT(*) AS n FROM result_revisions WHERE match_id = '51'"
    ).fetchone()["n"]
    assert scheduled == 0


def test_dashboard_filter_keeps_one_season(tmp_path: Path) -> None:
    from laboratory.dashboard import match_list
    from laboratory.__main__ import seed_demo

    connection = connect(tmp_path / "lab.db")
    seed_demo(connection)
    run_poisson(connection)
    page = match_list(
        connection,
        {
            "season": ["demo"],
            "verdict": ["correct"],
            "date_from": ["2020-01-01"],
            "date_to": ["2030-12-31"],
        },
    )
    assert "Matches" in page
    assert "Clear" in page
    assert 'name="engine" value="score_model"' in page
    assert "disabled" in page
    assert "Bot score model" in page
    assert 'name="league"' in page


def test_dashboard_matches_defaults_to_last_seven_days(tmp_path: Path) -> None:
    from datetime import date, timedelta

    from laboratory.dashboard import _default_match_date_range, match_list
    from laboratory.__main__ import seed_demo

    connection = connect(tmp_path / "lab.db")
    seed_demo(connection)
    today = date(2026, 11, 11)
    date_from, date_to = _default_match_date_range(today=today)
    assert date_from == "2026-11-04"
    assert date_to == "2026-11-11"
    assert date_to == today.isoformat()
    assert date.fromisoformat(date_from) == today - timedelta(days=7)

    page = match_list(connection, {})
    # Live default uses date.today(); assert the form fields are populated.
    live_from, live_to = _default_match_date_range()
    assert f'name="date_from" value="{live_from}"' in page
    assert f'name="date_to" value="{live_to}"' in page


def test_dashboard_league_filter_scopes_matches(tmp_path: Path) -> None:
    from laboratory.dashboard import match_list
    from laboratory.store import insert_evaluation, insert_prediction, record_run, save_snapshot

    connection = connect(tmp_path / "lab.db")
    ensure_experiment(
        connection,
        {
            "experiment_id": "BOT-PD-live",
            "competition": "PD",
            "status": "active",
            "engine": "score_model",
        },
    )
    ensure_experiment(
        connection,
        {
            "experiment_id": "BOT-PL-001",
            "competition": "PL",
            "status": "active",
            "engine": "score_model",
        },
    )
    for match_id, competition, home, away in (
        ("fd-pd-filter", "PD", "Atleti", "Barca"),
        ("fd-pl-filter", "PL", "Arsenal", "Chelsea"),
    ):
        upsert_match(
            connection,
            {
                "match_id": match_id,
                "dataset_version": "test",
                "competition": competition,
                "season": "2026",
                "kickoff": "2026-11-08T20:00:00Z",
                "home_id": home[:3],
                "home_name": home,
                "away_id": away[:3],
                "away_name": away,
            },
        )
        append_result(
            connection,
            match_id,
            {"ft_home": 1, "ft_away": 0, "ht_home": 0, "ht_away": 0},
            "test",
        )
        experiment_id = "BOT-PD-live" if competition == "PD" else "BOT-PL-001"
        payload = {
            "match_id": match_id,
            "payload_hash": match_id,
            "competition": competition,
            "kickoff": "2026-11-08T20:00:00Z",
            "home": {"id": home[:3], "name": home},
            "away": {"id": away[:3], "name": away},
            "prior_matches": [],
            "leakage_ok": True,
        }
        snapshot_id = save_snapshot(connection, payload, experiment_id=experiment_id)
        prediction_id = insert_prediction(
            connection,
            match_id=match_id,
            snapshot_id=snapshot_id,
            engine="score_model",
            model_id="bot-score-model-v1",
            body={
                "engine": "score_model",
                "full_time": {
                    "exact_score_most_likely": {"home_goals": 1, "away_goals": 0},
                    "result_1x2": {"most_likely": "home"},
                },
            },
            experiment_id=experiment_id,
        )
        record_run(
            connection,
            match_id=match_id,
            engine="score_model",
            status="completed",
            detail="priced",
            experiment_id=experiment_id,
        )
        insert_evaluation(
            connection,
            prediction_id,
            1,
            {"full_time_1x2": {"status": "scored", "correct": True}},
        )
    connection.commit()
    page = match_list(connection, {
        "engine": ["score_model"],
        "league": ["PD"],
        "date_from": ["2026-11-01"],
        "date_to": ["2026-11-30"],
    })
    assert "Atleti" in page
    assert "Barca" in page
    assert "Arsenal" not in page
    assert "La Liga" in page
    assert 'name="league"' in page
    assert "selected>La Liga" in page or 'value="PD" selected' in page
    assert 'class="match-card"' in page
    assert 'class="match-list"' in page
    assert 'class="match-row"' in page
    assert 'score-label">P<' in page
    assert "1X2" in page


def test_score_model_adapter_excludes_thin_club_history() -> None:
    from laboratory.score_model_adapter import price_from_snapshot

    payload = {
        "kickoff": "2024-09-01T15:00:00Z",
        "home": {"id": "H", "name": "Home"},
        "away": {"id": "A", "name": "Away"},
        "prior_matches": [
            {
                "match_id": "p1",
                "kickoff": "2024-08-01T15:00:00Z",
                "home_id": "H",
                "home_name": "Home",
                "away_id": "X",
                "away_name": "Other",
                "ft_home": 1,
                "ft_away": 0,
                "ht_home": 0,
                "ht_away": 0,
            }
        ],
    }
    outcome = price_from_snapshot(payload)
    assert outcome.excluded is True
    assert "abstain" in (outcome.reason or "").lower()


def test_score_model_adapter_builds_evaluable_nested_body() -> None:
    from laboratory.evaluate import evaluate_prediction
    from laboratory.score_model_adapter import price_from_snapshot

    priors = []
    day = 1
    for home_id, away_id, ft_h, ft_a, ht_h, ht_a in (
        ("H", "X", 2, 1, 1, 0),
        ("H", "Y", 1, 1, 0, 0),
        ("H", "Z", 3, 0, 1, 0),
        ("A", "X", 0, 2, 0, 1),
        ("A", "Y", 1, 1, 1, 0),
        ("A", "Z", 2, 1, 1, 1),
        ("X", "Y", 1, 0, 0, 0),
        ("Y", "Z", 2, 2, 1, 1),
        ("Z", "X", 0, 1, 0, 0),
    ):
        priors.append(
            {
                "match_id": f"p{day}",
                "kickoff": f"2024-08-{day:02d}T15:00:00Z",
                "home_id": home_id,
                "home_name": home_id,
                "away_id": away_id,
                "away_name": away_id,
                "ft_home": ft_h,
                "ft_away": ft_a,
                "ht_home": ht_h,
                "ht_away": ht_a,
            }
        )
        day += 1
    payload = {
        "kickoff": "2024-09-01T15:00:00Z",
        "home": {"id": "H", "name": "Home"},
        "away": {"id": "A", "name": "Away"},
        "prior_matches": priors,
    }
    outcome = price_from_snapshot(payload)
    assert outcome.excluded is False
    assert outcome.body is not None
    assert outcome.body["engine"] == "score_model"
    assert "full_time" in outcome.body
    assert "over_under_3_5" in outcome.body["full_time"]
    metrics = evaluate_prediction(
        outcome.body,
        {"ft_home": 2, "ft_away": 1, "ht_home": 1, "ht_away": 0},
    )
    assert metrics["full_time_1x2"]["status"] == "scored"
    assert "log_loss" in metrics["exact_score"]


def test_run_score_model_prices_and_evaluates(tmp_path: Path) -> None:
    from laboratory import BOT_EXPERIMENT_ID
    from laboratory.job import BOT_EXPERIMENT_CONFIG, run_score_model

    connection = connect(tmp_path / "lab.db")
    ensure_experiment(connection, BOT_EXPERIMENT_CONFIG)
    _seed_score_model_history(connection)
    counts = run_score_model(connection)
    assert counts["priced"] >= 1
    assert counts["excluded"] >= 1
    row = connection.execute(
        """
        SELECT p.engine, e.metrics_json
        FROM predictions p
        JOIN evaluations e ON e.prediction_id = p.prediction_id
        WHERE p.experiment_id = ? AND p.engine = 'score_model'
        LIMIT 1
        """,
        (BOT_EXPERIMENT_ID,),
    ).fetchone()
    assert row is not None
    metrics = json.loads(row["metrics_json"])
    assert metrics["full_time_1x2"]["status"] == "scored"


def test_dashboard_defaults_to_score_model_when_present(tmp_path: Path) -> None:
    from laboratory.dashboard import overview
    from laboratory.job import BOT_EXPERIMENT_CONFIG, run_score_model

    connection = connect(tmp_path / "lab.db")
    ensure_experiment(connection, BOT_EXPERIMENT_CONFIG)
    _seed_score_model_history(connection)
    run_score_model(connection)
    page = overview(connection, {})
    assert "Overview" in page
    assert "Bot score model" in page
    assert "FT 1X2 accuracy" in page
    assert 'href="/k-grid"' not in page
    assert 'href="/d3"' not in page


def test_dashboard_shows_non_pl_live_score_model_predictions(tmp_path: Path) -> None:
    from laboratory.dashboard import match_list
    from laboratory.store import insert_evaluation, insert_prediction, record_run, save_snapshot

    connection = connect(tmp_path / "lab.db")
    ensure_experiment(
        connection,
        {
            "experiment_id": "BOT-PD-live",
            "competition": "PD",
            "status": "active",
            "engine": "score_model",
            "score_model_config_id": "bot-score-model-live",
        },
    )
    upsert_match(
        connection,
        {
            "match_id": "fd-pd-1",
            "dataset_version": "football-data-pd",
            "competition": "PD",
            "season": "2026",
            "kickoff": "2026-11-08T20:00:00Z",
            "home_id": "ATM",
            "home_name": "Club Atlético de Madrid",
            "away_id": "FCB",
            "away_name": "FC Barcelona",
        },
    )
    append_result(
        connection,
        "fd-pd-1",
        {"ft_home": 2, "ft_away": 1, "ht_home": 1, "ht_away": 0},
        "test",
    )
    payload = {
        "match_id": "fd-pd-1",
        "payload_hash": "pdhash1",
        "competition": "PD",
        "kickoff": "2026-11-08T20:00:00Z",
        "home": {"id": "ATM", "name": "Club Atlético de Madrid"},
        "away": {"id": "FCB", "name": "FC Barcelona"},
        "prior_matches": [],
        "leakage_ok": True,
    }
    snapshot_id = save_snapshot(connection, payload, experiment_id="BOT-PD-live")
    body = {
        "engine": "score_model",
        "full_time": {
            "exact_score_most_likely": {"home_goals": 1, "away_goals": 2},
            "result_1x2": {"most_likely": "away", "home": 0.2, "draw": 0.3, "away": 0.5},
        },
    }
    prediction_id = insert_prediction(
        connection,
        match_id="fd-pd-1",
        snapshot_id=snapshot_id,
        engine="score_model",
        model_id="bot-score-model-live",
        body=body,
        experiment_id="BOT-PD-live",
    )
    assert prediction_id is not None
    record_run(
        connection,
        match_id="fd-pd-1",
        engine="score_model",
        status="completed",
        detail="priced",
        experiment_id="BOT-PD-live",
    )
    insert_evaluation(
        connection,
        prediction_id,
        1,
        {
            "full_time_1x2": {
                "status": "scored",
                "correct": False,
                "log_loss": 0.7,
                "brier": 0.3,
            }
        },
    )
    connection.commit()
    page = match_list(connection, {
        "engine": ["score_model"],
        "season": ["2026"],
        "date_from": ["2026-11-01"],
        "date_to": ["2026-11-30"],
    })
    assert "Club Atlético de Madrid" in page
    assert "1–2" in page
    assert "Away" in page
    assert "Incorrect" in page


def _seed_score_model_history(connection) -> None:
    fixtures = [
        ("s1", "2024-08-10T15:00:00Z", "H", "Home", "X", "OtherX", 2, 1, 1, 0),
        ("s2", "2024-08-11T15:00:00Z", "H", "Home", "Y", "OtherY", 1, 0, 0, 0),
        ("s3", "2024-08-12T15:00:00Z", "H", "Home", "Z", "OtherZ", 3, 1, 2, 0),
        ("s4", "2024-08-13T15:00:00Z", "A", "Away", "X", "OtherX", 0, 1, 0, 0),
        ("s5", "2024-08-14T15:00:00Z", "A", "Away", "Y", "OtherY", 2, 2, 1, 1),
        ("s6", "2024-08-15T15:00:00Z", "A", "Away", "Z", "OtherZ", 1, 0, 1, 0),
        ("s7", "2024-08-16T15:00:00Z", "X", "OtherX", "Y", "OtherY", 1, 1, 0, 0),
        ("s8", "2024-08-17T15:00:00Z", "Y", "OtherY", "Z", "OtherZ", 2, 0, 1, 0),
        ("target", "2024-09-01T15:00:00Z", "H", "Home", "A", "Away", 2, 1, 1, 0),
    ]
    for match_id, kickoff, home_id, home_name, away_id, away_name, ft_h, ft_a, ht_h, ht_a in fixtures:
        upsert_match(
            connection,
            {
                "match_id": match_id,
                "dataset_version": "score-model-test",
                "season": "2024",
                "kickoff": kickoff,
                "home_id": home_id,
                "home_name": home_name,
                "away_id": away_id,
                "away_name": away_name,
            },
        )
        append_result(
            connection,
            match_id,
            {"ft_home": ft_h, "ft_away": ft_a, "ht_home": ht_h, "ht_away": ht_a},
            "test",
        )
    connection.commit()


def _one_prediction(connection) -> str:
    return connection.execute("SELECT prediction_id FROM predictions LIMIT 1").fetchone()["prediction_id"]
