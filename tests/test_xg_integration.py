"""Offline tests for Secondary Expected Goals (xG) storage, blending, and Lab integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from db.team_mapping import UnmappedTeamError, register_team_mapping
from db.xg_store import attach_xg_to_matches, import_xg_records
from laboratory.lab_experiment import ExperimentConfig
from laboratory.lab_runner import run_experiment
from laboratory.store import connect
from modeling.score_model import estimate_score


def _make_prior_match(
    mid: int,
    kickoff: str,
    home_id: int,
    away_id: int,
    hg: int,
    ag: int,
    *,
    home_xg: float | None = None,
    away_xg: float | None = None,
) -> dict:
    item = {
        "id": mid,
        "utcDate": kickoff,
        "status": "FINISHED",
        "homeTeam": {"id": home_id, "name": f"Team-{home_id}"},
        "awayTeam": {"id": away_id, "name": f"Team-{away_id}"},
        "score": {"fullTime": {"home": hg, "away": ag}},
    }
    if home_xg is not None:
        item["home_xg"] = home_xg
    if away_xg is not None:
        item["away_xg"] = away_xg
    return item


def test_xg_weight_zero_and_missing_xg_are_bit_identical() -> None:
    matches_no_xg = [
        _make_prior_match(1, "2026-09-01T15:00:00Z", 1, 2, 2, 1),
        _make_prior_match(2, "2026-09-08T15:00:00Z", 2, 1, 1, 1),
        _make_prior_match(3, "2026-09-15T15:00:00Z", 1, 3, 3, 0),
        _make_prior_match(4, "2026-09-22T15:00:00Z", 3, 2, 0, 2),
    ]
    matches_with_xg = [
        _make_prior_match(1, "2026-09-01T15:00:00Z", 1, 2, 2, 1, home_xg=1.4, away_xg=0.9),
        _make_prior_match(2, "2026-09-08T15:00:00Z", 2, 1, 1, 1, home_xg=1.1, away_xg=1.5),
        _make_prior_match(3, "2026-09-15T15:00:00Z", 1, 3, 3, 0, home_xg=2.2, away_xg=0.3),
        _make_prior_match(4, "2026-09-22T15:00:00Z", 3, 2, 0, 2, home_xg=0.5, away_xg=1.8),
    ]
    payload_no = {"matches": matches_no_xg}
    payload_xg = {"matches": matches_with_xg}

    base = estimate_score(
        competition_finished_payload=payload_no,
        home_finished_payload=payload_no,
        away_finished_payload=payload_no,
        home_team_id=1,
        away_team_id=2,
        include_half_time=False,
        kickoff="2026-10-01T15:00:00Z",
        xg_weight=0.0,
    )
    with_zero = estimate_score(
        competition_finished_payload=payload_xg,
        home_finished_payload=payload_xg,
        away_finished_payload=payload_xg,
        home_team_id=1,
        away_team_id=2,
        include_half_time=False,
        kickoff="2026-10-01T15:00:00Z",
        xg_weight=0.0,
    )
    assert with_zero.lambda_home == base.lambda_home
    assert with_zero.lambda_away == base.lambda_away
    assert with_zero.probabilities == base.probabilities

    # Missing xG across all matches with xg_weight > 0 must fall back to 100% actual goals (never 0.0).
    missing_xg_blended = estimate_score(
        competition_finished_payload=payload_no,
        home_finished_payload=payload_no,
        away_finished_payload=payload_no,
        home_team_id=1,
        away_team_id=2,
        include_half_time=False,
        kickoff="2026-10-01T15:00:00Z",
        xg_weight=0.4,
    )
    assert missing_xg_blended.lambda_home == pytest.approx(base.lambda_home)
    assert missing_xg_blended.lambda_away == pytest.approx(base.lambda_away)


def test_high_xg_unlucky_team_gets_higher_lambda_with_xg_weight() -> None:
    matches = [
        _make_prior_match(1, "2026-09-01T15:00:00Z", 1, 2, 0, 1, home_xg=2.4, away_xg=0.7),
        _make_prior_match(2, "2026-09-08T15:00:00Z", 1, 3, 0, 0, home_xg=2.4, away_xg=0.4),
        _make_prior_match(3, "2026-09-15T15:00:00Z", 1, 4, 0, 1, home_xg=2.4, away_xg=0.6),
        _make_prior_match(4, "2026-09-22T15:00:00Z", 2, 1, 1, 1, home_xg=1.0, away_xg=1.5),
        _make_prior_match(5, "2026-09-29T15:00:00Z", 3, 2, 1, 2, home_xg=1.1, away_xg=1.6),
        _make_prior_match(6, "2026-10-06T15:00:00Z", 4, 2, 0, 2, home_xg=0.8, away_xg=1.7),
    ]
    payload = {"matches": matches}
    est_0 = estimate_score(
        competition_finished_payload=payload,
        home_finished_payload=payload,
        away_finished_payload=payload,
        home_team_id=1,
        away_team_id=2,
        include_half_time=False,
        kickoff="2026-10-15T15:00:00Z",
        xg_weight=0.0,
    )
    est_4 = estimate_score(
        competition_finished_payload=payload,
        home_finished_payload=payload,
        away_finished_payload=payload,
        home_team_id=1,
        away_team_id=2,
        include_half_time=False,
        kickoff="2026-10-15T15:00:00Z",
        xg_weight=0.4,
    )
    assert est_4.lambda_home > est_0.lambda_home


def test_import_xg_records_strict_team_mapping_and_attach(tmp_path: Path) -> None:
    db_path = tmp_path / "football.db"
    conn = connect(db_path)
    try:
        register_team_mapping(
            conn,
            canonical_name="Arsenal",
            provider="understat",
            provider_name="Arsenal",
            competition="PL",
            team_id="57",
        )
        register_team_mapping(
            conn,
            canonical_name="Chelsea",
            provider="understat",
            provider_name="Chelsea",
            competition="PL",
            team_id="61",
        )
        conn.commit()

        # Unknown team must raise UnmappedTeamError
        with pytest.raises(UnmappedTeamError):
            import_xg_records(
                conn,
                [
                    {
                        "home_team": "Arsenal",
                        "away_team": "Unknown FC",
                        "kickoff_utc": "2026-10-01T15:00:00Z",
                        "home_xg": 1.8,
                        "away_xg": 0.9,
                    }
                ],
                source="understat",
                competition="PL",
                season=2026,
            )

        res = import_xg_records(
            conn,
            [
                {
                    "home_team": "Arsenal",
                    "away_team": "Chelsea",
                    "kickoff_utc": "2026-10-01T15:00:00Z",
                    "home_xg": 1.85,
                    "away_xg": 0.75,
                }
            ],
            source="understat",
            competition="PL",
            season=2026,
        )
        assert res["imported"] == 1

        attached = attach_xg_to_matches(
            conn,
            [
                {
                    "competition": "PL",
                    "home_id": "57",
                    "away_id": "61",
                    "kickoff": "2026-10-01T17:00:00Z",
                }
            ],
        )
        assert attached[0]["home_xg"] == pytest.approx(1.85)
        assert attached[0]["away_xg"] == pytest.approx(0.75)
    finally:
        conn.close()


def test_run_experiment_xg_weight_grid_offline(tmp_path: Path) -> None:
    live_db = tmp_path / "live.db"
    lab_db = tmp_path / "lab.db"
    conn = connect(live_db)
    try:
        teams = [("1", "Club A"), ("2", "Club B"), ("3", "Club C"), ("4", "Club D")]
        for tid, name in teams:
            register_team_mapping(
                conn,
                canonical_name=name,
                provider="understat",
                provider_name=name,
                competition="PL",
                team_id=tid,
            )
        fixtures = [
            ("m1", "2025-08-01T15:00:00Z", "1", "Club A", "2", "Club B", 2, 1, 1.9, 0.8),
            ("m2", "2025-08-02T15:00:00Z", "3", "Club C", "4", "Club D", 1, 1, 1.2, 1.1),
            ("m3", "2025-08-08T15:00:00Z", "2", "Club B", "3", "Club C", 0, 2, 0.6, 1.7),
            ("m4", "2025-08-09T15:00:00Z", "4", "Club D", "1", "Club A", 1, 3, 0.9, 2.4),
            ("m5", "2025-08-15T15:00:00Z", "1", "Club A", "3", "Club C", 2, 0, 2.1, 0.5),
            ("m6", "2025-08-16T15:00:00Z", "2", "Club B", "4", "Club D", 1, 0, 1.3, 0.7),
            ("m7", "2025-08-22T15:00:00Z", "3", "Club C", "1", "Club A", 1, 2, 1.0, 1.8),
            ("m8", "2025-08-23T15:00:00Z", "4", "Club D", "2", "Club B", 2, 2, 1.5, 1.6),
            ("m9", "2025-08-29T15:00:00Z", "1", "Club A", "4", "Club D", 3, 1, 2.5, 0.9),
            ("m10", "2025-08-30T15:00:00Z", "2", "Club B", "1", "Club A", 0, 1, 0.7, 1.6),
            ("m11", "2025-09-05T15:00:00Z", "3", "Club C", "2", "Club B", 2, 1, 1.7, 1.0),
            ("m12", "2025-09-06T15:00:00Z", "4", "Club D", "3", "Club C", 0, 1, 0.8, 1.4),
        ]
        for mid, kick, hid, hname, aid, aname, hg, ag, hxg, axg in fixtures:
            conn.execute(
                """
                INSERT INTO matches (match_id, dataset_version, competition, season, kickoff, home_id, home_name, away_id, away_name)
                VALUES (?, 'v1', 'PL', '2025', ?, ?, ?, ?, ?)
                """,
                (mid, kick, hid, hname, aid, aname),
            )
            conn.execute(
                """
                INSERT INTO result_revisions (match_id, revision, ft_home, ft_away, source)
                VALUES (?, 1, ?, ?, 'test')
                """,
                (mid, hg, ag),
            )
        conn.commit()
        import_xg_records(
            conn,
            [
                {
                    "home_team": hname,
                    "away_team": aname,
                    "kickoff_utc": kick,
                    "home_xg": hxg,
                    "away_xg": axg,
                }
                for _, kick, _, hname, _, aname, _, _, hxg, axg in fixtures
            ],
            source="understat",
            competition="PL",
            season=2025,
        )
    finally:
        conn.close()

    cfg = ExperimentConfig(
        experiment_id="EXP-XG-TEST-01",
        competition="PL",
        seasons=(2025,),
        base_config={"k": 6.0, "prior_mean": "one", "xg_weight": 0.0},
        param_grid={"xg_weight": [0.0, 0.35]},
        min_matches=1,
    )
    res = run_experiment(
        lab_db_path=lab_db,
        live_db_path=live_db,
        config=cfg,
        max_workers=1,
    )
    assert res.completed_runs == 2
    assert res.failed_runs == 0


def test_live_pipeline_attach_xg_and_missing_match_fallback(tmp_path: Path) -> None:
    from db.storage import Storage
    from live.pipeline import Pipeline

    db_path = tmp_path / "live.db"
    store = Storage(db_path)
    try:
        conn = store._conn
        register_team_mapping(
            conn,
            canonical_name="Arsenal",
            provider="fpl_opta_github",
            provider_name="Arsenal",
            competition="PL",
            team_id="57",
        )
        register_team_mapping(
            conn,
            canonical_name="Chelsea",
            provider="fpl_opta_github",
            provider_name="Chelsea",
            competition="PL",
            team_id="61",
        )
        import_xg_records(
            conn,
            [
                {
                    "home_team": "Arsenal",
                    "away_team": "Chelsea",
                    "kickoff_utc": "2026-10-01T15:00:00Z",
                    "home_xg": 2.1,
                    "away_xg": 0.6,
                }
            ],
            source="fpl_opta_github",
            competition="PL",
            season=2026,
        )

        pipe = Pipeline(fetcher=None, predictor=None, store=store)  # type: ignore[arg-type]
        raw_payload = {
            "matches": [
                {
                    "id": 1,
                    "utcDate": "2026-10-01T15:00:00Z",
                    "status": "FINISHED",
                    "homeTeam": {"id": 57, "name": "Arsenal"},
                    "awayTeam": {"id": 61, "name": "Chelsea"},
                    "score": {"fullTime": {"home": 1, "away": 0}},
                },
                {
                    "id": 2,
                    "utcDate": "2026-10-15T15:00:00Z",
                    "status": "FINISHED",
                    "homeTeam": {"id": 61, "name": "Chelsea"},
                    "awayTeam": {"id": 57, "name": "Arsenal"},
                    "score": {"fullTime": {"home": 2, "away": 2}},
                },
            ]
        }
        enriched = pipe._attach_live_xg(raw_payload, "PL")
        assert enriched["matches"][0]["home_xg"] == pytest.approx(2.1)
        assert enriched["matches"][0]["away_xg"] == pytest.approx(0.6)
        assert "home_xg" not in enriched["matches"][1]
        assert "away_xg" not in enriched["matches"][1]
    finally:
        store.close()
