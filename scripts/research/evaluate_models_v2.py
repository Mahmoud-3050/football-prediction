"""Evaluation script to compare baseline vs enhanced prediction models - matching BOT-PL-001 exactly."""

import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from laboratory.store import connect
from laboratory.snapshot import MatchRecord, build_snapshot
from laboratory.score_model_adapter import price_from_snapshot
from modeling.enhanced import (
    estimate_score_ensemble,
    estimate_score_special,
    EnsembleConfig,
    DEFAULT_ENSEMBLE,
    _bayesian_exact_score,
    _get_1x2_pred,
    _get_1x2_label,
)


def run_baseline_evaluation(db_path: Path, season: str = "2026", competition: str = "PL") -> dict[str, float]:
    """Run baseline predictions matching BOT-PL-001 walk-forward exactly."""
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get finished matches for the season (by date range, matching BOT-PL-001)
    if season == "2026":
        date_from = "2026-08-01"
        date_to = "2027-06-01"
    elif season == "2025":
        date_from = "2025-08-01"
        date_to = "2026-06-01"
    elif season == "2024":
        date_from = "2024-08-01"
        date_to = "2025-06-01"
    elif season == "2023":
        date_from = "2023-08-01"
        date_to = "2024-06-01"
    else:
        date_from = f"{season}-08-01"
        next_year = str(int(season) + 1)
        date_to = f"{next_year}-06-01"

    matches = conn.execute("""
        SELECT m.match_id, m.season, m.kickoff, m.home_id, m.home_name, m.away_id, m.away_name,
               r.ft_home, r.ft_away, r.ht_home, r.ht_away
        FROM matches m
        JOIN result_revisions r ON r.match_id = m.match_id
        WHERE m.competition = ? AND r.ft_home IS NOT NULL
        AND r.revision = (SELECT MAX(revision) FROM result_revisions WHERE match_id = m.match_id)
        AND m.kickoff >= ? AND m.kickoff < ?
        ORDER BY m.kickoff
    """, (competition, date_from, date_to)).fetchall()

    # Build full history (all seasons, all matches) - matching BOT-PL-001 walk-forward
    all_rows = conn.execute("""
        SELECT m.match_id, m.dataset_version, m.competition, m.season, m.kickoff,
               m.home_id, m.home_name, m.away_id, m.away_name,
               r.ft_home, r.ft_away, r.ht_home, r.ht_away
        FROM matches m
        LEFT JOIN result_revisions r ON r.match_id = m.match_id
        WHERE m.competition = ?
        ORDER BY m.kickoff
    """, (competition,)).fetchall()

    history = []
    for row in all_rows:
        history.append(MatchRecord(
            match_id=row[0],
            season=row[3],
            kickoff=row[4],
            home_id=row[5],
            home_name=row[6],
            away_id=row[7],
            away_name=row[8],
            ft_home=row[9],
            ft_away=row[10],
            ht_home=row[11],
            ht_away=row[12],
            competition=row[2],
        ))

    from laboratory.score_model_adapter import team_int_id

    exact_hits = 0
    total = 0
    sum_exact_prob = 0.0
    sum_ll = 0.0
    ft1x2_correct = 0
    details = []

    for match in matches:
        target = MatchRecord(
            match_id=match["match_id"],
            season=match["season"],
            kickoff=match["kickoff"],
            home_id=match["home_id"],
            home_name=match["home_name"],
            away_id=match["away_id"],
            away_name=match["away_name"],
            ft_home=match["ft_home"],
            ft_away=match["ft_away"],
            ht_home=match["ht_home"],
            ht_away=match["ht_away"],
            competition=competition,
        )

        payload = build_snapshot(target, history)
        if not payload["leakage_ok"]:
            continue

        # Apply the same filter as price_from_snapshot (MINIMUM_SCORED_MATCHES=3)
        priors = payload["prior_matches"]
        home_prior_count = sum(1 for p in priors if p["home_id"] == target.home_id or p["away_id"] == target.home_id)
        away_prior_count = sum(1 for p in priors if p["home_id"] == target.away_id or p["away_id"] == target.away_id)
        if home_prior_count < 3 or away_prior_count < 3:
            continue

        # Run baseline using price_from_snapshot (exactly what BOT-PL-001 does)
        outcome = price_from_snapshot(payload)
        if outcome.excluded or outcome.body is None:
            continue

        body = outcome.body
        mode = body.get('full_time', {}).get('exact_score_most_likely', {})
        pred_hg = mode.get("home_goals", 0)
        pred_ag = mode.get("away_goals", 0)
        actual_hg = target.ft_home
        actual_ag = target.ft_away

        # Get exact score probability
        exact_prob = 0.0
        full_time = body.get("full_time", {})
        for cell in full_time.get("exact_scores", []):
            if int(cell.get("home_goals", -1)) == actual_hg and int(cell.get("away_goals", -1)) == actual_ag:
                exact_prob = float(cell.get("probability", 0))
                break

        exact_hit = (pred_hg == actual_hg and pred_ag == actual_ag)

        # Get 1X2 prediction
        ft1x2 = full_time.get("result_1x2", {})
        ft1x2_pred = ft1x2.get("most_likely", "home")
        ft1x2_actual = _get_1x2_label(actual_hg, actual_ag)
        ft1x2_hit = (ft1x2_pred == ft1x2_actual)

        exact_hits += exact_hit
        ft1x2_correct += ft1x2_hit
        total += 1
        sum_exact_prob += exact_prob
        sum_ll += -math.log(max(exact_prob, 1e-15))

        details.append({
            "match_id": target.match_id,
            "home": target.home_name,
            "away": target.away_name,
            "pred_score": f"{pred_hg}-{pred_ag}",
            "actual_score": f"{actual_hg}-{actual_ag}",
            "exact_hit": exact_hit,
            "exact_prob": exact_prob,
            "ft1x2_pred": ft1x2_pred,
            "ft1x2_actual": ft1x2_actual,
            "ft1x2_correct": ft1x2_hit,
        })

    conn.close()

    return {
        "model": "baseline",
        "total": total,
        "exact_hits": exact_hits,
        "exact_hit_rate": exact_hits / total if total > 0 else 0,
        "ft1x2_correct": ft1x2_correct,
        "ft1x2_accuracy": ft1x2_correct / total if total > 0 else 0,
        "avg_exact_prob": sum_exact_prob / total if total > 0 else 0,
        "avg_log_loss": sum_ll / total if total > 0 else 0,
        "details": details,
    }


