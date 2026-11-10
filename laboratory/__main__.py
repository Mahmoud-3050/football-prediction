"""Laboratory commands. They do not start the Telegram bot."""

from __future__ import annotations

import argparse
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from dotenv import load_dotenv

from db_paths import DEFAULT_DATABASE_PATH, resolve_database_path
from laboratory import BOT_BACKTEST_SEASONS
from laboratory.auth import (
    cookie_value,
    load_dashboard_auth,
    parse_form,
    read_request_body,
    require_auth_for_host,
)
from laboratory.dashboard import (
    login_page,
    match_detail,
    match_list,
    not_found_page,
    overview,
    safe_next_path,
    server_error_page,
    standings,
)
from laboratory.standings import STANDINGS_COMPETITIONS, import_all_standings
from laboratory.football_data import COMPETITION_NAMES, fetch_season, import_bodies
from laboratory.job import (
    BOT_EXPERIMENT_CONFIG,
    EXPERIMENT_CONFIG,
    run_poisson,
    run_score_model,
)
from laboratory.store import (
    append_result,
    connect,
    ensure_experiment,
    list_competitions,
    upsert_match,
)
from migrate_db import merge_into_shared

DEFAULT_DATABASE = Path(DEFAULT_DATABASE_PATH)
HISTORY_IMPORT_SEASONS = (2024, 2025, 2026)
# Current + two past seasons for the five continental leagues beyond PL/PD.
DEFAULT_LEAGUE_IMPORTS = ("FL1", "BL1", "SA", "PPL", "DED")
IMPORT_PAUSE_SECONDS = 7.0


def database_path() -> Path:
    """Shared SQLite file (same default as the Telegram bot)."""

    return resolve_database_path(prefer_lab_override=True)


def seed_demo(connection) -> None:
    ensure_experiment(connection, EXPERIMENT_CONFIG)
    fixtures = [
        ("m1", "demo", "2024-08-16T19:00:00Z", "ARS", "Arsenal", "WOL", "Wolves", 2, 0, 1, 0),
        ("m2", "demo", "2024-08-17T14:00:00Z", "CHE", "Chelsea", "LIV", "Liverpool", 1, 1, 0, 0),
        ("m3", "demo", "2024-08-24T14:00:00Z", "ARS", "Arsenal", "CHE", "Chelsea", 3, 1, 2, 0),
        ("m4", "demo", "2024-08-25T15:30:00Z", "LIV", "Liverpool", "WOL", "Wolves", 2, 2, 1, 1),
    ]
    for match_id, season, kickoff, home_id, home_name, away_id, away_name, ft_h, ft_a, ht_h, ht_a in fixtures:
        upsert_match(
            connection,
            {
                "match_id": match_id,
                "dataset_version": "demo-2024",
                "competition": "PL",
                "season": season,
                "kickoff": kickoff,
                "home_id": home_id,
                "home_name": home_name,
                "away_id": away_id,
                "away_name": away_name,
            },
        )
        append_result(
            connection,
            match_id,
            {"ft_home": ft_h, "ft_away": ft_a, "ht_home": ht_h, "ht_away": ht_a},
            source="demo",
        )
    connection.commit()


