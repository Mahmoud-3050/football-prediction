"""Laboratory commands. They do not start the Telegram bot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from dotenv import load_dotenv

from db.db_paths import DEFAULT_DATABASE_PATH, resolve_database_path, resolve_lab_database_path
from laboratory import BOT_BACKTEST_SEASONS, ExperimentConfig, run_experiment, promote_best_config, rollback_promotion
from laboratory.auth import (
    cookie_value,
    load_dashboard_auth,
    parse_form,
    read_request_body,
    require_auth_for_host,
)
from laboratory.dashboard import (
    database_busy_page,
    login_page,
    match_detail,
    match_list,
    not_found_page,
    overview,
    safe_next_path,
    server_error_page,
    standings,
    experiments_list,
    experiment_detail,
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
    available_competitions,
    available_seasons,
    cache_competition_logos,
    checkpoint_wal,
    complete_lab_experiment,
    connect,
    create_lab_experiment,
    delete_experiment_engine_outputs,
    delete_lab_experiment,
    ensure_experiment,
    fail_lab_experiment,
    get_lab_experiment,
    reset_lab_experiment_for_rerun,
    list_competitions,
    list_lab_experiments,
    upsert_match,
)
from db.migrate_db import merge_into_shared

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
    lab_db: Path | None = None,
) -> ThreadingHTTPServer:
    """Create the dashboard HTTP server without starting it."""

    load_dotenv(override=True)
    credentials = auth if auth is not None else load_dashboard_auth()
    require_auth_for_host(host, credentials)
    connection = connect(path)
    target_lab_db = lab_db or resolve_lab_database_path()

    def _spawn_detached(cmd: list[str], *, log_stem: str) -> None:
        """Run a lab CLI job in the background without attaching to the serve TTY.

        Inheriting the dashboard stdout caused sync JSON to print into
        ``make restart`` and contributed to unstable foreground serve sessions.
        """
        log_dir = Path(path).resolve().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{log_stem}.log"
        log_file = open(log_path, "a", encoding="utf-8")
        try:
            log_file.write(
                f"\n--- spawn {datetime.now(timezone.utc).isoformat()} ---\n"
                f"cmd: {' '.join(cmd)}\n"
            )
            log_file.flush()
            subprocess.Popen(
                cmd,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
        finally:
            log_file.close()

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
                    page = overview(connection, query, csrf_token=credentials.csrf_token())
                    status = 200
                elif parsed.path == "/matches":
                    page = match_list(connection, query, csrf_token=credentials.csrf_token())
                    status = 200
                elif parsed.path == "/matches/predict-filtered":
                    page = not_found_page(resource="Page")
                    status = 404
                elif parsed.path.startswith("/matches/"):
                    match_id = parsed.path.removeprefix("/matches/")
                    if match_id.endswith("/predict"):
                        page = not_found_page(resource="Page")
                        status = 404
                    else:
                        detail = match_detail(
                            connection,
                            match_id,
                            query,
                            csrf_token=credentials.csrf_token(),
                        )
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
                        ninjas_api_key=os.environ.get("NINJAS_API_KEY", "").strip()
                        or None,
                    )
                    status = 200
                elif parsed.path == "/experiments":
                    comps = available_competitions(connection)
                    szns = available_seasons(connection)
                    page = experiments_list(
                        connection,
                        query,
                        csrf_token=credentials.csrf_token(),
                        competitions=comps,
                        seasons=szns,
                        lab_db=self.server.lab_db,
                    )
                    status = 200
                elif parsed.path.startswith("/experiments/"):
                    exp_id = unquote(parsed.path.removeprefix("/experiments/"))
                    page = experiment_detail(
                        connection,
                        exp_id,
                        query,
                        csrf_token=credentials.csrf_token(),
                        lab_db=self.server.lab_db,
                    )
                    if page is None:
                        page = not_found_page(resource="Experiment")
                        status = 404
                    else:
                        status = 200
                else:
                    page = not_found_page(resource="Page")
                    status = 404
            except Exception as exc:  # noqa: BLE001 — keep dashboard usable
                detail = str(exc)
                if "database is locked" in detail.lower() or "database is busy" in detail.lower():
                    retry = parsed.path or "/experiments"
                    if parsed.query:
                        retry = f"{parsed.path}?{parsed.query}"
                    page = database_busy_page(retry_href=retry)
                    status = 503
                else:
                    page = server_error_page(detail=detail)
                    status = 500
            self._html(page, status=status)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/login":
                self._post_login()
                return
            if parsed.path == "/matches/predict-filtered":
                self._post_predict_filtered()
                return
            if parsed.path.startswith("/matches/") and parsed.path.endswith("/predict"):
                self._post_predict(parsed.path)
                return
            if parsed.path == "/experiments/run":
                self._post_run_experiment()
                return
            if parsed.path.startswith("/experiments/") and parsed.path.endswith("/delete"):
                self._post_delete_experiment(parsed.path)
                return
            if parsed.path.startswith("/experiments/") and parsed.path.endswith("/rerun"):
                self._post_rerun_experiment(parsed.path)
                return
            if parsed.path in ("/sync", "/ops/sync"):
                self._post_sync()
                return
            self._plain(b"Not found\n", status=404)

        def _post_login(self) -> None:
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

        def _post_predict(self, path: str) -> None:
            if credentials.enabled and not self._authenticated():
                self._redirect(f"/login?next={quote(path, safe='/?&=')}")
                return
            match_id = path.removeprefix("/matches/").removesuffix("/predict")
            form = parse_form(read_request_body(self))
            if credentials.enabled and not secrets.compare_digest(
                form.get("csrf") or "", credentials.csrf_token()
            ):
                self._redirect(
                    f"/matches/{quote(match_id, safe='')}?"
                    + urlencode(
                        {
                            "predict_status": "failed",
                            "predict_detail": "Session expired. Try again.",
                        }
                    )
                )
                return
            from laboratory.job import price_one_score_model

            try:
                result = price_one_score_model(connection, match_id, replace=True)
            except Exception as exc:  # noqa: BLE001 — surface on the match page
                result = {
                    "ok": False,
                    "status": "failed",
                    "detail": str(exc),
                    "match_id": match_id,
                }
            target_id = str(result.get("match_id") or match_id)
            params = {
                "predict_status": str(result.get("status") or "failed"),
                "predict_detail": str(result.get("detail") or ""),
            }
            if result.get("ok"):
                params["predicted"] = "1"
            retained = (form.get("next") or "").strip().lstrip("?")
            if retained.startswith("http") or retained.startswith("/"):
                retained = ""
            query = urlencode(params)
            if retained:
                query = f"{retained}&{query}"
            self._redirect(f"/matches/{quote(target_id, safe='')}?{query}")

        def _post_predict_filtered(self) -> None:
            if credentials.enabled and not self._authenticated():
                self._redirect(f"/login?next={quote('/matches', safe='/?&=')}")
                return
            form = parse_form(read_request_body(self))
            filter_keys = (
                "engine",
                "league",
                "season",
                "team",
                "date_from",
                "date_to",
                "fixture",
                "status",
                "verdict",
                "confidence",
                "order",
            )
            retained_params = {k: form[k] for k in filter_keys if form.get(k)}
            if credentials.enabled and not secrets.compare_digest(
                form.get("csrf") or "", credentials.csrf_token()
            ):
                params = {
                    **retained_params,
                    "bulk_predict_status": "failed",
                    "bulk_predict_detail": "Session expired. Try again.",
                }
                self._redirect(f"/matches?{urlencode(params)}")
                return

            from laboratory.dashboard import list_filtered_unfinished_match_ids
            from laboratory.job import price_one_score_model
            from laboratory.store import checkpoint_wal

            form_query = {k: [v] for k, v in retained_params.items()}
            all_match_ids = list_filtered_unfinished_match_ids(connection, form_query)
            match_ids = all_match_ids[:100]
            priced = 0
            excluded = 0
            failed = 0
            for mid in match_ids:
                try:
                    res = price_one_score_model(connection, mid, replace=True)
                    st = res.get("status")
                    if st == "priced":
                        priced += 1
                    elif st == "excluded":
                        excluded += 1
                    else:
                        failed += 1
                except Exception:  # noqa: BLE001
                    failed += 1
            if priced > 0:
                checkpoint_wal(connection, mode="TRUNCATE")
            detail = (
                f"Priced {priced} of {len(match_ids)} unfinished filtered match(es) "
                f"({excluded} excluded, {failed} failed)."
            )
            params = {
                **retained_params,
                "bulk_predict_status": "ok" if failed == 0 else "failed",
                "bulk_predict_detail": detail,
            }
            self._redirect(f"/matches?{urlencode(params)}")

        def _post_run_experiment(self) -> None:
            if credentials.enabled and not self._authenticated():
                self._redirect(f"/login?next={quote('/experiments', safe='/?&=')}")
                return
            body_bytes = read_request_body(self)
            body_qs = parse_qs(body_bytes.decode("utf-8", errors="replace"))
            form = {k: v[0] for k, v in body_qs.items() if v}
            if credentials.enabled and not secrets.compare_digest(
                form.get("csrf") or "", credentials.csrf_token()
            ):
                self._redirect("/experiments?run_status=failed&run_detail=Session+expired.+Try+again.")
                return

            from laboratory.lab_experiment import (
                ExperimentConfigError,
                parse_experiment_launch_json,
            )

            config_json = (form.get("config_json") or "").strip()
            launch_from_json = bool(config_json)

            if launch_from_json:
                try:
                    launch = parse_experiment_launch_json(config_json)
                except ExperimentConfigError as exc:
                    self._redirect(
                        "/experiments?run_status=failed&run_detail=" + quote(str(exc))
                    )
                    return
                exp_id_input = launch.experiment_id or ""
                competitions = list(launch.competitions)
                seasons = launch.seasons
                primary_metric = launch.primary_metric
                min_matches = launch.min_matches
                base_config = dict(launch.base_config)
                param_grid = dict(launch.param_grid)
                k = float(base_config.get("k", 6.0))
                prior_mean = str(base_config.get("prior_mean", "last_season"))
                dependence = str(base_config.get("dependence", "independent"))
            else:
                exp_id_input = form.get("experiment_id", "").strip()
                if exp_id_input and not re.match(r"^[A-Za-z0-9_-]+$", exp_id_input):
                    self._redirect(
                        "/experiments?run_status=failed&run_detail="
                        "Invalid+Experiment+ID.+Only+letters,+numbers,+underscores,+and+hyphens+allowed."
                    )
                    return

                competitions = [
                    c.upper().strip() for c in body_qs.get("competitions", []) if c.strip()
                ]
                if not competitions:
                    single_comp = form.get("competition", "").strip().upper()
                    competitions = [single_comp] if single_comp else ["PL"]

                seasons_raw = body_qs.get("seasons", [])
                if not seasons_raw and "seasons" in form:
                    seasons_raw = [form["seasons"]]
                parsed_seasons: list[int] = []
                for s in seasons_raw:
                    for part in str(s).replace("[", "").replace("]", "").split(","):
                        p = part.strip().strip("'\"")
                        if p.isdigit():
                            parsed_seasons.append(int(p))
                unique_seasons: list[int] = []
                for s in parsed_seasons:
                    if s not in unique_seasons:
                        unique_seasons.append(s)
                if not unique_seasons:
                    self._redirect(
                        "/experiments?run_status=failed&run_detail=At+least+one+season+required."
                    )
                    return
                seasons = tuple(unique_seasons)

                from laboratory.lab_experiment import ALLOWED_PRIMARY_METRIC

                primary_metric = form.get("primary_metric", "log_loss").strip()
                if primary_metric not in ALLOWED_PRIMARY_METRIC:
                    self._redirect(
                        "/experiments?run_status=failed&run_detail="
                        + quote(
                            "Unsupported primary_metric. "
                            f"Allowed: {', '.join(sorted(ALLOWED_PRIMARY_METRIC))}."
                        )
                    )
                    return
                if primary_metric == "accuracy":
                    primary_metric = "full_time_1x2_accuracy"
                prior_mean = form.get("prior_mean", "last_season").strip()
                dependence = form.get("dependence", "independent").strip()
                half_time_share_mode = form.get(
                    "half_time_share_mode", "fixed_0_45"
                ).strip()
                ratio_mode = form.get("ratio_mode", "one_pass").strip()
                try:
                    k = float(form.get("k", "6.0") or 6.0)
                    min_matches = int(form.get("min_matches", "50") or 50)
                    draw_boost = float(form.get("draw_boost", "1.0") or 1.0)
                    draw_weights_str = form.get("draw_weights", "").strip()
                    draw_weights = (
                        [float(w.strip()) for w in draw_weights_str.split(",") if w.strip()]
                        if draw_weights_str
                        else None
                    )
                    draw_pick_threshold = (
                        float(form["draw_pick_threshold"].strip())
                        if form.get("draw_pick_threshold", "").strip()
                        else None
                    )
                    draw_pick_margin = (
                        float(form["draw_pick_margin"].strip())
                        if form.get("draw_pick_margin", "").strip()
                        else None
                    )
                    draw_tightness_scale = (
                        float(form["draw_tightness_scale"].strip())
                        if form.get("draw_tightness_scale", "").strip()
                        else None
                    )
                    draw_tightness_ref = float(form.get("draw_tightness_ref", "1.0") or 1.0)
                    grid_k = [
                        float(x.strip())
                        for x in form.get("grid_k", "").split(",")
                        if x.strip()
                    ]
                    grid_draw_boost = [
                        float(x.strip())
                        for x in form.get("grid_draw_boost", "").split(",")
                        if x.strip()
                    ]
                except ValueError:
                    self._redirect(
                        "/experiments?run_status=failed&run_detail="
                        + quote(
                            "Invalid numeric parameter. Check k, grids, and advanced fields."
                        )
                    )
                    return
                param_grid = {
                    "k": grid_k if grid_k else [k],
                    "draw_boost": grid_draw_boost if grid_draw_boost else [draw_boost],
                }
                base_config = {
                    "k": k,
                    "prior_mean": prior_mean,
                    "dependence": dependence,
                    "half_time_share_mode": half_time_share_mode,
                    "ratio_mode": ratio_mode,
                    "draw_boost": draw_boost,
                }
                if draw_weights:
                    base_config["draw_weights"] = draw_weights
                if draw_pick_threshold is not None:
                    base_config["draw_pick_threshold"] = draw_pick_threshold
                if draw_pick_margin is not None:
                    base_config["draw_pick_margin"] = draw_pick_margin
                if draw_tightness_scale is not None:
                    base_config["draw_tightness_scale"] = draw_tightness_scale
                if draw_tightness_ref != 1.0:
                    base_config["draw_tightness_ref"] = draw_tightness_ref

            # Concurrency guard & stale check
            lab_conn = connect(self.server.lab_db)
            try:
                active_rows = lab_conn.execute(
                    "SELECT experiment_id, created_at FROM lab_experiments WHERE status = 'running'"
                ).fetchall()
                now_utc = datetime.now(timezone.utc)
                active_running = False
                for r in active_rows:
                    try:
                        c_dt = datetime.fromisoformat(r["created_at"])
                        if c_dt.tzinfo is None:
                            c_dt = c_dt.replace(tzinfo=timezone.utc)
                        age_h = (now_utc - c_dt).total_seconds() / 3600.0
                    except Exception:
                        age_h = 999.0
                    if age_h > 2.0:
                        lab_conn.execute(
                            "UPDATE lab_experiments SET status = 'failed', error = 'Execution timed out or aborted (stale running state)' WHERE experiment_id = ?",
                            (r["experiment_id"],),
                        )
                        lab_conn.commit()
                    else:
                        active_running = True
                if active_running:
                    self._redirect(
                        "/experiments?run_status=failed&run_detail="
                        "An+experiment+is+already+in+progress.+Please+wait+for+it+to+complete."
                    )
                    return

                ts = int(time.time())
                is_batch = len(competitions) > 1
                batch_id = f"BATCH-{ts}" if is_batch else None
                created_ids = []

                for comp in competitions:
                    seed = f"{comp}-{ts}-{k}-{prior_mean}-{dependence}"
                    h = hashlib.sha256(seed.encode()).hexdigest()[:6]
                    eid = (
                        f"{exp_id_input}-{comp}"
                        if (exp_id_input and is_batch)
                        else (
                            exp_id_input
                            if exp_id_input
                            else f"EXP-{comp}-{ts}-{h}"
                        )
                    )
                    cfg_data = {
                        "experiment_id": eid,
                        "competition": comp,
                        "seasons": seasons,
                        "base_config": base_config,
                        "param_grid": param_grid,
                        "primary_metric": primary_metric,
                        "min_matches": min_matches,
                    }
                    if batch_id:
                        cfg_data["batch_id"] = batch_id
                    create_lab_experiment(lab_conn, eid, comp, cfg_data)
                    created_ids.append(eid)
            finally:
                lab_conn.close()

            # Spawn detached background process with absolute DB paths
            live_path = Path(path).resolve()
            lab_path = Path(self.server.lab_db).resolve()
            # Serialize writers from the UI: parallel workers contend on lab.db WAL
            # and surface "database is locked" on the dashboard refresh loop.
            cmd = [
                sys.executable, "-m", "laboratory", "lab-run",
                "--lab-db", str(lab_path),
                "--live-db", str(live_path),
                "--workers", "1",
            ]
            if is_batch and batch_id:
                cmd.extend(["--batch-id", batch_id])
                _spawn_detached(cmd, log_stem="lab-run")
                self._redirect(f"/experiments?batch={quote(batch_id)}&run_status=started&run_detail={quote(f'Batch of {len(competitions)} experiments started in background.')}")
            else:
                eid = created_ids[0]
                cmd.extend(["--experiment-id", eid])
                _spawn_detached(cmd, log_stem="lab-run")
                self._redirect(f"/experiments/{quote(eid, safe='')}?run_status=started&run_detail={quote('Experiment started in background.')}")

        def _post_delete_experiment(self, path: str) -> None:
            if credentials.enabled and not self._authenticated():
                self._redirect(f"/login?next={quote('/experiments', safe='/?&=')}")
                return
            exp_id = unquote(path.removeprefix("/experiments/").removesuffix("/delete"))
            form = parse_form(read_request_body(self))
            if credentials.enabled and not secrets.compare_digest(
                form.get("csrf") or "", credentials.csrf_token()
            ):
                self._redirect("/experiments?run_status=failed&run_detail=Session+expired.+Try+again.")
                return

            lab_conn = connect(self.server.lab_db)
            try:
                deleted = delete_lab_experiment(lab_conn, exp_id, cascade_predictions=True)
                checkpoint_wal(lab_conn, mode="PASSIVE")
            finally:
                lab_conn.close()

            self._redirect(
                f"/experiments?run_status=deleted&run_detail="
                f"{quote(f'Experiment {exp_id} deleted ({deleted} records removed).')}"
            )

        def _post_rerun_experiment(self, request_path: str) -> None:
            detail_next = request_path.removesuffix("/rerun")
            if credentials.enabled and not self._authenticated():
                self._redirect(f"/login?next={quote(detail_next or '/experiments', safe='/?&=')}")
                return
            exp_id = unquote(request_path.removeprefix("/experiments/").removesuffix("/rerun"))
            form = parse_form(read_request_body(self))
            if credentials.enabled and not secrets.compare_digest(
                form.get("csrf") or "", credentials.csrf_token()
            ):
                self._redirect(
                    f"/experiments/{quote(exp_id, safe='')}?run_status=failed&run_detail="
                    "Session+expired.+Try+again."
                )
                return

            lab_conn = connect(self.server.lab_db)
            try:
                exp = get_lab_experiment(lab_conn, exp_id)
                if not exp:
                    self._redirect(
                        "/experiments?run_status=failed&run_detail="
                        + quote(f"Experiment {exp_id} not found.")
                    )
                    return
                if exp["status"] == "running":
                    self._redirect(
                        f"/experiments/{quote(exp_id, safe='')}?run_status=failed&run_detail="
                        + quote("Experiment is already running.")
                    )
                    return

                # Same concurrency guard as create: one active lab-run at a time.
                active_rows = lab_conn.execute(
                    "SELECT experiment_id, created_at FROM lab_experiments WHERE status = 'running'"
                ).fetchall()
                now_utc = datetime.now(timezone.utc)
                active_running = False
                for r in active_rows:
                    try:
                        c_dt = datetime.fromisoformat(r["created_at"])
                        if c_dt.tzinfo is None:
                            c_dt = c_dt.replace(tzinfo=timezone.utc)
                        age_h = (now_utc - c_dt).total_seconds() / 3600.0
                    except Exception:
                        age_h = 999.0
                    if age_h > 2.0:
                        lab_conn.execute(
                            "UPDATE lab_experiments SET status = 'failed', "
                            "error = 'Execution timed out or aborted (stale running state)' "
                            "WHERE experiment_id = ?",
                            (r["experiment_id"],),
                        )
                        lab_conn.commit()
                    else:
                        active_running = True
                if active_running:
                    self._redirect(
                        f"/experiments/{quote(exp_id, safe='')}?run_status=failed&run_detail="
                        + quote(
                            "An experiment is already in progress. "
                            "Please wait for it to complete."
                        )
                    )
                    return

                reset_lab_experiment_for_rerun(
                    lab_conn, exp_id, cascade_predictions=True
                )
                checkpoint_wal(lab_conn, mode="PASSIVE")
            finally:
                lab_conn.close()

            # `path` is the live DB from build_dashboard_server(...); not the HTTP path.
            live_path = Path(path).resolve()
            lab_path = Path(self.server.lab_db).resolve()
            cmd = [
                sys.executable,
                "-m",
                "laboratory",
                "lab-run",
                "--lab-db",
                str(lab_path),
                "--live-db",
                str(live_path),
                "--workers",
                "1",
                "--experiment-id",
                exp_id,
            ]
            _spawn_detached(cmd, log_stem="lab-run")
            self._redirect(
                f"/experiments/{quote(exp_id, safe='')}?run_status=started&run_detail="
                + quote("Experiment re-queued in background.")
            )

        def _post_sync(self) -> None:
            if credentials.enabled and not self._authenticated():
                self._redirect(f"/login?next={quote('/', safe='/?&=')}")
                return
            form = parse_form(read_request_body(self))
            if credentials.enabled and not secrets.compare_digest(
                form.get("csrf") or "", credentials.csrf_token()
            ):
                self._redirect("/?sync_status=failed&sync_detail=Session+expired.+Try+again.")
                return

            if not os.environ.get("FOOTBALL_API_KEY", "").strip():
                self._redirect("/?sync_status=config&sync_detail=Missing+FOOTBALL_API_KEY+in+environment.")
                return

            from laboratory.sync import can_start_sync, create_sync_job_run

            allowed, reason = can_start_sync(connection)
            if not allowed:
                self._redirect(f"/?sync_status=busy&sync_detail={quote(reason)}")
                return

            job_id = create_sync_job_run(connection)
            live_path = Path(path).resolve()
            cmd = [
                sys.executable,
                "-m",
                "laboratory",
                "sync-data",
                "--db",
                str(live_path),
                "--job-id",
                str(job_id),
            ]
            _spawn_detached(cmd, log_stem="data-sync")
            self._redirect(
                "/?sync_status=started&sync_detail="
                + quote("Sync running in background… refreshing this page.")
            )

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

    server = ThreadingHTTPServer((host, port), Handler)
    server.lab_db = target_lab_db
    return server


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


def _score_experiment_ft_1x2(
    connection: Any,
    experiment_id: str,
) -> dict[str, Any]:
    """Aggregate FT 1X2 metrics for one experiment (overall + by season)."""

    import json
    from collections import defaultdict

    rows = connection.execute(
        """
        SELECT m.season, pr.body_json, ev.metrics_json
        FROM predictions pr
        JOIN matches m ON m.match_id = pr.match_id
        JOIN evaluations ev
          ON ev.prediction_id = pr.prediction_id
         AND ev.revision = (
            SELECT MAX(revision) FROM evaluations WHERE prediction_id = pr.prediction_id
         )
        WHERE pr.experiment_id = ? AND pr.engine = 'score_model'
        """,
        (experiment_id,),
    ).fetchall()

    def _blank() -> dict[str, Any]:
        return {
            "n": 0,
            "correct": 0,
            "log_loss_sum": 0.0,
            "brier_sum": 0.0,
            "draw_picks": 0,
            "draw_hits": 0,
            "conf65_n": 0,
            "conf65_hits": 0,
            "conf70_n": 0,
            "conf70_hits": 0,
        }

    def _consume(bucket: dict[str, Any], market: dict[str, Any], confidence: float) -> None:
        loss = market.get("log_loss")
        brier = market.get("brier")
        if not isinstance(loss, (int, float)):
            return
        bucket["n"] += 1
        bucket["correct"] += int(bool(market.get("correct")))
        bucket["log_loss_sum"] += float(loss)
        if isinstance(brier, (int, float)):
            bucket["brier_sum"] += float(brier)
        if market.get("selection") == "draw":
            bucket["draw_picks"] += 1
            bucket["draw_hits"] += int(bool(market.get("correct")))
        if confidence >= 0.65:
            bucket["conf65_n"] += 1
            bucket["conf65_hits"] += int(bool(market.get("correct")))
        if confidence >= 0.70:
            bucket["conf70_n"] += 1
            bucket["conf70_hits"] += int(bool(market.get("correct")))

    def _finalize(bucket: dict[str, Any], *, experiment_id: str | None = None) -> dict[str, Any]:
        n = int(bucket["n"])
        draw_picks = int(bucket["draw_picks"])
        draw_hits = int(bucket["draw_hits"])
        conf65_n = int(bucket["conf65_n"])
        conf70_n = int(bucket["conf70_n"])
        out: dict[str, Any] = {
            "n": n,
            "correct": int(bucket["correct"]),
            "accuracy": (bucket["correct"] / n) if n else None,
            "log_loss": (bucket["log_loss_sum"] / n) if n else None,
            "brier": (bucket["brier_sum"] / n) if n else None,
            "draw_picks": draw_picks,
            "draw_hits": draw_hits,
            "draw_precision": (draw_hits / draw_picks) if draw_picks else None,
            "conf65_n": conf65_n,
            "conf65_accuracy": (
                bucket["conf65_hits"] / conf65_n if conf65_n else None
            ),
            "conf70_n": conf70_n,
            "conf70_accuracy": (
                bucket["conf70_hits"] / conf70_n if conf70_n else None
            ),
        }
        if experiment_id is not None:
            out["experiment_id"] = experiment_id
        return out

    overall = _blank()
    by_season: dict[str, dict[str, Any]] = defaultdict(_blank)
    for row in rows:
        metrics = json.loads(row["metrics_json"] or "{}")
        body = json.loads(row["body_json"] or "{}")
        market = metrics.get("full_time_1x2") or {}
        if "correct" not in market:
            continue
        probs = (body.get("full_time") or {}).get("result_1x2") or {}
        try:
            confidence = max(
                float(probs.get("home") or 0.0),
                float(probs.get("draw") or 0.0),
                float(probs.get("away") or 0.0),
            )
        except (TypeError, ValueError):
            confidence = 0.0
        _consume(overall, market, confidence)
        season = str(row["season"] or "")
        if season:
            _consume(by_season[season], market, confidence)

    summary = _finalize(overall, experiment_id=experiment_id)
    summary["by_season"] = {
        season: _finalize(bucket) for season, bucket in sorted(by_season.items())
    }
    if summary["by_season"]:
        season_ll = [
            float(block["log_loss"])
            for block in summary["by_season"].values()
            if block.get("log_loss") is not None
        ]
        season_acc = [
            float(block["accuracy"])
            for block in summary["by_season"].values()
            if block.get("accuracy") is not None
        ]
        summary["season_mean_log_loss"] = (
            sum(season_ll) / len(season_ll) if season_ll else None
        )
        summary["season_mean_accuracy"] = (
            sum(season_acc) / len(season_acc) if season_acc else None
        )
    else:
        summary["season_mean_log_loss"] = summary["log_loss"]
        summary["season_mean_accuracy"] = summary["accuracy"]
    return summary


def _print_spl_score_row(score: dict[str, Any]) -> None:
    def _pct(value: float | None) -> str:
        return "n/a" if value is None else f"{100.0 * value:.2f}%"

    def _num(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.4f}"

    print(
        f"  FT 1X2 n={score.get('n')} acc={_pct(score.get('accuracy'))} "
        f"brier={_num(score.get('brier'))} ll={_num(score.get('log_loss'))} "
        f"draw_picks={score.get('draw_picks')} draw_hits={score.get('draw_hits')} "
        f"draw_prec={_pct(score.get('draw_precision'))} "
        f"≥65% n={score.get('conf65_n')} acc={_pct(score.get('conf65_accuracy'))} "
        f"≥70% n={score.get('conf70_n')} acc={_pct(score.get('conf70_accuracy'))} "
        f"season_mean_ll={_num(score.get('season_mean_log_loss'))}",
        flush=True,
    )
    for season, block in (score.get("by_season") or {}).items():
        print(
            f"    {season}: n={block.get('n')} acc={_pct(block.get('accuracy'))} "
            f"ll={_num(block.get('log_loss'))} "
            f"draws={block.get('draw_picks')}/{block.get('draw_hits')} "
            f"prec={_pct(block.get('draw_precision'))}",
            flush=True,
        )


def _spl_challenger_grid() -> list[dict[str, Any]]:
    """Baseline + upgraded draw-calibration candidates for SPL walk-forward compare."""

    from modeling.league_stacks import DEFAULT_LIVE_STACK

    # Emphasize common low-scoring draws; de-emphasize 3-3 / 4-4+.
    low_score_weights = [1.35, 1.25, 1.05, 0.70, 0.45, 0.30]
    base = {
        **DEFAULT_LIVE_STACK,
        "k": 6.0,
        "dependence": "dixon_coles",
        "prior_mean": "last_season",
        "half_time_share_mode": "fixed_0_45",
    }
    specs: list[tuple[str, dict[str, Any]]] = [
        ("baseline-db1p25", {"draw_boost": 1.25}),
        ("db1p0", {"draw_boost": 1.0}),
        (
            "wlow-db1p0",
            {"draw_boost": 1.0, "draw_weights": list(low_score_weights)},
        ),
        (
            "wlow-db1p1",
            {"draw_boost": 1.1, "draw_weights": list(low_score_weights)},
        ),
        (
            "wlow-db1p15-t1",
            {
                "draw_boost": 1.15,
                "draw_weights": list(low_score_weights),
                "draw_tightness_scale": 1.0,
            },
        ),
        (
            "db1p0-pick",
            {
                "draw_boost": 1.0,
                "draw_pick_threshold": 0.28,
                "draw_pick_margin": 0.05,
            },
        ),
        (
            "wlow-db1p05-pick",
            {
                "draw_boost": 1.05,
                "draw_weights": list(low_score_weights),
                "draw_pick_threshold": 0.27,
                "draw_pick_margin": 0.06,
            },
        ),
        (
            "wlow-db1p1-t1-pick",
            {
                "draw_boost": 1.1,
                "draw_weights": list(low_score_weights),
                "draw_tightness_scale": 1.0,
                "draw_pick_threshold": 0.27,
                "draw_pick_margin": 0.05,
            },
        ),
    ]
    grid: list[dict[str, Any]] = []
    for tag, overlay in specs:
        stack = {**base, **overlay}
        grid.append(
            {
                "experiment_id": f"BOT-SPL-chal-{tag}",
                "stack": stack,
            }
        )
    return grid


def _select_spl_winner(scored: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick best walk-forward OOS stack: log-loss first, accuracy floor, then draw precision."""

    best_accuracy = max(float(row["accuracy"] or 0.0) for row in scored)
    accuracy_floor = best_accuracy - 0.005
    eligible = [
        row for row in scored if float(row["accuracy"] or 0.0) >= accuracy_floor
    ] or scored
    return min(
        eligible,
        key=lambda row: (
            float(
                row.get("season_mean_log_loss")
                if row.get("season_mean_log_loss") is not None
                else row["log_loss"]
            ),
            -float(row.get("draw_precision") or 0.0),
            -float(row["accuracy"] or 0.0),
            -int(row.get("draw_picks") or 0),
            str(row["experiment_id"]),
        ),
    )