def run_enhanced_evaluation(
    db_path: Path,
    season: str = "2026",
    competition: str = "PL",
    method: str = "ensemble",
) -> dict[str, float]:
    """Run enhanced predictions and evaluate (matching BOT-PL-001 filtering)."""
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get finished matches for the season (by date range)
    if season == "2026":
        date_from = "2026-08-01"
        date_to = "2027-06-01"
    elif season == "2025":
        date_from = "2025-08-01"
        date_to = "2026-06-01"
    elif season == "2024":
        date_from = "2024-08-01"
        date_to = "2025-06-01"
    elif season == "2023":
        date_from = "2023-08-01"
        date_to = "2024-06-01"
    else:
        date_from = f"{season}-08-01"
        next_year = str(int(season) + 1)
        date_to = f"{next_year}-06-01"

    matches = conn.execute("""
        SELECT m.match_id, m.season, m.kickoff, m.home_id, m.home_name, m.away_id, m.away_name,
               r.ft_home, r.ft_away, r.ht_home, r.ht_away
        FROM matches m
        JOIN result_revisions r ON r.match_id = m.match_id
        WHERE m.competition = ? AND r.ft_home IS NOT NULL
        AND r.revision = (SELECT MAX(revision) FROM result_revisions WHERE match_id = m.match_id)
        AND m.kickoff >= ? AND m.kickoff < ?
        ORDER BY m.kickoff
    """, (competition, date_from, date_to)).fetchall()

    # Build full history (all seasons, all matches) - matching BOT-PL-001 walk-forward
    all_rows = conn.execute("""
        SELECT m.match_id, m.dataset_version, m.competition, m.season, m.kickoff,
               m.home_id, m.home_name, m.away_id, m.away_name,
               r.ft_home, r.ft_away, r.ht_home, r.ht_away
        FROM matches m
        LEFT JOIN result_revisions r ON r.match_id = m.match_id
        WHERE m.competition = ?
        ORDER BY m.kickoff
    """, (competition,)).fetchall()

    history = []
    for row in all_rows:
        history.append(MatchRecord(
            match_id=row[0],
            season=row[3],
            kickoff=row[4],
            home_id=row[5],
            home_name=row[6],
            away_id=row[7],
            away_name=row[8],
            ft_home=row[9],
            ft_away=row[10],
            ht_home=row[11],
            ht_away=row[12],
            competition=row[2],
        ))

    from laboratory.score_model_adapter import team_int_id

    exact_hits = 0
    total = 0
    sum_exact_prob = 0.0
    sum_ll = 0.0
    ft1x2_correct = 0
    details = []

    for match in matches:
        target = MatchRecord(
            match_id=match["match_id"],
            season=match["season"],
            kickoff=match["kickoff"],
            home_id=match["home_id"],
            home_name=match["home_name"],
            away_id=match["away_id"],
            away_name=match["away_name"],
            ft_home=match["ft_home"],
            ft_away=match["ft_away"],
            ht_home=match["ht_home"],
            ht_away=match["ht_away"],
            competition=competition,
        )

        payload = build_snapshot(target, history)
        if not payload["leakage_ok"]:
            continue

        # Apply the same filter as price_from_snapshot (MINIMUM_SCORED_MATCHES=3)
        priors = payload["prior_matches"]
        home_prior_count = sum(1 for p in priors if p["home_id"] == target.home_id or p["away_id"] == target.home_id)
        away_prior_count = sum(1 for p in priors if p["home_id"] == target.away_id or p["away_id"] == target.away_id)
        if home_prior_count < 3 or away_prior_count < 3:
            continue

        # Convert priors using _to_fd_match format (matching price_from_snapshot)
        from laboratory.score_model_adapter import _to_fd_match
        competition_code = str(payload.get("competition") or competition)

        competition_payload = {
            "matches": [_to_fd_match(prior, competition_code=competition_code) for prior in priors]
        }
        home_payload = {
            "matches": [
                _to_fd_match(prior, competition_code=competition_code)
                for prior in priors
                if prior["home_id"] == target.home_id or prior["away_id"] == target.home_id
            ]
        }
        away_payload = {
            "matches": [
                _to_fd_match(prior, competition_code=competition_code)
                for prior in priors
                if prior["home_id"] == target.away_id or prior["away_id"] == target.away_id
            ]
        }

        home_int = team_int_id(str(target.home_id))
        away_int = team_int_id(str(target.away_id))

        if method == "ensemble":
            est = estimate_score_ensemble(
                competition_finished_payload=competition_payload,
                home_finished_payload=home_payload,
                away_finished_payload=away_payload,
                home_team_id=home_int,
                away_team_id=away_int,
                include_half_time=True,
                kickoff=target.kickoff,
                exact_score_method="zero_one",
            )
        elif method == "bivariate":
            est = estimate_score_special(
                competition_finished_payload=competition_payload,
                home_finished_payload=home_payload,
                away_finished_payload=away_payload,
                home_team_id=home_int,
                away_team_id=away_int,
                include_half_time=True,
                kickoff=target.kickoff,
                dependence="bivariate_poisson",
            )
        elif method == "copula":
            est = estimate_score_special(
                competition_finished_payload=competition_payload,
                home_finished_payload=home_payload,
                away_finished_payload=away_payload,
                home_team_id=home_int,
                away_team_id=away_int,
                include_half_time=True,
                kickoff=target.kickoff,
                dependence="copula_frank",
            )
        else:
            raise ValueError(f"Unknown method: {method}")

        pred_hg, pred_ag, _ = _bayesian_exact_score(est.matrix, "absolute")
        actual_hg = target.ft_home
        actual_ag = target.ft_away

        exact_prob = est.matrix[pred_hg][pred_ag] if pred_hg < len(est.matrix) and pred_ag < len(est.matrix[0]) else 0.0
        exact_hit = (pred_hg == actual_hg and pred_ag == actual_ag)

        ft1x2_pred = _get_1x2_pred(est.matrix)
        ft1x2_actual = _get_1x2_label(actual_hg, actual_ag)
        ft1x2_hit = (ft1x2_pred == ft1x2_actual)

        exact_hits += exact_hit
        ft1x2_correct += ft1x2_hit
        total += 1
        sum_exact_prob += exact_prob
        sum_ll += -math.log(max(exact_prob, 1e-15))

        details.append({
            "match_id": target.match_id,
            "home": target.home_name,
            "away": target.away_name,
            "pred_score": f"{pred_hg}-{pred_ag}",
            "actual_score": f"{actual_hg}-{actual_ag}",
            "exact_hit": exact_hit,
            "exact_prob": exact_prob,
            "ft1x2_pred": ft1x2_pred,
            "ft1x2_actual": ft1x2_actual,
            "ft1x2_correct": ft1x2_hit,
        })

    conn.close()

    return {
        "model": f"enhanced_{method}",
        "total": total,
        "exact_hits": exact_hits,
        "exact_hit_rate": exact_hits / total if total > 0 else 0,
        "ft1x2_correct": ft1x2_correct,
        "ft1x2_accuracy": ft1x2_correct / total if total > 0 else 0,
        "avg_exact_prob": sum_exact_prob / total if total > 0 else 0,
        "avg_log_loss": sum_ll / total if total > 0 else 0,
        "details": details,
    }


