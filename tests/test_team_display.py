"""Short display names for dashboard clubs."""

from __future__ import annotations

from pathlib import Path

from laboratory.store import connect, team_display_map, upsert_team
from laboratory.team_display import resolve_display_name


def test_resolve_user_examples() -> None:
    assert resolve_display_name("Brighton & Hove Albion FC") == "Brighton"
    assert resolve_display_name("Brentford FC") == "Brentford"
    assert resolve_display_name("AFC Bournemouth") == "Bournemouth"
    assert resolve_display_name("Nottingham Forest FC") == "Nottingham"


def test_resolve_fan_scoreboard_names() -> None:
    assert resolve_display_name("Manchester City FC") == "Man City"
    assert resolve_display_name("Manchester United FC") == "Man Utd"
    assert resolve_display_name("Tottenham Hotspur FC") == "Spurs"
    assert resolve_display_name("Wolverhampton Wanderers FC") == "Wolves"
    assert resolve_display_name("FC Internazionale Milano") == "Inter"
    assert resolve_display_name("Paris Saint-Germain FC") == "PSG"


def test_short_name_cleanup_fallback() -> None:
    assert resolve_display_name("Unknown FC", short_name="Brighton Hove") == "Brighton"
    assert resolve_display_name("Some New Club FC") == "Some New Club"


def test_strip_fd_match_prefix(tmp_path: Path) -> None:
    from laboratory.store import connect, migrate_strip_fd_match_prefix

    db = tmp_path / "fd.db"
    with connect(db) as connection:
        # Insert legacy-prefixed rows without going through the importer.
        connection.execute(
            """
            INSERT INTO matches (
                match_id, dataset_version, competition, season, kickoff,
                home_id, home_name, away_id, away_name
            ) VALUES ('fd-99', 'football-data-pl', 'PL', '2024', '2024-08-01T12:00:00Z',
                      '1', 'Home', '2', 'Away')
            """
        )
        connection.execute(
            """
            INSERT INTO result_revisions (
                match_id, revision, ft_home, ft_away, ht_home, ht_away, source
            ) VALUES ('fd-99', 1, 1, 0, 0, 0, 'football-data.org')
            """
        )
        connection.execute(
            """
            INSERT INTO engine_runs (run_id, experiment_id, match_id, engine, status, detail)
            VALUES ('EXP:fd-99:poisson', 'EXP', 'fd-99', 'poisson', 'ok', '')
            """
        )
        connection.execute(
            """
            INSERT INTO snapshots (snapshot_id, experiment_id, match_id, payload_json, leakage_ok)
            VALUES ('snap', 'EXP', 'fd-99', '{"match_id": "fd-99", "x": 1}', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO predictions (
                prediction_id, experiment_id, match_id, snapshot_id, engine,
                model_id, body_json, created_at
            ) VALUES ('p1', 'EXP', 'fd-99', 'snap', 'poisson', 'm', '{}', '2024-01-01T00:00:00Z')
            """
        )
        connection.commit()
        changed = migrate_strip_fd_match_prefix(connection)
        connection.commit()
        assert changed == 1
        assert connection.execute(
            "SELECT match_id FROM matches"
        ).fetchone()["match_id"] == "99"
        assert connection.execute(
            "SELECT match_id FROM result_revisions"
        ).fetchone()["match_id"] == "99"
        assert connection.execute(
            "SELECT run_id, match_id FROM engine_runs"
        ).fetchone()["run_id"] == "EXP:99:poisson"
        snap = connection.execute("SELECT match_id, payload_json FROM snapshots").fetchone()
        assert snap["match_id"] == "99"
        assert '"match_id": "99"' in snap["payload_json"]
        assert connection.execute(
            "SELECT match_id FROM predictions"
        ).fetchone()["match_id"] == "99"


def test_display_name_persisted(tmp_path: Path) -> None:
    db = tmp_path / "teams.db"
    with connect(db) as connection:
        upsert_team(connection, team_id="397", name="Brighton & Hove Albion FC")
        upsert_team(connection, team_id="65", name="Manchester City FC")
        connection.commit()
        labels = team_display_map(connection, ["397", "65"])
    assert labels["397"] == "Brighton"
    assert labels["65"] == "Man City"
