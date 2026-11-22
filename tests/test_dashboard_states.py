"""Dashboard empty, error, and edge-case states."""

from __future__ import annotations

from pathlib import Path

from laboratory.dashboard import (
    _apply,
    _confidence_threshold,
    _filter_form,
    match_detail,
    match_list,
    not_found_page,
    overview,
    server_error_page,
    standings,
)
from laboratory.store import connect


def test_confidence_threshold_parser() -> None:
    assert _confidence_threshold("all") is None
    assert _confidence_threshold("65") == 0.65
    assert _confidence_threshold("0.55") == 0.55


def test_market_confidences_cover_ft_ht_score_and_goals() -> None:
    from laboratory.dashboard import (
        _ft_1x2_from_body,
        _match_table,
        _prediction_market_confidences,
    )

    body = {
        "engine": "score_model",
        "full_time": {
            "result_1x2": {
                "home": 0.62,
                "draw": 0.22,
                "away": 0.16,
                "most_likely": "home",
            },
            "exact_score_most_likely": {"home_goals": 1, "away_goals": 0},
            "exact_scores": [
                {"home_goals": 1, "away_goals": 0, "probability": 0.18},
                {"home_goals": 2, "away_goals": 0, "probability": 0.12},
            ],
            "over_under_2_5": {"over": 0.44, "under": 0.56, "most_likely": "under"},
            "over_under_3_5": {"over": 0.28, "under": 0.72, "most_likely": "under"},
        },
        "half_time": {
            "withheld": False,
            "result_1x2": {
                "home": 0.41,
                "draw": 0.38,
                "away": 0.21,
                "most_likely": "home",
            },
        },
    }
    selection, probability = _ft_1x2_from_body(body)
    assert selection == "Home"
    assert probability == 0.62
    markets = _prediction_market_confidences(body)
    assert markets["full_time_1x2"]["selection"] == "Home"
    assert markets["full_time_1x2"]["confidence"] == 0.62
    assert markets["half_time_1x2"]["selection"] == "Home"
    assert markets["half_time_1x2"]["confidence"] == 0.41
    assert markets["exact_score"]["selection"] == "1–0"
    assert markets["exact_score"]["confidence"] == 0.18
    assert markets["over_under_2_5"]["selection"] == "Under"
    assert markets["over_under_2_5"]["confidence"] == 0.56
    assert markets["over_under_3_5"]["selection"] == "Under"
    assert markets["over_under_3_5"]["confidence"] == 0.72

    html_page = _match_table(
        [
            {
                "match_id": "m1",
                "competition": "SPL",
                "season": "2026",
                "home_name": "Al Hilal",
                "away_name": "Al Nassr",
                "kickoff": "2026-11-02T15:00:00Z",
                "ft_home": 1,
                "ft_away": 0,
                "engine_status": "completed",
                "metrics": {
                    "full_time_1x2": {
                        "status": "scored",
                        "selection": "home",
                        "actual": "home",
                        "correct": True,
                    }
                },
                "body": body,
            }
        ],
        {
            "league": "",
            "season": "",
            "team": "",
            "date_from": "",
            "date_to": "",
            "fixture": "all",
            "status": "all",
            "verdict": "all",
            "confidence": "all",
            "order": "newest",
        },
    )
    assert "market-strip" in html_page
    assert "FT 1X2" in html_page
    assert "HT" in html_page
    assert "Goals 2.5" in html_page
    assert "Goals 3.5" in html_page
    assert "62%" in html_page
    assert "56%" in html_page
    assert "72%" in html_page


def test_confidence_filter_keeps_high_prob_picks_only() -> None:
    rows = [
        {
            "competition": "PL",
            "season": "2026",
            "home_name": "A",
            "away_name": "B",
            "kickoff": "2026-11-01T15:00:00Z",
            "match_id": "1",
            "ft_home": 1,
            "ft_away": 0,
            "engine_status": "completed",
            "metrics": {
                "full_time_1x2": {
                    "status": "scored",
                    "selection": "home",
                    "actual": "home",
                    "correct": True,
                }
            },
            "body": {
                "engine": "score_model",
                "full_time": {"result_1x2": {"home": 0.7, "draw": 0.2, "away": 0.1}},
            },
        },
        {
            "competition": "PL",
            "season": "2026",
            "home_name": "C",
            "away_name": "D",
            "kickoff": "2026-11-02T15:00:00Z",
            "match_id": "2",
            "ft_home": 0,
            "ft_away": 1,
            "engine_status": "completed",
            "metrics": {
                "full_time_1x2": {
                    "status": "scored",
                    "selection": "away",
                    "actual": "away",
                    "correct": True,
                }
            },
            "body": {
                "engine": "score_model",
                "full_time": {"result_1x2": {"home": 0.3, "draw": 0.3, "away": 0.4}},
            },
        },
    ]
    filters = {
        "league": "",
        "season": "",
        "team": "",
        "date_from": "",
        "date_to": "",
        "fixture": "all",
        "status": "all",
        "verdict": "all",
        "confidence": "65",
        "order": "newest",
    }
    kept = _apply(rows, filters)
    assert [row["match_id"] for row in kept] == ["1"]
    form = _filter_form("/", filters, ["PL"], ["2026"], ["A"], submit="Show")
    assert 'name="confidence"' in form
    assert "≥ 65%" in form


