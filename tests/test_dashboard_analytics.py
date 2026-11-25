"""Tests for percentage-based dashboard analytics and prospective/retrospective separation."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from db.database import ensure_shared_schemas
from laboratory.__main__ import build_dashboard_server
from laboratory.analytics import compute_analytics_summary
from laboratory.auth import DashboardAuth
from laboratory.dashboard import analytics_view
from laboratory.store import connect


def _insert_retro_match(
    conn,
    *,
    match_id: str,
    competition: str,
    season: str,
    kickoff: str,
    home_id: str,
    away_id: str,
    ft_home: int,
    ft_away: int,
    p_home: float,
    p_draw: float,
    p_away: float,
    exact_scores: list[dict],
    top1_home: int,
    top1_away: int,
    p_over_25: float,
    model_id: str = "bot-score-model-pl-v5",
) -> None:
    conn.execute(
        """
        INSERT INTO matches(match_id, dataset_version, competition, season, kickoff, home_id, home_name, away_id, away_name)
        VALUES (?, 'v1', ?, ?, ?, ?, 'Home FC', ?, 'Away FC')
        """,
        (match_id, competition, season, kickoff, home_id, away_id),
    )
    conn.execute(
        """
        INSERT INTO result_revisions(match_id, revision, ft_home, ft_away, source)
        VALUES (?, 1, ?, ?, 'test')
        """,
        (match_id, ft_home, ft_away),
    )
    body = {
        "engine": "score_model",
        "full_time": {
            "result_1x2": {"home": p_home, "draw": p_draw, "away": p_away},
            "exact_score_most_likely": {"home_goals": top1_home, "away_goals": top1_away},
            "exact_scores": exact_scores,
            "over_under_2_5": {"over": p_over_25, "under": 1.0 - p_over_25},
        },
    }
    conn.execute(
        """
        INSERT INTO predictions(prediction_id, experiment_id, match_id, snapshot_id, engine, model_id, body_json, created_at)
        VALUES (?, ?, ?, ?, 'score_model', ?, ?, '2026-01-01T00:00:00Z')
        """,
        (f"pred-{match_id}", f"BOT-{competition}-live", match_id, f"snap-{match_id}", model_id, json.dumps(body)),
    )


def test_analytics_metrics_and_strict_prospective_separation(tmp_path: Path) -> None:
    db_path = tmp_path / "analytics.db"
    conn = connect(db_path)
    ensure_shared_schemas(conn)

    now = datetime.now(timezone.utc)
    recent_ko = (now - timedelta(days=5)).isoformat()
    old_ko = (now - timedelta(days=90)).isoformat()

    # Retro match 1 (PL): Home win 2-1, high confidence 0.65, actual 2-1 in top-3 (not top-1 1-0),
    # Double chance 1X hits, O/U 2.5 over hits (0.60 >= 0.5), O/U 1.5 over hits, BTTS yes hits.
    grid_1 = [
        {"home_goals": 1, "away_goals": 0, "probability": 0.16},
        {"home_goals": 2, "away_goals": 1, "probability": 0.14},
        {"home_goals": 2, "away_goals": 0, "probability": 0.12},
        {"home_goals": 1, "away_goals": 1, "probability": 0.10},
        {"home_goals": 0, "away_goals": 0, "probability": 0.05},
    ]
    _insert_retro_match(
        conn,
        match_id="m1",
        competition="PL",
        season="2026",
        kickoff=recent_ko,
        home_id="h1",
        away_id="a1",
        ft_home=2,
        ft_away=1,
        p_home=0.65,
        p_draw=0.20,
        p_away=0.15,
        exact_scores=grid_1,
        top1_home=1,
        top1_away=0,
        p_over_25=0.60,
    )
    # Closing odds for m1: home=2.0 (implied ~0.48 vig-free) -> model 0.65 beats closing (+0.17 >= 0.03), home wins!
    conn.execute(
        """
        INSERT INTO canonical_teams(team_id, canonical_name, created_at)
        VALUES ('h1', 'Home 1', '2026-01-01T00:00:00Z'),
               ('a1', 'Away 1', '2026-01-01T00:00:00Z'),
               ('h2', 'Home 2', '2026-01-01T00:00:00Z'),
               ('a2', 'Away 2', '2026-01-01T00:00:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO match_odds(competition, season, kickoff_utc, home_team_id, away_team_id, source,
                               close_home_odds, close_draw_odds, close_away_odds, imported_at_utc)
        VALUES ('PL', 2026, ?, 'h1', 'a1', 'test', 2.0, 3.5, 4.0, ?)
        """,
        (recent_ko, recent_ko),
    )

    # Retro match 2 (PD): Draw 0-0, low confidence 0.40, actual 0-0 is Top-1!
    grid_2 = [
        {"home_goals": 0, "away_goals": 0, "probability": 0.25},
        {"home_goals": 1, "away_goals": 0, "probability": 0.15},
        {"home_goals": 1, "away_goals": 1, "probability": 0.12},
    ]
    _insert_retro_match(
        conn,
        match_id="m2",
        competition="PD",
        season="2025",
        kickoff=old_ko,
        home_id="h2",
        away_id="a2",
        ft_home=0,
        ft_away=0,
        p_home=0.35,
        p_draw=0.40,
        p_away=0.25,
        exact_scores=grid_2,
        top1_home=0,
        top1_away=0,
        p_over_25=0.30,
        model_id="bot-score-model-pd-v4",
    )

    # Prospective match 3 (SA): Settled pre-kickoff prediction (must stay separated!)
    conn.execute(
        """
        INSERT INTO prospective_predictions(
            match_key, competition, season, home_team_id, away_team_id,
            home_team_name, away_team_name, kickoff_utc, predicted_at_utc,
            source, model_id, stack_json, prob_home, prob_draw, prob_away,
            prob_over_25, prob_under_25, most_likely_score, status,
            actual_home_goals, actual_away_goals, actual_outcome, exact_score_hit
        ) VALUES (
            'sa-p1', 'SA', 2026, 'h1', 'a2',
            'Inter', 'Milan', ?, ?,
            'telegram', 'bot-score-model-sa-v4', '{}', 0.55, 0.25, 0.20,
            0.52, 0.48, '2-1', 'SETTLED',
            2, 1, 'H', 1
        )
        """,
        (recent_ko, old_ko),
    )
    conn.commit()

    # 1. Retrospective only (default) must NOT include prospective row
    retro = compute_analytics_summary(conn, record_type="retrospective", now_utc=now)
    assert retro["counts"] == {"total": 2, "retrospective": 2, "prospective": 0}
    assert retro["overall"]["ft_1x2"] == {"hits": 2, "total": 2, "pct": 100.0}
    assert retro["overall"]["exact_top1"] == {"hits": 1, "total": 2, "pct": 50.0}
    assert retro["overall"]["exact_top3"] == {"hits": 2, "total": 2, "pct": 100.0}
    assert retro["overall"]["double_chance"] == {"hits": 2, "total": 2, "pct": 100.0}
    assert retro["overall"]["ou_25"] == {"hits": 2, "total": 2, "pct": 100.0}
    assert retro["overall"]["market_edge"]["n_matched"] == 1
    assert retro["overall"]["market_edge"]["beat_closing"] == {"hits": 1, "total": 1, "pct": 100.0}
    assert retro["overall"]["market_edge"]["value_pick"] == {"hits": 1, "total": 1, "pct": 100.0}

    # 2. Prospective only must NOT include retrospective rows
    prosp = compute_analytics_summary(conn, record_type="prospective", now_utc=now)
    assert prosp["counts"] == {"total": 1, "retrospective": 0, "prospective": 1}
    assert prosp["overall"]["ft_1x2"] == {"hits": 1, "total": 1, "pct": 100.0}
    assert prosp["overall"]["exact_top1"] == {"hits": 1, "total": 1, "pct": 100.0}
    assert prosp["overall"]["market_edge"]["n_matched"] == 0
    assert prosp["overall"]["market_edge"]["beat_closing"] == {"hits": 0, "total": 0, "pct": None}

    # 3. Last 30 days window filters out m2 (90 days ago)
    last_30 = compute_analytics_summary(conn, record_type="retrospective", season="last_30d", now_utc=now)
    assert last_30["counts"]["total"] == 1
    assert last_30["per_competition"][0]["competition"] == "PL"

    # 4. Empty bucket (e.g. CL) handles 0 matches cleanly
    empty_cl = compute_analytics_summary(conn, record_type="retrospective", competition="CL", now_utc=now)
    assert empty_cl["counts"]["total"] == 0
    assert empty_cl["overall"]["ft_1x2"]["pct"] is None
    assert empty_cl["per_competition"] == []

    # 5. Season-by-season grouping + YoY delta
    # Add a 2026 match for PD (which already has 2025 m2) to test YoY delta
    _insert_retro_match(
        conn,
        match_id="m4",
        competition="PD",
        season="2026",
        kickoff=recent_ko,
        home_id="h2",
        away_id="a2",
        ft_home=1,
        ft_away=2,
        p_home=0.50,
        p_draw=0.25,
        p_away=0.25,
        exact_scores=grid_2,
        top1_home=1,
        top1_away=0,
        p_over_25=0.55,
        model_id="bot-score-model-pd-v4",
    )
    conn.commit()
    szn_summary = compute_analytics_summary(conn, record_type="retrospective", competition="PD", now_utc=now)
    pd_seasons = szn_summary["by_season_competition"]
    assert [r["season"] for r in pd_seasons] == ["2025", "2026"]
    assert pd_seasons[0]["ft_1x2"]["pct"] == 100.0
    assert pd_seasons[0]["yoy_ft_1x2_delta_pp"] is None
    assert pd_seasons[0]["is_best_ft_1x2"] is True
    assert pd_seasons[1]["ft_1x2"]["pct"] == 0.0
    assert pd_seasons[1]["yoy_ft_1x2_delta_pp"] == -100.0
    assert "Season-by-Season Historical Breakdown" in analytics_view(conn, {"record_type": ["retrospective"]})
    conn.close()


def test_analytics_route_and_auth(tmp_path: Path) -> None:
    db_path = tmp_path / "route.db"
    conn = connect(db_path)
    ensure_shared_schemas(conn)
    html_out = analytics_view(conn, {"record_type": ["prospective"]})
    assert "Live Performance Analytics" in html_out
    assert "Prospective Only (is_prospective = 1)" in html_out
    assert "No evaluated predictions in this slice" in html_out
    conn.close()

    # Auth-off server test
    server = build_dashboard_server(
        db_path,
        "127.0.0.1",
        0,
        auth=DashboardAuth(username="admin", password="", session_secret="test-secret-key"),
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{port}/analytics") as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            assert "Per-Competition Comparison" in body
        with urlopen(f"http://127.0.0.1:{port}/api/analytics?record_type=retrospective") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["filters"]["record_type"] == "retrospective"
    finally:
        server.shutdown()
        server.server_close()

    # Auth-on redirect test
    class _NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    auth_server = build_dashboard_server(
        db_path,
        "127.0.0.1",
        0,
        auth=DashboardAuth(username="admin", password="secret", session_secret="test-secret-key"),
    )
    auth_port = auth_server.server_address[1]
    auth_thread = threading.Thread(target=auth_server.serve_forever, daemon=True)
    auth_thread.start()
    try:
        opener = build_opener(_NoRedirect())
        try:
            opener.open(Request(f"http://127.0.0.1:{auth_port}/analytics"))
            raise AssertionError("Expected 303 redirect to /login")
        except Exception as exc:
            assert getattr(exc, "code", None) == 303
            assert "/login?next=" in str(exc.headers.get("Location", ""))
    finally:
        auth_server.shutdown()
        auth_server.server_close()