def build_dashboard_server(
    path: Path,
    host: str,
    port: int,
    *,
    auth=None,
) -> ThreadingHTTPServer:
    """Create the dashboard HTTP server without starting it."""

    load_dotenv(override=True)
    credentials = auth if auth is not None else load_dashboard_auth()
    require_auth_for_host(host, credentials)
    connection = connect(path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._plain(b"ok\n", status=200)
                return
            if parsed.path == "/logout":
                self.send_response(303)
                self.send_header("Location", "/login")
                self.send_header("Set-Cookie", credentials.clear_session_cookie_header())
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if parsed.path == "/login":
                if credentials.enabled and self._authenticated():
                    self._redirect(
                        safe_next_path((parse_qs(parsed.query).get("next") or ["/"])[0])
                    )
                    return
                query = parse_qs(parsed.query)
                next_path = (query.get("next") or ["/"])[0]
                page = login_page(
                    csrf_token=credentials.csrf_token(),
                    next_path=next_path,
                )
                self._html(page, status=200)
                return
            if parsed.path.startswith("/crests/"):
                self._crest(parsed.path.removeprefix("/crests/"))
                return
            if credentials.enabled and not self._authenticated():
                target = parsed.path
                if parsed.query:
                    target = f"{parsed.path}?{parsed.query}"
                self._redirect(f"/login?next={quote(target, safe='/?&=')}")
                return
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/":
                    page = overview(connection, query)
                    status = 200
                elif parsed.path == "/matches":
                    page = match_list(connection, query)
                    status = 200
                elif parsed.path.startswith("/matches/"):
                    match_id = parsed.path.removeprefix("/matches/")
                    detail = match_detail(connection, match_id, query)
                    if detail is None:
                        page = not_found_page(resource="Match")
                        status = 404
                    else:
                        page = detail
                        status = 200
                elif parsed.path == "/standings":
                    page = standings(
                        connection,
                        query,
                        api_key=os.environ.get("FOOTBALL_API_KEY", "").strip() or None,
                    )
                    status = 200
                else:
                    page = not_found_page(resource="Page")
                    status = 404
            except Exception as exc:  # noqa: BLE001 — keep dashboard usable
                page = server_error_page(detail=str(exc))
                status = 500
            self._html(page, status=status)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/login":
                self._plain(b"Not found\n", status=404)
                return
            if not credentials.enabled:
                self._redirect("/")
                return
            form = parse_form(read_request_body(self))
            next_path = safe_next_path(form.get("next") or "/")
            if not secrets.compare_digest(form.get("csrf") or "", credentials.csrf_token()):
                page = login_page(
                    csrf_token=credentials.csrf_token(),
                    next_path=next_path,
                    error="Session expired. Try again.",
                )
                self._html(page, status=400)
                return
            username = form.get("username") or ""
            password = form.get("password") or ""
            if not credentials.check_password(username, password):
                page = login_page(
                    csrf_token=credentials.csrf_token(),
                    next_path=next_path,
                    error="Wrong username or password.",
                )
                self._html(page, status=401)
                return
            token = credentials.issue_session(credentials.username)
            self.send_response(303)
            self.send_header("Location", next_path)
            self.send_header("Set-Cookie", credentials.session_cookie_header(token))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _authenticated(self) -> bool:
            token = cookie_value(self.headers.get("Cookie"))
            return credentials.verify_session(token)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _plain(self, body: bytes, *, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _html(self, page: str, *, status: int) -> None:
            encoded = page.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(encoded)

        def _crest(self, name: str) -> None:
            from laboratory.store import crest_cache_dir

            safe = Path(name).name
            if safe != name or ".." in name or not safe.lower().endswith((".png", ".svg", ".jpg", ".jpeg", ".webp")):
                self._plain(b"Not found\n", status=404)
                return
            path = crest_cache_dir() / safe
            if not path.is_file():
                self._plain(b"Not found\n", status=404)
                return
            payload = path.read_bytes()
            content_type = {
                ".png": "image/png",
                ".svg": "image/svg+xml",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
            }.get(path.suffix.lower(), "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve(path: Path, host: str, port: int) -> None:
    server = build_dashboard_server(path, host, port)
    auth = load_dashboard_auth()
    mode = "auth on" if auth.enabled else "auth off (loopback only)"
    print(f"Laboratory dashboard at http://{host}:{port} ({mode})")
    server.serve_forever()


def _dataset_config(competition: str, seasons: list[int]) -> dict[str, Any]:
    code = competition.upper()
    label = COMPETITION_NAMES.get(code, code)
    return {
        "experiment_id": f"DATA-{code}-001",
        "competition": code,
        "seasons": seasons,
        "purpose": f"store {label} history",
        "pricing": False,
    }


def import_competition_history(
    connection: Any,
    *,
    api_key: str,
    competition: str,
    seasons: list[int],
    pause_seconds: float = IMPORT_PAUSE_SECONDS,
) -> dict[str, Any]:
    """Fetch and store finished/unfinished fixtures for one competition. No pricing."""

    code = competition.upper()
    if code not in COMPETITION_NAMES:
        raise SystemExit(
            f"Unknown competition {competition!r}. Known: {', '.join(COMPETITION_NAMES)}"
        )
    bodies: list[tuple[int, dict[str, Any]]] = []
    for index, season in enumerate(seasons):
        if index:
            time.sleep(pause_seconds)
        print(f"Fetching {code} {season}…", flush=True)
        bodies.append((season, fetch_season(api_key, season, competition=code)))
    config = _dataset_config(code, seasons)
    ensure_experiment(connection, config)
    counts = import_bodies(
        connection,
        bodies,
        competition=code,
        experiment_config=config,
    )
    return counts


def main() -> None:
    load_dotenv(override=True)
    parser = argparse.ArgumentParser(description="Historical testing laboratory")
    parser.add_argument(
        "command",
        choices=(
            "demo",
            "run",
            "serve",
            "import-pl",
            "import-pd",
            "import-leagues",
            "backtest-bot",
            "price-finished",
            "migrate-db",
            "import-standings",
        ),
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("DASHBOARD_HOST") or "127.0.0.1",
        help="Bind address. Non-loopback requires DASHBOARD_PASSWORD.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("DASHBOARD_PORT") or "8765"),
    )
    parser.add_argument(
        "--season",
        type=int,
        action="append",
        dest="seasons",
        help="Competition start year. Repeat to import more than one.",
    )
    parser.add_argument(
        "--competition",
        action="append",
        dest="competitions",
        help="Competition code for import-leagues / price-finished (e.g. FL1). Repeatable.",
    )
    parser.add_argument(
        "--exclude-competition",
        action="append",
        dest="exclude_competitions",
        help="Skip a competition for price-finished (e.g. PL). Repeatable.",
    )
    args = parser.parse_args()
    path = database_path()
    if args.command == "migrate-db":
        summary = merge_into_shared(destination=path)
        print(summary)
        print(f"Shared database ready at {summary['destination']}")
        return
    if args.command == "import-standings":
        api_key = _football_key()
        connection = connect(path)
        competitions = tuple(
            code.upper() for code in (args.competitions or list(STANDINGS_COMPETITIONS))
        )
        summary = import_all_standings(
            connection,
            api_key,
            competitions=competitions,
            force=True,
        )
        print({"standings": summary})
        print(f"Stored league standings in {path}")
        return
    if args.command == "import-pl":
        seasons = args.seasons or [2023, 2024, 2025, 2026]
        api_key = _football_key()
        bodies = [
            (season, fetch_season(api_key, season, competition="PL")) for season in seasons
        ]
        connection = connect(path)
        ensure_experiment(connection, EXPERIMENT_CONFIG)
        print(import_bodies(connection, bodies, competition="PL"))
        print(run_poisson(connection))
        print(f"Stored in {path}")
        return
    if args.command == "import-pd":
        seasons = args.seasons or list(HISTORY_IMPORT_SEASONS)
        api_key = _football_key()
        connection = connect(path)
        counts = import_competition_history(
            connection,
            api_key=api_key,
            competition="PD",
            seasons=seasons,
        )
        print(counts)
        print(f"Stored La Liga seasons {seasons} in {path} (no pricing)")
        return
    if args.command == "import-leagues":
        seasons = args.seasons or list(HISTORY_IMPORT_SEASONS)
        competitions = tuple(
            code.upper() for code in (args.competitions or list(DEFAULT_LEAGUE_IMPORTS))
        )
        api_key = _football_key()
        connection = connect(path)
        summary: list[dict[str, Any]] = []
        for index, code in enumerate(competitions):
            if index:
                time.sleep(IMPORT_PAUSE_SECONDS)
            counts = import_competition_history(
                connection,
                api_key=api_key,
                competition=code,
                seasons=seasons,
            )
            print(counts, flush=True)
            summary.append(counts)
        print(
            f"Stored {', '.join(competitions)} seasons {seasons} in {path} (no pricing)"
        )
        print({"leagues": summary})
        return
    if args.command == "backtest-bot":
        seasons = args.seasons or list(BOT_BACKTEST_SEASONS)
        api_key = _football_key()
        bodies = [
            (season, fetch_season(api_key, season, competition="PL")) for season in seasons
        ]
        connection = connect(path)
        ensure_experiment(connection, BOT_EXPERIMENT_CONFIG)
        print(import_bodies(connection, bodies, competition="PL"))
        print(run_score_model(connection, competition="PL"))
        print(f"Stored in {path} (experiment BOT-PL-001, engine score_model)")
        print(f"Dashboard: python -m laboratory serve")
        return
    if args.command == "price-finished":
        # Walk-forward with the Telegram bot score_model (no Gemini). Finished only.
        connection = connect(path)
        exclude = {
            code.upper() for code in (args.exclude_competitions or [])
        }
        if args.competitions:
            competitions = tuple(code.upper() for code in args.competitions)
        else:
            competitions = tuple(
                code for code in list_competitions(connection) if code not in exclude
            )
        competitions = tuple(code for code in competitions if code not in exclude)
        if not competitions:
            raise SystemExit("No competitions left to price after filters.")
        summary: list[dict[str, Any]] = []
        for code in competitions:
            experiment_id = f"BOT-{code}-live"
            ensure_experiment(
                connection,
                {
                    "experiment_id": experiment_id,
                    "competition": code,
                    "k": 6,
                    "prior_mean": "last_season",
                    "half_time_share_mode": "fixed_0_45",
                    "tail_tolerance": 0.001,
                    "engine": "score_model",
                    "score_model_config_id": "bot-score-model-live",
                    "gemini": False,
                    "note": (
                        "Telegram bot score_model walk-forward on finished matches "
                        "(live stack: last_season + fixed_0_45)."
                    ),
                },
            )
            print(f"=== Pricing finished {code} → {experiment_id} ===", flush=True)
            counts = run_score_model(
                connection,
                experiment_id=experiment_id,
                competition=code,
                k=6.0,
                model_id="bot-score-model-live",
                prior_mean="last_season",
                half_time_share_mode="fixed_0_45",
            )
            print(counts, flush=True)
            summary.append(counts)
        print(f"Stored bot-scorer predictions in {path}")
        print({"leagues": summary})
        return
    if args.command == "demo":
        connection = connect(path)
        seed_demo(connection)
        print(run_poisson(connection))
        print(f"Stored in {path}")
        return
    if args.command == "run":
        connection = connect(path)
        ensure_experiment(connection, EXPERIMENT_CONFIG)
        print(run_poisson(connection))
        return
    serve(path, args.host, args.port)


def _football_key() -> str:
    load_dotenv(override=True)
    key = os.environ.get("FOOTBALL_API_KEY", "").strip()
    if not key:
        raise SystemExit("FOOTBALL_API_KEY is missing from the environment.")
    return key


if __name__ == "__main__":
    main()
