"""Evaluation script to compare baseline vs enhanced prediction models."""

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from laboratory.store import connect
from laboratory.snapshot import MatchRecord, build_snapshot, priors_from_snapshot
from laboratory.poisson_engine import FinishedMatch
from laboratory.job import price_one_score_model
from modeling.enhanced import (
    estimate_score_ensemble,
    estimate_score_special,
    EnsembleConfig,
    DEFAULT_ENSEMBLE,
)


def evaluate_predictions(
    db_path: Path,
    experiment_id: str,
    season: str = "2026",
    competition: str = "PL",
) -> dict[str, Any]:
    """Evaluate predictions for a given experiment."""
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get all finished matches for the season
    matches = conn.execute("""
        SELECT m.match_id, m.season, m.kickoff, m.home_id, m.home_name, m.away_id, m.away_name,
               r.ft_home, r.ft_away, r.ht_home, r.ht_away
        FROM matches m
        JOIN result_revisions r ON r.match_id = m.match_id
        WHERE m.season = ? AND m.competition = ? AND r.ft_home IS NOT NULL
        AND r.revision = (SELECT MAX(revision) FROM result_revisions WHERE match_id = m.match_id)
        ORDER BY m.kickoff
    """, (season, competition)).fetchall()

    # Build history for snapshot creation
    all_rows = conn.execute("""
        SELECT m.*, r.ft_home, r.ft_away, r.ht_home, r.ht_away
        FROM matches m
        LEFT JOIN result_revisions r ON r.match_id = m.match_id
        WHERE m.competition = ?
        ORDER BY m.kickoff
    """, (competition,)).fetchall()

    history = []
    for row in all_rows:
        history.append(MatchRecord(
            match_id=row["match_id"],
            season=row["season"],
            kickoff=row["kickoff"],
            home_id=row["home_id"],
            home_name=row["home_name"],
            away_id=row["away_id"],
            away_name=row["away_name"],
            ft_home=row["ft_home"],
            ft_away=row["ft_away"],
            ht_home=row["ht_home"],
            ht_away=row["ht_away"],
            competition=row["competition"],
        ))

    results = []
    for match in matches:
        if match["ft_home"] is None or match["ft_away"] is None:
            continue

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

        priors = priors_from_snapshot(payload)

        # Run prediction
        try:
            # This will use the stored prediction from the experiment
            pass
        except Exception as e:
            print(f"Error: {e}")

    conn.close()
    return {}


