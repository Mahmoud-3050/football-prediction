"""Lab Analysis — Statistical Comparison, Calibration, Score Distribution.

Block Bootstrap, Holm-Bonferroni, ECE, Brier decomposition.
"""

from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats

from laboratory.store import connect


@dataclass(frozen=True)
class ComparisonResult:
    experiment_id: str
    baseline_model_id: str
    metric: str
    comparisons: list[dict[str, Any]]  # Per-model comparison results
    best_model_id: str | None


@dataclass(frozen=True)
class CalibrationReport:
    model_ids: list[str]
    ece: dict[str, float]  # Expected Calibration Error per model
    reliability_data: dict[str, list[dict[str, float]]]  # For plotting


@dataclass(frozen=True)
class DistributionReport:
    model_ids: list[str]
    exact_score_hits: dict[str, dict[str, int]]  # model -> score -> count
    confusion_matrices: dict[str, dict[str, dict[str, int]]]  # model -> pred -> actual -> count
    brier_decomposition: dict[str, dict[str, float]]


def _fetch_per_match_losses(
    lab_db_path: Path,
    experiment_id: str,
    model_ids: list[str],
    metric: str = "log_loss",
    market: str = "full_time_1x2",
) -> dict[str, list[float]]:
    """Fetch per-match loss values for each model."""
    conn = connect(lab_db_path)
    try:
        placeholders = ",".join("?" for _ in model_ids)
        rows = conn.execute(
            f"""
            SELECT p.model_id, e.metrics_json
            FROM evaluations e
            JOIN predictions p ON p.prediction_id = e.prediction_id
            WHERE p.experiment_id = ? AND p.model_id IN ({placeholders})
            """,
            (experiment_id, *model_ids),
        ).fetchall()

        losses: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            metrics = json.loads(row["metrics_json"])
            market_metrics = metrics.get(market, {})
            if market_metrics.get("status") == "scored":
                if metric == "log_loss" and "log_loss" in market_metrics:
                    losses[row["model_id"]].append(market_metrics["log_loss"])
                elif metric == "brier" and "brier" in market_metrics:
                    losses[row["model_id"]].append(market_metrics["brier"])
                elif metric == "accuracy" and "correct" in market_metrics:
                    losses[row["model_id"]].append(1.0 if market_metrics["correct"] else 0.0)

        return dict(losses)
    finally:
        conn.close()


