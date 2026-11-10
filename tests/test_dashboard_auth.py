"""Dashboard login page, session cookies, and public-bind fail-closed rules."""

from __future__ import annotations

import threading
import time
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
    urlopen,
)

import pytest

from laboratory.auth import (
    DashboardAuth,
    load_dashboard_auth,
    require_auth_for_host,
)
from laboratory.__main__ import build_dashboard_server, seed_demo
from laboratory.store import connect


def test_load_dashboard_auth_defaults() -> None:
    auth = load_dashboard_auth({})
    assert auth.username == "admin"
    assert auth.enabled is False


def test_session_round_trip() -> None:
    auth = DashboardAuth(
        username="ops",
        password="secret",
        session_secret="unit-test-secret",
    )
    token = auth.issue_session("ops", now=1_700_000_000)
    assert auth.verify_session(token, now=1_700_000_000) is True
    assert auth.verify_session(token, now=1_700_000_000 + 60 * 60 * 24 * 8) is False
    assert auth.verify_session("ops:1:bad", now=1_700_000_000) is False
    assert auth.check_password("ops", "secret") is True
    assert auth.check_password("ops", "wrong") is False


def test_public_bind_requires_password() -> None:
    empty = DashboardAuth(username="admin", password="", session_secret="x")
    with pytest.raises(SystemExit, match="DASHBOARD_PASSWORD"):
        require_auth_for_host("0.0.0.0", empty)
    require_auth_for_host("127.0.0.1", empty)
    require_auth_for_host(
        "0.0.0.0",
        DashboardAuth(username="admin", password="x", session_secret="x"),
    )


def test_login_page_and_session_cookie(tmp_path: Path) -> None:
    db = tmp_path / "football.db"
    connection = connect(db)
    seed_demo(connection)
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

        with urlopen(f"{base}/login", timeout=2) as response:
            login_html = response.read().decode()
            assert response.status == 200
            assert "Sign in" in login_html
            assert 'name="csrf"' in login_html

        # Protected pages redirect to the login form.
        class _NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        try:
            build_opener(_NoRedirect).open(f"{base}/", timeout=2)
            raise AssertionError("expected redirect to login")
        except HTTPError as error:
            assert error.code in {302, 303, 307}
            assert "/login" in error.headers.get("Location", "")

        csrf = auth.csrf_token()
        jar = CookieJar()
        authed = build_opener(HTTPCookieProcessor(jar))
        body = urlencode(
            {
                "csrf": csrf,
                "username": "ops",
                "password": "s3cret",
                "next": "/",
            }
        ).encode()
        request = Request(
            f"{base}/login",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with authed.open(request, timeout=2) as response:
            assert response.status == 200
            page = response.read().decode().lower()
            assert "<html" in page
            assert "overview" in page or "matches" in page

        # Bad password stays on the login page.
        bad = Request(
            f"{base}/login",
            data=urlencode(
                {
                    "csrf": csrf,
                    "username": "ops",
                    "password": "nope",
                    "next": "/",
                }
            ).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            urlopen(bad, timeout=2)
            raise AssertionError("expected 401")
        except HTTPError as error:
            assert error.code == 401
            assert b"Wrong username or password" in error.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