def evaluate_season(db_path: Path, season: str, competition: str = "PL"):
    """Evaluate all models on a single season."""
    print(f"\n{'='*60}")
    print(f"SEASON {season}")
    print(f"{'='*60}")

    baseline = run_baseline_evaluation(db_path, season=season, competition=competition)
    ensemble = run_enhanced_evaluation(db_path, season=season, competition=competition, method="ensemble")
    bivariate = run_enhanced_evaluation(db_path, season=season, competition=competition, method="bivariate")
    copula = run_enhanced_evaluation(db_path, season=season, competition=competition, method="copula")

    print(f"Baseline   : Exact={baseline['exact_hit_rate']:.1%} | 1X2={baseline['ft1x2_accuracy']:.1%} | LL={baseline['avg_log_loss']:.4f} | Prob={baseline['avg_exact_prob']:.4f}")
    print(f"Ensemble   : Exact={ensemble['exact_hit_rate']:.1%} | 1X2={ensemble['ft1x2_accuracy']:.1%} | LL={ensemble['avg_log_loss']:.4f} | Prob={ensemble['avg_exact_prob']:.4f}")
    print(f"Bivariate  : Exact={bivariate['exact_hit_rate']:.1%} | 1X2={bivariate['ft1x2_accuracy']:.1%} | LL={bivariate['avg_log_loss']:.4f} | Prob={bivariate['avg_exact_prob']:.4f}")
    print(f"Copula     : Exact={copula['exact_hit_rate']:.1%} | 1X2={copula['ft1x2_accuracy']:.1%} | LL={copula['avg_log_loss']:.4f} | Prob={copula['avg_exact_prob']:.4f}")

    return {
        "season": season,
        "baseline": baseline,
        "ensemble": ensemble,
        "bivariate": bivariate,
        "copula": copula,
    }


