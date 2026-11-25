"""Comprehensive percentage-based live prediction analytics (Prospective vs. Retrospective)."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from db.market_odds import remove_vig_1x2
from laboratory import LOG_LOSS_FLOOR
from modeling.league_stacks import live_stack_for

_MATCH_WINDOW_SECONDS = 24 * 3600
_VALUE_EDGE_THRESHOLD = 0.03

RECORD_TYPES = ("retrospective", "prospective", "all")

SORTABLE_LEAGUE_COLUMNS = {
    "competition",
    "total",
    "ft_1x2_pct",
    "exact_top1_pct",
    "exact_top3_pct",
    "double_chance_pct",
    "ou_25_pct",
    "btts_pct",
    "high_conf_pct",
    "beat_closing_pct",
    "last_20_pct",
}


def _parse_utc(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _poisson_pmf(lam: float, k: int) -> float:
    if lam <= 0.0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _poisson_score_grid(lambda_home: float, lambda_away: float, max_goals: int = 8) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for hg in range(max_goals + 1):
        ph = _poisson_pmf(lambda_home, hg)
        for ag in range(max_goals + 1):
            cells.append(
                {
                    "home_goals": hg,
                    "away_goals": ag,
                    "probability": ph * _poisson_pmf(lambda_away, ag),
                }
            )
    return cells


def _pct_stat(hits: int, total: int) -> dict[str, Any]:
    return {
        "hits": int(hits),
        "total": int(total),
        "pct": (hits / total * 100.0) if total > 0 else None,
    }


def _active_model_id_for(competition: str) -> str | None:
    try:
        stack = live_stack_for(competition)
        mid = str(stack.get("model_id") or "").strip()
        return mid or None
    except Exception:
        return None


def _load_odds_index(
    conn: sqlite3.Connection,
    *,
    competition: str | None = None,
) -> dict[tuple[str, str, str], list[tuple[datetime, sqlite3.Row]]]:
    sql = "SELECT * FROM match_odds"
    params: list[Any] = []
    if competition:
        sql += " WHERE UPPER(competition) = ?"
        params.append(competition.upper())
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return {}
    index: dict[tuple[str, str, str], list[tuple[datetime, sqlite3.Row]]] = {}
    for row in rows:
        dt = _parse_utc(row["kickoff_utc"])
        if dt is None:
            continue
        key = (
            str(row["competition"]).upper(),
            str(row["home_team_id"]),
            str(row["away_team_id"]),
        )
        index.setdefault(key, []).append((dt, row))
    return index


def _find_closing_odds(
    odds_index: dict[tuple[str, str, str], list[tuple[datetime, sqlite3.Row]]],
    *,
    competition: str,
    home_id: str,
    away_id: str,
    kickoff: str,
) -> tuple[float, float, float] | None:
    cands = odds_index.get((competition.upper(), str(home_id), str(away_id))) or []
    if not cands:
        return None
    m_dt = _parse_utc(kickoff)
    if m_dt is None:
        return None
    best_diff = _MATCH_WINDOW_SECONDS + 1.0
    best_row: sqlite3.Row | None = None
    for o_dt, o_row in cands:
        diff = abs((o_dt - m_dt).total_seconds())
        if diff <= _MATCH_WINDOW_SECONDS and diff < best_diff:
            best_diff = diff
            best_row = o_row
    if best_row is None:
        return None
    try:
        ch = float(best_row["close_home_odds"])
        cd = float(best_row["close_draw_odds"])
        ca = float(best_row["close_away_odds"])
        if ch > 1.0 and cd > 1.0 and ca > 1.0:
            return (ch, cd, ca)
    except (TypeError, ValueError):
        return None
    return None


def _parse_score_str(score: str | None) -> tuple[int, int] | None:
    raw = str(score or "").strip()
    for sep in ("-", ":", "–"):
        if sep in raw:
            parts = raw.split(sep, 1)
            try:
                return (int(parts[0].strip()), int(parts[1].strip()))
            except ValueError:
                return None
    return None


def _extract_retrospective_samples(
    conn: sqlite3.Connection,
    *,
    competition: str | None = None,
    season: str | None = None,
    cutoff_utc: datetime | None = None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT m.match_id, UPPER(m.competition) AS competition, m.season,
               m.kickoff, m.home_id, m.away_id,
               p.model_id, p.body_json, p.created_at,
               r.ft_home, r.ft_away
        FROM predictions p
        JOIN matches m ON m.match_id = p.match_id
        JOIN result_revisions r ON r.match_id = m.match_id
        WHERE p.experiment_id LIKE 'BOT-%-live'
          AND p.engine = 'score_model'
          AND r.ft_home IS NOT NULL AND r.ft_away IS NOT NULL
          AND r.revision = (
              SELECT MAX(r2.revision) FROM result_revisions r2 WHERE r2.match_id = m.match_id
          )
    """
    params: list[Any] = []
    if competition:
        sql += " AND UPPER(m.competition) = ?"
        params.append(competition.upper())
    if season and season not in ("all", "last_30d"):
        sql += " AND CAST(m.season AS TEXT) = ?"
        params.append(str(season))
    sql += " ORDER BY m.kickoff ASC, p.created_at DESC"

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []

    # Group by match_id and select active live model_id if present, else most recent created_at.
    active_cache: dict[str, str | None] = {}
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if cutoff_utc is not None:
            k_dt = _parse_utc(row["kickoff"])
            if k_dt is None or k_dt < cutoff_utc:
                continue
        grouped.setdefault(str(row["match_id"]), []).append(row)

    samples: list[dict[str, Any]] = []
    for mid, cands in grouped.items():
        comp = str(cands[0]["competition"]).upper()
        if comp not in active_cache:
            active_cache[comp] = _active_model_id_for(comp)
        active_mid = active_cache[comp]
        chosen = next((r for r in cands if active_mid and r["model_id"] == active_mid), cands[0])

        try:
            body = json.loads(chosen["body_json"])
        except Exception:
            continue
        ft = body.get("full_time") or {}
        r1x2 = ft.get("result_1x2") or {}
        if not all(k in r1x2 for k in ("home", "draw", "away")):
            continue

        ph = float(r1x2["home"])
        pd_ = float(r1x2["draw"])
        pa = float(r1x2["away"])

        exact_scores = ft.get("exact_scores")
        if not isinstance(exact_scores, list) or not exact_scores:
            lh = ft.get("lambda_home")
            la = ft.get("lambda_away")
            if isinstance(lh, (int, float)) and isinstance(la, (int, float)):
                exact_scores = _poisson_score_grid(float(lh), float(la))
            else:
                exact_scores = None

        top1_score: tuple[int, int] | None = None
        ml = ft.get("exact_score_most_likely")
        if isinstance(ml, dict) and "home_goals" in ml and "away_goals" in ml:
            top1_score = (int(ml["home_goals"]), int(ml["away_goals"]))

        ou25 = ft.get("over_under_2_5") or {}
        p_over_25 = float(ou25["over"]) if isinstance(ou25.get("over"), (int, float)) else None

        samples.append(
            {
                "match_id": mid,
                "is_prospective": False,
                "competition": comp,
                "season": str(chosen["season"]),
                "kickoff": str(chosen["kickoff"]),
                "home_id": str(chosen["home_id"]),
                "away_id": str(chosen["away_id"]),
                "model_id": str(chosen["model_id"]),
                "p_home": ph,
                "p_draw": pd_,
                "p_away": pa,
                "p_over_25": p_over_25,
                "top1_score": top1_score,
                "exact_scores": exact_scores,
                "actual_hg": int(chosen["ft_home"]),
                "actual_ag": int(chosen["ft_away"]),
            }
        )
    return samples


