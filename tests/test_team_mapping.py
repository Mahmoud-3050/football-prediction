"""Offline verification & coverage tests for central team_mapping and strict resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from db.storage import Storage
from db.team_mapping import (
    UnmappedTeamError,
    audit_team_mappings,
    backfill_team_mappings,
    register_team_mapping,
    resolve_team_id,
)
from laboratory.football_data import import_bodies
from laboratory.standings import parse_standings, replace_standings
from laboratory.store import connect


def test_strict_resolution_across_providers_and_no_fuzzy_fallback(tmp_path: Path) -> None:
    conn = connect(tmp_path / "mapping.db")
    try:
        tid_hilal = register_team_mapping(
            conn,
            canonical_name="Al Hilal",
            provider="live_football",
            provider_name="Al Hilal",
            provider_team_id="68898",
            competition="SPL",
        )
        register_team_mapping(
            conn,
            canonical_name="Al Hilal",
            provider="api_ninjas",
            provider_name="Al Hilal",
            provider_team_id="772867506",
            competition="SPL",
            team_id=tid_hilal,
        )
        tid_feiha = register_team_mapping(
            conn,
            canonical_name="Feiha",
            provider="live_football",
            provider_name="Feiha",
            provider_team_id="68895",
            competition="SPL",
        )
        register_team_mapping(
            conn,
            canonical_name="Feiha",
            provider="api_ninjas",
            provider_name="Al Fayha",
            provider_team_id="772867505",
            competition="SPL",
            team_id=tid_feiha,
        )
        tid_arsenal = register_team_mapping(
            conn,
            canonical_name="Arsenal FC",
            provider="football_data",
            provider_name="Arsenal FC",
            provider_team_id="57",
            competition="PL",
        )
        conn.commit()

        # 1. Exact resolution by provider_team_id and provider_name
        assert (
            resolve_team_id(conn, provider="live_football", raw_name="Al Hilal", provider_team_id="68898")
            == tid_hilal
        )
        assert (
            resolve_team_id(conn, provider="api_ninjas", raw_name="Al Fayha", provider_team_id="772867505")
            == tid_feiha
        )
        assert (
            resolve_team_id(conn, provider="api_ninjas", raw_name="al fayha")
            == tid_feiha
        )
        assert (
            resolve_team_id(conn, provider="football_data", raw_name="Arsenal FC", provider_team_id=57)
            == tid_arsenal
        )

        # 2. Strict failure on unknown or near-miss typo (zero fuzzy matching)
        with pytest.raises(UnmappedTeamError) as exc_info:
            resolve_team_id(conn, provider="football_data", raw_name="Arsnal FC", competition="PL")
        assert "football_data" in str(exc_info.value)
        assert "Arsnal FC" in str(exc_info.value)
        assert "python -m laboratory map-team" in str(exc_info.value)

        with pytest.raises(UnmappedTeamError):
            resolve_team_id(conn, provider="api_ninjas", raw_name="Unknown Saudi Club", competition="SPL")
    finally:
        conn.close()


def test_ingestion_strict_mapping_fails_loudly_on_unmapped_team(tmp_path: Path) -> None:
    conn = connect(tmp_path / "ingest.db")
    try:
        register_team_mapping(
            conn,
            canonical_name="Arsenal FC",
            provider="football_data",
            provider_name="Arsenal FC",
            provider_team_id="57",
            competition="PL",
        )
        conn.commit()

        # Unmapped away team must raise UnmappedTeamError when provider mappings exist
        body = {
            "competition": {"code": "PL", "name": "Premier League"},
            "matches": [
                {
                    "id": 999001,
                    "utcDate": "2026-08-15T15:00:00Z",
                    "status": "FINISHED",
                    "homeTeam": {"id": 57, "name": "Arsenal FC"},
                    "awayTeam": {"id": 999999, "name": "Unmapped Rovers FC"},
                    "score": {"fullTime": {"home": 2, "away": 0}, "halfTime": {"home": 1, "away": 0}},
                }
            ],
        }
        with pytest.raises(UnmappedTeamError):
            import_bodies(conn, [(2026, body)], competition="PL")

        # Unmapped standings row must also fail loudly when strict
        standings_payload = {
            "competition": {"code": "PL", "name": "Premier League"},
            "season": {"startDate": "2026-08-01"},
            "standings": [
                {
                    "type": "TOTAL",
                    "table": [
                        {
                            "position": 1,
                            "team": {"id": 888888, "name": "Mystery United"},
                            "playedGames": 1,
                            "won": 1,
                            "draw": 0,
                            "lost": 0,
                            "goalsFor": 2,
                            "goalsAgainst": 0,
                            "goalDifference": 2,
                            "points": 3,
                        }
                    ],
                }
            ],
        }
        parsed = parse_standings(standings_payload, competition="PL")
        with pytest.raises(UnmappedTeamError):
            replace_standings(conn, parsed, strict_team_mapping=True)
    finally:
        conn.close()


def test_backfill_and_spl_plus_supported_leagues_coverage(tmp_path: Path) -> None:
    db_path = tmp_path / "coverage.db"
    store = Storage(db_path)
    try:
        conn = store._conn
        # Seed SPL matches (LFA ids) + European matches + SPL standings (Ninjas ids)
        conn.execute(
            """
            INSERT INTO matches (match_id, dataset_version, competition, season, kickoff, home_id, home_name, away_id, away_name)
            VALUES
              ('spl-1', 'v1', 'SPL', '2025', '2025-09-01T18:00:00Z', '68895', 'Feiha', '68898', 'Al Hilal'),
              ('pl-1', 'v1', 'PL', '2025', '2025-09-01T15:00:00Z', '57', 'Arsenal FC', '65', 'Manchester City FC')
            """
        )
        conn.execute(
            """
            INSERT INTO standings_rows (competition, season, position, team_id, team_name, played, won, draw, lost, goals_for, goals_against, goal_difference, points, form)
            VALUES ('SPL', '2025', 1, '772867505', 'Al Fayha', 5, 3, 1, 1, 8, 4, 4, 10, 'WWDLW')
            """
        )
        conn.commit()

        summary = backfill_team_mappings(conn)
        assert summary["canonical_added"] >= 4
        assert summary["mappings_added"] >= 4

        audit = audit_team_mappings(conn)
        assert audit["ok"] is True
        assert audit["unmapped_match_teams"] == []
        assert audit["orphaned_mappings"] == 0

        # Verify SPL cross-provider mapping: Ninjas "Al Fayha" resolves to LFA "68895"
        assert (
            resolve_team_id(conn, provider="api_ninjas", raw_name="Al Fayha", provider_team_id="772867505")
            == "68895"
        )
        assert (
            resolve_team_id(conn, provider="live_football", raw_name="Feiha", provider_team_id="68895")
            == "68895"
        )
        assert (
            resolve_team_id(conn, provider="football_data", raw_name="Arsenal FC", provider_team_id="57")
            == "57"
        )
    finally:
        store.close()
