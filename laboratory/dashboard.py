"""Read-only laboratory dashboard. Defaults to the bot score_model experiment."""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from laboratory import BOT_EXPERIMENT_ID, EXPERIMENT_ID
from laboratory.football_data import COMPETITION_NAMES

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


def overview(connection: sqlite3.Connection, query: dict[str, list[str]] | None = None) -> str:
    filters = _filters(query, connection)
    loaded = _load(connection, filters["engine"], filters["experiment"])
    rows = _apply(loaded, filters)
    leagues, seasons, teams = _choices(loaded, filters.get("league", ""))
    body = [
        _masthead("Overview", filters, active="overview"),
        _jobs_panel(connection),
        _filter_form("/", filters, leagues, seasons, teams, submit="Update"),
    ]
    if not loaded:
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
    return _page("Overview", "\n".join(body), filters)


def _jobs_panel(connection: sqlite3.Connection) -> str:
    from laboratory.store import latest_job_runs

    try:
        runs = latest_job_runs(connection)
    except sqlite3.OperationalError:
        runs = []
    if not runs:
        return (
            '<section class="panel jobs-panel"><h2>Server jobs</h2>'
            '<p class="note">No job runs recorded yet. Start '
            "<code>python -m jobs</code> to price tomorrow and refresh standings.</p>"
            "</section>"
        )
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
            f"<strong class=\"job-{status}\">{html.escape(status)}</strong>"
            f"<p>{html.escape(when)} · {html.escape(detail)}</p>"
            "</article>"
        )
    return (
        '<section class="panel jobs-panel"><h2>Server jobs</h2>'
        f'<div class="job-cards">{"".join(cards)}</div></section>'
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
    from timeutil import format_clock_cairo

    return format_clock_cairo(value) or (value.strip() or "—")


def match_list(connection: sqlite3.Connection, query: dict[str, list[str]] | None = None) -> str:
    filters = _filters(query, connection)
    if not filters["date_from"] and not filters["date_to"]:
        filters["date_from"], filters["date_to"] = _default_match_date_range()
    loaded = _load(connection, filters["engine"], filters["experiment"])
    leagues, seasons, teams = _choices(loaded, filters.get("league", ""))
    rows = _apply(loaded, filters)
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(_page_number(filters), pages)
    start = (page - 1) * PAGE_SIZE
    window = rows[start : start + PAGE_SIZE]
    if not loaded:
        listing = _state(
            kind="empty",
            title="No matches in the laboratory yet",
            lead="Import competition history to populate this list.",
            action_href="/",
            action_label="Back to Overview",
        )
        toolbar = ""
        pager = ""
    elif not rows:
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
            f'<div class="toolbar"><p class="note">Showing '
            f"<strong>{len(window)}</strong> of <strong>{len(rows)}</strong>"
            f" · page {page} of {pages}</p></div>"
        )
        pager = _pager("/matches", filters, page, pages)
    body = [
        _masthead("Matches", filters, active="matches"),
        _filter_form("/matches", filters, leagues, seasons, teams, submit="Show matches"),
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
    snapshot = connection.execute(
        """
        SELECT payload_json FROM snapshots
        WHERE match_id = ? AND experiment_id = ?
        LIMIT 1
        """,
        (canonical_id, experiment_id),
    ).fetchone()
    if snapshot is None:
        snapshot = connection.execute(
            "SELECT payload_json FROM snapshots WHERE match_id = ? LIMIT 1",
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
                OR model_id IN ('bot-score-model-v1', 'bot-score-model-live')
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
    parts = [
        _masthead(
            f"{_club_label(match, 'home')} vs {_club_label(match, 'away')}",
            filters,
            active="matches",
            back_href=back,
        ),
        _scoreboard(match, result, connection),
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
        "order": one("order", "newest"),
        "page": one("page", "1"),
        "predicted": one("predicted"),
        "predict_status": one("predict_status"),
        "predict_detail": one("predict_detail"),
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

    from timeutil import cairo_today

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


def _load(connection: sqlite3.Connection, engine: str, experiment_id: str) -> list[dict[str, Any]]:
    if engine == "score_model":
        # Live bot view: PL stays on BOT-PL-001; other leagues use BOT-{code}-live.
        # Challenger variants (BOT-PL-001-k6, …) are intentionally excluded.
        rows = connection.execute(
            """
            SELECT m.match_id, m.competition, m.season, m.kickoff,
                   m.home_id, m.home_name, m.away_id, m.away_name,
                   r.ft_home, r.ft_away, r.ht_home, r.ht_away,
                   er.status AS engine_status,
                   ev.metrics_json AS metrics_json,
                   pr.body_json AS body_json
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
            ORDER BY m.kickoff DESC, m.match_id
            """,
            (BOT_EXPERIMENT_ID, BOT_EXPERIMENT_ID),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT m.match_id, m.competition, m.season, m.kickoff,
                   m.home_id, m.home_name, m.away_id, m.away_name,
                   r.ft_home, r.ft_away, r.ht_home, r.ht_away,
                   er.status AS engine_status,
                   ev.metrics_json AS metrics_json,
                   pr.body_json AS body_json
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
            ORDER BY m.kickoff DESC, m.match_id
            """,
            (engine, experiment_id, engine, experiment_id),
        ).fetchall()
    from laboratory.store import (
        competition_emblem_map,
        crest_url_for_team,
        team_crest_map,
        team_display_map,
    )

    loaded = []
    team_ids = {str(row["home_id"]) for row in rows} | {str(row["away_id"]) for row in rows}
    league_codes = {str(row["competition"] or "PL").upper() for row in rows}
    crests = team_crest_map(connection, team_ids)
    displays = team_display_map(connection, team_ids)
    emblems = competition_emblem_map(connection, league_codes)
    for row in rows:
        item = dict(row)
        item["metrics"] = _json(row["metrics_json"])
        item["body"] = _json(row["body_json"])
        home_id = str(row["home_id"])
        away_id = str(row["away_id"])
        code = str(row["competition"] or "PL").upper()
        item["home_crest"] = crests.get(home_id) or crest_url_for_team(home_id)
        item["away_crest"] = crests.get(away_id) or crest_url_for_team(away_id)
        item["home_display"] = displays.get(home_id) or str(row["home_name"])
        item["away_display"] = displays.get(away_id) or str(row["away_name"])
        item["league_emblem"] = emblems.get(code)
        loaded.append(item)
    return loaded


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
        chosen.append(row)
    if filters["order"] == "oldest":
        chosen.sort(key=lambda item: (item["kickoff"], item["match_id"]))
    return chosen


def _choices(
    rows: list[dict[str, Any]],
    league: str = "",
) -> tuple[list[str], list[str], list[str]]:
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
        pick = "—"
        predicted = "—"
        body = row.get("body") or {}
        if body.get("engine") in ("poisson", "score_model"):
            market = body.get("full_time", {}).get("result_1x2") or {}
            mode = body.get("full_time", {}).get("exact_score_most_likely") or {}
            if market.get("most_likely"):
                pick = str(market["most_likely"]).replace("_", " ").title()
            if mode:
                predicted = f"{mode.get('home_goals')}–{mode.get('away_goals')}"
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
            f'<div class="match-sub">'
            f'<span class="match-meta">'
            f'<span class="league-chip">{league_mark}{league}</span>'
            f"<span>{_when_short(row['kickoff'])}</span>"
            f'<span class="pick">1X2 · {html.escape(pick)}</span>'
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
    home_crest = crest_url_for_team(home_id)
    away_crest = crest_url_for_team(away_id)
    home_label = _club_label(match, "home")
    away_label = _club_label(match, "away")
    league_emblem = emblem_url_for_competition(code)
    if connection is not None:
        crests = team_crest_map(connection, [home_id, away_id])
        displays = team_display_map(connection, [home_id, away_id])
        emblems = competition_emblem_map(connection, [code])
        home_crest = crests.get(home_id) or home_crest
        away_crest = crests.get(away_id) or away_crest
        home_label = displays.get(home_id) or home_label
        away_label = displays.get(away_id) or away_label
        league_emblem = emblems.get(code) or league_emblem

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
    payload = json.loads(snapshot["payload_json"])
    leakage = "passed" if payload.get("leakage_ok") else "failed"
    return f"""
    <section class="cards compact">
      <article><span>Prior matches</span><strong>{payload['coverage']['prior_full_time_matches']}</strong></article>
      <article><span>Prior HT samples</span><strong>{payload['coverage']['prior_half_time_matches']}</strong></article>
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
        "<tr><th>Market</th><th>Pick</th><th>Actual</th>"
        "<th>Check</th><th>Log loss</th><th>Brier</th></tr>"
    ]
    for key, (label, actual) in labels.items():
        market = (metrics or {}).get(key) or {}
        if market.get("status") != "scored":
            rows.append(
                f"<tr><td>{label}</td><td>—</td><td>{html.escape(str(actual or '—'))}</td>"
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
            _prob_row("Home", market["home"])
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
        lines = [f"<p>{html.escape(str(body.get('analysis') or 'No nested markets.'))}</p>"]
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
        return (
            f'<article class="ou-card{" is-over" if lean_over else " is-under"}" '
            f'aria-label="Over {html.escape(line)}">'
            f'<div class="ou-top">'
            f'<span class="ou-label">Goals {html.escape(line)}</span>'
            f'<span class="ou-chip">{html.escape(lean)} lean</span>'
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
        zone = _standings_zone(position, total)
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
            '<span class="swatch europe"></span> European places · '
            '<span class="swatch drop"></span> Relegation zone'
            "</p>"
        ),
    ]
    return _page("Standings", "\n".join(body), filters)


def _standings_zone(position: int, total: int) -> str:
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
        {logout}
      </nav>
      <div class="title-row">{back}<h1>{html.escape(title)}</h1></div>
    </header>
    """


def _page(title: str, body: str, filters: dict[str, str] | None = None) -> str:
    _ = filters
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
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
    table.standings tr:nth-child(even):not(.zone-europe):not(.zone-drop) td {{
      background: rgba(255, 255, 255, 0.02);
    }}
    table.standings tr.zone-europe td {{
      background: rgba(52, 211, 153, 0.14);
    }}
    table.standings tr.zone-drop td {{
      background: rgba(251, 113, 133, 0.14);
    }}
    table.standings tr:hover td {{ background: rgba(52, 211, 153, 0.08); }}
    table.standings tr.zone-europe:hover td {{ background: rgba(52, 211, 153, 0.2); }}
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
      const button = event.target.closest("[data-copy]");
      if (!(button instanceof HTMLButtonElement)) return;
      const value = button.getAttribute("data-copy") || "";
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
        button.setAttribute("aria-label", "Copied");
        button.title = "Copied";
        window.clearTimeout(button._copyTimer);
        button._copyTimer = window.setTimeout(() => {{
          button.classList.remove("is-copied");
          button.setAttribute("aria-label", "Copy match id");
          button.title = "Copy";
        }}, 1400);
      }} catch (_error) {{
        button.title = "Copy failed";
      }}
    }});
  </script>
</body>
</html>
"""


def _query(filters: dict[str, str], **overrides: str) -> str:
    data = dict(filters)
    data.update(overrides)
    kept: dict[str, str] = {}
    for key, value in data.items():
        if not value:
            continue
        if key in {"predicted", "predict_status", "predict_detail"}:
            continue
        if key in {"fixture", "status", "verdict"} and value == "all":
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
    from timeutil import format_datetime_cairo

    formatted = format_datetime_cairo(value)
    return html.escape(formatted if formatted is not None else value)


def _when_short(value: str) -> str:
    from timeutil import format_datetime_short_cairo

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
