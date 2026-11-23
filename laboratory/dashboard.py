"""Read-only laboratory dashboard. Defaults to the bot score_model experiment."""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlencode

from laboratory import BOT_EXPERIMENT_ID, EXPERIMENT_ID
from laboratory.football_data import COMPETITION_NAMES
from laboratory.store import connect as lab_connect, emblem_url_for_competition

PAGE_SIZE = 40
MARKETS = (
    ("full_time_1x2", "Full-time result", "accuracy"),
    ("half_time_1x2", "Half-time result", "accuracy"),
    ("exact_score", "Exact score", "hit rate"),
    ("over_under_2_5", "Over/under 2.5", "accuracy"),
    ("over_under_3_5", "Over/under 3.5", "accuracy"),
)
ENGINE_LABELS = {
    "score_model": "Bot score model",
    "poisson": "Lab Poisson",
    "gemini": "Gemini",
}


def _market_pair_from_metrics(
    metrics: dict[str, Any] | None,
    market: str,
) -> tuple[float | None, float | None]:
    """Return ``(accuracy_or_hit, log_loss)`` from ``metrics[market]``.

    Prefers the nested aggregate/evaluation object. Falls back to flat keys
    ``{market}_accuracy`` / ``{market}_log_loss`` written by older runs.
    """

    if not metrics:
        return None, None
    nested = metrics.get(market)
    hit: float | None = None
    log_loss: float | None = None
    if isinstance(nested, dict):
        for key in ("hit", "accuracy", "hit_rate", "correct"):
            raw = nested.get(key)
            if raw is None or isinstance(raw, bool):
                continue
            hit = float(raw)
            break
        if nested.get("log_loss") is not None:
            log_loss = float(nested["log_loss"])
    if hit is None and metrics.get(f"{market}_accuracy") is not None:
        hit = float(metrics[f"{market}_accuracy"])
    if hit is None and metrics.get(f"{market}_hit_rate") is not None:
        hit = float(metrics[f"{market}_hit_rate"])
    if log_loss is None and metrics.get(f"{market}_log_loss") is not None:
        log_loss = float(metrics[f"{market}_log_loss"])
    return hit, log_loss


def _exact_score_from_metrics(metrics: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """Return ``(hit_rate, log_loss)`` for exact score.

    Accepts nested ``metrics['exact_score']`` (``hit`` / ``accuracy`` / ``hit_rate`` /
    numeric ``correct``, plus ``log_loss``) and flat keys ``exact_score_hit_rate``,
    ``exact_score_accuracy``, ``exact_score_log_loss``.
    """

    hit, log_loss = _market_pair_from_metrics(metrics, "exact_score")
    if metrics:
        if hit is None and metrics.get("exact_score_hit_rate") is not None:
            hit = float(metrics["exact_score_hit_rate"])
        if log_loss is None and metrics.get("exact_score_log_loss") is not None:
            log_loss = float(metrics["exact_score_log_loss"])
    return hit, log_loss


def _fmt_pct(value: float | None, *, digits: int = 1) -> str:
    return f"{value:.{digits}%}" if value is not None else "—"


def _fmt_ll(value: float | None) -> str:
    return f"{value:.4f}" if value is not None else "—"


def _metric_cell(text: str, *, win: bool = False) -> str:
    """Leaderboard cell; optional win highlight via existing good token."""

    if not win or text == "—":
        return text
    return f'<span class="metric-win mono">{text}</span>'


def overview(
    connection: sqlite3.Connection,
    query: dict[str, list[str]] | None = None,
    *,
    csrf_token: str | None = None,
) -> str:
    from laboratory.sync import get_latest_sync_job

    filters = _filters(query, connection)
    # SQL-prefilter when possible; still need bodies for calibration charts.
    loaded = _load(
        connection,
        filters["engine"],
        filters["experiment"],
        filters=filters,
        include_body=True,
    )
    rows = _apply(loaded, filters)
    leagues, seasons, teams = _filter_choices(connection, filters.get("league", ""))
    has_any = (
        connection.execute("SELECT 1 FROM matches LIMIT 1").fetchone() is not None
    )
    body = [
        _masthead("Overview", filters, active="overview"),
        _jobs_panel(connection, filters, csrf_token=csrf_token),
        _filter_form("/", filters, leagues, seasons, teams, submit="Update"),
    ]
    if not has_any:
        body.append(
            _state(
                kind="empty",
                title="No matches in the laboratory yet",
                lead="Import fixtures first, then open Overview to see scored market stats.",
                action_href="/matches",
                action_label="Open Matches",
            )
        )
    elif not rows:
        body.append(
            _state(
                kind="empty",
                title="No matches match these filters",
                lead="Stats below are empty because the current filters exclude every fixture.",
                action_href="/",
                action_label="Clear filters",
            )
        )
        body.extend(
            [
                _stat_cards(rows, filters["engine"]),
                "<h2>Market results</h2>",
                _market_table(rows),
            ]
        )
    else:
        body.extend(
            [
                _stat_cards(rows, filters["engine"]),
                "<h2>Market results</h2>",
                "<p class=\"note\">Scored evaluations only. Log loss and Brier are means over that sample.</p>",
                _market_table(rows),
                _overview_charts(rows),
            ]
        )
        if all(_summarize(rows, key)["n"] == 0 for key, _, _ in MARKETS):
            body.append(
                _state(
                    kind="info",
                    title="No scored evaluations yet",
                    lead="Fixtures are loaded, but this engine has no scored market metrics for the current filters.",
                )
            )
    try:
        latest_sync = get_latest_sync_job(connection)
    except sqlite3.OperationalError:
        latest_sync = None
    is_sync_running = latest_sync is not None and latest_sync.get("status") == "running"
    sync_flash = (filters.get("sync_status") or "").strip()
    # Keep polling while the job runs. After POST lands on sync_status=started,
    # bounce once to clean "/" so the final job summary (ok/partial/failed) shows.
    if is_sync_running:
        extra_head = '<meta http-equiv="refresh" content="5;url=/">'
    elif sync_flash == "started":
        extra_head = '<meta http-equiv="refresh" content="2;url=/">'
    else:
        extra_head = ""
    return _page("Overview", "\n".join(body), filters, extra_head=extra_head)


def _jobs_panel(
    connection: sqlite3.Connection,
    filters: dict[str, str] | None = None,
    *,
    csrf_token: str | None = None,
) -> str:
    from laboratory.store import latest_job_runs
    from laboratory.sync import get_latest_sync_job

    filters = filters or {}
    try:
        runs = latest_job_runs(connection)
    except sqlite3.OperationalError:
        runs = []

    try:
        sync_job = get_latest_sync_job(connection)
    except sqlite3.OperationalError:
        sync_job = None

    is_running = sync_job is not None and sync_job.get("status") == "running"
    sync_flash_status = (filters.get("sync_status") or "").strip()
    sync_flash_detail = (filters.get("sync_detail") or "").strip()

    header_html = (
        '<div class="jobs-header">'
        '<div class="jobs-title-wrap">'
        "<h2>Server jobs</h2>"
        '<p class="note">Fetches recent results, upcoming fixtures, and tables for all leagues (background).</p>'
        "</div>"
        '<form method="POST" action="/sync" class="sync-form">'
        f'<input type="hidden" name="csrf" value="{html.escape(csrf_token or "")}">'
        f'<button type="submit" class="sync-btn"{" disabled" if is_running else ""}>'
        f'{"Sync in progress…" if is_running else "Sync matches & standings"}'
        "</button>"
        "</form>"
        "</div>"
        '<p class="note">Incremental: recent results + upcoming fixtures (±14 days); '
        "full season only on first import.</p>"
    )

    status_callout = ""
    terminal_job = (
        sync_job
        if sync_job and sync_job.get("status") in {"ok", "partial", "failed", "empty"}
        else None
    )

    if is_running:
        status_callout = _state(
            kind="wait",
            title="Sync running in background… refreshing this page.",
            lead="Fetching fixtures, finished scores, and league standings across all competitions.",
        )
    elif sync_flash_status == "busy":
        status_callout = _state(
            kind="wait",
            title="A sync is already in progress. Wait for it to finish.",
            lead=sync_flash_detail,
        )
    elif sync_flash_status == "config":
        status_callout = _state(
            kind="error",
            title="Missing configuration",
            lead=sync_flash_detail or "Missing FOOTBALL_API_KEY in environment.",
        )
    elif sync_flash_status in ("unauthorized", "invalid", "auth"):
        status_callout = _state(
            kind="error",
            title="Authentication error",
            lead=sync_flash_detail or "Session expired. Try again.",
        )
    elif sync_flash_status == "failed" and not terminal_job:
        status_callout = _state(
            kind="error",
            title="Sync failed",
            lead=sync_flash_detail or "Session expired. Try again.",
        )
    elif terminal_job:
        # Prefer persisted job summary over a stale sync_status=started flash.
        st = str(terminal_job.get("status") or "")
        dtl = str(terminal_job.get("detail") or "")
        if st == "ok":
            status_callout = _state(kind="info", title="Sync completed", lead=dtl)
        elif st == "partial":
            status_callout = _state(kind="wait", title="Partial sync completed", lead=dtl)
        elif st == "failed":
            status_callout = _state(
                kind="error",
                title="Sync failed",
                lead=dtl or str(terminal_job.get("error") or ""),
            )
        elif st == "empty":
            status_callout = _state(kind="empty", title="No fixtures returned", lead=dtl)
    elif sync_flash_status == "started":
        status_callout = _state(
            kind="wait",
            title="Sync started… waiting for results.",
            lead=sync_flash_detail or "This page will refresh automatically when the sync finishes.",
        )
    elif sync_flash_status == "ok":
        status_callout = _state(
            kind="info",
            title="Sync completed",
            lead=sync_flash_detail,
        )
    elif sync_flash_status == "partial":
        status_callout = _state(
            kind="wait",
            title="Partial sync completed",
            lead=sync_flash_detail,
        )
    elif sync_flash_status == "empty":
        status_callout = _state(
            kind="empty",
            title="No fixtures returned",
            lead=sync_flash_detail or "No fixtures returned for any league (check season year / provider).",
        )
    elif sync_flash_status == "failed":
        status_callout = _state(
            kind="error",
            title="Sync failed",
            lead=sync_flash_detail or "Sync failed.",
        )

    breakdown_html = ""
    if sync_job and sync_job.get("leagues") and not is_running:
        breakdown_html = _sync_breakdown_table(sync_job["leagues"])

    if not runs:
        runs_html = (
            '<p class="note">No job runs recorded yet. Start '
            "<code>python -m jobs</code> to price tomorrow and refresh standings.</p>"
        )
    else:
        cards: list[str] = []
        for run in runs:
            summary = _json(run["summary_json"])
            ok = bool(run["ok"])
            status = "ok" if ok else "failed"
            title = "Tomorrow predictions" if run["name"] == "tomorrow" else "Standings refresh"
            when = _short_cairo(str(run["finished_at"]))
            detail = _job_summary_line(str(run["name"]), summary, ok=ok)
            cards.append(
                "<article>"
                f"<span>{html.escape(title)}</span>"
                f'<strong class="job-{status}">{html.escape(status)}</strong>'
                f"<p>{html.escape(when)} · {html.escape(detail)}</p>"
                "</article>"
            )
        runs_html = f'<div class="job-cards">{"".join(cards)}</div>'

    return (
        f'<section class="panel jobs-panel">'
        f"{header_html}"
        f"{status_callout}"
        f"{breakdown_html}"
        f"{runs_html}"
        f"</section>"
    )


def _sync_breakdown_table(leagues: dict[str, dict[str, Any]]) -> str:
    rows = []
    for code, item in leagues.items():
        name = item.get("name") or code
        matches = item.get("matches", 0)
        res_added = item.get("results_added", 0)
        res_unchanged = item.get("results_unchanged", 0)
        priced_new = item.get("priced_new", 0)
        evaluated = item.get("evaluated", 0)
        already_evaluated = item.get("already_evaluated", 0)
        st_status = item.get("standings_status", "n/a")
        st_rows = item.get("standings_rows", 0)
        if st_status == "ok":
            st_display = f"{st_rows} rows"
        elif st_status == "failed":
            st_display = f"failed ({item.get('standings_error', '')})"
        else:
            st_display = "n/a"

        m_status = item.get("matches_status", "pending")
        m_err = item.get("matches_error") or ""
        p_err = item.get("price_error") or ""

        if m_status == "failed" or st_status == "failed":
            badge = '<strong class="job-failed">failed</strong>'
        elif m_status in ("ok", "empty") and st_status in ("ok", "n/a"):
            badge = '<strong class="job-ok">ok</strong>'
        else:
            badge = f"<strong>{html.escape(m_status)}</strong>"

        notes = []
        if m_err:
            notes.append(m_err)
        if p_err:
            notes.append(f"price: {p_err}")
        err_note = (
            f' <span class="note" style="color:var(--bad)">({html.escape("; ".join(notes))})</span>'
            if notes
            else ""
        )
        eval_display = f"{evaluated}"
        if already_evaluated:
            eval_display = f"{evaluated} (+{already_evaluated} prior)"

        rows.append(
            f"<tr>"
            f"<td><strong>{html.escape(code)}</strong> · {html.escape(name)}{err_note}</td>"
            f'<td class="mono num">{matches}</td>'
            f'<td class="mono num">{res_added}</td>'
            f'<td class="mono num">{res_unchanged}</td>'
            f'<td class="mono num">{priced_new}</td>'
            f'<td class="mono num">{html.escape(eval_display)}</td>'
            f"<td>{html.escape(st_display)}</td>"
            f"<td>{badge}</td>"
            f"</tr>"
        )
    return (
        '<div class="sync-breakdown table-wrap">'
        '<table class="sync-table">'
        "<thead><tr>"
        "<th>League</th>"
        "<th>Fixtures</th>"
        "<th>Results added</th>"
        "<th>Results unchanged</th>"
        "<th>Priced</th>"
        "<th>Evaluated</th>"
        "<th>Standings</th>"
        "<th>Status</th>"
        "</tr></thead>"
        f'<tbody>{"".join(rows)}</tbody>'
        "</table>"
        "</div>"
    )


def _job_summary_line(name: str, summary: dict[str, Any], *, ok: bool) -> str:
    if not ok:
        return str(summary.get("error") or "failed")
    if name == "tomorrow":
        return (
            f"day {summary.get('day') or '—'} · "
            f"priced {summary.get('priced', 0)} / "
            f"candidates {summary.get('candidates', 0)} · "
            f"abstained {summary.get('abstained', 0)} · "
            f"failed {summary.get('failed', 0)}"
        )
    refreshed = summary.get("refreshed") or []
    if isinstance(refreshed, list):
        labels = ", ".join(str(item) for item in refreshed) or "none"
    else:
        labels = str(refreshed)
    return f"day {summary.get('day') or '—'} · refreshed {labels}"


def _short_cairo(value: str) -> str:
    from core.timeutil import format_clock_cairo

    return format_clock_cairo(value) or (value.strip() or "—")


def list_filtered_unfinished_match_ids(
    connection: sqlite3.Connection,
    query: dict[str, list[str]] | None = None,
) -> list[str]:
    """Return all unfinished (``ft_home IS NULL``) match IDs matching ``query`` filters."""

    filters = _filters(query, connection)
    if not filters["date_from"] and not filters["date_to"]:
        filters["date_from"], filters["date_to"] = _default_match_date_range()
    if filters.get("fixture") == "finished":
        return []
    unfinished_filters = {**filters, "fixture": "scheduled"}
    engine = unfinished_filters.get("engine") or "score_model"
    experiment_id = unfinished_filters.get("experiment") or BOT_EXPERIMENT_ID
    rows = _load(
        connection,
        engine,
        experiment_id,
        filters=unfinished_filters,
        include_body=_needs_body_postfilter(unfinished_filters),
    )
    if _needs_metrics_postfilter(unfinished_filters):
        rows = _apply(rows, unfinished_filters)
    return [str(r["match_id"]) for r in rows if r.get("ft_home") is None]


def _bulk_predict_notice(query: dict[str, str]) -> str:
    status = query.get("bulk_predict_status") or ""
    if not status:
        return ""
    detail = query.get("bulk_predict_detail") or ""
    if status == "ok":
        return _state(kind="info", title="Bulk prediction complete", lead=detail)
    return _state(
        kind="error",
        title="Bulk prediction failed",
        lead=detail or "Could not predict filtered matches.",
    )


def match_list(
    connection: sqlite3.Connection,
    query: dict[str, list[str]] | None = None,
    *,
    csrf_token: str | None = None,
) -> str:
    filters = _filters(query, connection)
    if not filters["date_from"] and not filters["date_to"]:
        filters["date_from"], filters["date_to"] = _default_match_date_range()
    leagues, seasons, teams = _filter_choices(connection, filters.get("league", ""))
    has_any = (
        connection.execute("SELECT 1 FROM matches LIMIT 1").fetchone() is not None
    )
    total, window = _load_match_page(connection, filters)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(_page_number(filters), pages)
    unfinished_ids = (
        list_filtered_unfinished_match_ids(connection, query)
        if has_any and total > 0
        else []
    )
    unfinished_count = len(unfinished_ids)
    notice = _bulk_predict_notice(filters)

    bulk_form = ""
    if unfinished_count > 0:
        csrf_input = (
            f'<input type="hidden" name="csrf" value="{html.escape(csrf_token)}">'
            if csrf_token
            else ""
        )
        hidden_filters = "".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(filters[k])}">'
            for k in (
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
            if filters.get(k)
        )
        cap_limit = 100
        capped_count = min(unfinished_count, cap_limit)
        confirm_title = "Confirm bulk prediction"
        confirm_msg = (
            f"Run or re-run predictions for {capped_count} unfinished "
            f"filtered match{'es' if capped_count != 1 else ''}? "
            "Finished matches will be skipped."
            + (
                f" (Capped at {cap_limit} of {unfinished_count} matching fixtures.)"
                if unfinished_count > cap_limit
                else ""
            )
        )
        bulk_form = (
            f'<button type="button" class="btn-bulk-predict" '
            f'onclick="document.getElementById(\'bulk-predict-dialog\').showModal()">'
            f"Predict unfinished ({unfinished_count})</button>"
            f'<dialog id="bulk-predict-dialog" class="confirm-dialog" '
            f'aria-labelledby="bulk-predict-title" aria-describedby="bulk-predict-desc">'
            f'<div class="confirm-dialog-card">'
            f'<h2 id="bulk-predict-title">{html.escape(confirm_title)}</h2>'
            f'<p id="bulk-predict-desc" class="note">{html.escape(confirm_msg)}</p>'
            f'<form class="bulk-predict-form" method="post" action="/matches/predict-filtered">'
            f"{csrf_input}{hidden_filters}"
            f'<div class="confirm-dialog-actions">'
            f'<button type="button" class="btn-dialog-cancel" '
            f'onclick="document.getElementById(\'bulk-predict-dialog\').close()">Cancel</button>'
            f'<button type="submit" class="btn-dialog-confirm">'
            f"Confirm ({capped_count})</button>"
            f"</div></form></div></dialog>"
        )

    if not has_any:
        listing = _state(
            kind="empty",
            title="No matches in the laboratory yet",
            lead="Import competition history to populate this list.",
            action_href="/",
            action_label="Back to Overview",
        )
        toolbar = ""
        pager = ""
    elif total == 0:
        listing = _state(
            kind="empty",
            title="No matches match these filters",
            lead="Try clearing filters or widening the date range.",
            action_href="/matches",
            action_label="Clear filters",
        )
        toolbar = (
            '<div class="toolbar"><p class="note" role="status">'
            "Showing <strong>0</strong> matches</p></div>"
        )
        pager = ""
    else:
        listing = _match_table(window, filters)
        toolbar = (
            f'<div class="toolbar" style="display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;">'
            f'<p class="note">Showing '
            f"<strong>{len(window)}</strong> of <strong>{total}</strong>"
            f" · page {page} of {pages}</p>{bulk_form}</div>"
        )
        pager = _pager("/matches", filters, page, pages)
    body = [
        _masthead("Matches", filters, active="matches"),
        _filter_form("/matches", filters, leagues, seasons, teams, submit="Show matches"),
        notice,
        toolbar,
        listing,
        pager,
    ]
    return _page("Matches", "\n".join(body), filters)


def match_detail(
    connection: sqlite3.Connection,
    match_id: str,
    query: dict[str, list[str]] | None = None,
    *,
    csrf_token: str | None = None,
) -> str | None:
    match_row = connection.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
    if match_row is None and match_id.startswith("fd-"):
        # Legacy bookmarks used the football-data `fd-` prefix.
        match_row = connection.execute(
            "SELECT * FROM matches WHERE match_id = ?",
            (match_id.removeprefix("fd-"),),
        ).fetchone()
    if match_row is None:
        return None
    from laboratory.store import team_display_map

    match = dict(match_row)
    canonical_id = str(match["match_id"])
    displays = team_display_map(connection, [str(match["home_id"]), str(match["away_id"])])
    match["home_display"] = displays.get(str(match["home_id"])) or str(match["home_name"])
    match["away_display"] = displays.get(str(match["away_id"])) or str(match["away_name"])
    filters = _filters(query, connection)
    experiment_id = _live_experiment_for_match(filters["engine"], str(match["competition"] or "PL"))
    result = connection.execute(
        """
        SELECT * FROM result_revisions
        WHERE match_id = ? ORDER BY revision DESC LIMIT 1
        """,
        (canonical_id,),
    ).fetchone()
    # Avoid SELECT payload_json (large blobs). Coverage fields + leakage_ok are enough for UI.
    snapshot = connection.execute(
        """
        SELECT leakage_ok,
               json_extract(payload_json, '$.coverage.prior_full_time_matches') AS prior_ft,
               json_extract(payload_json, '$.coverage.prior_half_time_matches') AS prior_ht
        FROM snapshots
        WHERE match_id = ? AND experiment_id = ?
        LIMIT 1
        """,
        (canonical_id, experiment_id),
    ).fetchone()
    if snapshot is None:
        snapshot = connection.execute(
            """
            SELECT leakage_ok,
                   json_extract(payload_json, '$.coverage.prior_full_time_matches') AS prior_ft,
                   json_extract(payload_json, '$.coverage.prior_half_time_matches') AS prior_ht
            FROM snapshots
            WHERE match_id = ?
            LIMIT 1
            """,
            (canonical_id,),
        ).fetchone()
    predictions = connection.execute(
        """
        SELECT * FROM predictions
        WHERE match_id = ? AND experiment_id = ?
        ORDER BY engine
        """,
        (canonical_id, experiment_id),
    ).fetchall()
    if not predictions and filters["engine"] == "score_model":
        predictions = connection.execute(
            """
            SELECT * FROM predictions
            WHERE match_id = ? AND engine = 'score_model'
              AND (
                experiment_id = ?
                OR experiment_id = ('BOT-' || ? || '-live')
                OR model_id IN (
                  'bot-score-model-v1',
                  'bot-score-model-live',
                  'bot-score-model-pl-v2'
                )
              )
            ORDER BY
              CASE
                WHEN experiment_id = ('BOT-' || ? || '-live') THEN 0
                WHEN experiment_id = ? THEN 1
                ELSE 2
              END,
              prediction_id
            """,
            (
                canonical_id,
                BOT_EXPERIMENT_ID,
                str(match["competition"] or "PL"),
                str(match["competition"] or "PL"),
                BOT_EXPERIMENT_ID,
            ),
        ).fetchall()
    runs = {
        row["engine"]: row
        for row in connection.execute(
            """
            SELECT * FROM engine_runs
            WHERE match_id = ? AND experiment_id = ?
            """,
            (canonical_id, experiment_id),
        )
    }
    back = "/matches"
    query_text = _query(filters, page="")
    if query_text:
        back = f"/matches?{query_text}"
    cl_stage = ""
    cl_prior_note = ""
    if str(match.get("competition") or "") == "CL":
        rp_id = match.get("raw_payload_id")
        if rp_id:
            rp_row = connection.execute(
                "SELECT body_json FROM raw_payloads WHERE payload_id = ?", (rp_id,)
            ).fetchone()
            if rp_row:
                try:
                    rp_body = json.loads(rp_row["body_json"])
                    for m_item in rp_body.get("matches") or []:
                        if str(m_item.get("id")) == canonical_id and m_item.get("stage"):
                            cl_stage = str(m_item["stage"]).replace("_", " ").title()
                            break
                except (json.JSONDecodeError, TypeError):
                    pass
        from modeling.league_stacks import live_stack_for

        cl_st = live_stack_for("CL")
        if cl_st.get("persistent_priors") or float(cl_st.get("domestic_prior_weight") or 0.0) > 0.0:
            cl_prior_note = "Prior: Persistent CL + Elo + Domestic"
    parts = [
        _masthead(
            f"{_club_label(match, 'home')} vs {_club_label(match, 'away')}",
            filters,
            active="matches",
            back_href=back,
        ),
        _scoreboard(match, result, connection),
        (
            f'<p class="note" style="margin:8px 0 0;">'
            + (" · ".join(x for x in (cl_stage, cl_prior_note) if x))
            + "</p>"
            if (cl_stage or cl_prior_note)
            else ""
        ),
        _predict_flash(filters),
        _predict_panel(
            match_id=canonical_id,
            has_prediction=bool(predictions),
            csrf_token=csrf_token,
            query=filters,
        ),
        _facts(match, snapshot),
    ]
    preferred = next((item for item in predictions if item["engine"] == filters["engine"]), None)
    if preferred is None and predictions:
        preferred = predictions[0]
    if preferred is not None:
        evaluation = connection.execute(
            """
            SELECT metrics_json FROM evaluations
            WHERE prediction_id = ? ORDER BY revision DESC LIMIT 1
            """,
            (preferred["prediction_id"],),
        ).fetchone()
        parts.append(_comparison(preferred, result, evaluation))
        parts.append(
            _engine_panel(
                preferred["engine"],
                preferred["model_id"],
                json.loads(preferred["body_json"]),
                evaluation,
                runs.get(preferred["engine"]),
            )
        )
    elif runs:
        for engine, run in runs.items():
            detail = str(run["detail"] or "").strip()
            parts.append(
                f'<section class="panel"><h2>{html.escape(ENGINE_LABELS.get(engine, engine))}</h2>'
                f'<p>{_badge(run["status"])}'
                f'{(" " + html.escape(detail)) if detail else ""}</p></section>'
            )
            if str(run["status"] or "") == "failed":
                parts.append(
                    _state(
                        kind="error",
                        title="Engine run failed",
                        lead=detail or "The engine recorded a failure for this match.",
                    )
                )
    else:
        parts.append(
            _state(
                kind="empty",
                title="No prediction stored for this match",
                lead="This fixture has no engine run or prediction body yet for the selected engine.",
                action_href=back,
                action_label="Back to matches",
            )
        )
    return _page(
        f"{_club_label(match, 'home')} vs {_club_label(match, 'away')}",
        "\n".join(parts),
        filters,
    )


