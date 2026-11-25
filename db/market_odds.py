"""Market Benchmark (Closing Odds), Closing Line Value (CLV), and Simulated ROI diagnostics."""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from db.team_mapping import resolve_team_id

_EPS = 1e-12
_MATCH_WINDOW_SECONDS = 24 * 3600

DISCLAIMER_AR = "أداة قياس علمي لأداء النموذج مقابل السوق فقط — وليست توصية أو نصيحة مراهنة بأي حال."


def remove_vig_1x2(
    odds_h: float,
    odds_d: float,
    odds_a: float,
) -> tuple[float, float, float]:
    """Convert decimal 1X2 odds (> 1.0) into vig-free fair probabilities summing to 1.0."""
    vals = (float(odds_h), float(odds_d), float(odds_a))
    for v in vals:
        if not math.isfinite(v) or v <= 1.0:
            raise ValueError(f"Decimal odds must be finite and > 1.0, got {vals!r}")
    inv = (1.0 / vals[0], 1.0 / vals[1], 1.0 / vals[2])
    overround = inv[0] + inv[1] + inv[2]
    return (inv[0] / overround, inv[1] / overround, inv[2] / overround)


def _parse_utc(value: str) -> datetime:
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    if len(raw) == 10 and "/" in raw:
        # dd/mm/yyyy or dd/mm/yy (football-data.co.uk format)
        parts = raw.split("/")
        day, mon, yr = int(parts[0]), int(parts[1]), int(parts[2])
        if yr < 100:
            yr += 2000
        return datetime(yr, mon, day, 15, 0, tzinfo=timezone.utc)
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _first_float(row: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for k in keys:
        raw = row.get(k)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(val) and val > 1.0:
            return val
    return None


def import_closing_odds_csv(
    conn: sqlite3.Connection,
    file_path: str | Path,
    *,
    source: str = "football_data_co_uk",
    competition: str,
    season: int,
) -> dict[str, int]:
    """Parse a closing-odds CSV, strictly resolve teams via team_mapping, and upsert match_odds."""
    src = str(source or "").strip().lower()
    comp = str(competition or "").strip().upper()
    season_int = int(season)
    target = Path(file_path)
    now_iso = datetime.now(timezone.utc).isoformat()

    rows_to_upsert: list[tuple[Any, ...]] = []
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            home_raw = str(
                row.get("HomeTeam") or row.get("home_team") or row.get("home") or ""
            ).strip()
            away_raw = str(
                row.get("AwayTeam") or row.get("away_team") or row.get("away") or ""
            ).strip()
            if not home_raw or not away_raw:
                continue

            date_raw = str(
                row.get("kickoff_utc") or row.get("kickoff") or row.get("Date") or ""
            ).strip()
            time_raw = str(row.get("Time") or "").strip()
            if not date_raw:
                raise ValueError(f"Missing Date/kickoff_utc in odds row: {dict(row)!r}")
            kick_dt = _parse_utc(date_raw)
            if time_raw and ":" in time_raw and "/" in date_raw:
                hh, mm = time_raw.split(":", 1)
                kick_dt = kick_dt.replace(hour=int(hh), minute=int(mm))
            kick_iso = kick_dt.isoformat()

            close_h = _first_float(row, ("close_home_odds", "PSCH", "B365CH", "AvgCH"))
            close_d = _first_float(row, ("close_draw_odds", "PSCD", "B365CD", "AvgCD"))
            close_a = _first_float(row, ("close_away_odds", "PSCA", "B365CA", "AvgCA"))
            if close_h is None or close_d is None or close_a is None:
                raise ValueError(f"Missing valid closing 1X2 odds (>1.0) in row: {dict(row)!r}")

            open_h = _first_float(row, ("open_home_odds", "PSH", "B365H", "AvgH"))
            open_d = _first_float(row, ("open_draw_odds", "PSD", "B365D", "AvgD"))
            open_a = _first_float(row, ("open_away_odds", "PSA", "B365A", "AvgA"))

            home_id = resolve_team_id(conn, provider=src, raw_name=home_raw, competition=comp)
            away_id = resolve_team_id(conn, provider=src, raw_name=away_raw, competition=comp)

            rows_to_upsert.append(
                (
                    comp,
                    season_int,
                    kick_iso,
                    str(home_id),
                    str(away_id),
                    src,
                    open_h,
                    open_d,
                    open_a,
                    close_h,
                    close_d,
                    close_a,
                    now_iso,
                )
            )

    if rows_to_upsert:
        conn.executemany(
            """
            INSERT INTO match_odds (
                competition, season, kickoff_utc,
                home_team_id, away_team_id, source,
                open_home_odds, open_draw_odds, open_away_odds,
                close_home_odds, close_draw_odds, close_away_odds,
                imported_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(competition, season, home_team_id, away_team_id, source)
            DO UPDATE SET
                kickoff_utc = excluded.kickoff_utc,
                open_home_odds = excluded.open_home_odds,
                open_draw_odds = excluded.open_draw_odds,
                open_away_odds = excluded.open_away_odds,
                close_home_odds = excluded.close_home_odds,
                close_draw_odds = excluded.close_draw_odds,
                close_away_odds = excluded.close_away_odds,
                imported_at_utc = excluded.imported_at_utc
            """,
            rows_to_upsert,
        )
        conn.commit()
    return {"imported": len(rows_to_upsert)}


def _score_bucket(
    samples: list[dict[str, Any]],
    *,
    edge_threshold: float,
) -> dict[str, Any]:
    n = len(samples)
    if n == 0:
        return {
            "n_matched": 0,
            "model_log_loss": None,
            "closing_market_log_loss": None,
            "delta_log_loss": None,
            "model_brier": None,
            "closing_market_brier": None,
            "mean_clv": None,
            "positive_clv_rate": None,
            "bets_count": 0,
            "hits": 0,
            "hit_rate": None,
            "net_units": 0.0,
            "roi_pct": None,
        }

    m_lls: list[float] = []
    c_lls: list[float] = []
    m_briers: list[float] = []
    c_briers: list[float] = []
    clvs: list[float] = []
    pos_clv = 0
    bets_count = 0
    hits = 0
    net_units = 0.0

    for s in samples:
        mp = s["model_probs"]
        cp = s["close_probs"]
        co = s["close_odds"]
        y = int(s["outcome_idx"])

        m_lls.append(-math.log(max(_EPS, mp[y])))
        c_lls.append(-math.log(max(_EPS, cp[y])))

        m_briers.append(sum((mp[i] - (1.0 if i == y else 0.0)) ** 2 for i in range(3)))
        c_briers.append(sum((cp[i] - (1.0 if i == y else 0.0)) ** 2 for i in range(3)))

        sel_idx = max(range(3), key=lambda i: mp[i])
        clv = mp[sel_idx] - cp[sel_idx]
        clvs.append(clv)
        if clv > 0.0:
            pos_clv += 1

        # Flat 1-unit diagnostic stake on the side with largest positive edge >= edge_threshold.
        edges = [mp[i] - cp[i] for i in range(3)]
        best_i = max(range(3), key=lambda i: edges[i])
        if edges[best_i] >= edge_threshold and co[best_i] > 1.0:
            bets_count += 1
            if best_i == y:
                hits += 1
                net_units += co[best_i] - 1.0
            else:
                net_units -= 1.0

    m_ll = sum(m_lls) / n
    c_ll = sum(c_lls) / n
    roi_pct = (net_units / bets_count) * 100.0 if bets_count > 0 else 0.0
    hit_rate = (hits / bets_count) if bets_count > 0 else None

    return {
        "n_matched": n,
        "model_log_loss": m_ll,
        "closing_market_log_loss": c_ll,
        "delta_log_loss": m_ll - c_ll,
        "model_brier": sum(m_briers) / n,
        "closing_market_brier": sum(c_briers) / n,
        "mean_clv": sum(clvs) / n,
        "positive_clv_rate": pos_clv / n,
        "bets_count": bets_count,
        "hits": hits,
        "hit_rate": hit_rate,
        "net_units": net_units,
        "roi_pct": roi_pct,
    }


def evaluate_market_edge(
    conn: sqlite3.Connection,
    *,
    competition: str | None = None,
    min_sample: int = 100,
    edge_threshold: float = 0.03,
) -> dict[str, Any]:
    """Compare model predictions against vig-free closing odds across Prospective & Backtest."""
    comp_filter = competition.strip().upper() if competition else None

    odds_sql = "SELECT * FROM match_odds"
    odds_params: list[Any] = []
    if comp_filter:
        odds_sql += " WHERE UPPER(competition) = ?"
        odds_params.append(comp_filter)
    try:
        odds_rows = conn.execute(odds_sql, odds_params).fetchall()
    except sqlite3.OperationalError:
        odds_rows = []

    odds_by_pair: dict[tuple[str, str, str], list[tuple[datetime, sqlite3.Row]]] = {}
    for o in odds_rows:
        try:
            k_dt = _parse_utc(o["kickoff_utc"])
        except Exception:
            continue
        key = (str(o["competition"]).upper(), str(o["home_team_id"]), str(o["away_team_id"]))
        odds_by_pair.setdefault(key, []).append((k_dt, o))

    def _find_odds(comp: str, hid: str, aid: str, kickoff: str) -> sqlite3.Row | None:
        cands = odds_by_pair.get((comp, hid, aid)) or []
        if not cands:
            return None
        try:
            m_dt = _parse_utc(kickoff)
        except Exception:
            return None
        best_diff = _MATCH_WINDOW_SECONDS + 1.0
        best_row: sqlite3.Row | None = None
        for o_dt, o_row in cands:
            diff = abs((o_dt - m_dt).total_seconds())
            if diff <= _MATCH_WINDOW_SECONDS and diff < best_diff:
                best_diff = diff
                best_row = o_row
        return best_row

    # 1. Live Prospective settled predictions
    prospective_samples: list[dict[str, Any]] = []
    try:
        p_sql = "SELECT * FROM prospective_predictions WHERE status = 'SETTLED'"
        p_params: list[Any] = []
        if comp_filter:
            p_sql += " AND UPPER(competition) = ?"
            p_params.append(comp_filter)
        p_rows = conn.execute(p_sql, p_params).fetchall()
    except sqlite3.OperationalError:
        p_rows = []

    for r in p_rows:
        if r["actual_outcome"] not in ("H", "D", "A"):
            continue
        o_row = _find_odds(
            str(r["competition"]).upper(),
            str(r["home_team_id"]),
            str(r["away_team_id"]),
            str(r["kickoff_utc"]),
        )
        if o_row is None:
            continue
        co = (
            float(o_row["close_home_odds"]),
            float(o_row["close_draw_odds"]),
            float(o_row["close_away_odds"]),
        )
        cp = remove_vig_1x2(*co)
        mp = (float(r["prob_home"]), float(r["prob_draw"]), float(r["prob_away"]))
        y = {"H": 0, "D": 1, "A": 2}[str(r["actual_outcome"])]
        prospective_samples.append(
            {"model_probs": mp, "close_probs": cp, "close_odds": co, "outcome_idx": y}
        )

    # 2. Backtest predictions (score_model)
    backtest_samples: list[dict[str, Any]] = []
    try:
        b_sql = """
            SELECT m.match_id, UPPER(m.competition) AS competition,
                   m.home_id, m.away_id, m.kickoff,
                   p.body_json, r.ft_home, r.ft_away
            FROM predictions p
            JOIN matches m ON m.match_id = p.match_id
            JOIN result_revisions r ON r.match_id = m.match_id
            WHERE p.engine = 'score_model'
              AND r.ft_home IS NOT NULL AND r.ft_away IS NOT NULL
              AND r.revision = (
                  SELECT MAX(r2.revision) FROM result_revisions r2 WHERE r2.match_id = m.match_id
              )
        """
        b_params: list[Any] = []
        if comp_filter:
            b_sql += " AND UPPER(m.competition) = ?"
            b_params.append(comp_filter)
        b_sql += " ORDER BY p.created_at DESC"
        b_rows = conn.execute(b_sql, b_params).fetchall()
    except sqlite3.OperationalError:
        b_rows = []

    seen_match_ids: set[str] = set()
    for r in b_rows:
        mid = str(r["match_id"])
        if mid in seen_match_ids:
            continue
        o_row = _find_odds(
            str(r["competition"]).upper(),
            str(r["home_id"]),
            str(r["away_id"]),
            str(r["kickoff"]),
        )
        if o_row is None:
            continue
        body = json.loads(r["body_json"])
        r1x2 = ((body.get("full_time") or {}).get("result_1x2") or {})
        if not all(k in r1x2 for k in ("home", "draw", "away")):
            continue
        seen_match_ids.add(mid)
        co = (
            float(o_row["close_home_odds"]),
            float(o_row["close_draw_odds"]),
            float(o_row["close_away_odds"]),
        )
        cp = remove_vig_1x2(*co)
        mp = (float(r1x2["home"]), float(r1x2["draw"]), float(r1x2["away"]))
        hg, ag = int(r["ft_home"]), int(r["ft_away"])
        y = 0 if hg > ag else (1 if hg == ag else 2)
        backtest_samples.append(
            {"model_probs": mp, "close_probs": cp, "close_odds": co, "outcome_idx": y}
        )

    prosp_stats = _score_bucket(prospective_samples, edge_threshold=edge_threshold)
    backtest_stats = _score_bucket(backtest_samples, edge_threshold=edge_threshold)
    combined_stats = _score_bucket(
        prospective_samples + backtest_samples,
        edge_threshold=edge_threshold,
    )

    n_matched = int(combined_stats["n_matched"])
    if n_matched < min_sample:
        verdict = "INSUFFICIENT_SAMPLE"
        verdict_ar = f"العينة غير كافية بعد — يلزم {min_sample} مباراة على الأقل"
    elif (
        combined_stats["model_log_loss"] is not None
        and combined_stats["closing_market_log_loss"] is not None
        and combined_stats["model_log_loss"] < combined_stats["closing_market_log_loss"]
        and (combined_stats["mean_clv"] or 0.0) > 0.0
        and (combined_stats["roi_pct"] or 0.0) > 0.0
    ):
        verdict = "EDGE_DETECTED"
        verdict_ar = "يوجد مؤشر تفوق إحصائي (Edge) على سعر الإغلاق"
    else:
        verdict = "NO_EDGE"
        verdict_ar = "لا يوجد تفوق إحصائي على سعر إغلاق السوق — السوق أكفأ حالياً"

    return {
        "competition": comp_filter or "ALL",
        "min_sample": int(min_sample),
        "edge_threshold": float(edge_threshold),
        "verdict": verdict,
        "verdict_ar": verdict_ar,
        "disclaimer": DISCLAIMER_AR,
        "assumptions": (
            f"Flat 1.0-unit simulated stake when (model_prob - vig_free_closing_prob) >= {edge_threshold:.0%}; "
            "closing odds vig removed proportionally (sum of implied probs = 1.0)."
        ),
        "combined": combined_stats,
        "prospective": prosp_stats,
        "backtest": backtest_stats,
    }
