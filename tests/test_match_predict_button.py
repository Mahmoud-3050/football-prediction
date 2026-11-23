"""Dashboard match-detail predict button and on-demand lab pricing."""

from __future__ import annotations

import json
import threading
import time
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from laboratory import BOT_EXPERIMENT_ID
from laboratory.__main__ import build_dashboard_server
from laboratory.auth import DashboardAuth
from laboratory.dashboard import match_detail
from laboratory.job import price_one_score_model
from laboratory.store import append_result, connect, upsert_match
from modeling.league_stacks import DEFAULT_LIVE_STACK, save_live_stack


def _seed_priced_history(connection, *, competition: str = "PL") -> str:
    """Two prior finished matches plus one target with enough form per club."""

    clubs = (("57", "Arsenal"), ("61", "Chelsea"), ("64", "Liverpool"))
    # Priors so each of Arsenal/Chelsea has >= 3 finished games before the target.
    priors = [
        ("m1", "57", "64", 2, 1),
        ("m2", "64", "57", 0, 1),
        ("m3", "61", "64", 1, 0),
        ("m4", "64", "61", 2, 2),
        ("m5", "57", "61", 3, 1),
        ("m6", "61", "57", 0, 2),
    ]
    for index, (match_id, home, away, fh, fa) in enumerate(priors, start=1):
        home_name = next(name for cid, name in clubs if cid == home)
        away_name = next(name for cid, name in clubs if cid == away)
        upsert_match(
            connection,
            {
                "match_id": match_id,
                "dataset_version": "test",
                "competition": competition,
                "season": "2026",
                "kickoff": f"2026-09-{index:02d}T15:00:00Z",
                "home_id": home,
                "home_name": home_name,
                "away_id": away,
                "away_name": away_name,
            },
        )
        append_result(
            connection,
            match_id,
            {"ft_home": fh, "ft_away": fa, "ht_home": 0, "ht_away": 0},
            "test",
        )
    target = "target-1"
    upsert_match(
        connection,
        {
            "match_id": target,
            "dataset_version": "test",
            "competition": competition,
            "season": "2026",
            "kickoff": "2026-10-01T15:00:00Z",
            "home_id": "57",
            "home_name": "Arsenal",
            "away_id": "61",
            "away_name": "Chelsea",
        },
    )
    append_result(
        connection,
        target,
        {"ft_home": 2, "ft_away": 1, "ht_home": 1, "ht_away": 0},
        "test",
    )
    connection.commit()
    return target


def test_price_one_score_model_stores_prediction_and_evaluation(tmp_path: Path) -> None:
    connection = connect(tmp_path / "lab.db")
    match_id = _seed_priced_history(connection)
    result = price_one_score_model(connection, match_id, replace=True)
    assert result["ok"] is True
    assert result["status"] == "priced"
    assert result["experiment_id"] == BOT_EXPERIMENT_ID
    assert result["evaluated"] is True
    row = connection.execute(
        """
        SELECT prediction_id FROM predictions
        WHERE match_id = ? AND engine = 'score_model'
        """,
        (match_id,),
    ).fetchone()
    assert row is not None
    evaluation = connection.execute(
        "SELECT 1 FROM evaluations WHERE prediction_id = ?",
        (row["prediction_id"],),
    ).fetchone()
    assert evaluation is not None


