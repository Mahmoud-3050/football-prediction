"""Offline tests for match_odds, vig removal, CLV, simulated ROI, and Dashboard verdict."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db.market_odds import (
    DISCLAIMER_AR,
    evaluate_market_edge,
    import_closing_odds_csv,
    remove_vig_1x2,
)
from db.prospective import record_prospective_prediction, settle_prospective_predictions
from db.team_mapping import UnmappedTeamError, register_team_mapping
from laboratory.dashboard import overview
from laboratory.store import connect


def test_remove_vig_1x2_normalizes_and_rejects_invalid() -> None:
    ph, pd, pa = remove_vig_1x2(2.0, 3.5, 4.0)
    assert ph + pd + pa == pytest.approx(1.0)
    assert ph > pd > pa

    with pytest.raises(ValueError):
        remove_vig_1x2(1.0, 3.5, 4.0)
    with pytest.raises(ValueError):
        remove_vig_1x2(2.0, 0.9, 4.0)


def test_import_closing_odds_csv_strict_team_mapping(tmp_path: Path) -> None:
    db_path = tmp_path / "football.db"
    csv_path = tmp_path / "odds.csv"
    conn = connect(db_path)
    try:
        register_team_mapping(
            conn,
            canonical_name="Arsenal",
            provider="football_data_co_uk",
            provider_name="Arsenal",
            competition="PL",
            team_id="57",
        )
        register_team_mapping(
            conn,
            canonical_name="Chelsea",
            provider="football_data_co_uk",
            provider_name="Chelsea",
            competition="PL",
            team_id="61",
        )
        conn.commit()

        csv_path.write_text(
            "Date,Time,HomeTeam,AwayTeam,PSCH,PSCD,PSCA,PSH,PSD,PSA\n"
            "01/11/2026,15:00,Arsenal,Unknown FC,2.0,3.5,4.0,2.1,3.4,3.8\n",
            encoding="utf-8",
        )
        with pytest.raises(UnmappedTeamError):
            import_closing_odds_csv(conn, csv_path, competition="PL", season=2026)

        csv_path.write_text(
            "Date,Time,HomeTeam,AwayTeam,PSCH,PSCD,PSCA,PSH,PSD,PSA\n"
            "01/11/2026,15:00,Arsenal,Chelsea,2.0,3.5,4.0,2.1,3.4,3.8\n",
            encoding="utf-8",
        )
        res = import_closing_odds_csv(conn, csv_path, competition="PL", season=2026)
        assert res["imported"] == 1
        row = conn.execute("SELECT * FROM match_odds").fetchone()
        assert row is not None
        assert row["close_home_odds"] == pytest.approx(2.0)
    finally:
        conn.close()


def test_evaluate_market_edge_three_state_verdict_and_dashboard(tmp_path: Path) -> None:
    db_path = tmp_path / "football.db"
    csv_path = tmp_path / "odds.csv"
    conn = connect(db_path)
    try:
        register_team_mapping(
            conn,
            canonical_name="Arsenal",
            provider="football_data_co_uk",
            provider_name="Arsenal",
            competition="PL",
            team_id="57",
        )
        register_team_mapping(
            conn,
            canonical_name="Chelsea",
            provider="football_data_co_uk",
            provider_name="Chelsea",
            competition="PL",
            team_id="61",
        )
        register_team_mapping(
            conn,
            canonical_name="Man City",
            provider="football_data_co_uk",
            provider_name="Man City",
            competition="PL",
            team_id="65",
        )

        # Seed 3 prospective settled matches across distinct fixtures
        fixtures = [
            ("m1", "57", "Arsenal", "61", "Chelsea"),
            ("m2", "61", "Chelsea", "57", "Arsenal"),
            ("m3", "57", "Arsenal", "65", "Man City"),
        ]
        lines = ["Date,Time,HomeTeam,AwayTeam,PSCH,PSCD,PSCA\n"]
        for i, (mid, hid, hname, aid, aname) in enumerate(fixtures, start=1):
            kickoff = f"2026-11-0{i}T15:00:00+00:00"
            pred_t = f"2026-10-2{i}T12:00:00+00:00"
            record_prospective_prediction(
                conn,
                competition="PL",
                fixture_id=mid,
                home_team_id=hid,
                away_team_id=aid,
                home_team_name=hname,
                away_team_name=aname,
                kickoff_utc=kickoff,
                predicted_at_utc=pred_t,
                source="telegram_predict",
                model_id="pl-v4",
                stack_json=json.dumps({"k": 12.0}),
                prob_home=0.65,
                prob_draw=0.20,
                prob_away=0.15,
                most_likely_score="2-0",
            )
            conn.execute(
                """
                INSERT INTO matches (match_id, dataset_version, competition, season, kickoff, home_id, home_name, away_id, away_name)
                VALUES (?, 'v1', 'PL', '2026', ?, ?, ?, ?, ?)
                """,
                (mid, kickoff, hid, hname, aid, aname),
            )
            conn.execute(
                """
                INSERT INTO result_revisions (match_id, revision, ft_home, ft_away, source)
                VALUES (?, 1, 2, 0, 'test')
                """,
                (mid,),
            )
            lines.append(f"0{i}/11/2026,15:00,{hname},{aname},2.20,3.40,3.50\n")
        conn.commit()
        settle_prospective_predictions(conn, now_utc="2026-11-10T00:00:00+00:00")
        csv_path.write_text("".join(lines), encoding="utf-8")
        import_closing_odds_csv(conn, csv_path, competition="PL", season=2026)

        # 1. INSUFFICIENT_SAMPLE when n < min_sample
        r_insuff = evaluate_market_edge(conn, competition="PL", min_sample=100)
        assert r_insuff["verdict"] == "INSUFFICIENT_SAMPLE"
        assert r_insuff["combined"]["n_matched"] == 3

        # 2. EDGE_DETECTED when min_sample=2 and model beats closing line with positive CLV & ROI
        r_edge = evaluate_market_edge(conn, competition="PL", min_sample=2)
        assert r_edge["verdict"] == "EDGE_DETECTED"
        assert r_edge["combined"]["model_log_loss"] < r_edge["combined"]["closing_market_log_loss"]
        assert r_edge["combined"]["mean_clv"] > 0.0
        assert r_edge["combined"]["roi_pct"] > 0.0

        # 3. NO_EDGE when closing market beats model (update odds to sharper 1.25 home)
        conn.execute("UPDATE match_odds SET close_home_odds = 1.25, close_draw_odds = 6.0, close_away_odds = 12.0")
        conn.commit()
        r_no_edge = evaluate_market_edge(conn, competition="PL", min_sample=2)
        assert r_no_edge["verdict"] == "NO_EDGE"

        # 4. Dashboard overview renders verdict card and non-betting disclaimer
        html_page = overview(conn, None)
        assert "Market Benchmark &amp; Closing Line Value (CLV)" in html_page
        assert DISCLAIMER_AR in html_page
    finally:
        conn.close()
