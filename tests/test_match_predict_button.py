"""Dashboard match-detail predict button and on-demand lab pricing."""

from __future__ import annotations

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