def _run_score_model_from_stack(
    connection: Any,
    *,
    experiment_id: str,
    stack: dict[str, Any],
) -> dict[str, Any]:
    from modeling.league_stacks import draw_calibration_kwargs

    return run_score_model(
        connection,
        experiment_id=experiment_id,
        competition="SPL",
        k=float(stack["k"]),
        model_id=str(stack["model_id"]),
        prior_mean=str(stack["prior_mean"]),
        dependence=str(stack["dependence"]),
        half_time_share_mode=str(stack["half_time_share_mode"]),
        ratio_mode=str(stack.get("ratio_mode") or "one_pass"),
        decay_half_life_days=(
            float(stack["decay_half_life_days"])
            if stack.get("decay_half_life_days") is not None
            else None
        ),
        **draw_calibration_kwargs(stack),
    )


def _tune_spl(
    connection: Any,
    *,
    database_path: Path,
    promote: bool = False,
) -> dict[str, Any]:
    """Walk-forward SPL challenger grid; optionally promote winner to spl_stack.json."""

    from modeling.league_stacks import save_spl_stack
    from laboratory.score_model_adapter import SCORE_MODEL_ENGINE

    grid = _spl_challenger_grid()
    challenger_scores: list[dict[str, Any]] = []
    for item in grid:
        experiment_id = str(item["experiment_id"])
        stack = dict(item["stack"])
        delete_experiment_engine_outputs(
            connection,
            experiment_id=experiment_id,
            engine=SCORE_MODEL_ENGINE,
        )
        ensure_experiment(
            connection,
            {
                "experiment_id": experiment_id,
                "competition": "SPL",
                "k": stack["k"],
                "prior_mean": stack["prior_mean"],
                "dependence": stack["dependence"],
                "half_time_share_mode": stack["half_time_share_mode"],
                "draw_boost": stack.get("draw_boost", 1.0),
                "draw_weights": stack.get("draw_weights"),
                "draw_pick_threshold": stack.get("draw_pick_threshold"),
                "draw_pick_margin": stack.get("draw_pick_margin"),
                "draw_tightness_scale": stack.get("draw_tightness_scale"),
                "tail_tolerance": 0.001,
                "engine": "score_model",
                "score_model_config_id": stack["model_id"],
                "gemini": False,
                "note": f"SPL challenger {experiment_id}",
            },
        )
        print(f"=== Challenger {experiment_id} ===", flush=True)
        counts = _run_score_model_from_stack(
            connection,
            experiment_id=experiment_id,
            stack=stack,
        )
        print(counts, flush=True)
        score = _score_experiment_ft_1x2(connection, experiment_id)
        score["stack"] = stack
        challenger_scores.append(score)
        _print_spl_score_row(score)

    scored = [row for row in challenger_scores if row.get("log_loss") is not None]
    if not scored:
        raise SystemExit("SPL tune failed: no scored challenger evaluations.")
    winner = _select_spl_winner(scored)
    print(
        f"=== Selected winner {winner['experiment_id']} "
        f"(promote={'yes' if promote else 'no'}) ===",
        flush=True,
    )
    _print_spl_score_row(winner)

    result: dict[str, Any] = {
        "winner": winner["experiment_id"],
        "stack": winner["stack"],
        "promoted": False,
        "challengers": challenger_scores,
    }
    if not promote:
        print(
            "Comparison only — data/spl_stack.json and BOT-SPL-live left unchanged.",
            flush=True,
        )
        return result

    stack_path = save_spl_stack(
        winner["stack"],
        database_path=database_path,
        meta={
            "selected_experiment_id": winner["experiment_id"],
            "log_loss": winner["log_loss"],
            "accuracy": winner["accuracy"],
            "brier": winner.get("brier"),
            "draw_picks": winner.get("draw_picks"),
            "draw_hits": winner.get("draw_hits"),
            "draw_precision": winner.get("draw_precision"),
            "season_mean_log_loss": winner.get("season_mean_log_loss"),
            "n": winner["n"],
            "note": "Promoted via walk-forward OOS log-loss with accuracy floor.",
            "challengers": [
                {
                    "experiment_id": row["experiment_id"],
                    "log_loss": row["log_loss"],
                    "season_mean_log_loss": row.get("season_mean_log_loss"),
                    "accuracy": row["accuracy"],
                    "brier": row.get("brier"),
                    "n": row["n"],
                    "draw_picks": row.get("draw_picks"),
                    "draw_hits": row.get("draw_hits"),
                    "draw_precision": row.get("draw_precision"),
                    "conf65_accuracy": row.get("conf65_accuracy"),
                    "conf70_accuracy": row.get("conf70_accuracy"),
                }
                for row in challenger_scores
            ],
        },
    )
    live_id = "BOT-SPL-live"
    delete_experiment_engine_outputs(
        connection,
        experiment_id=live_id,
        engine=SCORE_MODEL_ENGINE,
    )
    ensure_experiment(
        connection,
        {
            "experiment_id": live_id,
            "competition": "SPL",
            "k": winner["stack"]["k"],
            "prior_mean": winner["stack"]["prior_mean"],
            "dependence": winner["stack"]["dependence"],
            "half_time_share_mode": winner["stack"]["half_time_share_mode"],
            "draw_boost": winner["stack"].get("draw_boost", 1.0),
            "draw_weights": winner["stack"].get("draw_weights"),
            "draw_pick_threshold": winner["stack"].get("draw_pick_threshold"),
            "draw_pick_margin": winner["stack"].get("draw_pick_margin"),
            "draw_tightness_scale": winner["stack"].get("draw_tightness_scale"),
            "tail_tolerance": 0.001,
            "engine": "score_model",
            "score_model_config_id": winner["stack"]["model_id"],
            "gemini": False,
            "note": (
                f"SPL live stack promoted from {winner['experiment_id']} "
                f"(FT 1X2 log_loss={winner['log_loss']:.4f}, "
                f"draw_boost={winner['stack'].get('draw_boost', 1.0)})."
            ),
        },
    )
    print(f"=== Rebuild {live_id} with winner {winner['experiment_id']} ===", flush=True)
    live_counts = _run_score_model_from_stack(
        connection,
        experiment_id=live_id,
        stack=winner["stack"],
    )
    live_score = _score_experiment_ft_1x2(connection, live_id)
    result.update(
        {
            "promoted": True,
            "stack_path": str(stack_path),
            "live": {"counts": live_counts, "score": live_score},
        }
    )
    return result


