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
    assert 'role="status"' in page
    assert "Open Matches" in page


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