if __name__ == "__main__":
    import sqlite3
    db_path = Path("data/football.db")

    # Evaluate on all available seasons
    seasons = ["2023", "2024", "2025", "2026"]
    results = {}

    for season in seasons:
        results[season] = evaluate_season(db_path, season, "PL")

    # Overall summary
    print("\n" + "=" * 60)
    print("OVERALL SUMMARY (Weighted by matches)")
    print("=" * 60)

    for model_name in ["baseline", "ensemble", "bivariate", "copula"]:
        total_matches = 0
        total_exact_hits = 0
        total_ft1x2 = 0
        weighted_ll = 0.0
        weighted_prob = 0.0

        for season in seasons:
            r = results[season][model_name]
            total_matches += r['total']
            total_exact_hits += r['exact_hits']
            total_ft1x2 += r['ft1x2_correct']
            weighted_ll += r['avg_log_loss'] * r['total']
            weighted_prob += r['avg_exact_prob'] * r['total']

        if total_matches > 0:
            print(f"{model_name.capitalize():12s}: Exact={total_exact_hits/total_matches:.1%} | 1X2={total_ft1x2/total_matches:.1%} | LL={weighted_ll/total_matches:.4f} | Prob={weighted_prob/total_matches:.4f}")

    # Comparison with stored BOT-PL-001
    print("\n" + "=" * 60)
    print("COMPARISON WITH STORED BOT-PL-001 (2026/27)")
    print("=" * 60)
    print("Stored BOT-PL-001: Exact=15.7% | 1X2=40.0% | LL=3.06")
    print(f"Our Baseline:      Exact={results['2026']['baseline']['exact_hit_rate']:.1%} | 1X2={results['2026']['baseline']['ft1x2_accuracy']:.1%} | LL={results['2026']['baseline']['avg_log_loss']:.4f}")
    print(f"Our Ensemble:      Exact={results['2026']['ensemble']['exact_hit_rate']:.1%} | 1X2={results['2026']['ensemble']['ft1x2_accuracy']:.1%} | LL={results['2026']['ensemble']['avg_log_loss']:.4f}")