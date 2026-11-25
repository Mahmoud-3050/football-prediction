"""Automatic prospective live prediction log, kickoff lock, and settlement."""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_EPS = 1e-12
_SETTLE_WINDOW_SECONDS = 12 * 3600


def _parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _poisson_pmf(lam: float, k: int) -> float:
    if lam <= 0.0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def lock_started_predictions(
    conn: sqlite3.Connection,
    now_utc: str | datetime | None = None,
) -> int:
    """Transition any OPEN prospective rows whose kickoff_utc <= now_utc to LOCKED."""
    now_dt = _parse_utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT id, kickoff_utc FROM prospective_predictions WHERE status = 'OPEN'"
    ).fetchall()
    to_lock: list[tuple[int,]] = []
    for row in rows:
        try:
            kickoff_dt = _parse_utc(row["kickoff_utc"])
        except Exception:
            continue
        if kickoff_dt <= now_dt:
            to_lock.append((int(row["id"]),))
    if to_lock:
        conn.executemany(
            "UPDATE prospective_predictions SET status = 'LOCKED' WHERE id = ? AND status = 'OPEN'",
            to_lock,
        )
        conn.commit()
    return len(to_lock)


def record_prospective_prediction(
    conn: sqlite3.Connection,
    *,
    competition: str,
    home_team_id: str | int,
    away_team_id: str | int,
    home_team_name: str,
    away_team_name: str,
    kickoff_utc: str,
    source: str,
    model_id: str,
    stack_json: str,
    prob_home: float,
    prob_draw: float,
    prob_away: float,
    most_likely_score: str,
    most_likely_score_prob: float | None = None,
    prob_over_25: float | None = None,
    prob_under_25: float | None = None,
    display_pick: str | None = None,
    fixture_id: str | int | None = None,
    season: int | None = None,
    lambda_home: float | None = None,
    lambda_away: float | None = None,
    predicted_at_utc: str | datetime | None = None,
) -> int | None:
    """Record a pre-kickoff prediction; reject/lock if predicted_at_utc >= kickoff_utc."""
    kickoff_dt = _parse_utc(kickoff_utc)
    pred_dt = (
        _parse_utc(predicted_at_utc)
        if predicted_at_utc is not None
        else datetime.now(timezone.utc)
    )
    kickoff_iso = kickoff_dt.isoformat()
    pred_iso = pred_dt.isoformat()

    if pred_dt >= kickoff_dt:
        lock_started_predictions(conn, now_utc=pred_dt)
        logger.info(
            "prospective kickoff lock: skipped post-kickoff prediction %s %s vs %s (%s >= %s)",
            competition,
            home_team_name,
            away_team_name,
            pred_iso,
            kickoff_iso,
        )
        return None

    comp = str(competition).strip().upper()
    hid = str(home_team_id).strip()
    aid = str(away_team_id).strip()
    fid = None if fixture_id is None else str(fixture_id).strip()
    match_key = f"{comp}:{hid}:{aid}:{kickoff_iso}"

    existing = conn.execute(
        """
        SELECT id, status
        FROM prospective_predictions
        WHERE competition = ? AND home_team_id = ? AND away_team_id = ? AND kickoff_utc = ?
        """,
        (comp, hid, aid, kickoff_iso),
    ).fetchone()

    if existing is not None:
        if existing["status"] != "OPEN":
            logger.info(
                "prospective record id=%s is %s; refusing overwrite",
                existing["id"],
                existing["status"],
            )
            return None
        conn.execute(
            """
            UPDATE prospective_predictions
            SET fixture_id = COALESCE(?, fixture_id),
                match_key = ?,
                season = COALESCE(?, season),
                home_team_name = ?,
                away_team_name = ?,
                predicted_at_utc = ?,
                source = ?,
                model_id = ?,
                stack_json = ?,
                prob_home = ?,
                prob_draw = ?,
                prob_away = ?,
                prob_over_25 = ?,
                prob_under_25 = ?,
                most_likely_score = ?,
                most_likely_score_prob = ?,
                display_pick = ?,
                lambda_home = ?,
                lambda_away = ?
            WHERE id = ? AND status = 'OPEN'
            """,
            (
                fid,
                match_key,
                season,
                home_team_name,
                away_team_name,
                pred_iso,
                source,
                model_id,
                stack_json,
                float(prob_home),
                float(prob_draw),
                float(prob_away),
                None if prob_over_25 is None else float(prob_over_25),
                None if prob_under_25 is None else float(prob_under_25),
                str(most_likely_score),
                None if most_likely_score_prob is None else float(most_likely_score_prob),
                display_pick,
                None if lambda_home is None else float(lambda_home),
                None if lambda_away is None else float(lambda_away),
                int(existing["id"]),
            ),
        )
        conn.commit()
        return int(existing["id"])

    cursor = conn.execute(
        """
        INSERT INTO prospective_predictions (
            fixture_id, match_key, competition, season,
            home_team_id, away_team_id, home_team_name, away_team_name,
            kickoff_utc, predicted_at_utc, source, model_id, stack_json,
            prob_home, prob_draw, prob_away, prob_over_25, prob_under_25,
            most_likely_score, most_likely_score_prob, display_pick,
            lambda_home, lambda_away, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
        """,
        (
            fid,
            match_key,
            comp,
            season,
            hid,
            aid,
            home_team_name,
            away_team_name,
            kickoff_iso,
            pred_iso,
            source,
            model_id,
            stack_json,
            float(prob_home),
            float(prob_draw),
            float(prob_away),
            None if prob_over_25 is None else float(prob_over_25),
            None if prob_under_25 is None else float(prob_under_25),
            str(most_likely_score),
            None if most_likely_score_prob is None else float(most_likely_score_prob),
            display_pick,
            None if lambda_home is None else float(lambda_home),
            None if lambda_away is None else float(lambda_away),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def settle_prospective_predictions(
    conn: sqlite3.Connection,
    *,
    now_utc: str | datetime | None = None,
) -> dict[str, int]:
    """Lock started predictions and settle OPEN/LOCKED rows against finished matches."""
    now_dt = _parse_utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    locked_count = lock_started_predictions(conn, now_utc=now_dt)

    pending = conn.execute(
        """
        SELECT *
        FROM prospective_predictions
        WHERE status IN ('OPEN', 'LOCKED')
        ORDER BY kickoff_utc ASC, id ASC
        """
    ).fetchall()
    if not pending:
        return {"locked": locked_count, "settled": 0}

    # Latest finished revision per match from shared matches + result_revisions.
    finished_rows = conn.execute(
        """
        SELECT m.match_id, UPPER(m.competition) AS competition,
               m.home_id, m.away_id, m.kickoff,
               r.ft_home, r.ft_away
        FROM matches m
        JOIN result_revisions r ON r.match_id = m.match_id
        WHERE r.ft_home IS NOT NULL
          AND r.ft_away IS NOT NULL
          AND r.revision = (
              SELECT MAX(r2.revision)
              FROM result_revisions r2
              WHERE r2.match_id = m.match_id
          )
        """
    ).fetchall()

    by_match_id: dict[str, sqlite3.Row] = {}
    by_pair: dict[tuple[str, str, str], list[tuple[datetime, sqlite3.Row]]] = {}
    for frow in finished_rows:
        by_match_id[str(frow["match_id"])] = frow
        try:
            k_dt = _parse_utc(frow["kickoff"])
        except Exception:
            continue
        key = (str(frow["competition"]).upper(), str(frow["home_id"]), str(frow["away_id"]))
        by_pair.setdefault(key, []).append((k_dt, frow))

    settled_count = 0
    for prow in pending:
        matched: sqlite3.Row | None = None
        fid = prow["fixture_id"]
        if fid and str(fid) in by_match_id:
            matched = by_match_id[str(fid)]
        else:
            try:
                p_kick = _parse_utc(prow["kickoff_utc"])
            except Exception:
                continue
            key = (
                str(prow["competition"]).upper(),
                str(prow["home_team_id"]),
                str(prow["away_team_id"]),
            )
            candidates = by_pair.get(key) or []
            best_diff = _SETTLE_WINDOW_SECONDS + 1.0
            for c_kick, c_row in candidates:
                diff = abs((c_kick - p_kick).total_seconds())
                if diff <= _SETTLE_WINDOW_SECONDS and diff < best_diff:
                    best_diff = diff
                    matched = c_row

        if matched is None:
            continue

        hg = int(matched["ft_home"])
        ag = int(matched["ft_away"])
        outcome = "H" if hg > ag else ("D" if hg == ag else "A")

        p_h = max(_EPS, float(prow["prob_home"]))
        p_d = max(_EPS, float(prow["prob_draw"]))
        p_a = max(_EPS, float(prow["prob_away"]))
        total_p = p_h + p_d + p_a
        if total_p > 0.0:
            p_h, p_d, p_a = p_h / total_p, p_d / total_p, p_a / total_p

        p_actual = {"H": p_h, "D": p_d, "A": p_a}[outcome]
        ft_log_loss = -math.log(max(_EPS, p_actual))

        y_h = 1.0 if outcome == "H" else 0.0
        y_d = 1.0 if outcome == "D" else 0.0
        y_a = 1.0 if outcome == "A" else 0.0
        ft_brier = (p_h - y_h) ** 2 + (p_d - y_d) ** 2 + (p_a - y_a) ** 2

        exact_hit = 1 if f"{hg}-{ag}" == str(prow["most_likely_score"]).strip() else 0
        lam_h = prow["lambda_home"]
        lam_a = prow["lambda_away"]
        exact_ll: float | None = None
        if lam_h is not None and lam_a is not None and float(lam_h) > 0.0 and float(lam_a) > 0.0:
            p_score = _poisson_pmf(float(lam_h), hg) * _poisson_pmf(float(lam_a), ag)
            exact_ll = -math.log(max(_EPS, p_score))
        elif exact_hit == 1 and prow["most_likely_score_prob"] is not None:
            exact_ll = -math.log(max(_EPS, float(prow["most_likely_score_prob"])))

        conn.execute(
            """
            UPDATE prospective_predictions
            SET status = 'SETTLED',
                actual_home_goals = ?,
                actual_away_goals = ?,
                actual_outcome = ?,
                ft_1x2_log_loss = ?,
                ft_1x2_brier = ?,
                exact_score_log_loss = ?,
                exact_score_hit = ?,
                settled_at_utc = ?
            WHERE id = ?
            """,
            (
                hg,
                ag,
                outcome,
                ft_log_loss,
                ft_brier,
                exact_ll,
                exact_hit,
                now_iso,
                int(prow["id"]),
            ),
        )
        settled_count += 1

    if settled_count > 0:
        conn.commit()
    return {"locked": locked_count, "settled": settled_count}


def _empirical_hda_priors(
    conn: sqlite3.Connection,
    competition: str | None = None,
) -> tuple[float, float, float]:
    """Compute historical H/D/A frequencies from finished matches as a naive baseline."""
    sql = """
        SELECT r.ft_home, r.ft_away
        FROM matches m
        JOIN result_revisions r ON r.match_id = m.match_id
        WHERE r.ft_home IS NOT NULL AND r.ft_away IS NOT NULL
          AND r.revision = (
              SELECT MAX(r2.revision) FROM result_revisions r2 WHERE r2.match_id = m.match_id
          )
    """
    params: list[Any] = []
    if competition:
        sql += " AND UPPER(m.competition) = ?"
        params.append(competition.strip().upper())
    rows = conn.execute(sql, params).fetchall()
    if len(rows) < 10:
        return (0.45, 0.27, 0.28)
    h = sum(1 for r in rows if int(r["ft_home"]) > int(r["ft_away"]))
    d = sum(1 for r in rows if int(r["ft_home"]) == int(r["ft_away"]))
    a = len(rows) - h - d
    n = float(len(rows))
    return (max(_EPS, h / n), max(_EPS, d / n), max(_EPS, a / n))


def compute_prospective_summary(
    conn: sqlite3.Connection,
    competition: str | None = None,
    *,
    limit_recent: int = 25,
) -> dict[str, Any]:
    """Aggregate live prospective metrics separately from backtests."""
    where = ""
    params: list[Any] = []
    if competition:
        where = " WHERE UPPER(competition) = ?"
        params.append(competition.strip().upper())

    rows = conn.execute(
        f"SELECT * FROM prospective_predictions{where} ORDER BY kickoff_utc DESC, id DESC",
        params,
    ).fetchall()

    open_rows = [r for r in rows if r["status"] == "OPEN"]
    locked_rows = [r for r in rows if r["status"] == "LOCKED"]
    settled_rows = [r for r in rows if r["status"] == "SETTLED"]
    settled_count = len(settled_rows)

    acc: float | None = None
    ll: float | None = None
    brier: float | None = None
    exact_hit_rate: float | None = None
    exact_ll: float | None = None
    naive_uniform_ll: float | None = None
    naive_prior_ll: float | None = None

    if settled_count > 0:
        correct_1x2 = 0
        ll_vals: list[float] = []
        brier_vals: list[float] = []
        exact_hits = 0
        exact_ll_vals: list[float] = []

        p_prior = _empirical_hda_priors(conn, competition)
        prior_map = {"H": p_prior[0], "D": p_prior[1], "A": p_prior[2]}
        prior_ll_vals: list[float] = []

        for r in settled_rows:
            probs = {"H": float(r["prob_home"]), "D": float(r["prob_draw"]), "A": float(r["prob_away"])}
            pred_outcome = max(probs, key=lambda k: probs[k])
            if pred_outcome == r["actual_outcome"]:
                correct_1x2 += 1
            if r["ft_1x2_log_loss"] is not None:
                ll_vals.append(float(r["ft_1x2_log_loss"]))
            if r["ft_1x2_brier"] is not None:
                brier_vals.append(float(r["ft_1x2_brier"]))
            if r["exact_score_hit"]:
                exact_hits += 1
            if r["exact_score_log_loss"] is not None:
                exact_ll_vals.append(float(r["exact_score_log_loss"]))
            if r["actual_outcome"] in prior_map:
                prior_ll_vals.append(-math.log(max(_EPS, prior_map[str(r["actual_outcome"])])))

        acc = correct_1x2 / settled_count
        ll = sum(ll_vals) / len(ll_vals) if ll_vals else None
        brier = sum(brier_vals) / len(brier_vals) if brier_vals else None
        exact_hit_rate = exact_hits / settled_count
        exact_ll = sum(exact_ll_vals) / len(exact_ll_vals) if exact_ll_vals else None
        naive_uniform_ll = math.log(3.0)
        naive_prior_ll = sum(prior_ll_vals) / len(prior_ll_vals) if prior_ll_vals else None

    return {
        "competition": competition.strip().upper() if competition else "ALL",
        "total_count": len(rows),
        "open_count": len(open_rows),
        "locked_count": len(locked_rows),
        "pending_count": len(open_rows) + len(locked_rows),
        "settled_count": settled_count,
        "ft_1x2_accuracy": acc,
        "ft_1x2_log_loss": ll,
        "ft_1x2_brier": brier,
        "exact_score_hit_rate": exact_hit_rate,
        "exact_score_log_loss": exact_ll,
        "naive_uniform_log_loss": naive_uniform_ll,
        "naive_prior_log_loss": naive_prior_ll,
        "recent_predictions": [dict(r) for r in rows[:limit_recent]],
    }
