"""Dashboard empty, error, and edge-case states."""

from __future__ import annotations

from pathlib import Path

from laboratory.dashboard import (
    match_detail,
    match_list,
    not_found_page,
    overview,
    server_error_page,
    standings,
)
from laboratory.store import connect


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