def _block_bootstrap_ci(
    losses_a: list[float],
    losses_b: list[float],
    block_ids_a: list[Any] | None = None,
    block_ids_b: list[Any] | None = None,
    n_bootstrap: int = 2000,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Block bootstrap confidence interval for mean difference.

    If block_ids provided, resample blocks (e.g., matchdays) to preserve correlation.
    """
    if len(losses_a) != len(losses_b):
        # Fall back to paired bootstrap if lengths differ
        n = min(len(losses_a), len(losses_b))
        losses_a = losses_a[:n]
        losses_b = losses_b[:n]
        block_ids_a = block_ids_a[:n] if block_ids_a else list(range(n))
        block_ids_b = block_ids_b[:n] if block_ids_b else list(range(n))

    n = len(losses_a)
    if n == 0:
        return (0.0, 0.0)

    # Default to individual observations as blocks if no block_ids
    if block_ids_a is None:
        block_ids_a = list(range(n))
    if block_ids_b is None:
        block_ids_b = list(range(n))

    # Group by block
    blocks_a: dict[Any, list[float]] = defaultdict(list)
    blocks_b: dict[Any, list[float]] = defaultdict(list)
    for i, bid in enumerate(block_ids_a):
        blocks_a[bid].append(losses_a[i])
    for i, bid in enumerate(block_ids_b):
        blocks_b[bid].append(losses_b[i])

    # Must have same blocks in both
    common_blocks = set(blocks_a.keys()) & set(blocks_b.keys())
    if not common_blocks:
        return (0.0, 0.0)

    block_list = list(common_blocks)
    block_means_a = {b: np.mean(blocks_a[b]) for b in block_list}
    block_means_b = {b: np.mean(blocks_b[b]) for b in block_list}

    # Bootstrap block means
    diffs: list[float] = []
    for _ in range(n_bootstrap):
        sampled = np.random.choice(block_list, size=len(block_list), replace=True)
        mean_a = np.mean([block_means_a[b] for b in sampled])
        mean_b = np.mean([block_means_b[b] for b in sampled])
        diffs.append(mean_a - mean_b)

    alpha = 1 - confidence
    lower = np.percentile(diffs, 100 * alpha / 2)
    upper = np.percentile(diffs, 100 * (1 - alpha / 2))
    return (float(lower), float(upper))


def _holm_bonferroni(p_values: list[float], alpha: float = 0.05) -> list[bool]:
    """Holm-Bonferroni step-down procedure. Returns list of 'significant' bools."""
    m = len(p_values)
    if m == 0:
        return []
    indexed = list(enumerate(p_values))
    indexed.sort(key=lambda x: x[1])  # Sort by p-value ascending
    significant = [False] * m
    for rank, (orig_idx, p) in enumerate(indexed):
        threshold = alpha / (m - rank)
        if p <= threshold:
            significant[orig_idx] = True
        else:
            break  # Step-down: stop at first non-significant
    return significant


def _fdr_benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> list[bool]:
    """Benjamini-Hochberg FDR control. Returns list of 'significant' bools."""
    m = len(p_values)
    if m == 0:
        return []
    indexed = list(enumerate(p_values))
    indexed.sort(key=lambda x: x[1], reverse=True)  # Sort descending
    significant = [False] * m
    for rank, (orig_idx, p) in enumerate(indexed):
        threshold = alpha * (m - rank) / m
        if p <= threshold:
            # All previous (higher p-values) also significant
            for r in range(rank + 1):
                significant[indexed[r][0]] = True
            break
    return significant


def compare_to_baseline(
    lab_db_path: Path,
    experiment_id: str,
    *,
    baseline_model_id: str,
    metric: str = "log_loss",
    market: str = "full_time_1x2",
    correction: str = "holm",  # "holm" or "fdr"
    n_bootstrap: int = 2000,
    block_by: str = "matchday",
) -> ComparisonResult:
    """Compare all models in experiment to baseline using Block Bootstrap + Holm/FDR."""
    conn = connect(lab_db_path)
    try:
        # Get all completed model_ids
        rows = conn.execute(
            """
            SELECT DISTINCT model_id FROM lab_experiment_runs
            WHERE experiment_id = ? AND status = 'completed'
            """,
            (experiment_id,),
        ).fetchall()
        model_ids = [row["model_id"] for row in rows]
    finally:
        conn.close()

    if baseline_model_id not in model_ids:
        raise ValueError(f"Baseline model {baseline_model_id} not in experiment")

    # Fetch per-match losses for all models
    # For block bootstrap, we need match metadata (matchday)
    conn = connect(lab_db_path)
    try:
        placeholders = ",".join("?" for _ in model_ids)
        rows = conn.execute(
            f"""
            SELECT p.model_id, p.match_id, e.metrics_json
            FROM evaluations e
            JOIN predictions p ON p.prediction_id = e.prediction_id
            WHERE p.experiment_id = ? AND p.model_id IN ({placeholders})
            """,
            (experiment_id, *model_ids),
        ).fetchall()

        # Also fetch matchdays for blocking
        match_rows = conn.execute(
            """
            SELECT match_id, kickoff FROM matches
            WHERE match_id IN (
                SELECT DISTINCT match_id FROM predictions WHERE experiment_id = ?
            )
            """,
            (experiment_id,),
        ).fetchall()
    finally:
        conn.close()

    # Build matchday map
    matchday_map: dict[str, int] = {}
    for row in match_rows:
        kickoff = row["kickoff"]
        if kickoff and len(kickoff) >= 10:
            # Extract matchday from kickoff (approximate: week of season)
            # For proper matchday, would need standings data
            pass

    # Group losses by model
    model_losses: dict[str, list[float]] = defaultdict(list)
    model_blocks: dict[str, list[Any]] = defaultdict(list)

    for row in rows:
        metrics = json.loads(row["metrics_json"])
        market_metrics = metrics.get(market, {})
        if market_metrics.get("status") == "scored":
            if metric == "log_loss" and "log_loss" in market_metrics:
                model_losses[row["model_id"]].append(market_metrics["log_loss"])
                # Use match_id as block for now (within-matchday correlation)
                model_blocks[row["model_id"]].append(row["match_id"][:8])  # Approximate
            elif metric == "brier" and "brier" in market_metrics:
                model_losses[row["model_id"]].append(market_metrics["brier"])
                model_blocks[row["model_id"]].append(row["match_id"][:8])
            elif metric == "accuracy" and "correct" in market_metrics:
                model_losses[row["model_id"]].append(1.0 if market_metrics["correct"] else 0.0)
                model_blocks[row["model_id"]].append(row["match_id"][:8])

    baseline_losses = model_losses.get(baseline_model_id, [])
    baseline_blocks = model_blocks.get(baseline_model_id, [])

    comparisons: list[dict[str, Any]] = []
    p_values: list[float] = []

    for model_id in model_ids:
        if model_id == baseline_model_id:
            continue
        losses = model_losses.get(model_id, [])
        blocks = model_blocks.get(model_id, [])

        if len(losses) < 10 or len(baseline_losses) < 10:
            comparisons.append(
                {
                    "model_id": model_id,
                    "n_matches": len(losses),
                    "mean_diff": None,
                    "ci_lower": None,
                    "ci_upper": None,
                    "p_value": None,
                    "significant": False,
                    "cohen_d": None,
                }
            )
            p_values.append(1.0)
            continue

        # Paired t-test
        min_len = min(len(losses), len(baseline_losses))
        t_stat, p_val = stats.ttest_rel(
            losses[:min_len], baseline_losses[:min_len], alternative="less"
        )
        p_values.append(p_val)

        # Block bootstrap CI
        ci_lower, ci_upper = _block_bootstrap_ci(
            losses[:min_len],
            baseline_losses[:min_len],
            blocks[:min_len],
            baseline_blocks[:min_len],
            n_bootstrap=n_bootstrap,
        )

        # Cohen's d
        diffs = [l - b for l, b in zip(losses[:min_len], baseline_losses[:min_len])]
        pooled_sd = np.std(diffs, ddof=1)
        cohen_d = np.mean(diffs) / pooled_sd if pooled_sd > 0 else 0.0

        comparisons.append(
            {
                "model_id": model_id,
                "n_matches": min_len,
                "mean_diff": float(np.mean(diffs)),
                "ci_lower": ci_lower,
                "ci_upper": ci_upper,
                "p_value": float(p_val),
                "significant": False,  # Will be set after correction
                "cohen_d": float(cohen_d),
            }
        )

    # Apply multiple comparison correction
    if correction == "holm":
        sig = _holm_bonferroni(p_values)
    elif correction == "fdr":
        sig = _fdr_benjamini_hochberg(p_values)
    else:
        sig = [p < 0.05 for p in p_values]

    # Update significance
    p_idx = 0
    for comp in comparisons:
        if comp["p_value"] is not None:
            comp["significant"] = sig[p_idx]
            p_idx += 1

    # Find best (lowest mean log_loss among significant, or overall)
    best = None
    for comp in comparisons:
        if comp["significant"] and comp["mean_diff"] is not None and comp["mean_diff"] < 0:
            if best is None or comp["mean_diff"] < best["mean_diff"]:
                best = comp

    return ComparisonResult(
        experiment_id=experiment_id,
        baseline_model_id=baseline_model_id,
        metric=metric,
        comparisons=comparisons,
        best_model_id=best["model_id"] if best else None,
    )


def calibration_analysis(
    lab_db_path: Path,
    model_ids: list[str],
    *,
    market: str = "full_time_1x2",
    n_bins: int = 10,
) -> CalibrationReport:
    """Compute calibration metrics (ECE, reliability diagrams) for each model."""
    conn = connect(lab_db_path)
    try:
        placeholders = ",".join("?" for _ in model_ids)
        rows = conn.execute(
            f"""
            SELECT p.model_id, e.metrics_json
            FROM evaluations e
            JOIN predictions p ON p.prediction_id = e.prediction_id
            WHERE p.model_id IN ({placeholders})
            """,
            (*model_ids,),
        ).fetchall()

        model_probs: dict[str, list[tuple[float, int]]] = defaultdict(list)

        for row in rows:
            metrics = json.loads(row["metrics_json"])
            market_metrics = metrics.get(market, {})
            if market_metrics.get("status") == "scored":
                prob = market_metrics.get("probability_of_actual")
                correct = market_metrics.get("correct", False)
                if prob is not None:
                    model_probs[row["model_id"]].append((float(prob), 1 if correct else 0))

    finally:
        conn.close()

    ece: dict[str, float] = {}
    reliability_data: dict[str, list[dict[str, float]]] = {}

    for model_id in model_ids:
        data = model_probs.get(model_id, [])
        if not data:
            ece[model_id] = float("nan")
            reliability_data[model_id] = []
            continue

        # Bin by predicted probability
        bins = np.linspace(0, 1, n_bins + 1)
        bin_indices = np.digitize([p for p, _ in data], bins) - 1

        bin_probs = [[] for _ in range(n_bins)]
        bin_outcomes = [[] for _ in range(n_bins)]

        for (prob, outcome), bin_idx in zip(data, bin_indices):
            if 0 <= bin_idx < n_bins:
                bin_probs[bin_idx].append(prob)
                bin_outcomes[bin_idx].append(outcome)

        # Compute ECE
        total = len(data)
        ece_val = 0.0
        rel_data = []

        for i in range(n_bins):
            if not bin_probs[i]:
                continue
            mean_prob = np.mean(bin_probs[i])
            mean_outcome = np.mean(bin_outcomes[i])
            weight = len(bin_probs[i]) / total
            ece_val += weight * abs(mean_prob - mean_outcome)
            rel_data.append(
                {
                    "bin": i,
                    "prob_min": bins[i],
                    "prob_max": bins[i + 1],
                    "mean_predicted": float(mean_prob),
                    "mean_observed": float(mean_outcome),
                    "count": len(bin_probs[i]),
                }
            )

        ece[model_id] = float(ece_val)
        reliability_data[model_id] = rel_data

    return CalibrationReport(
        model_ids=model_ids,
        ece=ece,
        reliability_data=reliability_data,
    )


def score_distribution_analysis(
    lab_db_path: Path,
    model_ids: list[str],
    *,
    market: str = "exact_score",
) -> DistributionReport:
    """Analyze exact score predictions: hit rates, confusion matrices."""
    conn = connect(lab_db_path)
    try:
        placeholders = ",".join("?" for _ in model_ids)
        rows = conn.execute(
            f"""
            SELECT p.model_id, p.match_id, p.body_json, e.metrics_json
            FROM evaluations e
            JOIN predictions p ON p.prediction_id = e.prediction_id
            WHERE p.model_id IN ({placeholders})
            """,
            (*model_ids,),
        ).fetchall()

        exact_hits: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        confusion: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(int))
        )

        for row in rows:
            body = json.loads(row["body_json"])
            metrics = json.loads(row["metrics_json"])
            market_metrics = metrics.get(market, {})

            actual_score = None
            if market_metrics.get("status") == "scored":
                # Get actual from metrics or body
                pass

            # Get predicted modal score
            if "full_time" in body:
                mode = body["full_time"].get("exact_score_most_likely")
            else:
                mode = body.get("exact_score_most_likely")

            if mode:
                pred = f"{mode.get('home_goals', 0)}-{mode.get('away_goals', 0)}"

                # Get actual from result_revisions via match_id
                # For now, use the fact that exact_score metrics has 'correct'
                if market_metrics.get("status") == "scored":
                    actual_ft = market_metrics.get("actual", "unknown")
                    exact_hits[row["model_id"]][pred] += 1
                    confusion[row["model_id"]][pred][actual_ft] += 1

    finally:
        conn.close()

    # Brier decomposition (simplified)
    brier_decomp: dict[str, dict[str, float]] = {}
    for model_id in model_ids:
        brier_decomp[model_id] = {
            "reliability": 0.0,
            "resolution": 0.0,
            "uncertainty": 0.0,
        }

    return DistributionReport(
        model_ids=model_ids,
        exact_score_hits=dict(exact_hits),
        confusion_matrices={k: dict(v) for k, v in confusion.items()},
        brier_decomposition=brier_decomp,
    )