def run_baseline_evaluation(db_path: Path, season: str = "2026", competition: str = "PL") -> dict[str, float]:
    """Run baseline predictions and evaluate (matching BOT-PL-001 filtering)."""
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get finished matches
    matches = conn.execute("""
        SELECT m.match_id, m.season, m.kickoff, m.home_id, m.home_name, m.away_id, m.away_name,
               r.ft_home, r.ft_away, r.ht_home, r.ht_away
        FROM matches m
        JOIN result_revisions r ON r.match_id = m.match_id
        WHERE m.season = ? AND m.competition = ? AND r.ft_home IS NOT NULL
        AND r.revision = (SELECT MAX(revision) FROM result_revisions WHERE match_id = m.match_id)
        ORDER BY m.kickoff
    """, (season, competition)).fetchall()

    # Build history
    all_rows = conn.execute("""
        SELECT m.*, r.ft_home, r.ft_away, r.ht_home, r.ht_away
        FROM matches m
        LEFT JOIN result_revisions r ON r.match_id = m.match_id
        WHERE m.competition = ?
        ORDER BY m.kickoff
    """, (competition,)).fetchall()

    history = []
    for row in all_rows:
        history.append(MatchRecord(
            match_id=row["match_id"],
            season=row["season"],
            kickoff=row["kickoff"],
            home_id=row["home_id"],
            home_name=row["home_name"],
            away_id=row["away_id"],
            away_name=row["away_name"],
            ft_home=row["ft_home"],
            ft_away=row["ft_away"],
            ht_home=row["ht_home"],
            ht_away=row["ht_away"],
            competition=row["competition"],
        ))

    from modeling.score_model import estimate_score
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

        priors = priors_from_snapshot(payload)

        # Apply MINIMUM_SCORED_MATCHES=3 filter (matching BOT-PL-001)
        home_prior_count = sum(1 for p in priors if p.home_id == target.home_id or p.away_id == target.home_id)
        away_prior_count = sum(1 for p in priors if p.home_id == target.away_id or p.away_id == target.away_id)
        if home_prior_count < 3 or away_prior_count < 3:
            continue

        # Convert FinishedMatch to dict format
        priors_dicts = []
        for p in priors:
            priors_dicts.append({
                "home_id": p.home_id,
                "away_id": p.away_id,
                "ft_home": p.ft_home,
                "ft_away": p.ft_away,
                "ht_home": p.ht_home,
                "ht_away": p.ht_away,
                "kickoff": p.kickoff,
            })

        home_int = team_int_id(str(target.home_id))
        away_int = team_int_id(str(target.away_id))

        # Run baseline estimation
        est = estimate_score(
            competition_finished_payload={"matches": priors_dicts},
            home_finished_payload={"matches": [p for p in priors_dicts if p.get("home_id") == target.home_id or p.get("away_id") == target.home_id]},
            away_finished_payload={"matches": [p for p in priors_dicts if p.get("home_id") == target.away_id or p.get("away_id") == target.away_id]},
            home_team_id=home_int,
            away_team_id=away_int,
            include_half_time=True,
            kickoff=target.kickoff,
        )

        # Extract prediction
        pred_hg, pred_ag, _ = _mode(est.matrix)
        actual_hg = target.ft_home
        actual_ag = target.ft_away

        # Get exact score probability
        exact_prob = 0.0
        for hg, row in enumerate(est.matrix):
            for ag, prob in enumerate(row):
                if hg == actual_hg and ag == actual_ag:
                    exact_prob = prob

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
    """Run enhanced predictions and evaluate."""
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get finished matches
    matches = conn.execute("""
        SELECT m.match_id, m.season, m.kickoff, m.home_id, m.home_name, m.away_id, m.away_name,
               r.ft_home, r.ft_away, r.ht_home, r.ht_away
        FROM matches m
        JOIN result_revisions r ON r.match_id = m.match_id
        WHERE m.season = ? AND m.competition = ? AND r.ft_home IS NOT NULL
        AND r.revision = (SELECT MAX(revision) FROM result_revisions WHERE match_id = m.match_id)
        ORDER BY m.kickoff
    """, (season, competition)).fetchall()

    # Build history
    all_rows = conn.execute("""
        SELECT m.*, r.ft_home, r.ft_away, r.ht_home, r.ht_away
        FROM matches m
        LEFT JOIN result_revisions r ON r.match_id = m.match_id
        WHERE m.competition = ?
        ORDER BY m.kickoff
    """, (competition,)).fetchall()

    history = []
    for row in all_rows:
        history.append(MatchRecord(
            match_id=row["match_id"],
            season=row["season"],
            kickoff=row["kickoff"],
            home_id=row["home_id"],
            home_name=row["home_name"],
            away_id=row["away_id"],
            away_name=row["away_name"],
            ft_home=row["ft_home"],
            ft_away=row["ft_away"],
            ht_home=row["ht_home"],
            ht_away=row["ht_away"],
            competition=row["competition"],
        ))

    from modeling.enhanced import (
        estimate_score_ensemble,
        estimate_score_special,
        EnsembleConfig,
        DEFAULT_ENSEMBLE,
        _mode,
        _bayesian_exact_score,
        _market_probabilities,
        _get_1x2_pred,
    )
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

        priors = priors_from_snapshot(payload)

        # Apply MINIMUM_SCORED_MATCHES=3 filter (matching BOT-PL-001)
        home_prior_count = sum(1 for p in priors if p.home_id == target.home_id or p.away_id == target.home_id)
        away_prior_count = sum(1 for p in priors if p.home_id == target.away_id or p.away_id == target.away_id)
        if home_prior_count < 3 or away_prior_count < 3:
            continue

        # Convert FinishedMatch to dict format
        priors_dicts = []
        for p in priors:
            priors_dicts.append({
                "home_id": p.home_id,
                "away_id": p.away_id,
                "ft_home": p.ft_home,
                "ft_away": p.ft_away,
                "ht_home": p.ht_home,
                "ht_away": p.ht_away,
                "kickoff": p.kickoff,
            })

        home_int = team_int_id(str(target.home_id))
        away_int = team_int_id(str(target.away_id))

        if method == "ensemble":
            est = estimate_score_ensemble(
                competition_finished_payload={"matches": priors_dicts},
                home_finished_payload={"matches": [p for p in priors_dicts if p.get("home_id") == target.home_id or p.get("away_id") == target.home_id]},
                away_finished_payload={"matches": [p for p in priors_dicts if p.get("home_id") == target.away_id or p.get("away_id") == target.away_id]},
                home_team_id=home_int,
                away_team_id=away_int,
                include_half_time=True,
                kickoff=target.kickoff,
            )
        elif method == "bivariate":
            est = estimate_score_special(
                competition_finished_payload={"matches": priors_dicts},
                home_finished_payload={"matches": [p for p in priors_dicts if p.get("home_id") == target.home_id or p.get("away_id") == target.home_id]},
                away_finished_payload={"matches": [p for p in priors_dicts if p.get("home_id") == target.away_id or p.get("away_id") == target.away_id]},
                home_team_id=home_int,
                away_team_id=away_int,
                include_half_time=True,
                kickoff=target.kickoff,
                dependence="bivariate_poisson",
            )
        elif method == "copula":
            est = estimate_score_special(
                competition_finished_payload={"matches": priors_dicts},
                home_finished_payload={"matches": [p for p in priors_dicts if p.get("home_id") == target.home_id or p.get("away_id") == target.home_id]},
                away_finished_payload={"matches": [p for p in priors_dicts if p.get("home_id") == target.away_id or p.get("away_id") == target.away_id]},
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


def _mode(matrix):
    """Get modal score from matrix."""
    best_home = 0
    best_away = 0
    best_prob = matrix[0][0]
    for hg, row in enumerate(matrix):
        for ag, prob in enumerate(row):
            if prob > best_prob:
                best_prob = prob
                best_home = hg
                best_away = ag
    return best_home, best_away, best_prob


def _get_1x2_pred(matrix):
    home = draw = away = 0.0
    for hg, row in enumerate(matrix):
        for ag, prob in enumerate(row):
            if hg > ag:
                home += prob
            elif hg < ag:
                away += prob
            else:
                draw += prob
    if home >= draw and home >= away:
        return "home"
    elif draw >= away:
        return "draw"
    return "away"


def _get_1x2_label(hg, ag):
    if hg > ag:
        return "home"
    elif hg == ag:
        return "draw"
    return "away"


import math

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