def test_rerun_prediction_applies_promoted_pl_stack(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-run (replace=True) must pick up live stack knobs, not a stale default body."""

    monkeypatch.chdir(tmp_path)
    connection = connect(tmp_path / "lab.db")
    match_id = _seed_priced_history(connection)

    save_live_stack("PL", DEFAULT_LIVE_STACK)
    first = price_one_score_model(connection, match_id, replace=True)
    assert first["ok"] is True and first["status"] == "priced"
    row = connection.execute(
        """
        SELECT model_id, body_json FROM predictions
        WHERE match_id = ? AND experiment_id = ? AND engine = 'score_model'
        """,
        (match_id, BOT_EXPERIMENT_ID),
    ).fetchone()
    assert row is not None
    body = json.loads(row["body_json"])
    assert row["model_id"] == "bot-score-model-live"
    assert body["k"] == 6.0
    assert body["config_id"] == "bot-score-model-live"

    save_live_stack(
        "PL",
        {
            "k": 12.0,
            "prior_mean": "last_season",
            "dependence": "independent",
            "half_time_share_mode": "club_shrunk",
            "ratio_mode": "one_pass",
            "model_id": "bot-score-model-pl-v2",
            "decay_half_life_days": 150.0,
            "draw_boost": 1.06,
            "goal_cap": 4.0,
            "goal_cap_slope": 0.5,
            "nb_alpha": 0.0,
            "promoted_attack_prior": 0.80,
            "promoted_defence_prior": 1.25,
            "selected_experiment_id": "EXP-PL-FINAL-HOLD-21",
            "code_version": "score_model_v1",
        },
    )
    second = price_one_score_model(connection, match_id, replace=True)
    assert second["ok"] is True and second["status"] == "priced"

    rows = connection.execute(
        """
        SELECT model_id, body_json FROM predictions
        WHERE match_id = ? AND experiment_id = ? AND engine = 'score_model'
        """,
        (match_id, BOT_EXPERIMENT_ID),
    ).fetchall()
    assert len(rows) == 1
    body = json.loads(rows[0]["body_json"])
    assert rows[0]["model_id"] == "bot-score-model-pl-v2"
    assert body["config_id"] == "bot-score-model-pl-v2"
    assert body["k"] == 12.0
    assumptions = " ".join(body.get("assumptions") or [])
    assumptions += " " + " ".join((body.get("bot_public") or {}).get("assumptions") or [])
    assert "soft cap=4" in assumptions
    assert "Promoted-team prior fallback" in assumptions
    assert "half-life 150" in assumptions
    assert "Draw scorelines are boosted by ×1.06" in assumptions
    page = match_detail(connection, match_id, csrf_token="test-csrf")
    assert page is not None
    assert "Re-run prediction" in page
    assert "bot-score-model-pl-v2" in page


def test_match_detail_shows_predict_button(tmp_path: Path) -> None:
    connection = connect(tmp_path / "lab.db")
    match_id = _seed_priced_history(connection)
    page = match_detail(
        connection,
        match_id,
        csrf_token="test-csrf",
    )
    assert page is not None
    assert "predict-panel" in page
    assert f'action="/matches/{match_id}/predict"' in page
    assert "Run prediction" in page
    assert 'name="csrf" value="test-csrf"' in page


def test_post_predict_stores_and_redirects(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    connection = connect(db)
    match_id = _seed_priced_history(connection)
    connection.close()

    auth = DashboardAuth(
        username="ops",
        password="s3cret",
        session_secret="unit-test-secret",
    )
    server = build_dashboard_server(db, "127.0.0.1", 0, auth=auth)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with urlopen(f"{base}/healthz", timeout=0.2) as response:
                    assert response.read() == b"ok\n"
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("dashboard did not start")

        login_body = urlencode(
            {
                "username": "ops",
                "password": "s3cret",
                "csrf": auth.csrf_token(),
                "next": f"/matches/{match_id}",
            }
        ).encode()
        opener.open(
            Request(
                f"{base}/login",
                data=login_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            ),
            timeout=5,
        )

        predict_body = urlencode({"csrf": auth.csrf_token()}).encode()
        response = opener.open(
            Request(
                f"{base}/matches/{match_id}/predict",
                data=predict_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            ),
            timeout=30,
        )
        html = response.read().decode()
        assert response.geturl().startswith(f"{base}/matches/{match_id}")
        assert "Prediction stored" in html or "predict_status=priced" in response.geturl()
        assert "Re-run prediction" in html or "Bot score model" in html
    finally:
        server.shutdown()


def test_bulk_predict_filtered_unfinished_only_and_confirmation_dialog(tmp_path: Path) -> None:
    from laboratory.dashboard import list_filtered_unfinished_match_ids, match_list
    from laboratory.job import price_one_score_model
    from laboratory.store import connect, upsert_match, append_result_if_changed

    db = tmp_path / "football.db"
    conn = connect(db)
    _seed_priced_history(conn, competition="PL")
    # Add two unfinished matches in PL season 2025 (one already predicted, one new)
    for mid, hid, hname, aid, aname, ko in (
        ("99001", "57", "Arsenal", "61", "Chelsea", "2026-11-28T15:00:00Z"),
        ("99002", "64", "Liverpool", "61", "Chelsea", "2026-11-29T15:00:00Z"),
    ):
        upsert_match(
            conn,
            {
                "match_id": mid,
                "dataset_version": "test",
                "competition": "PL",
                "season": "2026",
                "kickoff": ko,
                "home_id": hid,
                "home_name": hname,
                "away_id": aid,
                "away_name": aname,
            },
        )
    conn.commit()

    # Pre-predict 99001 so we verify already-predicted unfinished matches ARE included
    first = price_one_score_model(conn, "99001", replace=True)
    assert first["ok"] is True

    query = {
        "league": ["PL"],
        "date_from": ["2024-01-01"],
        "date_to": ["2026-12-31"],
        "fixture": ["all"],
    }
    unfinished_ids = list_filtered_unfinished_match_ids(conn, query)
    assert set(unfinished_ids) == {"99001", "99002"}

    # Finished filter must yield 0 unfinished matches
    finished_only = list_filtered_unfinished_match_ids(conn, {**query, "fixture": ["finished"]})
    assert finished_only == []

    # Render match_list and verify in-page <dialog> confirmation + CSRF + action
    html = match_list(conn, query, csrf_token="tok-123")
    assert 'action="/matches/predict-filtered"' in html
    assert 'name="csrf" value="tok-123"' in html
    assert '<dialog id="bulk-predict-dialog"' in html
    assert "showModal()" in html
    assert "return confirm(" not in html
    assert "Predict unfinished (2)" in html

    # Re-predicting all filtered unfinished matches works with replace=True
    for mid in unfinished_ids:
        res = price_one_score_model(conn, mid, replace=True)
        assert res["ok"] is True
    conn.close()