def _extract_prospective_samples(
    conn: sqlite3.Connection,
    *,
    competition: str | None = None,
    season: str | None = None,
    cutoff_utc: datetime | None = None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT id, UPPER(competition) AS competition, season, kickoff_utc,
               home_team_id, away_team_id, model_id,
               prob_home, prob_draw, prob_away, prob_over_25,
               most_likely_score, lambda_home, lambda_away,
               actual_home_goals, actual_away_goals
        FROM prospective_predictions
        WHERE status = 'SETTLED'
          AND actual_home_goals IS NOT NULL
          AND actual_away_goals IS NOT NULL
    """
    params: list[Any] = []
    if competition:
        sql += " AND UPPER(competition) = ?"
        params.append(competition.upper())
    if season and season not in ("all", "last_30d"):
        sql += " AND CAST(season AS TEXT) = ?"
        params.append(str(season))
    sql += " ORDER BY kickoff_utc ASC"

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []

    samples: list[dict[str, Any]] = []
    for row in rows:
        if cutoff_utc is not None:
            k_dt = _parse_utc(row["kickoff_utc"])
            if k_dt is None or k_dt < cutoff_utc:
                continue
        lh = row["lambda_home"]
        la = row["lambda_away"]
        exact_scores = (
            _poisson_score_grid(float(lh), float(la))
            if isinstance(lh, (int, float)) and isinstance(la, (int, float))
            else None
        )
        top1_score = _parse_score_str(row["most_likely_score"])
        p_over_25 = float(row["prob_over_25"]) if isinstance(row["prob_over_25"], (int, float)) else None
        samples.append(
            {
                "match_id": f"prospective:{row['id']}",
                "is_prospective": True,
                "competition": str(row["competition"]).upper(),
                "season": str(row["season"] or ""),
                "kickoff": str(row["kickoff_utc"]),
                "home_id": str(row["home_team_id"]),
                "away_id": str(row["away_team_id"]),
                "model_id": str(row["model_id"]),
                "p_home": float(row["prob_home"]),
                "p_draw": float(row["prob_draw"]),
                "p_away": float(row["prob_away"]),
                "p_over_25": p_over_25,
                "top1_score": top1_score,
                "exact_scores": exact_scores,
                "actual_hg": int(row["actual_home_goals"]),
                "actual_ag": int(row["actual_away_goals"]),
            }
        )
    return samples


def _score_samples(
    samples: list[dict[str, Any]],
    odds_index: dict[tuple[str, str, str], list[tuple[datetime, sqlite3.Row]]],
) -> dict[str, Any]:
    ordered = sorted(samples, key=lambda s: str(s["kickoff"]))
    n = len(ordered)

    ft_hits = 0
    outcome_stats = {
        "H": {"pred": 0, "actual": 0, "correct": 0},
        "D": {"pred": 0, "actual": 0, "correct": 0},
        "A": {"pred": 0, "actual": 0, "correct": 0},
    }
    exact1_hits = 0
    exact1_total = 0
    exact3_hits = 0
    exact3_total = 0
    dc_hits = 0
    dc_by_pick = {"1X": [0, 0], "X2": [0, 0], "12": [0, 0]}
    ou25_hits = 0
    ou25_total = 0
    ou15_hits = 0
    ou15_total = 0
    btts_hits = 0
    btts_total = 0

    tiers = {
        "high": {"label": "High Confidence (≥ 60%)", "count": 0, "pred_sum": 0.0, "hits": 0},
        "medium": {"label": "Medium Confidence (45% – 59.9%)", "count": 0, "pred_sum": 0.0, "hits": 0},
        "low": {"label": "Low Confidence / Toss-up (< 45%)", "count": 0, "pred_sum": 0.0, "hits": 0},
    }

    clv_values: list[float] = []
    pos_clv_hits = 0
    value_hits = 0
    value_total = 0

    ft_lls: list[float] = []
    ft_briers: list[float] = []
    exact_lls: list[float] = []
    draw_pairs: list[tuple[float, float]] = []

    ft_hit_series: list[bool] = []

    for s in ordered:
        ph, pd_, pa = float(s["p_home"]), float(s["p_draw"]), float(s["p_away"])
        hg, ag = int(s["actual_hg"]), int(s["actual_ag"])
        actual_1x2 = "H" if hg > ag else ("D" if hg == ag else "A")

        probs_1x2 = (("H", ph), ("D", pd_), ("A", pa))
        pred_1x2, max_p = max(probs_1x2, key=lambda item: item[1])
        is_ft_hit = pred_1x2 == actual_1x2
        ft_hit_series.append(is_ft_hit)
        if is_ft_hit:
            ft_hits += 1

        outcome_stats[pred_1x2]["pred"] += 1
        outcome_stats[actual_1x2]["actual"] += 1
        if is_ft_hit:
            outcome_stats[pred_1x2]["correct"] += 1

        mp_tuple = (ph, pd_, pa)
        actual_idx = {"H": 0, "D": 1, "A": 2}[actual_1x2]
        ft_lls.append(-math.log(max(mp_tuple[actual_idx], LOG_LOSS_FLOOR)))
        ft_briers.append(sum((mp_tuple[i] - (1.0 if i == actual_idx else 0.0)) ** 2 for i in range(3)))
        draw_pairs.append((pd_, 1.0 if actual_1x2 == "D" else 0.0))

        # Confidence tier
        if max_p >= 0.60:
            t_key = "high"
        elif max_p >= 0.45:
            t_key = "medium"
        else:
            t_key = "low"
        tiers[t_key]["count"] += 1
        tiers[t_key]["pred_sum"] += max_p
        if is_ft_hit:
            tiers[t_key]["hits"] += 1

        # Double Chance (top-2 combined 1X2 outcomes)
        dc_probs = (
            ("1X", ph + pd_, {"H", "D"}),
            ("X2", pd_ + pa, {"D", "A"}),
            ("12", ph + pa, {"H", "A"}),
        )
        dc_pick, _, dc_set = max(dc_probs, key=lambda item: item[1])
        dc_by_pick[dc_pick][1] += 1
        if actual_1x2 in dc_set:
            dc_hits += 1
            dc_by_pick[dc_pick][0] += 1

        # Scoreline matrix derived markets
        exact_scores = s.get("exact_scores")
        sorted_cells: list[tuple[int, int, float]] = []
        if isinstance(exact_scores, list) and exact_scores:
            for cell in exact_scores:
                try:
                    sorted_cells.append(
                        (int(cell["home_goals"]), int(cell["away_goals"]), float(cell["probability"]))
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            sorted_cells.sort(key=lambda c: c[2], reverse=True)

        top1 = s.get("top1_score")
        if top1 is None and sorted_cells:
            top1 = (sorted_cells[0][0], sorted_cells[0][1])
        if top1 is not None:
            exact1_total += 1
            if top1 == (hg, ag):
                exact1_hits += 1

        if sorted_cells:
            exact3_total += 1
            if (hg, ag) in {(c[0], c[1]) for c in sorted_cells[:3]}:
                exact3_hits += 1

            p_actual_score = next((c[2] for c in sorted_cells if c[0] == hg and c[1] == ag), 0.0)
            exact_lls.append(-math.log(max(p_actual_score, LOG_LOSS_FLOOR)))

            p_o15 = sum(c[2] for c in sorted_cells if (c[0] + c[1]) >= 2)
            ou15_total += 1
            if (p_o15 >= 0.5) == ((hg + ag) >= 2):
                ou15_hits += 1

            p_btts = sum(c[2] for c in sorted_cells if c[0] >= 1 and c[1] >= 1)
            btts_total += 1
            if (p_btts >= 0.5) == (hg >= 1 and ag >= 1):
                btts_hits += 1

            p_o25 = s.get("p_over_25")
            if p_o25 is None:
                p_o25 = sum(c[2] for c in sorted_cells if (c[0] + c[1]) >= 3)
        else:
            p_o25 = s.get("p_over_25")

        if isinstance(p_o25, (int, float)):
            ou25_total += 1
            if (float(p_o25) >= 0.5) == ((hg + ag) >= 3):
                ou25_hits += 1

        # Market benchmark & CLV
        odds = _find_closing_odds(
            odds_index,
            competition=s["competition"],
            home_id=s["home_id"],
            away_id=s["away_id"],
            kickoff=s["kickoff"],
        )
        if odds is not None:
            cp = remove_vig_1x2(*odds)
            mp = (ph, pd_, pa)
            top_idx = {"H": 0, "D": 1, "A": 2}[pred_1x2]
            actual_idx = {"H": 0, "D": 1, "A": 2}[actual_1x2]
            clv = mp[top_idx] - cp[top_idx]
            clv_values.append(clv)
            if clv > 0.0:
                pos_clv_hits += 1

            edges = [mp[i] - cp[i] for i in range(3)]
            best_i = max(range(3), key=lambda i: edges[i])
            if edges[best_i] >= _VALUE_EDGE_THRESHOLD:
                value_total += 1
                if best_i == actual_idx:
                    value_hits += 1

    # Outcome breakdown
    outcome_breakdown: dict[str, Any] = {}
    for code, label in (("H", "Home (1)"), ("D", "Draw (X)"), ("A", "Away (2)")):
        st = outcome_stats[code]
        outcome_breakdown[code] = {
            "label": label,
            "predicted_count": st["pred"],
            "actual_count": st["actual"],
            "correct_count": st["correct"],
            "precision": _pct_stat(st["correct"], st["pred"]),
            "recall": _pct_stat(st["correct"], st["actual"]),
        }

    # Confidence tiers formatted
    confidence_tiers: list[dict[str, Any]] = []
    for key in ("high", "medium", "low"):
        t = tiers[key]
        cnt = t["count"]
        avg_pred_pct = (t["pred_sum"] / cnt * 100.0) if cnt > 0 else None
        act_pct = (t["hits"] / cnt * 100.0) if cnt > 0 else None
        gap = (act_pct - avg_pred_pct) if (act_pct is not None and avg_pred_pct is not None) else None
        confidence_tiers.append(
            {
                "tier": key,
                "label": t["label"],
                "count": cnt,
                "hits": t["hits"],
                "avg_predicted_pct": avg_pred_pct,
                "actual_hit_pct": act_pct,
                "calibration_gap_pct": gap,
            }
        )

    # Trend (Last 20 / Last 50 vs full)
    last_20_slice = ft_hit_series[-20:]
    last_50_slice = ft_hit_series[-50:]
    last_20 = _pct_stat(sum(1 for x in last_20_slice if x), len(last_20_slice))
    last_50 = _pct_stat(sum(1 for x in last_50_slice if x), len(last_50_slice))
    full_ft = _pct_stat(ft_hits, n)
    trend_delta_20 = (
        (last_20["pct"] - full_ft["pct"])
        if last_20["pct"] is not None and full_ft["pct"] is not None
        else None
    )

    clv_n = len(clv_values)
    mean_clv_pct = (sum(clv_values) / clv_n * 100.0) if clv_n > 0 else None

    draw_ece: float | None = None
    if draw_pairs:
        bins: list[list[tuple[float, float]]] = [[] for _ in range(10)]
        for p_d, y_d in draw_pairs:
            idx = min(9, max(0, int(p_d * 10.0)))
            bins[idx].append((p_d, y_d))
        draw_ece = sum(
            (len(b) / len(draw_pairs))
            * abs((sum(x[0] for x in b) / len(b)) - (sum(x[1] for x in b) / len(b)))
            for b in bins
            if b
        )

    return {
        "total": n,
        "ft_1x2": full_ft,
        "outcome_breakdown": outcome_breakdown,
        "exact_top1": _pct_stat(exact1_hits, exact1_total),
        "exact_top3": _pct_stat(exact3_hits, exact3_total),
        "double_chance": _pct_stat(dc_hits, n),
        "double_chance_breakdown": {
            k: _pct_stat(v[0], v[1]) for k, v in dc_by_pick.items()
        },
        "ou_25": _pct_stat(ou25_hits, ou25_total),
        "ou_15": _pct_stat(ou15_hits, ou15_total),
        "btts": _pct_stat(btts_hits, btts_total),
        "confidence_tiers": confidence_tiers,
        "probabilistic": {
            "exact_score_ll": (sum(exact_lls) / len(exact_lls)) if exact_lls else None,
            "ft_1x2_ll": (sum(ft_lls) / len(ft_lls)) if ft_lls else None,
            "ft_brier": (sum(ft_briers) / len(ft_briers)) if ft_briers else None,
            "draw_ece": draw_ece,
        },
        "market_edge": {
            "n_matched": clv_n,
            "beat_closing": _pct_stat(pos_clv_hits, clv_n),
            "mean_clv_pct": mean_clv_pct,
            "value_pick": _pct_stat(value_hits, value_total),
        },
        "trend": {
            "last_20": last_20,
            "last_50": last_50,
            "full": full_ft,
            "delta_20_pct": trend_delta_20,
        },
    }


def _league_sort_key(row: dict[str, Any], sort_by: str) -> tuple[int, Any]:
    if sort_by == "competition":
        return (0, str(row.get("competition") or ""))
    if sort_by == "total":
        return (0, int(row.get("total") or 0))
    val = row.get(sort_by)
    if val is None:
        return (1, 0.0)
    return (0, float(val))


def _season_sort_key(season: str) -> tuple[int, str]:
    raw = str(season or "").strip()
    digits = "".join(ch for ch in raw[:4] if ch.isdigit())
    return (int(digits) if digits else 0, raw)


def _build_season_rows(
    grouped: dict[tuple[str, str], list[dict[str, Any]]],
    odds_index: dict[tuple[str, str, str], list[tuple[datetime, sqlite3.Row]]],
) -> list[dict[str, Any]]:
    """Build chronologically ordered season rows with YoY deltas and best-season flags."""
    # Group keys by competition (or "ALL" for overall) to compute YoY deltas per league
    by_scope: dict[str, list[tuple[str, list[dict[str, Any]]]]] = {}
    for (scope, season), rows in grouped.items():
        by_scope.setdefault(scope, []).append((season, rows))

    out: list[dict[str, Any]] = []
    for scope in sorted(by_scope):
        season_items = sorted(by_scope[scope], key=lambda item: _season_sort_key(item[0]))
        prev_ft_pct: float | None = None
        prev_exact_ll: float | None = None
        scope_rows: list[dict[str, Any]] = []
        for season_val, rows in season_items:
            metrics = _score_samples(rows, odds_index)
            prob = metrics["probabilistic"]
            me = metrics["market_edge"]
            ob = metrics["outcome_breakdown"]
            high_tier = next((t for t in metrics["confidence_tiers"] if t["tier"] == "high"), None)
            model_ids = sorted({str(r.get("model_id") or "") for r in rows if r.get("model_id")})
            active_mid = _active_model_id_for(scope) if scope != "ALL" else None
            display_mid = active_mid if (active_mid and active_mid in model_ids) else (model_ids[-1] if model_ids else "—")

            ft_pct = metrics["ft_1x2"]["pct"]
            exact_ll = prob["exact_score_ll"]
            yoy_ft_delta = (
                (ft_pct - prev_ft_pct)
                if (ft_pct is not None and prev_ft_pct is not None)
                else None
            )
            yoy_ll_delta = (
                (exact_ll - prev_exact_ll)
                if (exact_ll is not None and prev_exact_ll is not None)
                else None
            )
            prev_ft_pct = ft_pct
            prev_exact_ll = exact_ll

            scope_rows.append(
                {
                    "competition": scope,
                    "season": season_val,
                    "model_id": display_mid,
                    "n_matches": metrics["total"],
                    "ft_1x2": metrics["ft_1x2"],
                    "home_hit": ob["H"]["precision"],
                    "draw_hit": ob["D"]["precision"],
                    "away_hit": ob["A"]["precision"],
                    "exact_top1": metrics["exact_top1"],
                    "exact_top3": metrics["exact_top3"],
                    "double_chance": metrics["double_chance"],
                    "ou_25": metrics["ou_25"],
                    "btts": metrics["btts"],
                    "high_conf": {
                        "hits": high_tier["hits"] if high_tier else 0,
                        "total": high_tier["count"] if high_tier else 0,
                        "pct": high_tier["actual_hit_pct"] if high_tier else None,
                    },
                    "exact_score_ll": exact_ll,
                    "ft_1x2_ll": prob["ft_1x2_ll"],
                    "ft_brier": prob["ft_brier"],
                    "draw_ece": prob["draw_ece"],
                    "beat_closing": me["beat_closing"],
                    "mean_clv_pct": me["mean_clv_pct"],
                    "yoy_ft_1x2_delta_pp": yoy_ft_delta,
                    "yoy_exact_ll_delta": yoy_ll_delta,
                    "is_best_ft_1x2": False,
                    "is_best_exact_ll": False,
                }
            )

        ft_candidates = [r for r in scope_rows if r["ft_1x2"]["pct"] is not None]
        if len(ft_candidates) > 1:
            best_ft = max(ft_candidates, key=lambda r: float(r["ft_1x2"]["pct"]))
            best_ft["is_best_ft_1x2"] = True
        ll_candidates = [r for r in scope_rows if r["exact_score_ll"] is not None]
        if len(ll_candidates) > 1:
            best_ll = min(ll_candidates, key=lambda r: float(r["exact_score_ll"]))
            best_ll["is_best_exact_ll"] = True

        out.extend(scope_rows)
    return out


def compute_analytics_summary(
    conn: sqlite3.Connection,
    *,
    record_type: str = "retrospective",
    competition: str | None = None,
    season: str | None = None,
    sort_by: str = "total",
    sort_dir: str = "desc",
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Compute global and per-competition percentage analytics without mixing record types silently."""
    rec_type = record_type.strip().lower() if record_type else "retrospective"
    if rec_type not in RECORD_TYPES:
        rec_type = "retrospective"

    comp_filter = competition.strip().upper() if competition and competition.strip().upper() != "ALL" else None
    season_filter = season.strip() if season and season.strip().lower() != "all" else None
    cutoff_utc: datetime | None = None
    if season_filter == "last_30d":
        ref_now = now_utc or datetime.now(timezone.utc)
        cutoff_utc = ref_now - timedelta(days=30)

    retro_samples: list[dict[str, Any]] = []
    prosp_samples: list[dict[str, Any]] = []

    if rec_type in ("retrospective", "all"):
        retro_samples = _extract_retrospective_samples(
            conn,
            competition=comp_filter,
            season=season_filter,
            cutoff_utc=cutoff_utc,
        )
    if rec_type in ("prospective", "all"):
        prosp_samples = _extract_prospective_samples(
            conn,
            competition=comp_filter,
            season=season_filter,
            cutoff_utc=cutoff_utc,
        )

    all_samples = retro_samples + prosp_samples
    odds_index = _load_odds_index(conn, competition=comp_filter)
    overall = _score_samples(all_samples, odds_index)

    by_comp_samples: dict[str, list[dict[str, Any]]] = {}
    for s in all_samples:
        by_comp_samples.setdefault(str(s["competition"]).upper(), []).append(s)

    per_competition: list[dict[str, Any]] = []
    for comp_code, comp_rows in by_comp_samples.items():
        metrics = _score_samples(comp_rows, odds_index)
        high_tier = next((t for t in metrics["confidence_tiers"] if t["tier"] == "high"), None)
        model_ids = sorted({str(r.get("model_id") or "") for r in comp_rows if r.get("model_id")})
        per_competition.append(
            {
                "competition": comp_code,
                "model_id": _active_model_id_for(comp_code) or (model_ids[-1] if model_ids else "—"),
                "total": metrics["total"],
                "ft_1x2": metrics["ft_1x2"],
                "ft_1x2_pct": metrics["ft_1x2"]["pct"],
                "exact_top1": metrics["exact_top1"],
                "exact_top1_pct": metrics["exact_top1"]["pct"],
                "exact_top3": metrics["exact_top3"],
                "exact_top3_pct": metrics["exact_top3"]["pct"],
                "double_chance": metrics["double_chance"],
                "double_chance_pct": metrics["double_chance"]["pct"],
                "ou_25": metrics["ou_25"],
                "ou_25_pct": metrics["ou_25"]["pct"],
                "ou_15": metrics["ou_15"],
                "ou_15_pct": metrics["ou_15"]["pct"],
                "btts": metrics["btts"],
                "btts_pct": metrics["btts"]["pct"],
                "high_conf": {
                    "hits": high_tier["hits"] if high_tier else 0,
                    "total": high_tier["count"] if high_tier else 0,
                    "pct": high_tier["actual_hit_pct"] if high_tier else None,
                },
                "high_conf_pct": high_tier["actual_hit_pct"] if high_tier else None,
                "beat_closing": metrics["market_edge"]["beat_closing"],
                "beat_closing_pct": metrics["market_edge"]["beat_closing"]["pct"],
                "mean_clv_pct": metrics["market_edge"]["mean_clv_pct"],
                "last_20": metrics["trend"]["last_20"],
                "last_20_pct": metrics["trend"]["last_20"]["pct"],
                "last_50": metrics["trend"]["last_50"],
            }
        )

    sort_col = sort_by if sort_by in SORTABLE_LEAGUE_COLUMNS else "total"
    reverse = str(sort_dir or "desc").lower() != "asc"
    if sort_col == "competition":
        per_competition.sort(key=lambda r: str(r["competition"]), reverse=reverse)
    else:
        # Keep None values at the bottom regardless of sort direction
        non_null = [r for r in per_competition if r.get(sort_col) is not None]
        null_rows = [r for r in per_competition if r.get(sort_col) is None]
        non_null.sort(key=lambda r: float(r[sort_col]), reverse=reverse)
        per_competition = non_null + null_rows

    by_season_all_grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    by_season_comp_grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for s in all_samples:
        szn = str(s.get("season") or "unknown")
        comp_c = str(s["competition"]).upper()
        by_season_all_grouped.setdefault(("ALL", szn), []).append(s)
        by_season_comp_grouped.setdefault((comp_c, szn), []).append(s)

    by_season_overall = _build_season_rows(by_season_all_grouped, odds_index)
    by_season_competition = _build_season_rows(by_season_comp_grouped, odds_index)

    return {
        "filters": {
            "record_type": rec_type,
            "competition": comp_filter or "ALL",
            "season": season_filter or "all",
            "sort_by": sort_col,
            "sort_dir": "asc" if not reverse else "desc",
        },
        "counts": {
            "total": len(all_samples),
            "retrospective": len(retro_samples),
            "prospective": len(prosp_samples),
        },
        "overall": overall,
        "per_competition": per_competition,
        "by_season_overall": by_season_overall,
        "by_season_competition": by_season_competition,
    }