def _filters(
    query: dict[str, list[str]] | None,
    _connection: sqlite3.Connection,
) -> dict[str, str]:
    source = query or {}

    def one(name: str, default: str = "") -> str:
        values = source.get(name) or []
        return values[0].strip() if values else default

    return {
        "engine": "score_model",
        "experiment": BOT_EXPERIMENT_ID,
        "league": one("league").upper(),
        "season": one("season"),
        "team": one("team"),
        "date_from": one("date_from"),
        "date_to": one("date_to"),
        "fixture": one("fixture", "all"),
        "status": one("status", "all"),
        "verdict": one("verdict", "all"),
        "confidence": one("confidence", "all"),
        "order": one("order", "newest"),
        "page": one("page", "1"),
        "predicted": one("predicted"),
        "predict_status": one("predict_status"),
        "predict_detail": one("predict_detail"),
        "run_status": one("run_status"),
        "run_detail": one("run_detail"),
        "sync_status": one("sync_status"),
        "sync_detail": one("sync_detail"),
    }


def _predict_flash(filters: dict[str, str]) -> str:
    status = (filters.get("predict_status") or "").strip()
    detail = (filters.get("predict_detail") or "").strip()
    predicted = (filters.get("predicted") or "").strip()
    if not status and not predicted:
        return ""
    if status in {"priced", "existing"} or predicted == "1":
        kind = "info"
        title = "Prediction stored"
        lead = detail or "Score model prediction is saved for this match."
    elif status == "excluded":
        kind = "info"
        title = "Prediction abstained"
        lead = detail or "Not enough prior data to price this match."
    else:
        kind = "error"
        title = "Prediction failed"
        lead = detail or "Could not price this match."
    return _state(kind=kind, title=title, lead=lead)


def _predict_panel(
    *,
    match_id: str,
    has_prediction: bool,
    csrf_token: str | None,
    query: dict[str, str],
) -> str:
    action = f"/matches/{html.escape(match_id)}/predict"
    retained = _query(
        query,
        predicted="",
        predict_status="",
        predict_detail="",
        page="",
    )
    label = "Re-run prediction" if has_prediction else "Run prediction"
    lead = (
        "Prices this fixture with the live score model (no Gemini) and stores it "
        "for the dashboard. Finished matches are also scored."
    )
    csrf = ""
    if csrf_token:
        csrf = (
            f'<input type="hidden" name="csrf" value="{html.escape(csrf_token)}">'
        )
    next_field = (
        f'<input type="hidden" name="next" value="'
        f'{html.escape(retained)}">'
        if retained
        else ""
    )
    return (
        '<section class="panel predict-panel">'
        "<h2>Predict</h2>"
        f'<p class="note">{html.escape(lead)}</p>'
        f'<form class="predict-form" method="post" action="{action}">'
        f"{csrf}{next_field}"
        f'<button type="submit">{html.escape(label)}</button>'
        "</form>"
        "</section>"
    )


def _default_match_date_range(*, today: date | None = None) -> tuple[str, str]:
    """Matches page default window: 7 Cairo days ago through Cairo today."""

    from core.timeutil import cairo_today

    end = today or cairo_today()
    start = end - timedelta(days=7)
    return start.isoformat(), end.isoformat()


def _live_experiment_for_match(engine: str, competition: str) -> str:
    """Canonical live experiment for a match when the bot engine is selected."""

    if engine != "score_model":
        return EXPERIMENT_ID
    code = (competition or "PL").upper()
    if code == "PL":
        return BOT_EXPERIMENT_ID
    return f"BOT-{code}-live"


def _needs_body_postfilter(filters: dict[str, str]) -> bool:
    return _confidence_threshold(filters.get("confidence") or "all") is not None


def _needs_metrics_postfilter(filters: dict[str, str]) -> bool:
    return (filters.get("verdict") or "all") != "all" or _needs_body_postfilter(filters)


def _match_join_sql(
    engine: str,
    experiment_id: str,
    *,
    include_body: bool = False,
) -> tuple[str, list[Any]]:
    """Shared FROM/JOIN for match list / overview queries."""

    _ = include_body  # SELECT decides body columns; join shape is identical.
    if engine == "score_model":
        # Live bot view: PL → BOT-PL-001; peers → BOT-{code}-live.
        sql = """
            FROM matches m
            LEFT JOIN result_revisions r
              ON r.match_id = m.match_id
             AND r.revision = (
                SELECT MAX(revision) FROM result_revisions WHERE match_id = m.match_id
             )
            LEFT JOIN engine_runs er
              ON er.match_id = m.match_id
             AND er.engine = 'score_model'
             AND er.experiment_id = CASE
                WHEN m.competition = 'PL' THEN ?
                ELSE 'BOT-' || m.competition || '-live'
             END
            LEFT JOIN predictions pr
              ON pr.match_id = m.match_id
             AND pr.engine = 'score_model'
             AND pr.experiment_id = CASE
                WHEN m.competition = 'PL' THEN ?
                ELSE 'BOT-' || m.competition || '-live'
             END
            LEFT JOIN evaluations ev
              ON ev.prediction_id = pr.prediction_id
             AND ev.revision = (
                SELECT MAX(revision) FROM evaluations WHERE prediction_id = pr.prediction_id
             )
        """
        return sql, [BOT_EXPERIMENT_ID, BOT_EXPERIMENT_ID]
    sql = """
            FROM matches m
            LEFT JOIN result_revisions r
              ON r.match_id = m.match_id
             AND r.revision = (
                SELECT MAX(revision) FROM result_revisions WHERE match_id = m.match_id
             )
            LEFT JOIN engine_runs er
              ON er.match_id = m.match_id
             AND er.engine = ?
             AND er.experiment_id = ?
            LEFT JOIN predictions pr
              ON pr.match_id = m.match_id
             AND pr.engine = ?
             AND pr.experiment_id = ?
            LEFT JOIN evaluations ev
              ON ev.prediction_id = pr.prediction_id
             AND ev.revision = (
                SELECT MAX(revision) FROM evaluations WHERE prediction_id = pr.prediction_id
             )
        """
    return sql, [engine, experiment_id, engine, experiment_id]


def _sql_filter_clauses(filters: dict[str, str] | None) -> tuple[str, list[Any]]:
    """Push list filters into SQL. Verdict/confidence stay in Python."""

    if not filters:
        return "", []
    clauses: list[str] = []
    params: list[Any] = []
    league = (filters.get("league") or "").strip()
    if league:
        clauses.append("m.competition = ?")
        params.append(league.upper())
    season = (filters.get("season") or "").strip()
    if season:
        clauses.append("m.season = ?")
        params.append(season)
    team = (filters.get("team") or "").strip()
    if team:
        clauses.append(
            """(
                m.home_name = ? OR m.away_name = ?
                OR EXISTS (
                    SELECT 1 FROM teams t
                    WHERE t.team_id = m.home_id
                      AND COALESCE(NULLIF(t.display_name, ''), t.name) = ?
                )
                OR EXISTS (
                    SELECT 1 FROM teams t
                    WHERE t.team_id = m.away_id
                      AND COALESCE(NULLIF(t.display_name, ''), t.name) = ?
                )
            )"""
        )
        params.extend([team, team, team, team])
    date_from = (filters.get("date_from") or "").strip()
    if date_from:
        clauses.append("substr(m.kickoff, 1, 10) >= ?")
        params.append(date_from)
    date_to = (filters.get("date_to") or "").strip()
    if date_to:
        clauses.append("substr(m.kickoff, 1, 10) <= ?")
        params.append(date_to)
    fixture = (filters.get("fixture") or "all").strip()
    if fixture == "finished":
        clauses.append("r.ft_home IS NOT NULL")
    elif fixture == "scheduled":
        clauses.append("r.ft_home IS NULL")
    status = (filters.get("status") or "all").strip()
    if status != "all":
        clauses.append("COALESCE(er.status, 'pending') = ?")
        params.append(status)
    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), params


def _order_sql(filters: dict[str, str] | None) -> str:
    if filters and filters.get("order") == "oldest":
        return " ORDER BY m.kickoff ASC, m.match_id ASC"
    return " ORDER BY m.kickoff DESC, m.match_id DESC"


def _enrich_match_rows(
    connection: sqlite3.Connection,
    rows: list[sqlite3.Row] | list[dict[str, Any]],
    *,
    include_body: bool,
) -> list[dict[str, Any]]:
    from laboratory.store import (
        competition_emblem_map,
        crest_url_for_team,
        team_crest_map,
        team_display_map,
    )

    if not rows:
        return []
    team_ids = {str(row["home_id"]) for row in rows} | {str(row["away_id"]) for row in rows}
    league_codes = {str(row["competition"] or "PL").upper() for row in rows}
    crests = team_crest_map(connection, team_ids)
    displays = team_display_map(connection, team_ids)
    # Never block match lists on crest HTTP downloads (fetch=False).
    emblems = competition_emblem_map(connection, league_codes, fetch=False)
    loaded: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["metrics"] = _json(item.get("metrics_json"))
        if include_body:
            item["body"] = _json(item.get("body_json"))
        else:
            item["body"] = item.get("body") or {}
        home_id = str(row["home_id"])
        away_id = str(row["away_id"])
        code = str(row["competition"] or "PL").upper()
        item["home_crest"] = crests.get(home_id) or crest_url_for_team(
            home_id, fetch=False
        )
        item["away_crest"] = crests.get(away_id) or crest_url_for_team(
            away_id, fetch=False
        )
        item["home_display"] = displays.get(home_id) or str(row["home_name"])
        item["away_display"] = displays.get(away_id) or str(row["away_name"])
        item["league_emblem"] = emblems.get(code)
        loaded.append(item)
    return loaded


def _attach_prediction_bodies(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    engine: str,
    experiment_id: str,
) -> None:
    """Fill ``body`` for an already-enriched page window (mutates rows)."""

    if not rows:
        return
    ids = [str(row["match_id"]) for row in rows]
    marks = ",".join("?" for _ in ids)
    if engine == "score_model":
        sql = f"""
            SELECT pr.match_id, pr.body_json, m.competition
            FROM predictions pr
            JOIN matches m ON m.match_id = pr.match_id
            WHERE pr.match_id IN ({marks})
              AND pr.engine = 'score_model'
              AND pr.experiment_id = CASE
                WHEN m.competition = 'PL' THEN ?
                ELSE 'BOT-' || m.competition || '-live'
              END
        """
        params: list[Any] = [*ids, BOT_EXPERIMENT_ID]
    else:
        sql = f"""
            SELECT match_id, body_json
            FROM predictions
            WHERE match_id IN ({marks})
              AND engine = ?
              AND experiment_id = ?
        """
        params = [*ids, engine, experiment_id]
    by_id = {
        str(row["match_id"]): _json(row["body_json"])
        for row in connection.execute(sql, params)
    }
    for row in rows:
        row["body"] = by_id.get(str(row["match_id"])) or {}


def _load(
    connection: sqlite3.Connection,
    engine: str,
    experiment_id: str,
    *,
    filters: dict[str, str] | None = None,
    include_body: bool = True,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    select = """
            SELECT m.match_id, m.competition, m.season, m.kickoff,
                   m.home_id, m.home_name, m.away_id, m.away_name,
                   r.ft_home, r.ft_away, r.ht_home, r.ht_away,
                   er.status AS engine_status,
                   ev.metrics_json AS metrics_json
    """
    if include_body:
        select += ",\n                   pr.body_json AS body_json"
    join_sql, join_params = _match_join_sql(
        engine, experiment_id, include_body=include_body
    )
    where_sql, where_params = _sql_filter_clauses(filters)
    sql = select + join_sql + where_sql + _order_sql(filters)
    params: list[Any] = [*join_params, *where_params]
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
    rows = connection.execute(sql, params).fetchall()
    return _enrich_match_rows(connection, rows, include_body=include_body)


def _count_matches(
    connection: sqlite3.Connection,
    engine: str,
    experiment_id: str,
    filters: dict[str, str] | None = None,
) -> int:
    join_sql, join_params = _match_join_sql(
        engine, experiment_id, include_body=False
    )
    where_sql, where_params = _sql_filter_clauses(filters)
    row = connection.execute(
        "SELECT COUNT(*) AS n " + join_sql + where_sql,
        [*join_params, *where_params],
    ).fetchone()
    return int(row["n"] if row else 0)


def _load_match_page(
    connection: sqlite3.Connection,
    filters: dict[str, str],
) -> tuple[int, list[dict[str, Any]]]:
    """Return ``(total_matching, enriched page window)`` for the Matches screen."""

    engine = filters.get("engine") or "score_model"
    experiment_id = filters.get("experiment") or BOT_EXPERIMENT_ID
    page = _page_number(filters)
    offset = (page - 1) * PAGE_SIZE

    if _needs_metrics_postfilter(filters):
        # Verdict / confidence need metrics (and maybe body) before paging.
        candidates = _load(
            connection,
            engine,
            experiment_id,
            filters=filters,
            include_body=_needs_body_postfilter(filters),
        )
        matched = _apply(candidates, filters)
        total = len(matched)
        window = matched[offset : offset + PAGE_SIZE]
        if not _needs_body_postfilter(filters):
            _attach_prediction_bodies(
                connection,
                window,
                engine=engine,
                experiment_id=experiment_id,
            )
        return total, window

    total = _count_matches(connection, engine, experiment_id, filters)
    if total == 0:
        return 0, []
    # Re-clamp page against total so OFFSET stays valid.
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    offset = (page - 1) * PAGE_SIZE
    window = _load(
        connection,
        engine,
        experiment_id,
        filters=filters,
        include_body=False,
        limit=PAGE_SIZE,
        offset=offset,
    )
    _attach_prediction_bodies(
        connection,
        window,
        engine=engine,
        experiment_id=experiment_id,
    )
    return total, window


def _apply(rows: list[dict[str, Any]], filters: dict[str, str]) -> list[dict[str, Any]]:
    chosen = []
    for row in rows:
        if filters["league"] and str(row.get("competition") or "") != filters["league"]:
            continue
        if filters["season"] and row["season"] != filters["season"]:
            continue
        if filters["team"] and filters["team"] not in (
            row["home_name"],
            row["away_name"],
            row.get("home_display"),
            row.get("away_display"),
        ):
            continue
        day = (row["kickoff"] or "")[:10]
        if filters["date_from"] and day < filters["date_from"]:
            continue
        if filters["date_to"] and day > filters["date_to"]:
            continue
        finished = row["ft_home"] is not None
        if filters["fixture"] == "finished" and not finished:
            continue
        if filters["fixture"] == "scheduled" and finished:
            continue
        status = row["engine_status"] or "pending"
        if filters["status"] != "all" and status != filters["status"]:
            continue
        verdict = _verdict(row["metrics"])
        if filters["verdict"] != "all" and verdict != filters["verdict"]:
            continue
        min_confidence = _confidence_threshold(filters.get("confidence") or "all")
        if min_confidence is not None:
            sample = _ft_selection_sample(row)
            if sample is None or sample[0] < min_confidence:
                continue
        chosen.append(row)
    if filters["order"] == "oldest":
        chosen.sort(key=lambda item: (item["kickoff"], item["match_id"]))
    return chosen


def _choices(
    rows: list[dict[str, Any]],
    league: str = "",
) -> tuple[list[str], list[str], list[str]]:
    """Build filter dropdowns from an already-loaded row set (tests / legacy)."""

    leagues = sorted({str(row.get("competition") or "PL") for row in rows})
    scoped = [
        row for row in rows if not league or str(row.get("competition") or "") == league
    ]
    seasons = sorted({row["season"] for row in scoped})
    teams = sorted(
        {
            str(row.get("home_display") or row["home_name"])
            for row in scoped
        }
        | {
            str(row.get("away_display") or row["away_name"])
            for row in scoped
        }
    )
    return leagues, seasons, teams


def _filter_choices(
    connection: sqlite3.Connection,
    league: str = "",
) -> tuple[list[str], list[str], list[str]]:
    """Lightweight DISTINCT queries for Matches/Overview filter dropdowns."""

    leagues = [
        str(row["competition"] or "PL")
        for row in connection.execute(
            """
            SELECT DISTINCT competition FROM matches
            WHERE competition IS NOT NULL AND competition != ''
            ORDER BY competition
            """
        )
    ]
    season_sql = "SELECT DISTINCT season FROM matches"
    season_params: list[Any] = []
    if league:
        season_sql += " WHERE competition = ?"
        season_params.append(league.upper())
    season_sql += " ORDER BY season"
    seasons = [
        str(row["season"])
        for row in connection.execute(season_sql, season_params)
        if row["season"] is not None
    ]
    team_sql = """
        SELECT DISTINCT label FROM (
            SELECT COALESCE(NULLIF(t.display_name, ''), m.home_name) AS label
            FROM matches m
            LEFT JOIN teams t ON t.team_id = m.home_id
            {where}
            UNION
            SELECT COALESCE(NULLIF(t.display_name, ''), m.away_name) AS label
            FROM matches m
            LEFT JOIN teams t ON t.team_id = m.away_id
            {where}
        )
        WHERE label IS NOT NULL AND label != ''
        ORDER BY label
    """
    if league:
        where = "WHERE m.competition = ?"
        team_params = [league.upper(), league.upper()]
    else:
        where = ""
        team_params = []
    teams = [
        str(row["label"])
        for row in connection.execute(team_sql.format(where=where), team_params)
    ]
    return leagues, seasons, teams


def _club_label(row: dict[str, Any] | sqlite3.Row, side: str) -> str:
    display = None
    try:
        display = row[f"{side}_display"]
    except (KeyError, IndexError):
        display = None
    if isinstance(display, str) and display.strip():
        return display
    return str(row[f"{side}_name"])


def _league_label(code: str) -> str:
    return COMPETITION_NAMES.get(code, code)


def _verdict(metrics: dict[str, Any] | None) -> str:
    market = (metrics or {}).get("full_time_1x2") or {}
    if market.get("status") != "scored":
        return "unscored"
    return "correct" if market.get("correct") else "incorrect"


def _stat_cards(rows: list[dict[str, Any]], engine: str) -> str:
    finished = sum(row["ft_home"] is not None for row in rows)
    priced = sum((row["engine_status"] or "") == "completed" for row in rows)
    excluded = sum((row["engine_status"] or "") == "excluded" for row in rows)
    evaluated = sum(_verdict(row["metrics"]) != "unscored" for row in rows)
    ft = _summarize(rows, "full_time_1x2")
    exact = _summarize(rows, "exact_score")
    accuracy = "—" if ft["n"] == 0 else f"{ft['correct'] / ft['n']:.1%}"
    hit = "—" if exact["n"] == 0 else f"{exact['correct'] / exact['n']:.1%}"
    label = ENGINE_LABELS.get(engine, engine)
    return f"""
    <section class="cards">
      <article><span>Engine</span><strong>{html.escape(label)}</strong></article>
      <article><span>Matches</span><strong>{len(rows)}</strong></article>
      <article><span>Finished</span><strong>{finished}</strong></article>
      <article><span>Priced</span><strong>{priced}</strong></article>
      <article><span>Excluded</span><strong>{excluded}</strong></article>
      <article><span>Evaluated</span><strong>{evaluated}</strong></article>
      <article><span>FT 1X2 accuracy</span><strong>{accuracy}</strong></article>
      <article><span>Exact-score hit</span><strong>{hit}</strong></article>
    </section>
    """


def _market_table(rows: list[dict[str, Any]]) -> str:
    head = "<tr><th>Market</th><th>Rate</th><th>Sample</th><th>Log loss</th><th>Brier</th></tr>"
    body = []
    for key, label, kind in MARKETS:
        summary = _summarize(rows, key)
        sample = "—" if summary["n"] == 0 else str(int(summary["n"]))
        body.append(
            "<tr>"
            f"<td>{label}</td>"
            f"<td>{_rate(summary, kind)}</td>"
            f'<td class="mono">{sample}</td>'
            f"<td>{_mean(summary, 'log_loss')}</td>"
            f"<td>{_mean(summary, 'brier')}</td>"
            "</tr>"
        )
    return f'<div class="table-wrap"><table>{head}{"".join(body)}</table></div>'


def _overview_charts(rows: list[dict[str, Any]]) -> str:
    """Accuracy, monthly trend, and FT calibration for scored evaluations."""

    market = _market_accuracy_chart(rows)
    monthly = _monthly_accuracy_chart(rows)
    calibration = _calibration_chart(rows)
    if not market and not monthly and not calibration:
        return ""
    parts = [item for item in (market, monthly, calibration) if item]
    return (
        '<section class="panel charts-panel" aria-label="Evaluation charts">'
        "<h2>Charts</h2>"
        '<p class="note">Rates use scored evaluations only under the current filters.</p>'
        f'<div class="chart-grid">{"".join(parts)}</div>'
        "</section>"
    )


def _market_accuracy_chart(rows: list[dict[str, Any]]) -> str:
    bars: list[str] = []
    any_scored = False
    for key, label, _kind in MARKETS:
        summary = _summarize(rows, key)
        if summary["n"] == 0:
            continue
        any_scored = True
        rate = summary["correct"] / summary["n"]
        width = max(2.0, rate * 100.0)
        bars.append(
            "<div class=\"hbar-row\">"
            f"<span class=\"hbar-label\">{html.escape(label)}</span>"
            '<span class="hbar-track" role="img" '
            f'aria-label="{html.escape(label)} {rate:.1%} on {int(summary["n"])}">'
            f'<i style="width:{width:.1f}%"></i>'
            "</span>"
            f'<span class="hbar-value mono">{rate:.0%} · {int(summary["n"])}</span>'
            "</div>"
        )
    if not any_scored:
        return ""
    return (
        '<figure class="chart">'
        "<figcaption>Market accuracy</figcaption>"
        f'<div class="hbar-chart">{"".join(bars)}</div>'
        "</figure>"
    )


def _monthly_accuracy_chart(rows: list[dict[str, Any]]) -> str:
    buckets: dict[str, dict[str, float]] = {}
    for row in rows:
        metrics = row.get("metrics") or {}
        market = metrics.get("full_time_1x2") or {}
        if market.get("status") != "scored":
            continue
        month = str(row.get("kickoff") or "")[:7]
        if len(month) != 7:
            continue
        bucket = buckets.setdefault(month, {"n": 0.0, "correct": 0.0})
        bucket["n"] += 1
        bucket["correct"] += float(bool(market.get("correct")))
    if len(buckets) < 2:
        return ""
    months = sorted(buckets)
    # Cap to the most recent 12 months for readability.
    months = months[-12:]
    max_rate = max((buckets[m]["correct"] / buckets[m]["n"] for m in months), default=1.0)
    scale = 100.0 / max(max_rate, 0.01)
    columns: list[str] = []
    for month in months:
        n = buckets[month]["n"]
        rate = buckets[month]["correct"] / n
        height = max(4.0, rate * scale)
        label = f"{month[5:]}'{month[2:4]}"
        columns.append(
            "<div class=\"vbar\">"
            f'<span class="vbar-value mono">{rate:.0%}</span>'
            '<span class="vbar-track">'
            f'<i style="height:{height:.1f}%" title="{html.escape(month)}: '
            f'{rate:.1%} · {int(n)}"></i>'
            "</span>"
            f'<span class="vbar-label">{html.escape(label)}</span>'
            "</div>"
        )
    return (
        '<figure class="chart">'
        "<figcaption>Full-time accuracy by month</figcaption>"
        f'<div class="vbar-chart" role="img" aria-label="Monthly full-time accuracy">'
        f'{"".join(columns)}</div>'
        "</figure>"
    )


def _calibration_chart(rows: list[dict[str, Any]]) -> str:
    # Five equal-width confidence buckets for the FT selection probability.
    edges = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0001)
    labels = ("0–20%", "20–40%", "40–60%", "60–80%", "80–100%")
    totals = [{"n": 0, "correct": 0, "prob_sum": 0.0} for _ in labels]
    for row in rows:
        sample = _ft_selection_sample(row)
        if sample is None:
            continue
        probability, correct = sample
        index = min(int(probability * 5), 4)
        if probability >= 1.0:
            index = 4
        totals[index]["n"] += 1
        totals[index]["correct"] += int(correct)
        totals[index]["prob_sum"] += probability
    scored = sum(item["n"] for item in totals)
    if scored < 5:
        return ""
    width, height = 320, 200
    pad_l, pad_r, pad_t, pad_b = 36, 12, 12, 36
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    points_pred: list[str] = []
    points_obs: list[str] = []
    bars: list[str] = []
    legend_n = 0
    for index, item in enumerate(totals):
        mid = (edges[index] + min(edges[index + 1], 1.0)) / 2.0
        x = pad_l + mid * plot_w
        # Ideal calibration diagonal marker at this bucket midpoint.
        y_ideal = pad_t + (1.0 - mid) * plot_h
        points_pred.append(f"{x:.1f},{y_ideal:.1f}")
        if item["n"] == 0:
            continue
        legend_n += 1
        observed = item["correct"] / item["n"]
        predicted = item["prob_sum"] / item["n"]
        y_obs = pad_t + (1.0 - observed) * plot_h
        points_obs.append(f"{x:.1f},{y_obs:.1f}")
        # Bucket bar: height = observed frequency, opacity scales with sample.
        bar_w = plot_w / 5.0 * 0.55
        bar_h = observed * plot_h
        bar_x = pad_l + (index + 0.5) * (plot_w / 5.0) - bar_w / 2.0
        bar_y = pad_t + plot_h - bar_h
        opacity = 0.25 + min(0.55, item["n"] / max(scored, 1) * 2.5)
        bars.append(
            f'<rect x="{bar_x:.1f}" y="{bar_y:.1f}" width="{bar_w:.1f}" '
            f'height="{max(bar_h, 1.0):.1f}" rx="2" '
            f'fill="rgba(52,211,153,{opacity:.2f})"></rect>'
            f'<text x="{x:.1f}" y="{height - 10}" text-anchor="middle" '
            f'class="chart-axis">{html.escape(labels[index])}</text>'
            f'<title>{html.escape(labels[index])}: predicted {predicted:.0%}, '
            f"observed {observed:.0%} · n={item['n']}</title>"
        )
    if legend_n < 2:
        return ""
    ideal_line = " ".join(points_pred)
    obs_line = " ".join(points_obs)
    obs_dots = "".join(
        f'<circle cx="{pt.split(",")[0]}" cy="{pt.split(",")[1]}" r="3.5" '
        f'fill="var(--accent)" stroke="#042f24" stroke-width="1"></circle>'
        for pt in points_obs
    )
    return (
        '<figure class="chart">'
        "<figcaption>FT selection calibration</figcaption>"
        f'<svg class="cal-chart" viewBox="0 0 {width} {height}" role="img" '
        'aria-label="Full-time selection calibration">'
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t + plot_h}" '
        'stroke="#35506f" stroke-width="1"></line>'
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{pad_l + plot_w}" '
        f'y2="{pad_t + plot_h}" stroke="#35506f" stroke-width="1"></line>'
        f'<polyline fill="none" stroke="rgba(148,163,184,0.55)" stroke-width="1.5" '
        f'stroke-dasharray="4 4" points="{ideal_line}"></polyline>'
        f"{''.join(bars)}"
        f'<polyline fill="none" stroke="var(--accent)" stroke-width="2" '
        f'points="{obs_line}"></polyline>'
        f"{obs_dots}"
        f'<text x="{pad_l - 8}" y="{pad_t + 4}" text-anchor="end" class="chart-axis">100%</text>'
        f'<text x="{pad_l - 8}" y="{pad_t + plot_h}" text-anchor="end" class="chart-axis">0%</text>'
        "</svg>"
        '<p class="chart-legend note">Dashed = perfect calibration · Bars/dots = observed hit rate by stated confidence</p>'
        "</figure>"
    )