def test_overview_empty_database(tmp_path: Path) -> None:
    connection = connect(tmp_path / "lab.db")
    page = overview(connection)
    assert "No matches in the laboratory yet" in page
    assert "Server jobs" in page
    assert 'role="status"' in page
    assert "Open Matches" in page
    assert 'aria-label="Evaluation charts"' not in page


def test_overview_charts_render_for_scored_rows() -> None:
    from laboratory.dashboard import _overview_charts

    rows = []
    for index, (month, pick, actual, home_p) in enumerate(
        (
            ("2026-08-01", "home", "home", 0.55),
            ("2026-08-15", "home", "away", 0.62),
            ("2026-09-01", "draw", "draw", 0.41),
            ("2026-09-20", "away", "away", 0.48),
            ("2026-10-05", "home", "home", 0.71),
            ("2026-10-18", "home", "draw", 0.66),
            ("2026-11-01", "away", "home", 0.39),
            ("2026-11-08", "home", "home", 0.58),
        ),
        start=1,
    ):
        rows.append(
            {
                "kickoff": f"{month}T15:00:00Z",
                "metrics": {
                    "full_time_1x2": {
                        "status": "scored",
                        "selection": pick,
                        "actual": actual,
                        "correct": pick == actual,
                        "probability_of_actual": home_p if actual == "home" else 0.25,
                        "log_loss": 0.5,
                        "brier": 0.4,
                    },
                    "exact_score": {
                        "status": "scored",
                        "correct": index % 4 == 0,
                        "log_loss": 2.0,
                    },
                    "over_under_2_5": {
                        "status": "scored",
                        "correct": True,
                        "log_loss": 0.4,
                        "brier": 0.2,
                    },
                    "over_under_3_5": {
                        "status": "scored",
                        "correct": False,
                        "log_loss": 0.7,
                        "brier": 0.3,
                    },
                    "half_time_1x2": {"status": "withheld"},
                },
                "body": {
                    "engine": "score_model",
                    "full_time": {
                        "result_1x2": {
                            "home": home_p if pick == "home" else 0.25,
                            "draw": home_p if pick == "draw" else 0.25,
                            "away": home_p if pick == "away" else 0.25,
                            "most_likely": pick,
                        }
                    },
                },
            }
        )
    html_page = _overview_charts(rows)
    assert "charts-panel" in html_page
    assert "Market accuracy" in html_page
    assert "Full-time accuracy by month" in html_page
    assert "FT selection calibration" in html_page
    assert "hbar-chart" in html_page
    assert "vbar-chart" in html_page
    assert "cal-chart" in html_page


def test_matches_empty_database(tmp_path: Path) -> None:
    connection = connect(tmp_path / "lab.db")
    page = match_list(connection)
    assert "No matches in the laboratory yet" in page
    assert "Back to Overview" in page


def test_match_not_found_and_server_error_pages() -> None:
    missing = not_found_page(resource="Match")
    assert "Match not found" in missing
    assert "Browse matches" in missing
    assert 'class="state state-empty"' in missing
    error = server_error_page(detail="boom")
    assert "Something went wrong" in error
    assert "boom" in error
    assert 'role="alert"' in error
    assert 'class="state state-error"' in error


def test_standings_empty_without_api_key(tmp_path: Path) -> None:
    connection = connect(tmp_path / "lab.db")
    page = standings(connection, {"league": ["PL"]}, api_key=None)
    assert "No standings stored for this league yet" in page
    assert "FOOTBALL_API_KEY" in page or "import-standings" in page


def test_match_detail_missing_returns_none(tmp_path: Path) -> None:
    connection = connect(tmp_path / "lab.db")
    assert match_detail(connection, "missing-id") is None