def import_competition_history(
    connection: Any,
    *,
    api_key: str,
    competition: str,
    seasons: list[int],
    pause_seconds: float = IMPORT_PAUSE_SECONDS,
    ninjas_api_key: str | None = None,
    live_football_api_key: str | None = None,
) -> dict[str, Any]:
    """Fetch and store finished/unfinished fixtures for one competition. No pricing."""

    code = competition.upper()
    if code not in COMPETITION_NAMES:
        raise SystemExit(
            f"Unknown competition {competition!r}. Known: {', '.join(COMPETITION_NAMES)}"
        )
    bodies: list[tuple[int, dict[str, Any]]] = []
    errors: list[str] = []
    for index, season in enumerate(seasons):
        if index:
            time.sleep(pause_seconds)
        print(f"Fetching {code} {season}…", flush=True)
        try:
            bodies.append(
                (
                    season,
                    fetch_season(
                        api_key,
                        season,
                        competition=code,
                        ninjas_api_key=ninjas_api_key,
                        live_football_api_key=live_football_api_key,
                    ),
                )
            )
        except RuntimeError as exc:
            print(f"  skipped {code} {season}: {exc}", flush=True)
            errors.append(f"{code} {season}: {exc}")
    if not bodies:
        raise SystemExit(
            f"No {code} seasons imported. "
            + ("; ".join(errors) if errors else "No seasons requested.")
        )
    config = _dataset_config(code, [season for season, _ in bodies])
    ensure_experiment(connection, config)
    counts = import_bodies(
        connection,
        bodies,
        competition=code,
        experiment_config=config,
    )
    if errors:
        counts["season_errors"] = len(errors)
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
            "cache-logos",
            "tune-spl",
            "lab-run",
            "lab-list",
            "lab-analyze",
            "lab-promote",
            "lab-rollback",
            "sync-data",
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
    parser.add_argument(
        "--promote",
        action="store_true",
        help="For tune-spl: write winner to data/spl_stack.json and rebuild BOT-SPL-live.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to experiment config YAML (for lab-run).",
    )
    parser.add_argument(
        "--lab-db",
        type=str,
        default=None,
        help="Path to lab database file (for lab-run).",
    )
    parser.add_argument(
        "--live-db",
        type=str,
        default=None,
        help="Path to live database file (for lab-run).",
    )
    parser.add_argument(
        "--experiment-id",
        type=str,
        default=None,
        help="Run a specific experiment registered in SQLite.",
    )
    parser.add_argument(
        "--batch-id",
        type=str,
        default=None,
        help="Run all pending/running experiments for a batch in SQLite.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel workers for lab-run.",
    )
    parser.add_argument(
        "--job-id",
        type=int,
        default=None,
        help="Existing job_runs id to update for sync-data.",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Database SQLite path for sync-data.",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=IMPORT_PAUSE_SECONDS,
        help="Pause seconds between league fetches (default 7.0).",
    )
    args = parser.parse_args()
    path = database_path()
    if args.command == "sync-data":
        db_path = Path(args.db).resolve() if args.db else path
        connection = connect(db_path)
        seasons = args.seasons or [2026]
        competitions = tuple(code.upper() for code in args.competitions) if args.competitions else None
        pause_seconds = args.pause if args.pause is not None else IMPORT_PAUSE_SECONDS
        from laboratory.sync import run_sync

        summary = run_sync(
            connection,
            seasons=seasons,
            competitions=competitions,
            pause_seconds=pause_seconds,
            job_id=args.job_id,
        )
        print(json.dumps(summary, indent=2, default=str))
        return
    if args.command == "migrate-db":
        summary = merge_into_shared(destination=path)
        print(summary)
        print(f"Shared database ready at {summary['destination']}")
        return
    if args.command == "import-standings":
        api_key = _football_key()
        ninjas_key = os.environ.get("NINJAS_API_KEY", "").strip() or None
        connection = connect(path)
        competitions = tuple(
            code.upper() for code in (args.competitions or list(STANDINGS_COMPETITIONS))
        )
        summary = import_all_standings(
            connection,
            api_key,
            competitions=competitions,
            force=True,
            ninjas_api_key=ninjas_key,
        )
        print({"standings": summary})
        print(f"Stored league standings in {path}")
        return
    if args.command == "cache-logos":
        connection = connect(path)
        competitions = tuple(
            code.upper() for code in (args.competitions or ["SPL"])
        )
        summary = [
            cache_competition_logos(connection, code) for code in competitions
        ]
        print({"logos": summary})
        print(f"Cached crests under {path.parent / 'crests'}")
        return
    if args.command == "tune-spl":
        connection = connect(path)
        summary = _tune_spl(
            connection,
            database_path=path,
            promote=bool(args.promote),
        )
        print(summary)
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
            ninjas_api_key=os.environ.get("NINJAS_API_KEY", "").strip() or None,
            live_football_api_key=os.environ.get("LIVE_FOOTBALL_API_KEY", "").strip()
            or None,
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
        ninjas_key = os.environ.get("NINJAS_API_KEY", "").strip() or None
        live_football_key = (
            os.environ.get("LIVE_FOOTBALL_API_KEY", "").strip() or None
        )
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
                ninjas_api_key=ninjas_key,
                live_football_api_key=live_football_key,
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
        from modeling.league_stacks import draw_calibration_kwargs, live_stack_for

        for code in competitions:
            experiment_id = f"BOT-{code}-live"
            stack = live_stack_for(code, database_path=path)
            draw_kwargs = draw_calibration_kwargs(stack)
            ensure_experiment(
                connection,
                {
                    "experiment_id": experiment_id,
                    "competition": code,
                    "k": stack["k"],
                    "prior_mean": stack["prior_mean"],
                    "dependence": stack["dependence"],
                    "half_time_share_mode": stack["half_time_share_mode"],
                    "draw_boost": draw_kwargs["draw_boost"],
                    "draw_weights": draw_kwargs.get("draw_weights"),
                    "draw_pick_threshold": draw_kwargs.get("draw_pick_threshold"),
                    "draw_pick_margin": draw_kwargs.get("draw_pick_margin"),
                    "draw_tightness_scale": draw_kwargs.get("draw_tightness_scale"),
                    "tail_tolerance": 0.001,
                    "engine": "score_model",
                    "score_model_config_id": stack["model_id"],
                    "gemini": False,
                    "note": (
                        f"Telegram bot score_model walk-forward on finished matches "
                        f"({code} live stack: k={stack['k']}, {stack['prior_mean']}, "
                        f"{stack['dependence']}, {stack['half_time_share_mode']})."
                    ),
                },
            )
            print(f"=== Pricing finished {code} → {experiment_id} ===", flush=True)
            counts = run_score_model(
                connection,
                experiment_id=experiment_id,
                competition=code,
                k=float(stack["k"]),
                model_id=str(stack["model_id"]),
                prior_mean=str(stack["prior_mean"]),
                dependence=str(stack["dependence"]),
                half_time_share_mode=str(stack["half_time_share_mode"]),
                ratio_mode=str(stack.get("ratio_mode") or "one_pass"),
                decay_half_life_days=(
                    float(stack["decay_half_life_days"])
                    if stack.get("decay_half_life_days") is not None
                    else None
                ),
                **draw_kwargs,
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

    # Lab Experiment Commands
    if args.command == "lab-run":
        target_lab_db = Path(args.lab_db) if args.lab_db else resolve_lab_database_path()
        target_live_db = Path(args.live_db) if args.live_db else database_path()

        if args.batch_id:
            lab_conn = connect(target_lab_db)
            rows = lab_conn.execute(
                "SELECT experiment_id, config_json FROM lab_experiments WHERE status = 'running' ORDER BY created_at"
            ).fetchall()
            matching_ids = []
            for r in rows:
                c = json.loads(r["config_json"]) if r["config_json"] else {}
                if c.get("batch_id") == args.batch_id:
                    matching_ids.append(r["experiment_id"])
            lab_conn.close()

            for eid in matching_ids:
                try:
                    lab_conn = connect(target_lab_db)
                    exp_data = get_lab_experiment(lab_conn, eid)
                    lab_conn.close()
                    if not exp_data:
                        continue
                    # Strip dashboard-only keys like batch_id before constructing.
                    exp_cfg = ExperimentConfig.from_stored_dict(exp_data["config"])
                    res = run_experiment(
                        lab_db_path=target_lab_db,
                        live_db_path=target_live_db,
                        config=exp_cfg,
                        max_workers=args.workers,
                    )
                    lab_conn = connect(target_lab_db)
                    complete_lab_experiment(lab_conn, eid, res.best_config, res.best_metrics)
                    checkpoint_wal(lab_conn, mode="TRUNCATE")
                    lab_conn.close()
                except Exception:
                    err_msg = traceback.format_exc()
                    lab_conn = connect(target_lab_db)
                    fail_lab_experiment(lab_conn, eid, err_msg)
                    checkpoint_wal(lab_conn, mode="TRUNCATE")
                    lab_conn.close()
            return

        if args.experiment_id:
            eid = args.experiment_id
            try:
                lab_conn = connect(target_lab_db)
                exp_data = get_lab_experiment(lab_conn, eid)
                lab_conn.close()
                if not exp_data:
                    raise SystemExit(f"Experiment {eid} not found in {target_lab_db}")
                exp_cfg = ExperimentConfig.from_stored_dict(exp_data["config"])
                res = run_experiment(
                    lab_db_path=target_lab_db,
                    live_db_path=target_live_db,
                    config=exp_cfg,
                    max_workers=args.workers,
                )
                lab_conn = connect(target_lab_db)
                complete_lab_experiment(lab_conn, eid, res.best_config, res.best_metrics)
                checkpoint_wal(lab_conn, mode="TRUNCATE")
                lab_conn.close()
                print(f"Experiment {eid} completed successfully.")
            except Exception:
                err_msg = traceback.format_exc()
                try:
                    lab_conn = connect(target_lab_db)
                    fail_lab_experiment(lab_conn, eid, err_msg)
                    checkpoint_wal(lab_conn, mode="TRUNCATE")
                    lab_conn.close()
                except Exception:
                    pass
                raise
            return

        import yaml
        config_path = Path(args.config) if args.config else Path("experiments/default.yaml")
        with open(config_path) as f:
            config_data = yaml.safe_load(f)
        exp_config = ExperimentConfig.from_stored_dict(config_data)
        result = run_experiment(
            lab_db_path=target_lab_db,
            live_db_path=target_live_db,
            config=exp_config,
            max_workers=args.workers,
        )
        lab_conn = connect(target_lab_db)
        complete_lab_experiment(lab_conn, exp_config.experiment_id, result.best_config, result.best_metrics)
        checkpoint_wal(lab_conn, mode="TRUNCATE")
        lab_conn.close()
        print(f"Experiment {result.experiment_id} completed:")
        print(f"  Completed: {result.completed_runs}, Failed: {result.failed_runs}")
        if result.best_model_id:
            ll = result.best_metrics.get('full_time_1x2_log_loss')
            ll_str = f"{ll:.4f}" if isinstance(ll, (int, float)) else "N/A"
            print(f"  Best: {result.best_model_id} ({ll_str} log_loss)")
        return

    if args.command == "lab-list":
        from laboratory.store import list_lab_experiments
        lab_conn = connect(Path("data/lab.db"))
        exps = list_lab_experiments(lab_conn, competition=args.competitions[0].upper() if args.competitions else None)
        lab_conn.close()
        for exp in exps:
            print(f"{exp['experiment_id']} | {exp['competition']} | {exp['status']} | {exp['created_at'][:19]}")
        return

    if args.command == "lab-analyze":
        if not args.seasons:
            raise SystemExit("lab-analyze requires experiment_id as first positional argument")
        from laboratory import compare_to_baseline
        exp_id = args.seasons[0]
        conn = connect(Path("data/lab.db"))
        # Find baseline (best completed run)
        baseline_row = conn.execute("""
            SELECT model_id FROM lab_experiment_runs
            WHERE experiment_id = ? AND status = 'completed'
            ORDER BY matches_evaluated DESC LIMIT 1
        """, (exp_id,)).fetchone()
        conn.close()
        if not baseline_row:
            raise SystemExit("No completed runs found for baseline")
        baseline_id = baseline_row["model_id"]
        result = compare_to_baseline(
            lab_db_path=Path("data/lab.db"),
            experiment_id=exp_id,
            baseline_model_id=baseline_id,
        )
        print(f"Comparison vs baseline ({baseline_id}):")
        for comp in result.comparisons:
            sig = " ✓" if comp["significant"] else ""
            print(f"  {comp['model_id']}: Δ={comp['mean_diff']:+.4f} p={comp['p_value']:.4f}{sig}")
        return

    if args.command == "lab-promote":
        if not args.seasons:
            raise SystemExit("lab-promote requires experiment_id and competition")
        exp_id = args.seasons[0]
        competition = args.seasons[1] if len(args.seasons) > 1 else "PL"
        dry_run = getattr(args, 'dry_run', False)
        result = promote_best_config(
            lab_db_path=Path("data/lab.db"),
            experiment_id=exp_id,
            competition=competition,
            dry_run=dry_run,
        )
        print(f"Promotion: {result.message}")
        if result.bundle_path:
            print(f"Bundle: {result.bundle_path}")
        return

    if args.command == "lab-rollback":
        competition = args.seasons[0] if args.seasons else "PL"
        promo_id = args.seasons[1] if len(args.seasons) > 1 else None
        result = rollback_promotion(
            lab_db_path=Path("data/lab.db"),
            competition=competition,
            promotion_id=promo_id,
        )
        print(f"Rollback: {result.message}")
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