def _ft_1x2_from_body(body: dict[str, Any] | None) -> tuple[str | None, float | None]:
    """Return (selection, probability) for the FT 1X2 pick from a prediction body."""

    markets = _prediction_market_confidences(body)
    item = markets.get("full_time_1x2") or {}
    selection = item.get("selection")
    confidence = item.get("confidence")
    return (
        None if selection is None else str(selection),
        None if confidence is None else float(confidence),
    )


def _prediction_market_confidences(
    body: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Pick + confidence for FT 1X2, HT, exact score, O/U 2.5, and O/U 3.5."""

    empty = {
        "full_time_1x2": {"label": "FT 1X2", "selection": None, "confidence": None},
        "half_time_1x2": {"label": "HT", "selection": None, "confidence": None},
        "exact_score": {"label": "Score", "selection": None, "confidence": None},
        "over_under_2_5": {"label": "Goals 2.5", "selection": None, "confidence": None},
        "over_under_3_5": {"label": "Goals 3.5", "selection": None, "confidence": None},
    }
    if not body:
        return empty

    def _clamp(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number:  # NaN
            return None
        return max(0.0, min(1.0, number))

    def _result_pick(market: dict[str, Any]) -> tuple[str | None, float | None]:
        selection = market.get("most_likely")
        if not selection:
            return None, None
        label = str(selection)
        return label, _clamp(market.get(label))

    def _ou_pick(market: dict[str, Any]) -> tuple[str | None, float | None]:
        selection = market.get("most_likely")
        if selection in {"over", "under"}:
            return str(selection), _clamp(market.get(selection))
        try:
            over = float(market.get("over"))
        except (TypeError, ValueError):
            return None, None
        under = _clamp(market.get("under", 1.0 - over))
        over_c = _clamp(over)
        if over_c is None or under is None:
            return None, None
        if over_c >= under:
            return "over", over_c
        return "under", under

    out = dict(empty)
    if body.get("engine") in ("poisson", "score_model"):
        full_time = body.get("full_time") or {}
        sel, conf = _result_pick(full_time.get("result_1x2") or {})
        out["full_time_1x2"] = {
            "label": "FT 1X2",
            "selection": None if sel is None else sel.replace("_", " ").title(),
            "confidence": conf,
        }
        half = body.get("half_time") or {}
        if half.get("withheld"):
            out["half_time_1x2"] = {
                "label": "HT",
                "selection": "withheld",
                "confidence": None,
            }
        else:
            sel, conf = _result_pick(half.get("result_1x2") or {})
            out["half_time_1x2"] = {
                "label": "HT",
                "selection": None if sel is None else sel.replace("_", " ").title(),
                "confidence": conf,
            }
        mode = full_time.get("exact_score_most_likely") or {}
        cells = full_time.get("exact_scores") or []
        if mode:
            home = mode.get("home_goals")
            away = mode.get("away_goals")
            chance = None
            for cell in cells:
                if int(cell.get("home_goals", -1)) == int(home) and int(
                    cell.get("away_goals", -1)
                ) == int(away):
                    chance = _clamp(cell.get("probability"))
                    break
            out["exact_score"] = {
                "label": "Score",
                "selection": f"{home}–{away}",
                "confidence": chance,
            }
        sel, conf = _ou_pick(full_time.get("over_under_2_5") or {})
        out["over_under_2_5"] = {
            "label": "Goals 2.5",
            "selection": None if sel is None else sel.title(),
            "confidence": conf,
        }
        sel, conf = _ou_pick(full_time.get("over_under_3_5") or {})
        out["over_under_3_5"] = {
            "label": "Goals 3.5",
            "selection": None if sel is None else sel.title(),
            "confidence": conf,
        }
        return out

    # Flat Gemini-shaped bodies.
    sel, conf = _result_pick(body.get("full_time_1x2") or {})
    if sel is None and body.get("full_time_most_likely"):
        sel = str(body.get("full_time_most_likely"))
        conf = _clamp((body.get("full_time_1x2") or {}).get(sel))
    out["full_time_1x2"] = {
        "label": "FT 1X2",
        "selection": None if sel is None else sel.replace("_", " ").title(),
        "confidence": conf,
    }
    if body.get("half_time_withheld"):
        out["half_time_1x2"] = {
            "label": "HT",
            "selection": "withheld",
            "confidence": None,
        }
    else:
        sel, conf = _result_pick(body.get("half_time_1x2") or {})
        if sel is None and body.get("half_time_most_likely"):
            sel = str(body.get("half_time_most_likely"))
            conf = _clamp((body.get("half_time_1x2") or {}).get(sel))
        out["half_time_1x2"] = {
            "label": "HT",
            "selection": None if sel is None else sel.replace("_", " ").title(),
            "confidence": conf,
        }
    mode = body.get("exact_score_most_likely") or {}
    if mode:
        out["exact_score"] = {
            "label": "Score",
            "selection": f"{mode.get('home_goals')}–{mode.get('away_goals')}",
            "confidence": None,
        }
    for key, label, flat_key in (
        ("over_under_2_5", "Goals 2.5", "over_under_2_5"),
        ("over_under_3_5", "Goals 3.5", "over_under_3_5"),
    ):
        sel, conf = _ou_pick(body.get(flat_key) or {})
        most = body.get(f"{flat_key}_most_likely")
        if sel is None and most:
            sel = str(most)
            conf = _clamp((body.get(flat_key) or {}).get(sel))
        out[key] = {
            "label": label,
            "selection": None if sel is None else sel.title(),
            "confidence": conf,
        }
    return out


def _market_confidence_strip(body: dict[str, Any] | None) -> str:
    """Compact pick+confidence chips for match cards."""

    markets = _prediction_market_confidences(body)
    order = (
        "full_time_1x2",
        "half_time_1x2",
        "exact_score",
        "over_under_2_5",
        "over_under_3_5",
    )
    chips: list[str] = []
    for key in order:
        item = markets[key]
        selection = item.get("selection")
        if not selection:
            continue
        conf = item.get("confidence")
        conf_html = (
            f'<span class="ft-conf mono">{float(conf):.0%}</span>'
            if conf is not None
            else ""
        )
        chips.append(
            '<span class="market-chip">'
            f'<span class="market-chip-label">{html.escape(str(item["label"]))}</span>'
            f'<span class="market-chip-pick">{html.escape(str(selection))}</span>'
            f"{conf_html}"
            "</span>"
        )
    if not chips:
        return ""
    return f'<div class="market-strip" aria-label="Market confidences">{"".join(chips)}</div>'


def _market_confidence_panel(body: dict[str, Any] | None) -> str:
    """Detail panel listing each market pick with confidence."""

    markets = _prediction_market_confidences(body)
    order = (
        "full_time_1x2",
        "half_time_1x2",
        "exact_score",
        "over_under_2_5",
        "over_under_3_5",
    )
    cards: list[str] = []
    for key in order:
        item = markets[key]
        selection = item.get("selection") or "—"
        conf = item.get("confidence")
        conf_html = (
            f'<span class="ft-conf mono">{float(conf):.0%}</span>'
            if conf is not None
            else '<span class="ft-conf muted">—</span>'
        )
        cards.append(
            '<article class="ft-pick market-conf-card" role="group" '
            f'aria-label="{html.escape(str(item["label"]))} '
            f'{html.escape(str(selection))}'
            f'{f", {float(conf):.0%}" if conf is not None else ""}">'
            f'<span class="ft-pick-label">{html.escape(str(item["label"]))}</span>'
            f'<strong class="ft-pick-value">{html.escape(str(selection))}</strong>'
            f"{conf_html}"
            "</article>"
        )
    return (
        '<div class="market-conf-grid" role="group" aria-label="Prediction confidences">'
        f"{''.join(cards)}"
        "</div>"
    )


def _ft_selection_sample(row: dict[str, Any]) -> tuple[float, bool] | None:
    metrics = row.get("metrics") or {}
    market = metrics.get("full_time_1x2") or {}
    if market.get("status") != "scored":
        return None
    selection = market.get("selection")
    if not selection:
        return None
    body = row.get("body") or {}
    probability = None
    if body.get("engine") in ("poisson", "score_model"):
        block = (body.get("full_time") or {}).get("result_1x2") or {}
        if selection in block:
            probability = float(block[selection])
    if probability is None:
        block = body.get("full_time_1x2") or {}
        if selection in block:
            probability = float(block[selection])
    if probability is None:
        # Fall back to probability of actual when selection probability is absent.
        if "probability_of_actual" in market and market.get("correct"):
            probability = float(market["probability_of_actual"])
        else:
            return None
    return max(0.0, min(1.0, probability)), bool(market.get("correct"))


def _summarize(rows: list[dict[str, Any]], market: str) -> dict[str, float]:
    totals = {"n": 0, "correct": 0, "log_loss": 0.0, "brier": 0.0, "brier_n": 0}
    for row in rows:
        metrics = row.get("metrics") or {}
        item = metrics.get(market) or {}
        if item.get("status") != "scored":
            continue
        totals["n"] += 1
        totals["correct"] += int(bool(item.get("correct")))
        if "log_loss" in item:
            totals["log_loss"] += float(item["log_loss"])
        if "brier" in item:
            totals["brier"] += float(item["brier"])
            totals["brier_n"] += 1
    return totals


def _rate(summary: dict[str, float], kind: str) -> str:
    if summary["n"] == 0:
        return "—"
    return f"{summary['correct'] / summary['n']:.1%} {kind}"


def _mean(summary: dict[str, float], key: str) -> str:
    count = summary["brier_n"] if key == "brier" else summary["n"]
    if count == 0:
        return "—"
    return f"{summary[key] / count:.3f}"


def _match_table(rows: list[dict[str, Any]], filters: dict[str, str]) -> str:
    items = []
    for row in rows:
        href = f"/matches/{html.escape(row['match_id'])}"
        query = _query(filters, page="")
        if query:
            href = f"{href}?{html.escape(query)}"
        score = "—"
        if row["ft_home"] is not None:
            score = f"{row['ft_home']}–{row['ft_away']}"
        predicted = "—"
        body = row.get("body") or {}
        markets = _prediction_market_confidences(body)
        score_pick = markets.get("exact_score") or {}
        if score_pick.get("selection"):
            predicted = str(score_pick["selection"])
        verdict = _verdict(row["metrics"])
        status = verdict if verdict != "unscored" else (row["engine_status"] or "pending")
        match_id = str(row["match_id"])
        league_code = str(row.get("competition") or "")
        league = html.escape(_league_label(league_code))
        league_mark = _logo_img(
            row.get("league_emblem"),
            _league_label(league_code),
            kind="emblem",
            size=14,
        )
        home_label = _club_label(row, "home")
        away_label = _club_label(row, "away")
        home_crest = _logo_img(row.get("home_crest"), home_label, size=32)
        away_crest = _logo_img(row.get("away_crest"), away_label, size=32)
        items.append(
            f'<a class="match-card" href="{href}" title="{html.escape(match_id)}">'
            f'<div class="match-row">'
            f'<span class="club-name home">{home_crest}'
            f'<span class="club-text">{html.escape(home_label)}</span></span>'
            f'<span class="score-pair" aria-label="Predicted and actual scores">'
            f'<span class="score predicted" title="Predicted">'
            f'<span class="score-label">P</span>'
            f'<span class="mono">{html.escape(predicted)}</span></span>'
            f'<span class="score actual" title="Full time">'
            f'<span class="score-label">FT</span>'
            f'<span class="mono">{html.escape(score)}</span></span>'
            f"</span>"
            f'<span class="club-name away">'
            f'<span class="club-text">{html.escape(away_label)}</span>{away_crest}</span>'
            f"</div>"
            f'{_market_confidence_strip(body)}'
            f'<div class="match-sub">'
            f'<span class="match-meta">'
            f'<span class="league-chip">{league_mark}{league}</span>'
            f"<span>{_when_short(row['kickoff'])}</span>"
            f"</span>"
            f"{_badge(status)}"
            f"</div>"
            "</a>"
        )
    return f'<div class="match-list">{"".join(items)}</div>'


def _filter_form(
    action: str,
    filters: dict[str, str],
    leagues: list[str],
    seasons: list[str],
    teams: list[str],
    *,
    submit: str,
) -> str:
    league_labels = {code: _league_label(code) for code in leagues}
    return f"""
    <form class="filters" method="get" action="{html.escape(action)}">
      <label class="filter-span-2">Engine
        <select disabled aria-disabled="true" title="Bot score model only">
          <option value="score_model" selected>Bot score model</option>
        </select>
        <input type="hidden" name="engine" value="score_model">
      </label>
      <label>League
        <select name="league">
          <option value="">All leagues</option>
          {_options(leagues, filters["league"], labels=league_labels)}
        </select>
      </label>
      <label>Season
        <select name="season">
          <option value="">All seasons</option>
          {_options(seasons, filters["season"])}
        </select>
      </label>
      <label class="filter-span-2">Club
        <select name="team">
          <option value="">Any club</option>
          {_options(teams, filters["team"])}
        </select>
      </label>
      <label>From <input type="date" name="date_from" value="{html.escape(filters["date_from"])}"></label>
      <label>To <input type="date" name="date_to" value="{html.escape(filters["date_to"])}"></label>
      <label>Fixture
        <select name="fixture">{_options(
            ["all", "finished", "scheduled"],
            filters["fixture"],
            labels={"all": "All", "finished": "Finished", "scheduled": "Not played"},
        )}</select>
      </label>
      <label>Status
        <select name="status">{_options(
            ["all", "completed", "excluded", "pending", "failed"],
            filters["status"],
            labels={
                "all": "Any",
                "completed": "Priced",
                "excluded": "Excluded",
                "pending": "Pending",
                "failed": "Failed",
            },
        )}</select>
      </label>
      <label>FT check
        <select name="verdict">{_options(
            ["all", "correct", "incorrect", "unscored"],
            filters["verdict"],
            labels={
                "all": "Any",
                "correct": "Correct",
                "incorrect": "Incorrect",
                "unscored": "Not scored",
            },
        )}</select>
      </label>
      <label>FT confidence
        <select name="confidence">{_options(
            ["all", "45", "55", "65"],
            filters.get("confidence") or "all",
            labels={
                "all": "Any",
                "45": "≥ 45%",
                "55": "≥ 55%",
                "65": "≥ 65%",
            },
        )}</select>
      </label>
      <label>Order
        <select name="order">{_options(
            ["newest", "oldest"],
            filters["order"],
            labels={"newest": "Newest", "oldest": "Oldest"},
        )}</select>
      </label>
      <div class="filter-actions">
        <button type="submit">{html.escape(submit)}</button>
        <a class="clear" href="{html.escape(action)}">Clear</a>
      </div>
    </form>
    """


def _options(values: list[str], selected: str, labels: dict[str, str] | None = None) -> str:
    labels = labels or {}
    parts = []
    for value in values:
        mark = " selected" if value == selected else ""
        label = labels.get(value, value)
        parts.append(f'<option value="{html.escape(value)}"{mark}>{html.escape(label)}</option>')
    return "".join(parts)


def _pager(path: str, filters: dict[str, str], page: int, pages: int) -> str:
    if pages <= 1:
        return ""
    links = []
    if page > 1:
        links.append(f'<a href="{path}?{html.escape(_query(filters, page=str(page - 1)))}">Previous</a>')
    links.append(f"<span>Page {page} of {pages}</span>")
    if page < pages:
        links.append(f'<a href="{path}?{html.escape(_query(filters, page=str(page + 1)))}">Next</a>')
    return f'<nav class="pager">{"".join(links)}</nav>'


def _scoreboard(
    match: sqlite3.Row,
    result: sqlite3.Row | None,
    connection: sqlite3.Connection | None = None,
) -> str:
    if result is None or result["ft_home"] is None:
        home_goals = "—"
        away_goals = "—"
        half = "Half time unavailable"
    else:
        home_goals = str(int(result["ft_home"]))
        away_goals = str(int(result["ft_away"]))
        half = (
            "Half time unavailable"
            if result["ht_home"] is None
            else f"HT {result['ht_home']}–{result['ht_away']}"
        )
    from laboratory.store import (
        competition_emblem_map,
        crest_url_for_team,
        emblem_url_for_competition,
        team_crest_map,
        team_display_map,
    )

    home_id = str(match["home_id"])
    away_id = str(match["away_id"])
    code = str(match["competition"] or "PL").upper()
    # Prefer DB + local cache; never block match detail on crest HTTP (same as match list).
    home_crest = None
    away_crest = None
    home_label = _club_label(match, "home")
    away_label = _club_label(match, "away")
    league_emblem = None
    if connection is not None:
        crests = team_crest_map(connection, [home_id, away_id])
        displays = team_display_map(connection, [home_id, away_id])
        emblems = competition_emblem_map(connection, [code], fetch=False)
        home_crest = crests.get(home_id)
        away_crest = crests.get(away_id)
        home_label = displays.get(home_id) or home_label
        away_label = displays.get(away_id) or away_label
        league_emblem = emblems.get(code)
    home_crest = home_crest or crest_url_for_team(home_id, fetch=False)
    away_crest = away_crest or crest_url_for_team(away_id, fetch=False)
    league_emblem = league_emblem or emblem_url_for_competition(code, fetch=False)

    def club_row(side: str, crest: str | None, name: str, goals: str) -> str:
        return (
            f'<div class="club {side}">'
            f"{_logo_img(crest, name, size=48)}"
            f'<span class="club-text">{html.escape(name)}</span>'
            f'<span class="club-score mono" aria-label="{html.escape(name)} goals">'
            f"{html.escape(goals)}</span>"
            f"</div>"
        )

    return f"""
    <section class="scoreboard">
      <div class="scoreboard-teams" role="group" aria-label="Full time score">
        {club_row("home", home_crest, home_label, home_goals)}
        {club_row("away", away_crest, away_label, away_goals)}
      </div>
      <p class="scoreboard-meta note">{_logo_img(league_emblem, _league_label(code), kind="emblem", size=20)}{html.escape(half)} · {_when(match['kickoff'])} · {html.escape(match['season'])}</p>
    </section>
    """


def _facts(match: sqlite3.Row, snapshot: sqlite3.Row | None) -> str:
    match_id = html.escape(str(match["match_id"]))
    match_id_card = (
        f'<article class="match-id-card">'
        f"<span>Match id</span>"
        f'<strong class="small match-id-value">'
        f'<span class="mono match-id-text">{match_id}</span>'
        f'<button type="button" class="copy-btn" data-copy="{match_id}" '
        f'aria-label="Copy match id" title="Copy">'
        f'<svg class="copy-icon" width="16" height="16" viewBox="0 0 16 16" '
        f'fill="none" aria-hidden="true">'
        f'<rect x="5.5" y="5.5" width="8" height="8" rx="1.5" '
        f'stroke="currentColor" stroke-width="1.5"/>'
        f'<path d="M10.5 5.5V4A1.5 1.5 0 0 0 9 2.5H4A1.5 1.5 0 0 0 2.5 4v5'
        f'A1.5 1.5 0 0 0 4 10.5h1.5" stroke="currentColor" stroke-width="1.5" '
        f'stroke-linecap="round"/>'
        f"</svg>"
        f'<svg class="copied-icon" width="16" height="16" viewBox="0 0 16 16" '
        f'fill="none" aria-hidden="true" hidden>'
        f'<path d="M3.5 8.5l3 3 6-6" stroke="currentColor" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        f"</svg>"
        f"</button>"
        f"</strong>"
        f"</article>"
    )
    if snapshot is None:
        return (
            _state(
                kind="info",
                title="No pre-match snapshot",
                lead="Coverage and leakage checks are unavailable until a snapshot is stored.",
            )
            + f'<section class="cards compact">{match_id_card}</section>'
        )
    # Prefer slim query columns (leakage_ok / json_extract). Legacy rows with payload_json still work.
    if "payload_json" in snapshot.keys():
        payload = json.loads(snapshot["payload_json"] or "{}")
        coverage = payload.get("coverage") or {}
        prior_ft = coverage.get("prior_full_time_matches", "—")
        prior_ht = coverage.get("prior_half_time_matches", "—")
        leakage_ok = payload.get("leakage_ok")
    else:
        prior_ft = snapshot["prior_ft"] if snapshot["prior_ft"] is not None else "—"
        prior_ht = snapshot["prior_ht"] if snapshot["prior_ht"] is not None else "—"
        leakage_ok = bool(snapshot["leakage_ok"])
    leakage = "passed" if leakage_ok else "failed"
    return f"""
    <section class="cards compact">
      <article><span>Prior matches</span><strong>{html.escape(str(prior_ft))}</strong></article>
      <article><span>Prior HT samples</span><strong>{html.escape(str(prior_ht))}</strong></article>
      <article><span>Leakage</span><strong>{leakage}</strong></article>
      {match_id_card}
    </section>
    """


def _comparison(
    prediction: sqlite3.Row,
    result: sqlite3.Row | None,
    evaluation: sqlite3.Row | None,
) -> str:
    if result is None or result["ft_home"] is None:
        return _state(
            kind="info",
            title="Result unavailable",
            lead="Full-time score is not stored yet, so market comparison stays pending.",
        )
    metrics = _json(evaluation["metrics_json"]) if evaluation else {}
    try:
        body = _json(prediction["body_json"]) if prediction is not None else {}
    except (KeyError, IndexError):
        body = {}
    confidences = _prediction_market_confidences(body)
    actual_ft = _outcome(int(result["ft_home"]), int(result["ft_away"]))
    labels = {
        "full_time_1x2": ("Full-time result", actual_ft),
        "half_time_1x2": (
            "Half-time result",
            None if result["ht_home"] is None else _outcome(int(result["ht_home"]), int(result["ht_away"])),
        ),
        "exact_score": ("Exact score", f"{result['ft_home']}–{result['ft_away']}"),
        "over_under_2_5": (
            "Over/under 2.5",
            "over" if int(result["ft_home"]) + int(result["ft_away"]) >= 3 else "under",
        ),
        "over_under_3_5": (
            "Over/under 3.5",
            "over" if int(result["ft_home"]) + int(result["ft_away"]) >= 4 else "under",
        ),
    }
    rows = [
        "<tr><th>Market</th><th>Pick</th><th>Confidence</th><th>Actual</th>"
        "<th>Check</th><th>Log loss</th><th>Brier</th></tr>"
    ]
    for key, (label, actual) in labels.items():
        market = (metrics or {}).get(key) or {}
        conf = (confidences.get(key) or {}).get("confidence")
        conf_cell = "—" if conf is None else f"{float(conf):.0%}"
        if market.get("status") != "scored":
            rows.append(
                f"<tr><td>{label}</td><td>—</td><td>{conf_cell}</td>"
                f"<td>{html.escape(str(actual or '—'))}</td>"
                f"<td>{_badge('withheld' if market.get('status') == 'withheld' else 'pending')}</td>"
                "<td>—</td><td>—</td></tr>"
            )
            continue
        selection = market.get("selection")
        if key == "exact_score":
            selection = "hit" if market.get("correct") else "miss"
        log_loss = market.get("log_loss")
        brier = market.get("brier")
        log_cell = "—" if log_loss is None else f"{float(log_loss):.3f}"
        brier_cell = "—" if brier is None else f"{float(brier):.3f}"
        rows.append(
            "<tr>"
            f"<td>{label}</td>"
            f"<td>{html.escape(str(selection).replace('_', ' '))}</td>"
            f"<td>{conf_cell}</td>"
            f"<td>{html.escape(str(actual))}</td>"
            f"<td>{_badge('correct' if market.get('correct') else 'incorrect')}</td>"
            f"<td>{log_cell}</td>"
            f"<td>{brier_cell}</td>"
            "</tr>"
        )
    return (
        f'<section class="panel"><h2>Actual comparison</h2>'
        f'<div class="table-wrap"><table>{"".join(rows)}</table></div></section>'
    )


def _engine_panel(
    engine: str,
    model_id: str,
    body: dict[str, Any],
    evaluation: sqlite3.Row | None,
    run: sqlite3.Row | None,
) -> str:
    title = ENGINE_LABELS.get(engine, engine.title())
    status = _badge(run["status"]) if run is not None else ""
    if body.get("engine") in ("poisson", "score_model"):
        market = body["full_time"]["result_1x2"]
        mode = body["full_time"]["exact_score_most_likely"]
        lines = [
            (
                f"<p>λ home {body['lambda_home']:.2f} · λ away {body['lambda_away']:.2f} · "
                f"k={body.get('k', '—')} · priors {body.get('prior_match_count', '—')}</p>"
            ),
            _market_confidence_panel(body),
            "<h3>Full time 1X2</h3>"
            + _prob_row("Home", market["home"])
            + _prob_row("Draw", market["draw"])
            + _prob_row("Away", market["away"]),
            _most_likely_score(mode, body["full_time"].get("exact_scores") or []),
            _score_list(body["full_time"]["exact_scores"]),
        ]
        if body["half_time"].get("withheld"):
            lines.append(
                f"<p class=\"note\">Half time withheld. "
                f"{html.escape(str(body['half_time'].get('reason') or ''))}</p>"
            )
        else:
            half = body["half_time"]["result_1x2"]
            lines.append(
                "<h3>Half time</h3>"
                + _prob_row("Home", half["home"])
                + _prob_row("Draw", half["draw"])
                + _prob_row("Away", half["away"])
            )
        overs = body["full_time"]["over_under_2_5"]
        overs_35 = body["full_time"]["over_under_3_5"]
        lines.append(_over_under_cards(overs, overs_35))
        notes = list(body.get("limitations") or []) + list(body.get("assumptions") or [])
        if notes:
            items = "".join(f"<li>{html.escape(item)}</li>" for item in notes)
            lines.append(f"<ul class=\"notes\">{items}</ul>")
    else:
        lines = [
            _market_confidence_panel(body),
            f"<p>{html.escape(str(body.get('analysis') or 'No nested markets.'))}</p>",
        ]
    return (
        f'<section class="panel"><h2>{html.escape(title)} '
        f'<span class="model">{html.escape(model_id)}</span> {status}</h2>'
        f"{''.join(lines)}</section>"
    )


def _prob_row(label: str, probability: float) -> str:
    width = max(0.0, min(100.0, float(probability) * 100))
    return (
        '<div class="prob">'
        f"<span>{html.escape(label)}</span>"
        f'<span class="meter"><i style="width:{width:.1f}%"></i></span>'
        f"<span>{probability:.1%}</span>"
        "</div>"
    )


def _most_likely_score(mode: dict[str, Any], cells: list[dict[str, Any]]) -> str:
    home = int(mode["home_goals"])
    away = int(mode["away_goals"])
    chance = None
    for cell in cells:
        if int(cell.get("home_goals", -1)) == home and int(cell.get("away_goals", -1)) == away:
            chance = float(cell["probability"])
            break
    chip = (
        f'<span class="mls-chip mono">{chance:.1%}</span>'
        if chance is not None
        else ""
    )
    return (
        f'<article class="mls" role="group" '
        f'aria-label="Most likely score {home}–{away}'
        f'{f", {chance:.1%}" if chance is not None else ""}">'
        '<div class="mls-top">'
        '<span class="mls-label">Most likely score</span>'
        f"{chip}"
        "</div>"
        '<div class="mls-scoreline" aria-hidden="true">'
        '<div class="mls-team">'
        f'<span class="mls-goal mono">{home}</span>'
        '<span class="mls-team-label">Home</span>'
        "</div>"
        '<span class="mls-sep">–</span>'
        '<div class="mls-team">'
        f'<span class="mls-goal mono">{away}</span>'
        '<span class="mls-team-label">Away</span>'
        "</div>"
        "</div>"
        "</article>"
    )


def _over_under_cards(over_25: dict[str, Any], over_35: dict[str, Any]) -> str:
    def card(line: str, market: dict[str, Any]) -> str:
        over = float(market["over"])
        under = float(market.get("under", 1.0 - over))
        lean_over = over >= under
        over_w = max(0.0, min(100.0, over * 100))
        under_w = max(0.0, min(100.0, under * 100))
        lean = "Over" if lean_over else "Under"
        confidence = over if lean_over else under
        return (
            f'<article class="ou-card{" is-over" if lean_over else " is-under"}" '
            f'aria-label="Goals {html.escape(line)} {html.escape(lean)} '
            f'{confidence:.0%}">'
            f'<div class="ou-top">'
            f'<span class="ou-label">Goals {html.escape(line)}</span>'
            f'<span class="ou-chip">{html.escape(lean)} · {confidence:.0%}</span>'
            f"</div>"
            f'<div class="ou-sides">'
            f'<div class="ou-side{" active" if lean_over else ""}">'
            f'<span class="ou-side-label">Over</span>'
            f'<span class="ou-side-value mono">{over:.1%}</span>'
            f"</div>"
            f'<div class="ou-side{" active" if not lean_over else ""}">'
            f'<span class="ou-side-label">Under</span>'
            f'<span class="ou-side-value mono">{under:.1%}</span>'
            f"</div>"
            f"</div>"
            f'<span class="ou-split" aria-hidden="true">'
            f'<i class="over" style="width:{over_w:.1f}%"></i>'
            f'<i class="under" style="width:{under_w:.1f}%"></i>'
            f"</span>"
            "</article>"
        )

    return (
        '<div class="ou-grid" role="group" aria-label="Over under markets">'
        f"{card('2.5', over_25)}"
        f"{card('3.5', over_35)}"
        "</div>"
    )


def _score_list(cells: list[dict[str, Any]]) -> str:
    ranked = sorted(cells, key=lambda cell: float(cell["probability"]), reverse=True)[:8]
    rows = [
        f"<tr><td>{int(cell['home_goals'])}–{int(cell['away_goals'])}</td>"
        f"<td>{float(cell['probability']):.1%}</td></tr>"
        for cell in ranked
    ]
    return (
        '<div class="table-wrap narrow"><table>'
        f"<tr><th>Score</th><th>Probability</th></tr>{''.join(rows)}</table></div>"
    )


def _badge(status: str) -> str:
    kind = {
        "correct": "good",
        "completed": "good",
        "incorrect": "bad",
        "failed": "bad",
        "excluded": "muted",
        "pending": "wait",
        "unscored": "wait",
        "withheld": "muted",
    }.get(status, "muted")
    label = {
        "completed": "Priced",
        "excluded": "Excluded",
        "pending": "Pending",
        "correct": "Correct",
        "incorrect": "Incorrect",
        "unscored": "Not scored",
        "withheld": "Withheld",
        "failed": "Failed",
    }.get(status, status.replace("_", " ").title())
    return f'<span class="badge {kind}">{html.escape(label)}</span>'


def standings(
    connection: sqlite3.Connection,
    query: dict[str, list[str]] | None = None,
    *,
    api_key: str | None = None,
    ninjas_api_key: str | None = None,
) -> str:
    """League table from SQLite. Fetches from the provider once if missing."""

    from laboratory.standings import (
        STANDINGS_COMPETITIONS,
        backfill_form_from_matches,
        ensure_standings,
        list_standings_rows,
    )

    filters = _filters(query, connection)
    competition = (filters.get("league") or "").strip().upper()
    if competition not in STANDINGS_COMPETITIONS:
        competition = STANDINGS_COMPETITIONS[0]
    filters["league"] = competition
    notice = ""
    can_fetch = (
        bool((ninjas_api_key or "").strip())
        if competition == "SPL"
        else bool((api_key or "").strip())
    )
    if can_fetch:
        try:
            result = ensure_standings(
                connection,
                api_key or "",
                competition,
                force=False,
                ninjas_api_key=ninjas_api_key,
            )
            if result["source"] == "api":
                source_name = (
                    "API Ninjas" if competition == "SPL" else "Football-Data.org"
                )
                if result["rows"]:
                    notice = _state(
                        kind="info",
                        title="Standings refreshed",
                        lead=(
                            f"Fetched {result['rows']} rows for {competition} "
                            f"from {source_name} and stored them."
                        ),
                    )
                else:
                    notice = _state(
                        kind="empty",
                        title="Standings refresh returned no rows",
                        lead=f"{source_name} responded without a TOTAL table for {competition}.",
                    )
        except Exception as exc:  # noqa: BLE001 — show error-state guidance
            notice = _state(
                kind="error",
                title="Could not refresh standings",
                lead=str(exc),
            )

    from laboratory.store import (
        competition_emblem_map,
        crest_url_for_team,
        emblem_url_for_competition,
        upsert_competition,
    )

    for code in STANDINGS_COMPETITIONS:
        upsert_competition(
            connection,
            code=code,
            name=COMPETITION_NAMES.get(code, code),
        )
    connection.commit()
    emblems = competition_emblem_map(connection, STANDINGS_COMPETITIONS)
    meta, rows = list_standings_rows(connection, competition)
    if meta is not None and rows and any(
        not row["form"] or not str(row["form"]).strip() for row in rows
    ):
        backfill_form_from_matches(connection, competition, str(meta["season"]))
        meta, rows = list_standings_rows(connection, competition)
    pills = "".join(
        f'<a class="pill{" active" if code == competition else ""}" '
        f'href="/standings?league={html.escape(code)}">'
        f'{_logo_img(emblems.get(code) or emblem_url_for_competition(code), COMPETITION_NAMES.get(code, code), kind="emblem")}'
        f"{html.escape(COMPETITION_NAMES.get(code, code))}</a>"
        for code in STANDINGS_COMPETITIONS
    )
    selector = f'<nav class="league-pills" aria-label="Leagues">{pills}</nav>'
    if not rows:
        if api_key:
            empty = _state(
                kind="empty",
                title="No standings stored for this league yet",
                lead=(
                    "A refresh was attempted with FOOTBALL_API_KEY. "
                    "If this persists, check API access or import standings manually."
                ),
            )
        else:
            empty = _state(
                kind="empty",
                title="No standings stored for this league yet",
                lead=(
                    "Run python -m laboratory import-standings "
                    "or open this page with FOOTBALL_API_KEY set."
                ),
            )
        body = [
            _masthead("Standings", filters, active="standings"),
            selector,
            notice,
            empty,
        ]
        return _page("Standings", "\n".join(body), filters)

    fetched_raw = meta["fetched_at"] if meta else None
    fetched = _when(str(fetched_raw)) if fetched_raw else "—"
    matchday = meta["matchday"] if meta is not None else None
    season = html.escape(str(meta["season"] if meta else rows[0]["season"]))
    leader_crest = rows[0]["crest_url"] if rows else None
    if not leader_crest and rows:
        leader_crest = crest_url_for_team(rows[0]["team_id"])
    leader_name = (
        str(rows[0]["display_name"] or rows[0]["team_name"]) if rows else "—"
    )
    leader = f'{_logo_img(leader_crest, leader_name, size=32)}{html.escape(leader_name)}'
    league_mark = _logo_img(
        emblems.get(competition) or emblem_url_for_competition(competition),
        COMPETITION_NAMES.get(competition, competition),
        kind="emblem",
        size=20,
    )
    meta_cards = f"""
    <section class="cards standings-meta">
      <article><span>League</span><strong class="small">{league_mark}{html.escape(COMPETITION_NAMES.get(competition, competition))}</strong></article>
      <article><span>Season</span><strong class="mono">{season}</strong></article>
      <article><span>Matchday</span><strong class="mono">{int(matchday) if isinstance(matchday, int) else "—"}</strong></article>
      <article><span>Leader</span><strong class="small">{leader}</strong></article>
      <article><span>Stored</span><strong class="small">{fetched}</strong></article>
    </section>
    """
    show_form = any(_form_tokens(row["form"]) for row in rows)
    table_rows = []
    total = len(rows)
    for row in rows:
        position = int(row["position"])
        zone = _standings_zone(position, total, competition)
        gd = row["goal_difference"]
        gd_text = "—" if gd is None else (f"+{gd}" if int(gd) > 0 else str(gd))
        gd_class = ""
        if gd is not None and int(gd) > 0:
            gd_class = " pos"
        elif gd is not None and int(gd) < 0:
            gd_class = " neg"
        crest = row["crest_url"] or crest_url_for_team(row["team_id"])
        club = str(row["display_name"] or row["team_name"])
        form_cell = (
            f'<td class="form-cell">{_form_pills(row["form"])}</td>' if show_form else ""
        )
        table_rows.append(
            f'<tr class="{zone}">'
            f'<td class="mono pos-cell"><span class="pos-num">{position}</span></td>'
            f'<td class="club-cell">'
            f'{_logo_img(crest, club, size=32)}'
            f'<span>{html.escape(club)}</span></td>'
            f'<td class="mono num">{row["played"] if row["played"] is not None else "—"}</td>'
            f'<td class="mono num">{row["won"] if row["won"] is not None else "—"}</td>'
            f'<td class="mono num">{row["draw"] if row["draw"] is not None else "—"}</td>'
            f'<td class="mono num">{row["lost"] if row["lost"] is not None else "—"}</td>'
            f'<td class="mono num">{row["goals_for"] if row["goals_for"] is not None else "—"}</td>'
            f'<td class="mono num">{row["goals_against"] if row["goals_against"] is not None else "—"}</td>'
            f'<td class="mono num{gd_class}">{html.escape(gd_text)}</td>'
            f'<td class="mono num pts"><strong>{row["points"] if row["points"] is not None else "—"}</strong></td>'
            f"{form_cell}"
            "</tr>"
        )
    form_head = '<th class="form-cell">Form</th>' if show_form else ""
    head = (
        '<tr><th class="num">#</th><th>Club</th>'
        '<th class="num">P</th><th class="num">W</th><th class="num">D</th>'
        '<th class="num">L</th><th class="num">GF</th><th class="num">GA</th>'
        '<th class="num">GD</th><th class="num pts">Pts</th>'
        f"{form_head}</tr>"
    )
    body = [
        _masthead("Standings", filters, active="standings"),
        selector,
        notice,
        meta_cards,
        (
            '<div class="table-wrap standings-wrap">'
            f'<table class="standings">{head}{"".join(table_rows)}</table></div>'
        ),
        (
            '<p class="legend note">'
            + (
                '<span class="swatch europe"></span> Round of 16 (Top 8) · '
                '<span class="swatch playoff"></span> Playoff phase (9–24) · '
                '<span class="swatch drop"></span> Eliminated (25–36)'
                if competition == "CL"
                else (
                    '<span class="swatch europe"></span> European places · '
                    '<span class="swatch drop"></span> Relegation zone'
                )
            )
            + "</p>"
        ),
    ]
    return _page("Standings", "\n".join(body), filters)


def _standings_zone(position: int, total: int, competition: str = "") -> str:
    if competition == "CL":
        # UEFA Champions League 36-team league phase:
        # 1-8: Direct Round of 16 qualification (zone-europe)
        # 9-24: Playoff round qualification (zone-playoff)
        # 25-36: Eliminated (zone-drop)
        if position <= 8:
            return "zone-europe"
        if position <= 24:
            return "zone-playoff"
        return "zone-drop"
    if position <= 4:
        return "zone-europe"
    if total >= 18 and position > total - 3:
        return "zone-drop"
    return ""


def _logo_img(
    url: str | None,
    label: str,
    *,
    kind: str = "crest",
    size: int | None = None,
) -> str:
    if not url:
        return ""
    css = "crest" if kind == "crest" else "emblem"
    dim = size if size is not None else (22 if kind == "emblem" else 32)
    return (
        f'<img class="{css}" src="{html.escape(str(url))}" alt="" '
        f'width="{dim}" height="{dim}" loading="lazy" decoding="async" '
        f'title="{html.escape(str(label))}">'
    )


def _form_tokens(form: str | None) -> list[str]:
    if not form:
        return []
    marks = []
    for token in str(form).replace(" ", "").split(","):
        mark = token.strip().upper()[:1]
        if mark in {"W", "D", "L"}:
            marks.append(mark)
    return marks


def _form_pills(form: str | None) -> str:
    marks = _form_tokens(form)
    if not marks:
        return ""
    kinds = {"W": "win", "D": "draw", "L": "loss"}
    parts = [
        f'<span class="form-pill {kinds[mark]}" title="{mark}">{mark}</span>'
        for mark in marks
    ]
    return f'<span class="form-row">{"".join(parts)}</span>'


def _state(
    *,
    kind: str,
    title: str,
    lead: str = "",
    action_href: str = "",
    action_label: str = "",
) -> str:
    """Shared empty / error / info / loading callout used across dashboard pages."""

    role = "alert" if kind == "error" else "status"
    lead_html = (
        f'<p class="note state-lead">{html.escape(lead)}</p>' if lead else ""
    )
    action_html = ""
    if action_href and action_label:
        action_html = (
            f'<p class="state-action">'
            f'<a class="state-link" href="{html.escape(action_href)}">'
            f"{html.escape(action_label)}</a></p>"
        )
    spinner = ""
    if kind == "loading":
        spinner = '<span class="state-spinner" aria-hidden="true"></span>'
    return (
        f'<div class="state state-{html.escape(kind)}" role="{role}">'
        f"{spinner}"
        f"<strong>{html.escape(title)}</strong>"
        f"{lead_html}{action_html}"
        f"</div>"
    )


def _confidence_threshold(value: str) -> float | None:
    """Parse FT confidence filter (`45` / `55` / `65`) into a probability floor."""

    text = (value or "all").strip().casefold()
    if text in {"", "all", "any"}:
        return None
    try:
        percent = float(text)
    except ValueError:
        return None
    if percent <= 0:
        return None
    return percent / 100.0 if percent > 1 else percent


def _default_filters() -> dict[str, str]:
    return {
        "engine": "score_model",
        "experiment": BOT_EXPERIMENT_ID,
        "league": "",
        "season": "",
        "team": "",
        "date_from": "",
        "date_to": "",
        "fixture": "all",
        "status": "all",
        "verdict": "all",
        "confidence": "all",
        "order": "newest",
        "page": "1",
    }


def not_found_page(*, resource: str = "Page") -> str:
    return _page(
        f"{resource} not found",
        "\n".join(
            [
                _masthead(f"{resource} not found", _default_filters(), active=""),
                _state(
                    kind="empty",
                    title=f"{resource} not found",
                    lead="The link may be outdated, or this item is not in the laboratory database.",
                    action_href="/matches",
                    action_label="Browse matches",
                ),
            ]
        ),
    )


def server_error_page(*, detail: str = "") -> str:
    lead = detail.strip() or "An unexpected error occurred while rendering this page."
    return _page(
        "Something went wrong",
        "\n".join(
            [
                _masthead("Something went wrong", _default_filters(), active=""),
                _state(
                    kind="error",
                    title="Something went wrong",
                    lead=lead,
                    action_href="/",
                    action_label="Back to Overview",
                ),
            ]
        ),
    )


def database_busy_page(*, retry_href: str = "/experiments") -> str:
    """Soft wait page when SQLite returns database is locked (lab-run writers)."""

    return _page(
        "Database busy",
        "\n".join(
            [
                _masthead("Database busy", _default_filters(), active="experiments"),
                _state(
                    kind="loading",
                    title="Database is busy",
                    lead=(
                        "A lab experiment is writing to SQLite (WAL). "
                        "This page retries automatically — or open Experiments after a moment. "
                        "If it persists: make dash-restart, then retry with a smaller grid."
                    ),
                    action_href=retry_href,
                    action_label="Retry now",
                ),
            ]
        ),
        extra_head='<meta http-equiv="refresh" content="3">',
    )


def login_page(
    *,
    csrf_token: str,
    next_path: str = "/",
    error: str | None = None,
) -> str:
    """Standalone sign-in page for the dashboard."""

    message = (
        f'<p class="login-error" role="alert">{html.escape(error)}</p>'
        if error
        else '<p class="login-note">Sign in to open the laboratory dashboard.</p>'
    )
    safe_next = html.escape(safe_next_path(next_path))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sign in · Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #0b1220;
      --bg-2: #111a2b;
      --panel: #152238;
      --line: #243552;
      --text: #e8eef8;
      --muted: #93a4bd;
      --accent: #34d399;
      --accent-2: #10b981;
      --bad: #fb7185;
      --focus: #6ee7b7;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font: 15px/1.45 "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
      background:
        radial-gradient(1200px 500px at 10% -10%, rgba(52, 211, 153, 0.14), transparent 55%),
        radial-gradient(900px 400px at 90% 0%, rgba(56, 189, 248, 0.08), transparent 50%),
        linear-gradient(180deg, var(--bg), var(--bg-2));
      display: grid;
      place-items: center;
      padding: 1.5rem;
    }}
    a:focus-visible, button:focus-visible, input:focus-visible {{
      outline: 2px solid var(--focus); outline-offset: 2px;
    }}
    .login-shell {{
      width: min(24rem, 100%);
      background: rgba(21, 34, 56, 0.94);
      border: 1px solid var(--line);
      border-radius: 1rem;
      padding: 1.6rem 1.45rem 1.45rem;
      backdrop-filter: blur(10px);
      box-shadow: 0 24px 60px rgba(0, 0, 0, 0.35);
    }}
    h1 {{
      margin: 0 0 0.35rem; font-size: 1.55rem; font-weight: 700; letter-spacing: -0.035em;
    }}
    .login-note, .login-error {{ margin: 0 0 1.1rem; color: var(--muted); }}
    .login-error {{ color: var(--bad); }}
    form {{ display: grid; gap: 0.85rem; }}
    label {{ display: grid; gap: 0.28rem; color: var(--muted); font-size: 0.8rem; font-weight: 500; }}
    input {{
      font: inherit; color: var(--text); background: var(--bg);
      border: 1px solid var(--line); border-radius: 0.45rem; padding: 0.55rem 0.65rem;
    }}
    button {{
      margin-top: 0.2rem;
      font: inherit; font-weight: 700; cursor: pointer;
      background: linear-gradient(180deg, var(--accent), var(--accent-2));
      color: #042f24; border: none; border-radius: 0.45rem; padding: 0.62rem 0.85rem;
    }}
    button:hover {{ filter: brightness(1.05); }}
  </style>
</head>
<body>
  <main class="login-shell">
    <h1>Sign in</h1>
    {message}
    <form method="post" action="/login" autocomplete="on">
      <input type="hidden" name="csrf" value="{html.escape(csrf_token)}">
      <input type="hidden" name="next" value="{safe_next}">
      <label>Username
        <input name="username" type="text" autocomplete="username" required autofocus>
      </label>
      <label>Password
        <input name="password" type="password" autocomplete="current-password" required>
      </label>
      <button type="submit">Continue</button>
    </form>
  </main>
</body>
</html>
"""


def safe_next_path(path: str) -> str:
    """Allow only same-origin relative paths after login."""

    candidate = (path or "/").strip() or "/"
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    if candidate.startswith("/login") or candidate.startswith("/logout"):
        return "/"
    return candidate


def _masthead(
    title: str,
    filters: dict[str, str],
    *,
    active: str = "overview",
    back_href: str | None = None,
) -> str:
    match_query = _query(filters, page="")
    match_href = "/matches" if not match_query else f"/matches?{match_query}"
    overview_href = "/" if not match_query else f"/?{match_query}"

    def link(href: str, label: str, key: str) -> str:
        cls = ' class="active"' if active == key else ""
        return f'<a href="{html.escape(href)}"{cls}>{label}</a>'

    logout = ""
    try:
        from laboratory.auth import load_dashboard_auth

        if load_dashboard_auth().enabled:
            logout = link("/logout", "Log out", "logout")
    except Exception:
        logout = ""

    back = ""
    if back_href:
        back = (
            f'<a class="title-back" href="{html.escape(back_href)}" '
            f'aria-label="Back to matches">'
            f'<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false">'
            f'<path d="M10 3L5 8l5 5" fill="none" stroke="currentColor" '
            f'stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"/>'
            f"</svg></a>"
        )

    return f"""
    <header class="bar">
      <nav>
        {link(overview_href, "Overview", "overview")}
        {link(match_href, "Matches", "matches")}
        {link("/standings", "Standings", "standings")}
        {link("/experiments", "Experiments", "experiments")}
        {logout}
      </nav>
      <div class="title-row">{back}<h1>{html.escape(title)}</h1></div>
    </header>
    """


def _page(title: str, body: str, filters: dict[str, str] | None = None, *, extra_head: str = "") -> str:
    _ = filters
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  {extra_head}
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #0b1220;
      --bg-2: #111a2b;
      --panel: #152238;
      --line: #243552;
      --text: #e8eef8;
      --muted: #93a4bd;
      --accent: #34d399;
      --accent-2: #10b981;
      --good: #34d399;
      --bad: #fb7185;
      --wait: #fbbf24;
      --focus: #6ee7b7;
      --space-1: 4px;
      --space-2: 8px;
      --space-3: 12px;
      --space-4: 16px;
      --space-5: 24px;
      --space-6: 32px;
      --radius-sm: 8px;
      --radius-md: 12px;
      --radius-lg: 16px;
      --ease: cubic-bezier(0.2, 0, 0, 1);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font: 16px/1.5 "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
      background:
        radial-gradient(1200px 500px at 10% -10%, rgba(52, 211, 153, 0.14), transparent 55%),
        radial-gradient(900px 400px at 90% 0%, rgba(56, 189, 248, 0.08), transparent 50%),
        linear-gradient(180deg, var(--bg), var(--bg-2));
    }}
    a {{ color: var(--accent); text-decoration: none; cursor: pointer; }}
    a:hover {{ text-decoration: underline; text-underline-offset: 0.16em; }}
    a:focus-visible, button:focus-visible, select:focus-visible, input:focus-visible {{
      outline: 2px solid var(--focus); outline-offset: 2px;
    }}
    .bar, main {{ width: min(78rem, calc(100% - 32px)); margin: 0 auto; }}
    main {{ padding-bottom: 48px; }}
    .bar {{
      display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px 24px;
      padding: 24px 0 8px;
    }}
    .bar > nav {{ display: flex; flex-wrap: wrap; gap: 8px 16px; }}
    .bar > nav a {{
      color: var(--muted); font-weight: 500; padding: 8px 4px;
      min-height: 44px; display: inline-flex; align-items: center;
      transition: color 160ms var(--ease);
    }}
    .bar > nav a:hover {{ color: var(--text); text-decoration: none; }}
    .bar > nav a.active {{
      color: var(--text); box-shadow: inset 0 -2px 0 var(--accent);
    }}
    .title-row {{
      width: 100%; margin: 8px 0 0;
      display: flex; align-items: center; gap: 12px; min-width: 0;
    }}
    .title-back {{
      flex-shrink: 0; width: 40px; height: 40px;
      display: inline-grid; place-items: center;
      border-radius: 999px; color: var(--text);
      background: rgba(148, 163, 184, 0.14);
      border: 1px solid var(--line); text-decoration: none;
      transition: color 160ms var(--ease), background 160ms var(--ease),
        border-color 160ms var(--ease), transform 160ms var(--ease);
    }}
    .title-back:hover {{
      color: var(--text); background: rgba(52, 211, 153, 0.16);
      border-color: rgba(52, 211, 153, 0.35); text-decoration: none;
    }}
    .title-back:active {{ transform: scale(0.96); }}
    .title-back svg {{ display: block; }}
    h1 {{
      margin: 0; min-width: 0; flex: 1;
      font-size: clamp(1.5rem, 2.4vw, 2rem);
      font-weight: 700; letter-spacing: -0.035em; line-height: 1.2;
      text-wrap: balance;
    }}
    h2 {{ margin: 24px 0 12px; font-size: 1.05rem; }}
    h3 {{ margin: 16px 0 8px; font-size: 0.95rem; color: var(--muted); }}
    .note {{ color: var(--muted); margin: 0; }}
    .toolbar {{ margin: 8px 0 12px; }}
    .filters {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
      gap: 12px;
      align-items: end;
      background: rgba(21, 34, 56, 0.92); border: 1px solid var(--line);
      border-radius: var(--radius-lg); padding: 16px; margin: 16px 0 8px;
      backdrop-filter: blur(8px);
    }}
    .filters > label {{
      display: grid; gap: 4px; min-width: 0;
      color: var(--muted); font-size: 0.78rem; font-weight: 500;
    }}
    label {{ display: grid; gap: 4px; color: var(--muted); font-size: 0.78rem; font-weight: 500; }}
    select, input, button {{
      font: inherit; color: var(--text); background: var(--bg);
      border: 1px solid var(--line); border-radius: var(--radius-sm); padding: 8px 12px;
      min-height: 44px;
    }}
    .filters select,
    .filters input[type="date"] {{
      width: 100%; min-width: 0; color-scheme: dark;
    }}
    .filters select {{
      appearance: none;
      padding: 8px 40px 8px 12px;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 16 16' fill='none'%3E%3Cpath d='M4 6l4 4 4-4' stroke='%23b7c6dc' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
      background-repeat: no-repeat;
      background-position: right 12px center;
      background-size: 16px 16px;
    }}
    .filters input[type="date"] {{
      padding: 8px 12px;
    }}
    .filters input[type="date"]::-webkit-calendar-picker-indicator {{
      cursor: pointer;
      opacity: 1;
      width: 20px;
      height: 20px;
      margin-inline-end: 0;
      padding: 0;
      border-radius: 4px;
      background-color: transparent;
      /* Force pure white regardless of native/dark-scheme icon color. */
      filter: brightness(0) invert(1);
      transform: scale(1.25);
      transform-origin: center;
    }}
    select:disabled {{
      opacity: 0.72; cursor: not-allowed; color: var(--muted);
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 16 16' fill='none'%3E%3Cpath d='M4 6l4 4 4-4' stroke='%2393a4bd' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
    }}
    button {{
      background: linear-gradient(180deg, var(--accent), var(--accent-2));
      color: #042f24; border: none; font-weight: 700; cursor: pointer;
      transition: transform 160ms var(--ease), filter 160ms var(--ease),
        box-shadow 160ms var(--ease);
    }}
    button:hover {{ filter: brightness(1.05); }}
    button:active {{ transform: scale(0.96); }}
    .filter-actions {{
      display: flex; align-items: center; gap: 12px;
      align-self: end; min-height: 44px;
    }}
    .filters .filter-actions button {{
      box-sizing: border-box;
      height: 44px; min-height: 44px; padding: 0 16px;
      border-radius: var(--radius-sm);
      font-size: 0.9rem; letter-spacing: -0.01em; line-height: 1;
      box-shadow: 0 4px 12px rgba(16, 185, 129, 0.2);
    }}
    .filter-span-2 {{ grid-column: span 1; }}
    .filters a.clear {{
      box-sizing: border-box;
      display: inline-flex; align-items: center; justify-content: center;
      height: 44px; min-height: 44px; padding: 0 16px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--line); background: var(--bg);
      color: #d7e2f2; font-weight: 600; font-size: 0.9rem; line-height: 1;
      text-decoration: none; white-space: nowrap; cursor: pointer;
      transition: color 160ms var(--ease), background 160ms var(--ease),
        border-color 160ms var(--ease), transform 160ms var(--ease);
    }}
    .filters a.clear:hover {{
      color: var(--text); background: rgba(148, 163, 184, 0.14);
      border-color: #35506f; text-decoration: none;
    }}
    .filters a.clear:active {{ transform: scale(0.96); }}
    .cards {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(9.5rem, 1fr));
      gap: 12px; margin: 16px 0 8px;
    }}
    .cards.compact {{ grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); }}
    .cards article {{
      background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-md);
      padding: 12px 16px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
    }}
    .cards span {{ display: block; color: var(--muted); font-size: 0.78rem; margin-bottom: 4px; }}
    .cards strong {{ font-size: 1.2rem; font-weight: 700; letter-spacing: -0.02em; }}
    .cards .small {{ font-size: 0.85rem; font-weight: 600; word-break: break-word; }}
    .jobs-panel {{ margin: 12px 0 16px; }}
    .job-cards {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
      gap: 10px;
    }}
    .job-cards article {{
      background: var(--bg); border: 1px solid var(--line); border-radius: var(--radius-sm);
      padding: 12px 14px;
    }}
    .job-cards span {{ display: block; color: var(--muted); font-size: 0.78rem; margin-bottom: 4px; }}
    .job-cards strong {{ font-size: 1.05rem; font-weight: 700; text-transform: uppercase; }}
    .job-cards .job-ok {{ color: var(--accent); }}
    .job-cards .job-failed {{ color: #f87171; }}
    .job-cards p {{ margin: 6px 0 0; color: var(--muted); font-size: 0.85rem; line-height: 1.35; }}
    .jobs-header {{
      display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
      gap: 12px 16px; margin-bottom: 16px;
    }}
    .jobs-title-wrap h2 {{ margin: 0; font-size: 1.05rem; }}
    .jobs-title-wrap p.note {{ margin: 4px 0 0; font-size: 0.85rem; }}
    .sync-form {{ margin: 0; display: inline-flex; }}
    .sync-btn {{
      display: inline-flex; align-items: center; justify-content: center;
      min-height: 44px; padding: 0 16px; border: 0; border-radius: var(--radius-sm);
      background: linear-gradient(180deg, var(--accent), var(--accent-2));
      color: #042f24; font: inherit; font-size: 0.9rem; font-weight: 700;
      letter-spacing: -0.01em; cursor: pointer;
      box-shadow: 0 4px 12px rgba(16, 185, 129, 0.2);
      transition: transform 160ms var(--ease), filter 160ms var(--ease);
    }}
    .sync-btn:hover {{ filter: brightness(1.05); }}
    .sync-btn:active {{ transform: scale(0.96); }}
    .sync-btn:disabled {{
      opacity: 0.65; cursor: not-allowed; filter: none; transform: none; box-shadow: none;
    }}
    .sync-breakdown {{ margin: 16px 0; overflow-x: auto; }}
    .sync-table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
    .sync-table th, .sync-table td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--line); }}
    .sync-table th {{
      color: var(--muted); font-size: 0.75rem; text-transform: uppercase;
      font-weight: 600; letter-spacing: 0.04em;
    }}
    .sync-table tr:hover td {{ background: rgba(255, 255, 255, 0.02); }}
    .predict-panel {{ margin: 16px 0; }}
    .predict-form {{
      display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-top: 12px;
    }}
    .predict-form button {{
      display: inline-flex; align-items: center; justify-content: center;
      min-height: 44px; padding: 0 16px; border: 0; border-radius: var(--radius-sm);
      background: linear-gradient(180deg, var(--accent), var(--accent-2));
      color: #042f24; font: inherit; font-weight: 700; cursor: pointer;
      transition: transform 160ms var(--ease), filter 160ms var(--ease);
    }}
    .predict-form button:hover {{ filter: brightness(1.05); }}
    .predict-form button:active {{ transform: scale(0.96); }}
    .bulk-predict-form {{ display: inline-flex; align-items: center; }}
    .btn-bulk-predict {{
      display: inline-flex; align-items: center; justify-content: center;
      min-height: 44px; padding: 0 16px; border: 1px solid var(--accent);
      border-radius: var(--radius-sm); background: rgba(52, 211, 153, 0.14);
      color: var(--accent); font: inherit; font-weight: 700; cursor: pointer;
      transition: transform 160ms var(--ease), background 160ms var(--ease);
    }}
    .btn-bulk-predict:hover {{ background: rgba(52, 211, 153, 0.24); }}
    .btn-bulk-predict:active {{ transform: scale(0.96); }}
    dialog.confirm-dialog {{
      margin: auto; padding: 0; border: 1px solid var(--line);
      border-radius: var(--radius-lg); background: var(--panel); color: var(--text);
      max-width: min(440px, calc(100vw - 32px)); box-shadow: 0 24px 64px rgba(0, 0, 0, 0.55);
    }}
    dialog.confirm-dialog::backdrop {{
      background: rgba(11, 18, 32, 0.72); backdrop-filter: blur(4px);
    }}
    .confirm-dialog-card {{ padding: 24px; display: grid; gap: 12px; }}
    .confirm-dialog-card h2 {{ margin: 0; font-size: 1.15rem; }}
    .confirm-dialog-card .note {{ margin: 0; line-height: 1.5; }}
    .confirm-dialog-actions {{
      display: flex; justify-content: flex-end; gap: 12px; margin-top: 8px; flex-wrap: wrap;
    }}
    .btn-dialog-cancel, .btn-dialog-confirm {{
      display: inline-flex; align-items: center; justify-content: center;
      min-height: 44px; padding: 0 16px; border-radius: var(--radius-sm);
      font: inherit; font-weight: 600; cursor: pointer;
      transition: transform 160ms var(--ease), background 160ms var(--ease), filter 160ms var(--ease);
    }}
    .btn-dialog-cancel {{
      border: 1px solid var(--line); background: transparent; color: var(--muted);
    }}
    .btn-dialog-cancel:hover {{ background: rgba(255, 255, 255, 0.05); color: var(--text); }}
    .btn-dialog-confirm {{
      border: 0; background: linear-gradient(180deg, var(--accent), var(--accent-2));
      color: #042f24; font-weight: 700;
    }}
    .btn-dialog-confirm:hover {{ filter: brightness(1.05); }}
    .btn-dialog-cancel:active, .btn-dialog-confirm:active {{ transform: scale(0.96); }}
    .charts-panel {{ margin: 32px 0 24px; }}
    .chart-grid {{
      display: grid; grid-template-columns: 1fr;
      gap: 16px; margin-top: 16px;
    }}
    .chart {{
      margin: 0; padding: 16px 24px 24px;
      background: var(--bg); border: 1px solid var(--line); border-radius: var(--radius-sm);
      min-width: 0;
    }}
    .chart figcaption {{
      font-size: 0.92rem; font-weight: 700; letter-spacing: -0.02em; margin: 0 0 16px;
    }}
    .hbar-chart {{ display: grid; gap: 12px; }}
    .hbar-row {{
      display: grid; grid-template-columns: minmax(5.5rem, 7.5rem) 1fr minmax(4.5rem, 5.5rem);
      gap: 12px; align-items: center;
    }}
    .hbar-label {{ color: #b7c6dc; font-size: 0.78rem; line-height: 1.25; }}
    .hbar-track {{
      display: block; height: 10px; background: #0b1220; border-radius: 999px; overflow: hidden;
    }}
    .hbar-track i {{
      display: block; height: 100%;
      background: linear-gradient(90deg, #059669, var(--accent));
      border-radius: 999px;
      transform-origin: left center;
      animation: chart-grow-x 520ms var(--ease) both;
    }}
    .hbar-value {{
      color: var(--muted); font-size: 0.78rem; text-align: right;
      font-variant-numeric: tabular-nums;
    }}
    .vbar-chart {{
      display: flex; align-items: flex-end; gap: 12px; min-height: 140px; padding-top: 4px;
    }}
    .vbar {{
      flex: 1 1 0; min-width: 0; display: grid; justify-items: center;
      grid-template-rows: auto 1fr auto; gap: 8px; height: 140px;
    }}
    .vbar-value {{
      color: var(--muted); font-size: 0.7rem; font-variant-numeric: tabular-nums; line-height: 1;
    }}
    .vbar-track {{
      width: 100%; max-width: 28px; height: 100%; align-self: stretch;
      display: flex; align-items: flex-end; background: #0b1220; border-radius: 6px 6px 2px 2px;
      overflow: hidden;
    }}
    .vbar-track i {{
      display: block; width: 100%;
      background: linear-gradient(180deg, var(--accent), #059669);
      border-radius: 6px 6px 2px 2px;
      transform-origin: bottom center;
      animation: chart-grow-y 560ms var(--ease) both;
    }}
    .vbar-label {{
      color: #8fa3bd; font-size: 0.68rem; line-height: 1; white-space: nowrap;
    }}
    .cal-chart {{ width: 100%; height: auto; display: block; }}
    .chart-axis {{ fill: #8fa3bd; font-size: 9px; font-family: inherit; }}
    .chart-legend {{ margin: 12px 0 0; font-size: 0.78rem; }}
    @keyframes chart-grow-x {{
      from {{ transform: scaleX(0.15); opacity: 0.35; }}
      to {{ transform: scaleX(1); opacity: 1; }}
    }}
    @keyframes chart-grow-y {{
      from {{ transform: scaleY(0.15); opacity: 0.35; }}
      to {{ transform: scaleY(1); opacity: 1; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      .hbar-track i, .vbar-track i {{ animation: none; }}
    }}
    .table-wrap {{
      overflow-x: auto; border: 1px solid var(--line); border-radius: var(--radius-lg);
      background: var(--panel); margin: 8px 0 16px;
      box-shadow: 0 12px 32px rgba(0, 0, 0, 0.2);
    }}
    .table-wrap.narrow {{ max-width: 22rem; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 12px; border-bottom: 1px solid var(--line); }}
    th {{
      color: var(--muted); font-size: 0.78rem; font-weight: 600;
      position: sticky; top: 0; background: #17263c; z-index: 1;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    tr:hover td {{ background: rgba(52, 211, 153, 0.04); }}
    .mono {{
      font-family: "IBM Plex Mono", ui-monospace, monospace; font-weight: 500;
      font-variant-numeric: tabular-nums;
    }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .num.pos {{ color: var(--good); }}
    .num.neg {{ color: var(--bad); }}
    .pts strong {{
      font-size: 18px; font-weight: 700; letter-spacing: -0.02em; line-height: 1;
      color: var(--text);
    }}
    .badge {{
      display: inline-flex; align-items: center; border-radius: 999px;
      padding: 4px 8px; font-size: 0.75rem; font-weight: 600; min-height: 28px;
    }}
    .good {{ background: rgba(52, 211, 153, 0.15); color: var(--good); }}
    .bad {{ background: rgba(251, 113, 133, 0.15); color: var(--bad); }}
    .wait {{ background: rgba(251, 191, 36, 0.14); color: var(--wait); }}
    .muted {{ background: rgba(147, 164, 189, 0.14); color: var(--muted); }}
    .pager {{
      display: flex; gap: 16px; align-items: center; margin: 8px 0 32px; color: var(--muted);
    }}
    .pager a {{
      display: inline-flex; align-items: center; min-height: 44px; padding: 0 4px;
      font-weight: 600;
    }}
    .scoreboard {{
      display: grid; gap: 16px; margin: 16px 0; padding: 16px;
      border: 1px solid var(--line); border-radius: var(--radius-lg); background: var(--panel);
    }}
    .scoreboard-teams {{
      display: grid; gap: 12px;
    }}
    .club {{
      margin: 0; font-size: 1.15rem; font-weight: 650; line-height: 1.2;
      display: grid; grid-template-columns: auto minmax(0, 1fr) auto;
      gap: 12px; align-items: center; min-width: 0;
    }}
    .scoreboard .club-text {{
      min-width: 0; overflow-wrap: anywhere; text-wrap: balance;
    }}
    .club-score {{
      font-size: clamp(1.5rem, 3vw, 1.85rem); font-weight: 700;
      letter-spacing: -0.03em; line-height: 1; color: var(--accent);
      font-variant-numeric: tabular-nums; text-align: end; min-width: 1.5ch;
    }}
    .scoreboard .crest {{
      width: 48px; height: 48px; display: block;
    }}
    .scoreboard-meta {{
      display: inline-flex; flex-wrap: wrap; align-items: center; gap: 8px;
      margin: 0; line-height: 1.3;
    }}
    .scoreboard-meta .emblem {{
      display: block;
    }}
    .crest, .emblem {{
      display: inline-block; flex-shrink: 0; object-fit: contain;
      vertical-align: middle; border-radius: 4px;
      outline: 1px solid oklch(1 0 0 / 0.1); outline-offset: -1px;
      background: rgba(255, 255, 255, 0.04);
    }}
    /* League emblems are often dark purple/black wordmarks — lift them on a light plate. */
    .emblem {{
      background: #f8fafc;
      padding: 4px;
      box-sizing: content-box;
      border-radius: 6px;
    }}
    .league-chip {{ display: inline-flex; align-items: center; gap: 8px; }}
    .cards strong.small {{
      display: inline-flex; align-items: center; gap: 8px; flex-wrap: wrap;
    }}
    .match-id-value {{
      display: inline-flex; align-items: center; gap: 8px; min-width: 0;
    }}
    .match-id-text {{
      min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }}
    .copy-btn {{
      flex-shrink: 0;
      display: inline-flex; align-items: center; justify-content: center;
      width: 32px; height: 32px; min-height: 32px; padding: 0;
      border-radius: var(--radius-sm);
      border: 1px solid var(--line);
      background: rgba(11, 18, 32, 0.72);
      color: var(--muted);
      box-shadow: none;
      cursor: pointer;
      transition: color 160ms var(--ease), background 160ms var(--ease),
        border-color 160ms var(--ease), transform 160ms var(--ease);
    }}
    .copy-btn:hover {{
      color: var(--text); background: rgba(148, 163, 184, 0.14);
      border-color: #35506f; filter: none;
    }}
    .copy-btn:active {{ transform: scale(0.96); filter: none; }}
    .copy-btn:focus-visible {{
      outline: 2px solid var(--focus); outline-offset: 2px;
    }}
    .copy-btn.is-copied {{
      color: var(--accent); border-color: rgba(52, 211, 153, 0.42);
      background: rgba(52, 211, 153, 0.16);
    }}
    .copy-btn .copied-icon[hidden],
    .copy-btn.is-copied .copy-icon {{ display: none; }}
    .copy-btn.is-copied .copied-icon {{ display: block; }}
    .panel {{
      margin: 16px 0; padding: 16px; border: 1px solid var(--line);
      border-radius: var(--radius-lg); background: rgba(21, 34, 56, 0.88);
    }}
    .mls {{
      display: grid; gap: 16px; margin: 16px 0 12px; padding: 16px;
      border-radius: calc(var(--radius-sm) + 16px);
      border: 1px solid var(--line);
      background: rgba(11, 18, 32, 0.72);
      box-shadow: 0 8px 20px rgba(0, 0, 0, 0.18);
    }}
    .mls-top {{
      display: flex; align-items: center; justify-content: space-between; gap: 8px;
    }}
    .mls-label {{
      color: #b7c6dc; font-size: 0.72rem; font-weight: 600;
      text-transform: uppercase; letter-spacing: 0.06em; line-height: 1;
    }}
    .mls-chip {{
      flex-shrink: 0; padding: 4px 8px; border-radius: 999px;
      font-size: 0.78rem; font-weight: 700; letter-spacing: -0.02em; line-height: 1;
      color: var(--accent); background: rgba(52, 211, 153, 0.16);
      border: 1px solid rgba(52, 211, 153, 0.42);
      font-variant-numeric: tabular-nums;
    }}
    .mls-scoreline {{
      display: grid; grid-template-columns: 1fr auto 1fr; gap: 12px; align-items: center;
      min-width: 0;
    }}
    .mls-team {{
      display: grid; justify-items: center; gap: 8px; min-width: 0;
    }}
    .mls-goal {{
      display: grid; place-items: center; width: 100%; min-height: 64px;
      padding: 12px 8px; border-radius: var(--radius-sm);
      background: rgba(52, 211, 153, 0.1);
      border: 1px solid rgba(52, 211, 153, 0.28);
      font-size: clamp(1.75rem, 4vw, 2.25rem); font-weight: 700;
      letter-spacing: -0.04em; line-height: 1;
      color: var(--text); font-variant-numeric: tabular-nums;
    }}
    .mls-team-label {{
      color: #b7c6dc; font-size: 0.72rem; font-weight: 600;
      text-transform: uppercase; letter-spacing: 0.05em; line-height: 1;
    }}
    .mls-sep {{
      color: #d7e2f2; font-size: 1.5rem; font-weight: 600; line-height: 1;
      padding-bottom: 22px;
    }}
    .ou-grid {{
      display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px; margin: 16px 0 12px;
    }}
    .ou-card {{
      display: grid; gap: 12px; padding: 16px;
      border-radius: calc(var(--radius-sm) + 16px); border: 1px solid var(--line);
      background: rgba(11, 18, 32, 0.72);
      box-shadow: 0 8px 20px rgba(0, 0, 0, 0.18);
    }}
    .ou-top {{
      display: flex; align-items: center; justify-content: space-between; gap: 8px;
    }}
    .ou-label {{
      color: #b7c6dc; font-size: 0.72rem; font-weight: 600;
      text-transform: uppercase; letter-spacing: 0.06em; line-height: 1;
    }}
    .ou-chip {{
      flex-shrink: 0; padding: 4px 8px; border-radius: 999px;
      font-size: 0.68rem; font-weight: 700; letter-spacing: 0.04em;
      text-transform: uppercase; line-height: 1;
      color: #d7e2f2; background: rgba(148, 163, 184, 0.16);
      border: 1px solid rgba(148, 163, 184, 0.32);
    }}
    .ou-card.is-over .ou-chip {{
      color: var(--accent); background: rgba(52, 211, 153, 0.16);
      border-color: rgba(52, 211, 153, 0.42);
    }}
    .ou-sides {{
      display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
    }}
    .ou-side {{
      display: grid; gap: 4px; min-width: 0; padding: 8px 12px;
      border-radius: var(--radius-sm);
      background: rgba(11, 18, 32, 0.55);
      border: 1px solid transparent;
    }}
    .ou-side.active {{
      border-color: rgba(52, 211, 153, 0.28);
      background: rgba(52, 211, 153, 0.1);
    }}
    .ou-side-label {{
      color: #b7c6dc; font-size: 0.72rem; font-weight: 600;
      text-transform: uppercase; letter-spacing: 0.05em; line-height: 1;
    }}
    .ou-side.active .ou-side-label {{ color: #d7e2f2; }}
    .ou-side-value {{
      font-size: 1.35rem; font-weight: 700; letter-spacing: -0.03em; line-height: 1;
      color: #d7e2f2; font-variant-numeric: tabular-nums;
    }}
    .ou-side.active .ou-side-value {{ color: var(--text); }}
    .ou-split {{
      display: flex; height: 8px; border-radius: 999px; overflow: hidden;
      background: #0b1220;
    }}
    .ou-split i {{ display: block; height: 100%; }}
    .ou-split .over {{ background: linear-gradient(90deg, #059669, var(--accent)); }}
    .ou-split .under {{ background: rgba(148, 163, 184, 0.35); }}
    .prob {{
      display: grid; grid-template-columns: 4.2rem 1fr 3.6rem; gap: 12px;
      align-items: center; margin: 8px 0;
    }}
    .meter {{
      display: block; height: 8px; background: #0b1220; border-radius: 999px; overflow: hidden;
    }}
    .meter i {{ display: block; height: 100%; background: linear-gradient(90deg, #059669, var(--accent)); }}
    .model {{ color: var(--muted); font-size: 0.82rem; font-weight: 500; }}
    .notes {{ color: var(--muted); padding-left: 16px; }}
    .empty,
    .state {{
      background: var(--panel); border: 1px dashed var(--line);
      border-radius: var(--radius-lg); padding: 24px 16px; margin: 16px 0;
    }}
    .state strong {{ display: block; font-size: 1.05rem; letter-spacing: -0.02em; }}
    .state-lead {{ margin: 8px 0 0; }}
    .state-action {{ margin: 12px 0 0; }}
    .state-link {{
      display: inline-flex; align-items: center; justify-content: center;
      min-height: 44px; padding: 0 16px; border-radius: var(--radius-sm);
      border: 1px solid var(--line); background: var(--bg); color: var(--text);
      font-weight: 600; text-decoration: none;
      transition: color 160ms var(--ease), background 160ms var(--ease),
        border-color 160ms var(--ease);
    }}
    .state-link:hover {{
      background: rgba(148, 163, 184, 0.14); border-color: #35506f; text-decoration: none;
    }}
    .state-error {{
      border-style: solid; border-color: rgba(251, 113, 133, 0.45);
      background: rgba(251, 113, 133, 0.08);
    }}
    .state-info {{
      border-style: solid; border-color: rgba(56, 189, 248, 0.35);
      background: rgba(56, 189, 248, 0.06);
    }}
    .state-wait {{
      border-style: solid; border-color: rgba(251, 191, 36, 0.45);
      background: rgba(251, 191, 36, 0.08);
    }}
    .state-good {{
      border-style: solid; border-color: rgba(52, 211, 153, 0.45);
      background: rgba(52, 211, 153, 0.08);
    }}
    .state-loading {{
      display: flex; flex-wrap: wrap; align-items: center; gap: 12px;
      border-style: solid; border-color: rgba(52, 211, 153, 0.35);
      background: rgba(52, 211, 153, 0.06);
    }}
    .state-spinner {{
      width: 16px; height: 16px; border-radius: 999px;
      border: 2px solid rgba(52, 211, 153, 0.25);
      border-top-color: var(--accent);
      animation: state-spin 700ms linear infinite;
    }}
    @keyframes state-spin {{ to {{ transform: rotate(360deg); }} }}
    .empty-lead {{ margin: 8px 0 0; }}
    .empty-action {{ margin: 12px 0 0; }}
    .league-pills {{
      display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 16px 0 8px;
    }}
    .pill {{
      display: inline-flex; align-items: center; gap: 8px; min-height: 40px; padding: 8px 12px;
      border-radius: 999px; border: 1px solid var(--line); color: var(--muted);
      background: rgba(11, 18, 32, 0.55); font-weight: 600; font-size: 0.9rem; line-height: 1;
      white-space: nowrap;
      transition: color 160ms var(--ease), border-color 160ms var(--ease), background 160ms var(--ease);
    }}
    .pill:hover {{ color: var(--text); border-color: #35506f; text-decoration: none; }}
    .pill.active {{
      color: #042f24; background: linear-gradient(180deg, var(--accent), var(--accent-2));
      border-color: transparent;
    }}
    .pill .emblem {{
      width: 24px; height: 24px; padding: 4px; box-sizing: border-box;
      flex-shrink: 0; display: block; object-fit: contain;
    }}
    .match-list {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin: 8px 0 16px;
    }}
    .match-card {{
      display: grid; gap: 8px; padding: 8px 12px; border: 1px solid var(--line);
      border-radius: var(--radius-sm); background: var(--panel); color: inherit;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.14);
      transition: border-color 160ms var(--ease), transform 160ms var(--ease), background 160ms var(--ease);
    }}
    .match-card:hover {{
      border-color: #35506f; background: #182840; text-decoration: none; transform: translateY(-1px);
    }}
    .match-card:active {{ transform: scale(0.96); }}
    .match-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
      gap: 8px; align-items: center;
    }}
    .match-sub {{
      display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center;
      justify-content: space-between; color: var(--muted);
      font-size: 0.72rem; line-height: 1.3; padding-left: 0;
    }}
    .match-meta {{
      display: inline-flex; flex-wrap: wrap; gap: 8px 12px; align-items: center;
      min-width: 0;
    }}
    .match-card .badge {{
      min-height: 24px; padding: 4px 8px; font-size: 0.68rem; margin-left: auto;
      flex-shrink: 0;
    }}
    .club-name {{
      font-weight: 650; letter-spacing: -0.02em; font-size: 0.88rem; line-height: 1.2;
      display: inline-flex; align-items: center; gap: 8px; min-width: 0;
    }}
    .club-text {{
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0;
    }}
    .club-name.away {{ text-align: right; justify-content: flex-end; }}
    .score-pair {{
      display: inline-flex; gap: 4px; align-items: center; flex-shrink: 0;
    }}
    .score {{
      display: inline-flex; align-items: center; gap: 4px;
      padding: 4px 8px; border-radius: 4px; background: rgba(11, 18, 32, 0.7);
      border: 1px solid var(--line); white-space: nowrap;
    }}
    .score .mono {{ font-size: 0.85rem; }}
    .score.predicted .mono {{ color: #cbd5e1; }}
    .score.actual .mono {{ color: var(--text); font-weight: 700; }}
    .score-label {{
      color: var(--muted); font-size: 0.58rem; font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.04em; line-height: 1;
    }}
    .pick {{ color: var(--muted); font-size: inherit; font-weight: 500; }}
    .market-strip {{
      display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
    }}
    .market-chip {{
      display: inline-flex; align-items: center; gap: 4px;
      padding: 2px 6px; border-radius: 4px;
      border: 1px solid var(--line); background: rgba(11, 18, 32, 0.55);
      font-size: 0.68rem; line-height: 1.2; color: var(--muted);
    }}
    .market-chip-label {{
      font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em;
      color: var(--muted);
    }}
    .market-chip-pick {{ color: var(--text); font-weight: 650; }}
    .market-chip .ft-conf, .ft-conf {{
      margin-left: 2px; padding: 1px 5px; border-radius: 4px;
      background: rgba(56, 189, 248, 0.12); color: #7dd3fc;
      border: 1px solid rgba(56, 189, 248, 0.28); font-size: 0.66rem; font-weight: 700;
    }}
    .ft-conf.muted {{
      background: transparent; color: var(--muted); border-color: transparent;
    }}
    .market-conf-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 8px; margin: 0 0 14px;
    }}
    .ft-pick, .market-conf-card {{
      display: inline-flex; align-items: center; gap: 8px; flex-wrap: wrap;
      margin: 0; padding: 8px 12px; border-radius: var(--radius-sm);
      border: 1px solid var(--line); background: rgba(11, 18, 32, 0.55);
    }}
    .ft-pick-label {{
      color: var(--muted); font-size: 0.72rem; font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.04em;
    }}
    .ft-pick-value {{ font-size: 1rem; letter-spacing: -0.02em; }}
    .ft-pick .ft-conf, .market-conf-card .ft-conf {{
      padding: 2px 8px; border-radius: 4px;
      background: rgba(56, 189, 248, 0.14); color: #7dd3fc;
      border: 1px solid rgba(56, 189, 248, 0.3); font-size: 0.85rem; font-weight: 700;
    }}
    .match-sub .league-chip {{ gap: 8px; }}
    .match-sub .emblem {{ padding: 4px; border-radius: 4px; }}
    table.standings {{ table-layout: auto; }}
    table.standings th, table.standings td {{
      padding: 12px 8px; vertical-align: middle;
    }}
    table.standings th:first-child,
    table.standings td:first-child {{
      padding-left: 12px;
    }}
    table.standings th:last-child,
    table.standings td:last-child {{
      padding-right: 12px;
    }}
    table.standings .club-cell {{
      font-weight: 600;
      padding-right: 16px;
      white-space: nowrap;
    }}
    table.standings .club-cell .crest {{ margin-right: 8px; }}
    .pill .emblem {{ margin: 0; }}
    table.standings th.num,
    table.standings td.num {{
      text-align: right;
      width: 1%;
      white-space: nowrap;
      padding-left: 8px;
      padding-right: 8px;
      font-size: 16px;
    }}
    table.standings th.pts,
    table.standings td.pts {{
      padding-left: 12px;
      padding-right: 12px;
      font-size: 18px;
    }}
    table.standings td.pts strong {{
      font-size: 18px; font-weight: 700; letter-spacing: -0.02em; line-height: 1;
    }}
    table.standings .form-cell {{
      width: 1%;
      white-space: nowrap;
      padding-left: 12px;
    }}
    table.standings .pos-num {{
      display: inline-flex; min-width: 1.5rem; justify-content: flex-end; font-weight: 700;
    }}
    table.standings tr:nth-child(even):not(.zone-europe):not(.zone-playoff):not(.zone-drop) td {{
      background: rgba(255, 255, 255, 0.02);
    }}
    table.standings tr.zone-europe td {{
      background: rgba(52, 211, 153, 0.14);
    }}
    table.standings tr.zone-playoff td {{
      background: rgba(96, 165, 250, 0.14);
    }}
    table.standings tr.zone-drop td {{
      background: rgba(251, 113, 133, 0.14);
    }}
    table.standings tr:hover td {{ background: rgba(52, 211, 153, 0.08); }}
    table.standings tr.zone-europe:hover td {{ background: rgba(52, 211, 153, 0.2); }}
    table.standings tr.zone-playoff:hover td {{ background: rgba(96, 165, 250, 0.2); }}
    table.standings tr.zone-drop:hover td {{ background: rgba(251, 113, 133, 0.2); }}
    .form-row {{ display: inline-flex; gap: 4px; }}
    .form-pill {{
      display: inline-flex; width: 24px; height: 24px; align-items: center; justify-content: center;
      border-radius: 6px; font-size: 0.7rem; font-weight: 700;
    }}
    .form-pill.win {{ background: rgba(52, 211, 153, 0.2); color: var(--good); }}
    .form-pill.draw {{ background: rgba(147, 164, 189, 0.2); color: var(--muted); }}
    .form-pill.loss {{ background: rgba(251, 113, 133, 0.2); color: var(--bad); }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; margin: 0 0 24px; }}
    .swatch {{
      display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: 4px;
      vertical-align: -1px;
    }}
    .swatch.europe {{ background: #34d399; }}
    .swatch.playoff {{ background: #60a5fa; }}
    .swatch.drop {{ background: #fb7185; }}
    @media (max-width: 960px) {{
      .match-list {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 720px) {{
      .bar, main {{ width: min(78rem, calc(100% - 24px)); }}
      main {{ padding-bottom: 32px; }}
      .bar {{
        gap: 8px 16px; padding: 16px 0 8px;
      }}
      h1 {{ font-size: 1.4rem; }}
      h2 {{ margin: 16px 0 8px; }}
      .filters {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 12px; padding: 16px; margin: 12px 0 8px;
      }}
      .filters > label {{
        gap: 8px;
      }}
      .filter-span-2 {{
        grid-column: 1 / -1;
      }}
      .filter-actions {{
        grid-column: 1 / -1; gap: 12px; margin-top: 0;
      }}
      .filters .filter-actions button {{
        flex: 1 1 auto;
      }}
      .filters a.clear {{
        flex: 0 0 auto; min-width: 5.5rem;
      }}
      .cards, .cards.compact {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px; margin: 12px 0 8px;
      }}
      .scoreboard {{
        gap: 12px; margin: 12px 0; padding: 12px;
      }}
      .scoreboard-teams {{ gap: 8px; }}
      .club {{ gap: 8px; font-size: 1rem; }}
      .club-score {{ font-size: 1.5rem; }}
      .panel {{ margin: 12px 0; padding: 12px; }}
      .mls {{ margin: 12px 0; padding: 12px; gap: 12px; }}
      .mls-scoreline {{ gap: 8px; }}
      .mls-goal {{ min-height: 56px; padding: 8px; }}
      .ou-grid {{
        grid-template-columns: 1fr; gap: 8px; margin: 12px 0 8px;
      }}
      .ou-card {{ gap: 8px; padding: 12px; }}
      .prob {{
        grid-template-columns: 3.5rem minmax(0, 1fr) 3rem; gap: 8px; margin: 8px 0;
      }}
      th, td {{ padding: 8px 12px; }}
      .pager {{
        flex-wrap: wrap; gap: 8px 12px; margin: 8px 0 24px;
      }}
      .league-pills {{ gap: 8px; margin: 12px 0 8px; }}
      .match-list {{ gap: 8px; margin: 8px 0 12px; }}
      .match-card {{ padding: 8px 12px; }}
      .match-row {{
        grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
        gap: 6px; align-items: center;
      }}
      .club-name {{ font-size: 0.82rem; gap: 6px; }}
      .score {{ padding: 4px 6px; }}
      .score .mono {{ font-size: 0.8rem; }}
      .match-sub {{
        gap: 6px 10px;
      }}
      .match-meta {{ gap: 6px 10px; }}
      .legend {{ gap: 8px 12px; margin: 0 0 16px; }}
    }}
    @media (max-width: 480px) {{
      .bar, main {{ width: min(78rem, calc(100% - 16px)); }}
      .filters {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 12px; padding: 16px;
      }}
      .cards, .cards.compact {{
        grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px;
      }}
      .match-row {{ gap: 4px; }}
      .club-name {{ font-size: 0.78rem; gap: 4px; }}
      .match-card .badge {{
        min-height: 22px; padding: 2px 6px; font-size: 0.62rem;
      }}
      .prob {{
        grid-template-columns: 1fr; gap: 4px;
      }}
      .prob .meter {{ order: 1; }}
    }}
    .pill {{
      display: inline-flex; align-items: center; justify-content: center;
      gap: 8px; padding: 8px 14px; min-height: 44px; min-width: 44px;
      border-radius: 999px; cursor: pointer; border: 1px solid var(--line);
      background: rgba(11, 18, 32, 0.55); font-size: 0.9rem; font-weight: 600;
      color: var(--muted); transition: background 160ms var(--ease), border-color 160ms var(--ease), color 160ms var(--ease);
      user-select: none;
      white-space: nowrap;
    }}
    .pill:hover {{ border-color: #35506f; color: var(--text); }}
    .pill.active,
    .pill:has(input:checked) {{
      color: #042f24;
      background: linear-gradient(180deg, var(--accent), var(--accent-2));
      border-color: transparent;
      font-weight: 600;
    }}
    .pill.active span,
    .pill:has(input:checked) span {{
      color: #042f24;
    }}
    .pill:has(input:focus-visible) {{
      outline: 2px solid var(--focus); outline-offset: 2px;
    }}
    .pill input[type="checkbox"] {{
      width: 0; height: 0; opacity: 0; position: absolute; pointer-events: none;
    }}
    .pill .emblem {{
      width: 24px; height: 24px; padding: 4px; box-sizing: border-box;
      flex-shrink: 0; display: block; object-fit: contain; margin: 0;
    }}
    .btn-danger {{
      min-height: 44px; padding: 8px 16px; background: transparent;
      color: var(--bad); border: 1px solid var(--bad); border-radius: var(--radius-sm);
      font-weight: 600; font-size: 0.85rem; cursor: pointer;
      transition: background 160ms var(--ease), transform 160ms var(--ease);
    }}
    .btn-danger:hover {{ background: rgba(251, 113, 133, 0.15); }}
    .btn-danger:active {{ transform: scale(0.97); }}
    .btn-rerun {{
      min-height: 44px; padding: 8px 16px; border: none;
      background: linear-gradient(180deg, var(--accent), var(--accent-2));
      color: #042f24; border-radius: var(--radius-sm);
      font-weight: 700; font-size: 0.85rem; cursor: pointer;
      transition: filter 160ms var(--ease), transform 160ms var(--ease);
    }}
    .btn-rerun:hover {{ filter: brightness(1.05); }}
    .btn-rerun:active {{ transform: scale(0.97); }}
    .btn-rerun:disabled {{
      opacity: 0.45; cursor: not-allowed; filter: none; transform: none;
    }}
    .exp-actions {{
      display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: flex-end;
    }}
    .btn-secondary {{
      min-height: 44px; padding: 8px 16px; background: rgba(255, 255, 255, 0.06);
      color: var(--text); border: 1px solid var(--line); border-radius: var(--radius-sm);
      font-weight: 600; font-size: 0.85rem; cursor: pointer;
      transition: background 160ms var(--ease), border-color 160ms var(--ease);
    }}
    .btn-secondary:hover {{
      background: rgba(255, 255, 255, 0.12); border-color: rgba(255, 255, 255, 0.2);
    }}
    dialog.confirm-dialog {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius-md);
      padding: 0;
      color: var(--text);
      box-shadow: 0 24px 64px rgba(0, 0, 0, 0.65);
      max-width: 460px;
      width: calc(100vw - 32px);
      margin: auto;
    }}
    dialog.confirm-dialog::backdrop {{
      background: rgba(4, 7, 13, 0.78);
      backdrop-filter: blur(4px);
    }}
    .confirm-dialog-content {{
      padding: 24px;
    }}
    .confirm-dialog-header {{
      display: flex; align-items: center; gap: 10px; margin-bottom: 12px;
    }}
    .confirm-dialog-icon {{
      font-size: 1.4rem; line-height: 1;
    }}
    .confirm-dialog-header h3 {{
      margin: 0; font-size: 1.15rem; color: var(--text); font-weight: 600;
    }}
    .confirm-dialog-text {{
      font-size: 0.95rem; margin: 0 0 8px; color: var(--text);
    }}
    .confirm-dialog-sub {{
      font-size: 0.82rem; line-height: 1.5; color: var(--muted); margin: 0 0 20px;
    }}
    .confirm-dialog-actions {{
      display: flex; justify-content: flex-end; align-items: center; gap: 10px;
      padding-top: 14px; border-top: 1px solid var(--line);
    }}
    .experiment-grid {{ display: grid; gap: 16px; margin: 16px 0; }}
    .experiment-card {{
      background: var(--panel); border: 1px solid var(--line);
      border-radius: var(--radius-md); padding: 20px 24px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
      transition: border-color 160ms var(--ease), box-shadow 160ms var(--ease);
    }}
    .experiment-card:hover {{
      border-color: rgba(52, 211, 153, 0.35); box-shadow: 0 12px 32px rgba(0, 0, 0, 0.25);
    }}
    .exp-top-row {{
      display: flex; flex-wrap: wrap; align-items: center;
      justify-content: space-between; gap: 12px; margin-bottom: 12px;
    }}
    .exp-title-group {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }}
    .exp-title-group h3 {{ margin: 0; font-size: 1.1rem; color: var(--text); }}
    .exp-hero {{
      display: flex; flex-wrap: wrap; gap: 16px 28px; align-items: baseline;
      margin: 14px 0 12px; padding: 14px 18px; background: rgba(11, 18, 32, 0.5);
      border: 1px solid var(--line); border-radius: var(--radius-sm);
    }}
    .exp-hero .hero-metric {{ display: flex; flex-direction: column; gap: 2px; }}
    .exp-hero .num {{ font: 600 1.55rem/1.1 "IBM Plex Mono", monospace; letter-spacing: -0.02em; }}
    .exp-hero .label {{ font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }}
    .metric-win {{ color: var(--good); font-weight: 700; }}
    .scope-badges {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
    .scope-badge {{
      display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px;
      font-size: 0.75rem; font-weight: 600; background: rgba(148, 163, 184, 0.12);
      border: 1px solid var(--line); color: var(--muted);
    }}
    .scope-badge.league {{ color: var(--accent); border-color: rgba(52, 211, 153, 0.3); }}
    .scope-badge .emblem {{
      width: 16px; height: 16px; padding: 2px; border-radius: 4px;
      box-sizing: border-box; flex-shrink: 0; display: inline-block; object-fit: contain; margin: 0;
    }}
    .pass-bar-wrap {{ width: 100%; margin-top: 8px; }}
    .pass-bar {{ height: 6px; border-radius: 999px; background: rgba(52, 211, 153, 0.2); overflow: hidden; position: relative; }}
    .pass-bar.has-failures {{ background: rgba(251, 113, 133, 0.3); }}
    .pass-bar .fill {{ display: block; height: 100%; border-radius: 999px; background: var(--good); }}
    .pass-bar.running {{ background: rgba(251, 191, 36, 0.2); }}
    .pass-bar.running .fill {{
      background: var(--wait);
      animation: bar-pulse 1.8s ease-in-out infinite;
    }}
    @keyframes bar-pulse {{
      0% {{ width: 20%; margin-left: 0%; }}
      50% {{ width: 50%; margin-left: 35%; }}
      100% {{ width: 20%; margin-left: 80%; }}
    }}
    .exp-card-footer {{
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
      gap: 12px; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line);
    }}
    .error-box {{
      background: rgba(251, 113, 133, 0.08); border: 1px solid var(--bad);
      border-radius: var(--radius-sm); padding: 16px; margin: 16px 0;
    }}
    .error-box h3 {{ margin: 0 0 8px; color: var(--bad); font-size: 0.95rem; }}
    .error-box pre {{
      margin: 0; font: 0.8rem/1.45 "IBM Plex Mono", monospace; color: var(--text);
      white-space: pre-wrap; word-break: break-all; max-height: 280px; overflow-y: auto;
    }}
    .prompt-export-panel {{
      background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-md);
      padding: 20px; margin: 24px 0;
    }}
    .prompt-header {{
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
      gap: 12px; margin-bottom: 12px;
    }}
    .prompt-header h3 {{ margin: 0; font-size: 1.05rem; color: var(--text); }}
    .copy-prompt-btn {{
      min-height: 44px; padding: 8px 18px; font-weight: 600; cursor: pointer;
      background: linear-gradient(180deg, var(--accent), var(--accent-2));
      color: #042f24; border: none; border-radius: var(--radius-sm);
      display: inline-flex; align-items: center; gap: 8px;
      transition: filter 160ms var(--ease), background 160ms var(--ease);
    }}
    .copy-prompt-btn:hover {{ filter: brightness(1.08); }}
    .copy-prompt-btn.is-copied {{ background: #10b981; color: #ffffff; }}
    .prompt-export-panel textarea {{
      width: 100%; min-height: 240px; box-sizing: border-box;
      font: 0.82rem/1.5 "IBM Plex Mono", monospace; background: var(--bg);
      color: var(--text); border: 1px solid var(--line); border-radius: var(--radius-sm);
      padding: 14px; resize: vertical;
    }}
    .pills-group {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 6px 0 12px; }}
    @media (prefers-reduced-motion: reduce) {{
      *, *::before, *::after {{
        animation: none !important;
        transition: none !important;
        scroll-behavior: auto !important;
      }}
    }}
  </style>
</head>
<body>
  <main>
  {body}
  </main>
  <script>
    document.addEventListener("click", async (event) => {{
      const button = event.target.closest("[data-copy], [data-copy-target]");
      if (!(button instanceof HTMLButtonElement)) return;
      let value = button.getAttribute("data-copy") || "";
      const targetId = button.getAttribute("data-copy-target");
      if (targetId) {{
        const el = document.getElementById(targetId);
        if (el) value = el.value || el.textContent || "";
      }}
      if (!value) return;
      try {{
        if (navigator.clipboard && window.isSecureContext) {{
          await navigator.clipboard.writeText(value);
        }} else {{
          const field = document.createElement("textarea");
          field.value = value;
          field.setAttribute("readonly", "");
          field.style.position = "fixed";
          field.style.opacity = "0";
          document.body.appendChild(field);
          field.select();
          document.execCommand("copy");
          field.remove();
        }}
        button.classList.add("is-copied");
        const originalText = button.textContent;
        button.setAttribute("aria-label", "Copied");
        button.title = "Copied";
        if (targetId) {{
          button.textContent = "Copied to Clipboard!";
        }}
        window.clearTimeout(button._copyTimer);
        button._copyTimer = window.setTimeout(() => {{
          button.classList.remove("is-copied");
          button.setAttribute("aria-label", "Copy");
          button.title = "Copy";
          if (targetId) {{
            button.textContent = originalText;
          }}
        }}, 1800);
      }} catch (_error) {{
        button.title = "Copy failed";
      }}
    }});
    document.addEventListener("click", (event) => {{
      if (event.target && event.target.tagName === "DIALOG") {{
        event.target.close();
      }}
    }});
  </script>
</body>
</html>
"""


# =====================================================================
# Lab Experiment Dashboard Views
# =====================================================================

def _experiment_delete_form(experiment_id: str, csrf_token: str | None) -> str:
    import re
    from urllib.parse import quote

    csrf = f'<input type="hidden" name="csrf" value="{html.escape(csrf_token)}">' if csrf_token else ""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", experiment_id)
    dialog_id = f"del-dialog-{safe_id}"
    form_id = f"form-del-{safe_id}"
    del_url = f"/experiments/{quote(experiment_id, safe='')}/delete"

    return (
        f'<button type="button" class="btn-danger" '
        f'onclick="const d=document.getElementById(\'{dialog_id}\'); if(d&&d.showModal){{d.showModal();}}else if(confirm(\'Delete experiment {html.escape(experiment_id)}? This cannot be undone.\')){{document.getElementById(\'{form_id}\').submit();}}">'
        f'Delete</button>'
        f'<dialog id="{dialog_id}" class="confirm-dialog">'
        f'  <div class="confirm-dialog-content">'
        f'    <div class="confirm-dialog-header">'
        f'      <span class="confirm-dialog-icon">⚠️</span>'
        f'      <h3>Delete Experiment</h3>'
        f'    </div>'
        f'    <p class="confirm-dialog-text">Are you sure you want to delete <code class="mono">{html.escape(experiment_id)}</code>?</p>'
        f'    <p class="confirm-dialog-sub">All candidate runs, evaluated model benchmarks, and linked predictions for this experiment will be permanently removed. This action cannot be undone.</p>'
        f'    <div class="confirm-dialog-actions">'
        f'      <button type="button" class="btn-secondary" onclick="this.closest(\'dialog\').close()">Cancel</button>'
        f'      <form id="{form_id}" method="post" action="{del_url}" style="display:inline; margin:0;">'
        f'        {csrf}'
        f'        <button type="submit" class="btn-danger">Confirm Delete</button>'
        f'      </form>'
        f'    </div>'
        f'  </div>'
        f'</dialog>'
    )


def _experiment_rerun_form(
    experiment_id: str,
    csrf_token: str | None,
    *,
    disabled: bool = False,
) -> str:
    """Confirm + POST form to wipe outputs and re-queue ``lab-run`` for this id."""

    import re
    from urllib.parse import quote

    csrf = f'<input type="hidden" name="csrf" value="{html.escape(csrf_token)}">' if csrf_token else ""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", experiment_id)
    dialog_id = f"rerun-dialog-{safe_id}"
    form_id = f"form-rerun-{safe_id}"
    rerun_url = f"/experiments/{quote(experiment_id, safe='')}/rerun"
    disabled_attr = " disabled" if disabled else ""
    title = "Already running" if disabled else "Re-run with the same stored config"

    return (
        f'<button type="button" class="btn-rerun"{disabled_attr} title="{html.escape(title)}" '
        f'onclick="const d=document.getElementById(\'{dialog_id}\'); if(d&&d.showModal){{d.showModal();}}else if(confirm(\'Re-run experiment {html.escape(experiment_id)}? Previous runs for this id will be cleared.\')){{document.getElementById(\'{form_id}\').submit();}}">'
        f'Re-run</button>'
        f'<dialog id="{dialog_id}" class="confirm-dialog">'
        f'  <div class="confirm-dialog-content">'
        f'    <div class="confirm-dialog-header">'
        f'      <h3>Re-run Experiment</h3>'
        f'    </div>'
        f'    <p class="confirm-dialog-text">Re-queue <code class="mono">{html.escape(experiment_id)}</code> with its stored config?</p>'
        f'    <p class="confirm-dialog-sub">Candidate runs and linked predictions for this experiment will be cleared, then walk-forward will start again in the background. The experiment row and config are kept.</p>'
        f'    <div class="confirm-dialog-actions">'
        f'      <button type="button" class="btn-secondary" onclick="this.closest(\'dialog\').close()">Cancel</button>'
        f'      <form id="{form_id}" method="post" action="{rerun_url}" style="display:inline; margin:0;">'
        f'        {csrf}'
        f'        <button type="submit" class="btn-rerun">Confirm Re-run</button>'
        f'      </form>'
        f'    </div>'
        f'  </div>'
        f'</dialog>'
    )


def _run_experiment_form(
    filters: dict[str, str],
    competitions: list[str] | None = None,
    seasons: list[str] | None = None,
    csrf_token: str | None = None,
    connection: sqlite3.Connection | None = None,
) -> str:
    """Multi-competition and multi-season experiment launch form."""
    from laboratory.football_data import COMPETITION_NAMES
    from laboratory.store import (
        competition_emblem_map,
        emblem_url_for_competition,
    )

    csrf = f'<input type="hidden" name="csrf" value="{html.escape(csrf_token)}">' if csrf_token else ""
    available_comps = list(competitions) if competitions else list(COMPETITION_NAMES.keys())

    # Preferred league ordering matching Standings and priority
    league_order = ("PL", "PD", "FL1", "BL1", "SA", "PPL", "DED", "CL", "SPL")
    available_comps.sort(key=lambda c: (league_order.index(c) if c in league_order else 99, c))

    emblems = competition_emblem_map(connection, available_comps) if connection else {}

    default_comp = filters.get("league", "PL").upper()
    comp_pills = []
    for code in available_comps:
        checked = ' checked' if code == default_comp else ''
        name = COMPETITION_NAMES.get(code, code)
        emblem_url = emblems.get(code) or emblem_url_for_competition(code)
        logo = _logo_img(emblem_url, name, kind="emblem", size=22)
        comp_pills.append(
            f'<label class="pill">'
            f'<input type="checkbox" name="competitions" value="{html.escape(code)}"{checked}>'
            f'{logo}<span>{html.escape(name)}</span>'
            f'</label>'
        )

    if not seasons:
        from datetime import datetime
        cur = datetime.now().year
        available_szns = [str(cur - 2), str(cur - 1), str(cur)]
    else:
        available_szns = seasons

    szn_pills = []
    for s in available_szns:
        szn_pills.append(
            f'<label class="pill">'
            f'<input type="checkbox" name="seasons" value="{html.escape(s)}" checked>'
            f'<span>{html.escape(s)}</span>'
            f'</label>'
        )

    from laboratory.lab_experiment import example_experiment_launch_json

    example_json = example_experiment_launch_json()
    example_json_attr = html.escape(example_json, quote=True)

    return f"""
    <section class="panel run-experiment-panel">
      <h2>Run New Experiment</h2>
      <p class="note">Configure and launch background walk-forward grid search. Multiple competitions create a coordinated batch. Prefer the JSON box below for a full config paste; when it is non-empty it overrides the form fields.</p>
      <form class="run-experiment-form" method="post" action="/experiments/run" style="margin-top:16px;">
        {csrf}
        <details class="advanced-params" open style="margin: 0 0 18px; padding: 12px 16px; background: rgba(11,18,32,0.5); border: 1px solid var(--line); border-radius: var(--radius-sm);">
          <summary style="cursor:pointer; font-weight:600; color:var(--text); font-size:0.9rem;">JSON config (optional — overrides form)</summary>
          <p class="note" style="margin:10px 0 8px;">Paste a full experiment object. Omitted keys use defaults. <code>seasons</code> is required. Invalid JSON or missing fields return an error without starting a run.</p>
          <div class="json-config-row" style="display:flex; gap:12px; align-items:flex-end; flex-wrap:wrap;">
            <label style="flex:1 1 280px; min-width:0; display:block;">
              <textarea name="config_json" class="mono" rows="16" spellcheck="false" placeholder="{example_json_attr}" style="width:100%; min-height:220px; font: 0.82rem/1.45 'IBM Plex Mono', monospace; background: rgba(4,7,13,0.55); color: var(--text); border: 1px solid var(--line); border-radius: var(--radius-sm); padding: 12px; resize: vertical;"></textarea>
            </label>
            <button type="submit" name="launch_source" value="json" style="flex:0 0 auto; min-height:44px; min-width:120px; padding:10px 20px; font-weight:700; cursor:pointer; background:linear-gradient(180deg, var(--accent), var(--accent-2)); color:#042f24; border:none; border-radius:var(--radius-sm);">Run</button>
          </div>
        </details>
        <div style="margin-bottom:14px;">
          <label style="display:block; font-size:0.85rem; font-weight:600; color:var(--text); margin-bottom:6px;">Competitions</label>
          <div class="league-pills" style="margin:6px 0 14px;">{''.join(comp_pills)}</div>
        </div>

        <div style="margin-bottom:14px;">
          <label style="display:block; font-size:0.85rem; font-weight:600; color:var(--text); margin-bottom:6px;">Seasons</label>
          <div class="league-pills" style="margin:6px 0 14px;">{''.join(szn_pills)}</div>
        </div>

        <div class="filters" style="grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); margin-bottom:14px;">
          <label>Experiment ID (optional slug)
            <input name="experiment_id" type="text" placeholder="EXP-CUSTOM-001" pattern="^[A-Za-z0-9_-]+$" title="Alphanumeric, dashes, and underscores only">
          </label>
          <label>Primary Metric
            <select name="primary_metric">
              <option value="log_loss" selected>Log Loss (lower is better)</option>
              <option value="brier">Brier Score (lower is better)</option>
              <option value="full_time_1x2_accuracy">FT 1X2 Accuracy (higher is better)</option>
              <option value="exact_score_log_loss">Exact Score Log-Loss (lower is better)</option>
            </select>
          </label>
          <label>Min Matches
            <input name="min_matches" type="number" value="50" min="10">
            <span class="note">Best config requires at least this many evaluated matches.</span>
          </label>
        </div>

        <div class="filters" style="grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); margin-bottom:14px;">
          <label>Base k (shrinkage)
            <input name="k" type="number" step="0.5" value="6.0">
            <span class="note">Used when k Grid Search is empty; otherwise the grid wins.</span>
          </label>
          <label>Prior Mean
            <select name="prior_mean">
              <option value="last_season" selected>Last Season</option>
              <option value="one">One (default)</option>
            </select>
          </label>
          <label>Dependence
            <select name="dependence">
              <option value="independent" selected>Independent</option>
              <option value="dixon_coles">Dixon-Coles</option>
            </select>
          </label>
          <label>Half-Time Share
            <select name="half_time_share_mode">
              <option value="fixed_0_45" selected>Fixed 0.45</option>
              <option value="club_shrunk">Club Shrunk</option>
              <option value="league_only">League Only</option>
            </select>
          </label>
          <label>Ratio Mode
            <select name="ratio_mode">
              <option value="one_pass" selected>One Pass</option>
              <option value="joint">Joint</option>
            </select>
          </label>
        </div>

        <div class="filters" style="grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); margin-bottom:14px;">
          <label>k Grid Search (comma-separated)
            <input name="grid_k" type="text" placeholder="5.0, 6.0, 7.0">
          </label>
          <label>Draw Boost Grid (comma-separated)
            <input name="grid_draw_boost" type="text" placeholder="1.0, 1.1, 1.2">
          </label>
        </div>

        <details class="advanced-params" style="margin: 12px 0 18px; padding: 12px 16px; background: rgba(11,18,32,0.5); border: 1px solid var(--line); border-radius: var(--radius-sm);">
          <summary style="cursor:pointer; font-weight:600; color:var(--text); font-size:0.9rem;">Advanced Calibration Parameters</summary>
          <div class="filters" style="grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); margin-top:12px;">
            <label>Draw Boost
              <input name="draw_boost" type="number" step="0.05" value="1.0">
            </label>
            <label>Draw Weights (comma-separated)
              <input name="draw_weights" type="text" placeholder="1.35, 1.25, 1.05, 0.70">
            </label>
            <label>Draw Pick Threshold
              <input name="draw_pick_threshold" type="number" step="0.01" placeholder="0.28">
            </label>
            <label>Draw Pick Margin
              <input name="draw_pick_margin" type="number" step="0.01" placeholder="0.05">
            </label>
            <label>Draw Tightness Scale
              <input name="draw_tightness_scale" type="number" step="0.1" placeholder="1.0">
            </label>
            <label>Draw Tightness Ref
              <input name="draw_tightness_ref" type="number" step="0.1" value="1.0">
            </label>
          </div>
        </details>

        <button type="submit" style="min-height:44px; padding:10px 24px; font-weight:700; cursor:pointer; background:linear-gradient(180deg, var(--accent), var(--accent-2)); color:#042f24; border:none; border-radius:var(--radius-sm);">
          Run Experiment
        </button>
      </form>
    </section>
    """


def experiments_list(
    connection: sqlite3.Connection,
    query: dict[str, list[str]] | None = None,
    *,
    csrf_token: str | None = None,
    competitions: list[str] | None = None,
    seasons: list[str] | None = None,
    lab_db: Path | None = None,
) -> str:
    """List all lab experiments with hierarchical outcome cards and pass/fail rates."""
    from db.db_paths import resolve_lab_database_path
    from laboratory.store import connect as lab_connect, emblem_url_for_competition

    filters = _filters(query, connection) if query else _default_filters()
    target_lab_db = lab_db or resolve_lab_database_path()
    lab_conn = lab_connect(target_lab_db)

    batch_filter = (query.get("batch") or [""])[0].strip() if query else ""

    try:
        rows = lab_conn.execute("""
            SELECT experiment_id, competition, config_json, status, created_at, completed_at,
                   best_config_json, best_metrics_json, error
            FROM lab_experiments
            ORDER BY created_at DESC
        """).fetchall()

        # Fetch runs summary per experiment
        runs_summary_rows = lab_conn.execute("""
            SELECT experiment_id,
                   COUNT(*) AS total_runs,
                   SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS passed_runs,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_runs,
                   MAX(matches_evaluated) AS max_evaluated
            FROM lab_experiment_runs
            GROUP BY experiment_id
        """).fetchall()
        runs_map = {row["experiment_id"]: row for row in runs_summary_rows}
    finally:
        lab_conn.close()

    body = [
        _masthead("Lab Experiments", filters, active="experiments"),
    ]

    # Flash messages for execution/deletion
    run_status = (query.get("run_status") or [""])[0].strip() if query else (filters.get("run_status") or "").strip()
    run_detail = (query.get("run_detail") or [""])[0].strip() if query else (filters.get("run_detail") or "").strip()
    if run_status:
        kind = "info" if run_status in ("started", "completed", "deleted") else "error"
        body.append(
            _state(
                kind=kind,
                title=f"Experiment {run_status.title()}",
                lead=run_detail or f"Status: {run_status}",
            )
        )

    if batch_filter:
        body.append(
            f'<div class="state state-info" role="status">'
            f'<strong>Viewing Coordinated Batch: <code>{html.escape(batch_filter)}</code></strong>'
            f'<p class="state-action"><a class="state-link" href="/experiments">View All Experiments</a></p>'
            f'</div>'
        )

    body.append(_run_experiment_form(filters, competitions, seasons, csrf_token, connection=connection))

    filtered_rows = []
    for row in rows:
        cfg = json.loads(row["config_json"]) if row["config_json"] else {}
        if batch_filter and cfg.get("batch_id") != batch_filter:
            continue
        filtered_rows.append((row, cfg))

    if not filtered_rows:
        body.append(
            _state(
                kind="empty",
                title="No experiments found" if batch_filter else "No experiments yet",
                lead="Configure and launch your first experiment using the form above.",
                action_href="/experiments" if batch_filter else "/",
                action_label="Clear batch filter" if batch_filter else "Back to Overview",
            )
        )
    else:
        cards = []
        for row, cfg in filtered_rows:
            exp_id = row["experiment_id"]
            status = row["status"]
            summary = runs_map.get(exp_id)
            total_runs = summary["total_runs"] if summary else 0
            passed_runs = summary["passed_runs"] if summary else 0
            failed_runs = summary["failed_runs"] if summary else 0
            max_n = summary["max_evaluated"] if summary else 0

            # Badges
            if status == "completed":
                badge = '<span class="badge good">Completed</span>'
            elif status == "running":
                badge = '<span class="badge wait">Running</span>'
            elif status == "failed":
                badge = '<span class="badge bad">Failed</span>'
            else:
                badge = f'<span class="badge muted">{html.escape(status.title())}</span>'

            # Scope badges
            selected_comp = row["competition"]
            selected_szns = cfg.get("seasons", [])
            comp_name = COMPETITION_NAMES.get(selected_comp, selected_comp)
            comp_logo = _logo_img(emblem_url_for_competition(selected_comp), comp_name, kind="emblem", size=16)
            scope_badges = [f'<span class="scope-badge league">{comp_logo}<span>{html.escape(comp_name)}</span></span>']
            for s in selected_szns:
                scope_badges.append(f'<span class="scope-badge">{html.escape(str(s))}</span>')

            # Metric extraction (defensive: legacy best_metrics may omit Exact / O/U keys)
            best_metrics = json.loads(row["best_metrics_json"]) if row["best_metrics_json"] else {}
            acc = best_metrics.get("full_time_1x2_accuracy")
            log_loss = best_metrics.get("full_time_1x2_log_loss")
            brier = best_metrics.get("full_time_1x2_brier")
            exact_hit, exact_ll = _exact_score_from_metrics(best_metrics)
            n_matches = best_metrics.get("n_matches") or max_n or 0

            acc_str = f"{acc:.1%}" if isinstance(acc, (int, float)) else "—"
            ll_str = f"{log_loss:.4f}" if isinstance(log_loss, (int, float)) else "—"
            brier_str = f"{brier:.4f}" if isinstance(brier, (int, float)) else "—"
            exact_hit_str = _fmt_pct(exact_hit)
            exact_ll_str = _fmt_ll(exact_ll)

            # Calculate planned runs from config if total_runs in DB is 0
            param_grid = cfg.get("param_grid") or {}
            planned_count = 1
            for vals in param_grid.values():
                if isinstance(vals, list) and vals:
                    planned_count *= len(vals)
            display_total = total_runs if total_runs > 0 else (planned_count if status == "running" else total_runs)

            # Pass bar percentage & styling
            pass_pct = (passed_runs / total_runs * 100.0) if total_runs > 0 else 0.0
            if status == "running":
                bar_class = "pass-bar running"
                fill_html = '<span class="fill"></span>'
            elif failed_runs > 0:
                bar_class = "pass-bar has-failures"
                fill_html = f'<span class="fill" style="width:{pass_pct:.1f}%;"></span>'
            else:
                bar_class = "pass-bar"
                fill_html = f'<span class="fill" style="width:{pass_pct:.1f}%;"></span>'

            delete_form = _experiment_delete_form(exp_id, csrf_token)

            cards.append(
                f'<article class="experiment-card">'
                f'  <div class="exp-top-row">'
                f'    <div class="exp-title-group">'
                f'      <h3><code>{html.escape(exp_id)}</code></h3>'
                f'      {badge}'
                f'      <div class="scope-badges">{"".join(scope_badges)}</div>'
                f'    </div>'
                f'    <div class="muted" style="font-size:0.8rem;">{_when(row["created_at"])}</div>'
                f'  </div>'
                f'  <div class="exp-hero">'
                f'    <div class="hero-metric">'
                f'      <span class="num">{passed_runs}/{display_total}</span>'
                f'      <span class="label">Runs Passed</span>'
                f'    </div>'
                f'    <div class="hero-metric">'
                f'      <span class="num" style="color:var(--good);">{acc_str}</span>'
                f'      <span class="label">Best FT Accuracy</span>'
                f'    </div>'
                f'    <div class="hero-metric">'
                f'      <span class="num">{ll_str}</span>'
                f'      <span class="label">Best Log-Loss</span>'
                f'    </div>'
                f'    <div class="hero-metric">'
                f'      <span class="num">{exact_hit_str}</span>'
                f'      <span class="label">Exact Hit %</span>'
                f'    </div>'
                f'    <div class="hero-metric">'
                f'      <span class="num">{exact_ll_str}</span>'
                f'      <span class="label">Exact LL</span>'
                f'    </div>'
                f'    <div class="hero-metric">'
                f'      <span class="num">{brier_str}</span>'
                f'      <span class="label">Best Brier</span>'
                f'    </div>'
                f'    <div class="hero-metric">'
                f'      <span class="num">{n_matches}</span>'
                f'      <span class="label">Sample Size (n)</span>'
                f'    </div>'
                f'  </div>'
                f'  <div class="pass-bar-wrap">'
                f'    <div class="{bar_class}">{fill_html}</div>'
                f'  </div>'
                f'  <div class="exp-card-footer">'
                f'    <a href="/experiments/{quote(exp_id, safe="")}" style="font-weight:600; font-size:0.9rem;">View Full Analysis →</a>'
                f'    {delete_form}'
                f'  </div>'
                f'</article>'
            )

        body.append(f'<section class="experiment-grid">{"".join(cards)}</section>')

    has_running = any(row["status"] == "running" for row, _ in filtered_rows)
    extra_head = '<meta http-equiv="refresh" content="5">' if has_running else None
    return _page("Lab Experiments", "\n".join(body), filters, extra_head=extra_head)


def _ai_diagnostic_prompt(exp: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    """Generate structured markdown prompt for AI diagnostics and improvement."""
    from laboratory.lab_experiment import metric_sort_value, resolve_primary_metric

    cfg = exp.get("config") or {}
    exp_id = exp["experiment_id"]
    comp = exp.get("competition", "PL")
    seasons = cfg.get("seasons", [])
    primary_metric = cfg.get("primary_metric", "log_loss")
    metric_key, higher_is_better = resolve_primary_metric(primary_metric)
    min_matches = int(cfg.get("min_matches") or 0)
    base_cfg = cfg.get("base_config", {})
    param_grid = cfg.get("param_grid", {})

    total_runs = len(runs)
    completed_runs = sum(1 for r in runs if r.get("status") == "completed")
    failed_runs = sum(1 for r in runs if r.get("status") == "failed")
    max_eval = max((r.get("matches_evaluated") or 0) for r in runs) if runs else 0

    table_rows = []
    # Completed first, then by mapped metric key (not the UI label).
    sorted_runs = sorted(
        runs,
        key=lambda r: (
            0 if r.get("status") == "completed" else 1,
            (
                -metric_sort_value(
                    r.get("metrics") or {},
                    metric_key,
                    higher_is_better=higher_is_better,
                )
                if higher_is_better
                else metric_sort_value(
                    r.get("metrics") or {},
                    metric_key,
                    higher_is_better=higher_is_better,
                )
            ),
        ),
    )

    for rank, r in enumerate(sorted_runs, start=1):
        mid = r.get("model_id", "—")
        st = r.get("status", "—")
        m = r.get("metrics") or {}
        n = r.get("matches_evaluated") or 0
        acc = f"{m.get('full_time_1x2_accuracy'):.2%}" if "full_time_1x2_accuracy" in m else "—"
        ll = f"{m.get('full_time_1x2_log_loss'):.4f}" if "full_time_1x2_log_loss" in m else "—"
        brier = f"{m.get('full_time_1x2_brier'):.4f}" if "full_time_1x2_brier" in m else "—"
        exact_hit, exact_ll = _exact_score_from_metrics(m)
        ht_hit, ht_ll = _market_pair_from_metrics(m, "half_time_1x2")
        ou25_hit, ou25_ll = _market_pair_from_metrics(m, "over_under_2_5")
        ou35_hit, ou35_ll = _market_pair_from_metrics(m, "over_under_3_5")
        exact_hit_s = _fmt_pct(exact_hit)
        exact_ll_s = _fmt_ll(exact_ll)
        r_cfg = r.get("config") or {}
        # highlight changed params
        diffs = [f"{k}={v}" for k, v in r_cfg.items() if base_cfg.get(k) != v and k != "model_id"]
        diff_str = ", ".join(diffs) if diffs else "baseline"
        table_rows.append(
            f"| {rank} | `{mid}` | {st} | {acc} | {ll} | {brier} "
            f"| {exact_hit_s} | {exact_ll_s} "
            f"| {_fmt_pct(ht_hit)} | {_fmt_ll(ht_ll)} "
            f"| {_fmt_pct(ou25_hit)} | {_fmt_ll(ou25_ll)} "
            f"| {_fmt_pct(ou35_hit)} | {_fmt_ll(ou35_ll)} "
            f"| {n} | {diff_str} |"
        )

    table_md = "\n".join(table_rows) if table_rows else "No evaluated runs recorded."

    return f"""# AI Diagnostic Analysis Request: Experiment {exp_id}

## 1. Experiment Scope & Setup
- **Experiment ID:** {exp_id}
- **Competition:** {comp}
- **Evaluated Seasons:** {', '.join(str(s) for s in seasons)}
- **Primary Optimization Metric:** {primary_metric} → `{metric_key}` ({'higher' if higher_is_better else 'lower'} is better); min_matches={min_matches}
- **Sample Size (Evaluated Matches):** {max_eval}
- **Run Summary:** {completed_runs}/{total_runs} Completed ({failed_runs} Failed)

## 2. Model Configuration
### Base Configuration
```json
{json.dumps(base_cfg, indent=2)}
```

### Parameter Grid Tested
```json
{json.dumps(param_grid, indent=2)}
```

## 3. Results Leaderboard
| Rank | Model ID | Status | FT 1X2 Acc | Log-Loss | Brier | Exact Hit % | Exact LL | HT Acc % | HT Log-Loss | O/U 2.5 Acc | O/U 2.5 LL | Goals 3.5 Acc % | Goals 3.5 Log-Loss | Evaluated (n) | Varied Hyperparameters |
|:---|:---|:---|:---|:---|:---|:---|:---|:---|:---|:---|:---|:---|:---|:---|:---|
{table_md}

## 4. Diagnostic & Improvement Instructions
Please review this experiment output and provide:
1. **Strengths & Winning Patterns:** Which hyperparameter variations yielded real metric improvements vs. baseline?
2. **Weaknesses, Calibration Gaps & Overfitting Risks:** Are differences statistically reliable given n={max_eval}, or do you see signs of noise fitting?
3. **Parameter Sensitivity Insights:** How do parameters like shrinkage `k`, `draw_boost`, and draw-calibration weights affect log-loss and accuracy?
4. **Exact Score & Goal Volume:** Evaluate Exact Score calibration (Exact LL on the actual score cell) versus mode hit rate (Exact Hit %), and check for goal-volume bias on O/U 2.5 (Acc vs LL). Call out configs that look sharp on FT 1X2 but poorly calibrated on exact scores or totals.
5. **Concrete Next-Step Experiments:** Suggest 3 specific, targeted follow-up configurations or grid searches to test next.
"""


def experiment_detail(
    connection: sqlite3.Connection,
    experiment_id: str,
    query: dict[str, list[str]] | None = None,
    *,
    csrf_token: str | None = None,
    lab_db: Path | None = None,
) -> str | None:
    """Detailed view for a single experiment: status, KPI strip, traceback, leaderboard, prompt export."""
    from db.db_paths import resolve_lab_database_path
    from laboratory.store import connect as lab_connect, emblem_url_for_competition, get_lab_experiment, get_lab_experiment_runs
    from urllib.parse import quote

    filters = _filters(query, connection) if query else _default_filters()
    target_lab_db = lab_db or resolve_lab_database_path()
    lab_conn = lab_connect(target_lab_db)

    try:
        exp = get_lab_experiment(lab_conn, experiment_id)
        if not exp:
            return None
        runs = get_lab_experiment_runs(lab_conn, experiment_id)
    finally:
        lab_conn.close()

    status = exp["status"]
    extra_head = '<meta http-equiv="refresh" content="5">' if status == "running" else ""

    from laboratory.lab_experiment import metric_sort_value, resolve_primary_metric

    cfg = exp.get("config") or {}
    base_cfg = cfg.get("base_config") or {}
    param_grid = cfg.get("param_grid") or {}
    primary_metric = cfg.get("primary_metric", "log_loss")
    metric_key, higher_is_better = resolve_primary_metric(primary_metric)
    min_matches = int(cfg.get("min_matches") or 0)
    seasons = cfg.get("seasons", [])

    body = [
        _masthead(f"Experiment: {html.escape(experiment_id)}", filters, active="experiments", back_href="/experiments"),
    ]

    # Flash messages
    run_status = (filters.get("run_status") or "").strip()
    run_detail = (filters.get("run_detail") or "").strip()
    if run_status:
        kind = "info" if run_status in ("started", "completed") else "error"
        body.append(
            _state(
                kind=kind,
                title=f"Experiment Run {run_status.title()}",
                lead=run_detail or f"Status: {run_status}",
            )
        )

    # Header Panel
    status_badge = {
        "completed": "good",
        "running": "wait",
        "failed": "bad",
    }.get(status, "muted")

    comp_name = COMPETITION_NAMES.get(exp["competition"], exp["competition"])
    comp_logo = _logo_img(emblem_url_for_competition(exp["competition"]), comp_name, kind="emblem", size=16)
    scope_badges = [f'<span class="scope-badge league">{comp_logo}<span>{html.escape(comp_name)}</span></span>']
    for s in seasons:
        scope_badges.append(f'<span class="scope-badge">{html.escape(str(s))}</span>')

    delete_form = _experiment_delete_form(experiment_id, csrf_token)
    rerun_form = _experiment_rerun_form(
        experiment_id,
        csrf_token,
        disabled=status == "running",
    )

    total_runs = len(runs)
    completed_runs = sum(1 for r in runs if r["status"] == "completed")
    failed_runs = sum(1 for r in runs if r["status"] == "failed")
    max_eval = max((r.get("matches_evaluated") or 0) for r in runs) if runs else 0

    best_m = exp.get("best_metrics") or {}
    best_cfg = exp.get("best_config") or {}

    b_acc = best_m.get("full_time_1x2_accuracy")
    b_ll = best_m.get("full_time_1x2_log_loss")
    b_brier = best_m.get("full_time_1x2_brier")
    b_exact_hit, b_exact_ll = _exact_score_from_metrics(best_m)
    b_ou25_acc, _b_ou25_ll = _market_pair_from_metrics(best_m, "over_under_2_5")

    body.append(
        f'<section class="panel">'
        f'  <div class="exp-top-row">'
        f'    <div>'
        f'      <h2 style="margin:0 0 4px;"><code>{html.escape(experiment_id)}</code></h2>'
        f'      <div style="display:flex; align-items:center; gap:8px; margin-top:6px;">'
        f'        <span class="badge {status_badge}">{html.escape(status.title())}</span>'
        f'        <div class="scope-badges">{"".join(scope_badges)}</div>'
        f'      </div>'
        f'    </div>'
        f'    <div class="exp-actions">{rerun_form}{delete_form}</div>'
        f'  </div>'
        f'  <div class="cards" style="margin-top:16px;">'
        f'    <article><span>Total Runs</span><strong>{completed_runs}/{total_runs}</strong></article>'
        f'    <article><span>Evaluated (n)</span><strong>{max_eval}</strong></article>'
        f'    <article><span>Best FT Acc</span><strong style="color:var(--good);">{f"{b_acc:.1%}" if isinstance(b_acc, (int, float)) else "—"}</strong></article>'
        f'    <article><span>Best Log-Loss</span><strong>{f"{b_ll:.4f}" if isinstance(b_ll, (int, float)) else "—"}</strong></article>'
        f'    <article><span>Best Brier</span><strong>{f"{b_brier:.4f}" if isinstance(b_brier, (int, float)) else "—"}</strong></article>'
        f'    <article><span>Best Exact Hit %</span><strong class="mono">{_fmt_pct(b_exact_hit)}</strong></article>'
        f'    <article><span>Best Exact LL</span><strong class="mono">{_fmt_ll(b_exact_ll)}</strong></article>'
        f'    <article><span>Best O/U 2.5 Acc</span><strong class="mono">{_fmt_pct(b_ou25_acc)}</strong></article>'
        f'  </div>'
        f'</section>'
    )

    # Error Box (if failed)
    if exp.get("error") or status == "failed":
        err_text = exp.get("error") or "Execution failed without a detailed traceback."
        body.append(
            f'<div class="error-box">'
            f'  <h3>Failure Traceback</h3>'
            f'  <pre>{html.escape(err_text)}</pre>'
            f'</div>'
        )

    # Config Inspector Panel
    body.append(
        f'<section class="panel">'
        f'  <h2>Configuration & Search Grid Inspector</h2>'
        f'  <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap:16px; margin-top:12px;">'
        f'    <div>'
        f'      <h3 style="margin-top:0;">Base Configuration</h3>'
        f'      <pre style="background:var(--bg); padding:12px; border:1px solid var(--line); border-radius:var(--radius-sm); font-size:0.8rem; overflow-x:auto;">'
        f'{html.escape(json.dumps(base_cfg, indent=2))}'
        f'      </pre>'
        f'    </div>'
        f'    <div>'
        f'      <h3 style="margin-top:0;">Search Grid</h3>'
        f'      <pre style="background:var(--bg); padding:12px; border:1px solid var(--line); border-radius:var(--radius-sm); font-size:0.8rem; overflow-x:auto;">'
        f'{html.escape(json.dumps(param_grid, indent=2))}'
        f'      </pre>'
        f'    </div>'
        f'  </div>'
        f'</section>'
    )

    # Leaderboard Table
    if runs:
        # Baseline = best completed run on the mapped primary metric (among all n).
        completed = [r for r in runs if r["status"] == "completed" and r.get("metrics")]
        completed_sorted = sorted(
            completed,
            key=lambda r: metric_sort_value(
                r.get("metrics") or {},
                metric_key,
                higher_is_better=higher_is_better,
            ),
            reverse=higher_is_better,
        )
        from laboratory.lab_experiment import metric_value

        baseline = completed_sorted[0] if completed_sorted else None
        baseline_metric_val = (
            metric_value(baseline.get("metrics") or {}, metric_key) if baseline else None
        )

        # Column winners among completed runs (Exact + O/U 2.5 highlighted).
        win_exact_hit = max(
            (v for v in (_exact_score_from_metrics(r.get("metrics") or {})[0] for r in completed) if v is not None),
            default=None,
        )
        win_exact_ll = min(
            (v for v in (_exact_score_from_metrics(r.get("metrics") or {})[1] for r in completed) if v is not None),
            default=None,
        )
        win_ou25_acc = max(
            (
                v
                for v in (
                    _market_pair_from_metrics(r.get("metrics") or {}, "over_under_2_5")[0]
                    for r in completed
                )
                if v is not None
            ),
            default=None,
        )
        win_ou25_ll = min(
            (
                v
                for v in (
                    _market_pair_from_metrics(r.get("metrics") or {}, "over_under_2_5")[1]
                    for r in completed
                )
                if v is not None
            ),
            default=None,
        )

        table_rows = []
        rank = 0
        sorted_runs = sorted(
            runs,
            key=lambda r: (
                0 if r["status"] == "completed" else 1,
                (
                    -metric_sort_value(
                        r.get("metrics") or {},
                        metric_key,
                        higher_is_better=higher_is_better,
                    )
                    if higher_is_better
                    else metric_sort_value(
                        r.get("metrics") or {},
                        metric_key,
                        higher_is_better=higher_is_better,
                    )
                ),
            ),
        )

        for r in sorted_runs:
            rank += 1
            mid = r["model_id"]
            st = r["status"]
            m = r.get("metrics") or {}
            n = r.get("matches_evaluated") or 0
            err = r.get("error")
            below_floor = min_matches > 0 and n < min_matches

            acc = f"{m.get('full_time_1x2_accuracy'):.2%}" if "full_time_1x2_accuracy" in m else "—"
            ll = f"{m.get('full_time_1x2_log_loss'):.4f}" if "full_time_1x2_log_loss" in m else "—"
            br = f"{m.get('full_time_1x2_brier'):.4f}" if "full_time_1x2_brier" in m else "—"
            exact_hit, exact_ll = _exact_score_from_metrics(m)
            ht_hit, ht_ll = _market_pair_from_metrics(m, "half_time_1x2")
            ou25_hit, ou25_ll = _market_pair_from_metrics(m, "over_under_2_5")
            ou35_hit, ou35_ll = _market_pair_from_metrics(m, "over_under_3_5")

            # Delta calculation vs baseline on the mapped metric key
            delta_str = "—"
            cur_metric = metric_value(m, metric_key)
            if baseline_metric_val is not None and cur_metric is not None and r is not baseline:
                diff = float(cur_metric) - float(baseline_metric_val)
                improved = diff > 0 if higher_is_better else diff < 0
                cls = "good" if improved else "bad"
                delta_str = f'<span style="color:var(--{cls}); font-weight:600;">{diff:+.4f}</span>'
            elif r is baseline:
                delta_str = '<span class="badge muted">Baseline</span>'

            # Highlight changed params
            r_cfg = r.get("config") or {}
            diffs = [f"{k}={v}" for k, v in r_cfg.items() if base_cfg.get(k) != v and k != "model_id"]
            params_str = f"<code>{html.escape(', '.join(diffs))}</code>" if diffs else '<span class="muted">Default</span>'
            if below_floor:
                params_str += f' <span class="badge muted" title="Below min_matches={min_matches}">n&lt;{min_matches}</span>'

            st_badge = '<span class="badge good">Passed</span>' if st == "completed" else f'<span class="badge bad" title="{html.escape(err or "")}">Failed</span>' if st == "failed" else f'<span class="badge wait">{st}</span>'

            exact_hit_s = _fmt_pct(exact_hit)
            exact_ll_s = _fmt_ll(exact_ll)
            ou25_hit_s = _fmt_pct(ou25_hit)
            ou25_ll_s = _fmt_ll(ou25_ll)

            table_rows.append(
                f'<tr>'
                f'  <td>{rank}</td>'
                f'  <td><code>{html.escape(mid)}</code></td>'
                f'  <td>{st_badge}</td>'
                f'  <td>{params_str}</td>'
                f'  <td>{n}</td>'
                f'  <td style="font-weight:600;">{acc}</td>'
                f'  <td>{ll}</td>'
                f'  <td>{br}</td>'
                f'  <td>{_metric_cell(exact_hit_s, win=exact_hit is not None and win_exact_hit is not None and exact_hit == win_exact_hit)}</td>'
                f'  <td>{_metric_cell(exact_ll_s, win=exact_ll is not None and win_exact_ll is not None and exact_ll == win_exact_ll)}</td>'
                f'  <td>{_fmt_pct(ht_hit)}</td>'
                f'  <td>{_fmt_ll(ht_ll)}</td>'
                f'  <td>{_metric_cell(ou25_hit_s, win=ou25_hit is not None and win_ou25_acc is not None and ou25_hit == win_ou25_acc)}</td>'
                f'  <td>{_metric_cell(ou25_ll_s, win=ou25_ll is not None and win_ou25_ll is not None and ou25_ll == win_ou25_ll)}</td>'
                f'  <td>{_fmt_pct(ou35_hit)}</td>'
                f'  <td>{_fmt_ll(ou35_ll)}</td>'
                f'  <td>{delta_str}</td>'
                f'</tr>'
            )

        direction = "higher is better" if higher_is_better else "lower is better"
        floor_note = (
            f' Best config requires n≥{min_matches}.'
            if min_matches > 0
            else ""
        )
        body.append(
            f'<section class="panel">'
            f'  <h2>Runs Leaderboard (Ranked by {html.escape(metric_key)})</h2>'
            f'  <p class="note" style="margin-bottom:12px;">'
            f'UI metric “{html.escape(primary_metric)}” → <code>{html.escape(metric_key)}</code>'
            f' ({html.escape(direction)}).{html.escape(floor_note)}'
            f'  </p>'
            f'  <div class="table-wrap"><table>'
            f'    <thead><tr>'
            f'      <th>#</th><th>Model ID</th><th>Status</th><th>Tested Params</th>'
            f'      <th>n</th><th>FT Acc</th><th>Log-Loss</th><th>Brier</th>'
            f'      <th>Exact Hit %</th><th>Exact LL</th>'
            f'      <th>HT Acc %</th><th>HT Log-Loss</th>'
            f'      <th>O/U 2.5 Acc</th><th>O/U 2.5 LL</th>'
            f'      <th>Goals 3.5 Acc %</th><th>Goals 3.5 Log-Loss</th>'
            f'      <th>Δ vs Base</th>'
            f'    </tr></thead>'
            f'    <tbody>{"".join(table_rows)}</tbody>'
            f'  </table></div>'
            f'</section>'
        )

        # Visual Comparison Bars
        if len(completed) >= 2:
            bars_html = []
            best_ll = min((r["metrics"].get("full_time_1x2_log_loss") for r in completed if r["metrics"].get("full_time_1x2_log_loss")), default=1.0)
            for r in completed[:8]:
                mid = r["model_id"]
                cur_ll = r["metrics"].get("full_time_1x2_log_loss", 1.0)
                # Proportional width
                bar_pct = max(10.0, min(100.0, (best_ll / cur_ll) * 100.0))
                bars_html.append(
                    f'<div style="margin-bottom:8px;">'
                    f'  <div style="display:flex; justify-content:space-between; font-size:0.8rem; margin-bottom:2px;">'
                    f'    <code>{html.escape(mid)}</code>'
                    f'    <span class="mono">{cur_ll:.4f}</span>'
                    f'  </div>'
                    f'  <div style="height:10px; border-radius:999px; background:var(--line); overflow:hidden;">'
                    f'    <div style="height:100%; border-radius:999px; background:var(--accent); width:{bar_pct:.1f}%;"></div>'
                    f'  </div>'
                    f'</div>'
                )

            body.append(
                f'<section class="panel">'
                f'  <h2>Model Quality Relative Calibration (Log-Loss)</h2>'
                f'  <p class="note" style="margin-bottom:12px;">Top candidate models compared against the winning log-loss profile.</p>'
                f'  {"".join(bars_html)}'
                f'</section>'
            )

    # AI Diagnostic Prompt Card
    prompt_content = _ai_diagnostic_prompt(exp, runs)
    body.append(
        f'<section class="prompt-export-panel">'
        f'  <div class="prompt-header">'
        f'    <h3>Ready-to-Copy AI Diagnostic Prompt</h3>'
        f'    <button type="button" class="copy-prompt-btn" data-copy-target="ai-prompt-text">'
        f'      Copy Prompt for AI Analysis'
        f'    </button>'
        f'  </div>'
        f'  <p class="note" style="margin-bottom:12px;">'
        f'    Pre-populated with this experiment\'s parameters, sample sizes, and evaluated accuracy metrics. Paste into your LLM of choice for instant model critique.'
        f'  </p>'
        f'  <textarea id="ai-prompt-text" readonly>{html.escape(prompt_content)}</textarea>'
        f'</section>'
    )

    return _page(f"Experiment: {experiment_id}", "\n".join(body), filters, extra_head=extra_head)


def _default_filters() -> dict[str, str]:
    return {
        "engine": "score_model",
        "experiment": "BOT-PL-001",
        "league": "",
        "season": "",
        "team": "",
        "date_from": "",
        "date_to": "",
        "fixture": "all",
        "status": "all",
        "verdict": "all",
        "confidence": "all",
        "order": "newest",
        "page": "1",
        "predicted": "",
        "predict_status": "",
        "predict_detail": "",
        "run_status": "",
        "run_detail": "",
    }


def _filters(
    query: dict[str, list[str]] | None,
    _connection: sqlite3.Connection,
) -> dict[str, str]:
    source = query or {}

    def one(name: str, default: str = "") -> str:
        values = source.get(name) or []
        return values[0].strip() if values else default

    return {
        "engine": "score_model",
        "experiment": "BOT-PL-001",
        "league": one("league").upper(),
        "season": one("season"),
        "team": one("team"),
        "date_from": one("date_from"),
        "date_to": one("date_to"),
        "fixture": one("fixture", "all"),
        "status": one("status", "all"),
        "verdict": one("verdict", "all"),
        "confidence": one("confidence", "all"),
        "order": one("order", "newest"),
        "page": one("page", "1"),
        "predicted": one("predicted"),
        "predict_status": one("predict_status"),
        "predict_detail": one("predict_detail"),
        "run_status": one("run_status"),
        "run_detail": one("run_detail"),
        "sync_status": one("sync_status"),
        "sync_detail": one("sync_detail"),
    }


def _query(filters: dict[str, str], **overrides: str) -> str:
    data = dict(filters)
    data.update(overrides)
    kept: dict[str, str] = {}
    for key, value in data.items():
        if not value:
            continue
        if key in {"predicted", "predict_status", "predict_detail"}:
            continue
        if key in {"fixture", "status", "verdict", "confidence"} and value == "all":
            continue
        if key == "order" and value == "newest":
            continue
        if key == "page" and value in {"", "1"}:
            continue
        kept[key] = value
    return urlencode(kept)


def _page_number(filters: dict[str, str]) -> int:
    try:
        return max(1, int(filters["page"]))
    except ValueError:
        return 1


def _when(value: str) -> str:
    from core.timeutil import format_datetime_cairo

    formatted = format_datetime_cairo(value)
    return html.escape(formatted if formatted is not None else value)


def _when_short(value: str) -> str:
    from core.timeutil import format_datetime_short_cairo

    formatted = format_datetime_short_cairo(value)
    return html.escape(formatted if formatted is not None else value)


def _outcome(home: int, away: int) -> str:
    if home > away:
        return "home"
    if home == away:
        return "draw"
    return "away"


def _json(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    return json.loads(value)
