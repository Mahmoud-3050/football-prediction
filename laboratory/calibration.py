"""Reliability diagrams, Expected Calibration Error (ECE), and out-of-sample 1X2 calibration."""

from __future__ import annotations

import math
from typing import Any, Sequence

_EPS = 1e-12


def compute_reliability_bins(
    probs: Sequence[float],
    outcomes: Sequence[int],
    *,
    n_bins: int = 10,
) -> dict[str, Any]:
    """Compute equal-width reliability diagram bins, ECE, and Brier decomposition."""
    if len(probs) != len(outcomes):
        raise ValueError("probs and outcomes must have equal length.")
    if n_bins <= 0:
        raise ValueError("n_bins must be positive.")

    n = len(probs)
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for p_raw, y_raw in zip(probs, outcomes):
        p = max(0.0, min(1.0, float(p_raw)))
        y = 1 if int(y_raw) else 0
        idx = min(n_bins - 1, int(p * n_bins))
        buckets[idx].append((p, y))

    bins_out: list[dict[str, Any]] = []
    ece = 0.0
    reliability = 0.0
    resolution = 0.0
    base_rate = (sum(1 if int(y) else 0 for y in outcomes) / n) if n > 0 else 0.0
    uncertainty = base_rate * (1.0 - base_rate)

    for i, bucket in enumerate(buckets):
        lower = i / n_bins
        upper = (i + 1) / n_bins
        cnt = len(bucket)
        if cnt == 0:
            bins_out.append(
                {
                    "bin_index": i,
                    "lower": lower,
                    "upper": upper,
                    "count": 0,
                    "mean_predicted": None,
                    "empirical_frequency": None,
                    "abs_gap": None,
                }
            )
            continue
        mean_p = sum(p for p, _ in bucket) / cnt
        emp_f = sum(y for _, y in bucket) / cnt
        gap = abs(mean_p - emp_f)
        weight = cnt / n
        ece += weight * gap
        reliability += weight * ((mean_p - emp_f) ** 2)
        resolution += weight * ((emp_f - base_rate) ** 2)
        bins_out.append(
            {
                "bin_index": i,
                "lower": lower,
                "upper": upper,
                "count": cnt,
                "mean_predicted": mean_p,
                "empirical_frequency": emp_f,
                "abs_gap": gap,
            }
        )

    brier = (
        sum((max(0.0, min(1.0, float(p))) - (1 if int(y) else 0)) ** 2 for p, y in zip(probs, outcomes)) / n
        if n > 0
        else None
    )

    return {
        "n": n,
        "n_bins": n_bins,
        "ece": ece if n > 0 else None,
        "brier": brier,
        "reliability": reliability if n > 0 else None,
        "resolution": resolution if n > 0 else None,
        "uncertainty": uncertainty if n > 0 else None,
        "bins": bins_out,
    }


def apply_1x2_calibration(
    probs_1x2: tuple[float, float, float],
    *,
    temperature: float = 1.0,
    draw_bias: float = 0.0,
) -> tuple[float, float, float]:
    """Apply temperature scaling and draw-logit bias to (p_home, p_draw, p_away)."""
    if not math.isfinite(temperature) or temperature <= 0.0:
        raise ValueError("temperature must be a finite positive number.")
    if not math.isfinite(draw_bias):
        raise ValueError("draw_bias must be finite.")

    p0, p1, p2 = float(probs_1x2[0]), float(probs_1x2[1]), float(probs_1x2[2])
    if temperature == 1.0 and draw_bias == 0.0:
        total = p0 + p1 + p2
        if abs(total - 1.0) <= 1e-12:
            return (p0, p1, p2)
        if total > 0.0:
            return (p0 / total, p1 / total, p2 / total)

    logits = [
        math.log(max(p0, _EPS)) / temperature,
        (math.log(max(p1, _EPS)) / temperature) + draw_bias,
        math.log(max(p2, _EPS)) / temperature,
    ]
    m = max(logits)
    exps = [math.exp(x - m) for x in logits]
    denom = sum(exps)
    return (exps[0] / denom, exps[1] / denom, exps[2] / denom)


def _coerce_outcome_index(outcome: int | str) -> int:
    if isinstance(outcome, int) and not isinstance(outcome, bool):
        if outcome in (0, 1, 2):
            return outcome
    raw = str(outcome).strip().lower()
    if raw in {"0", "h", "home", "1"}:
        return 0
    if raw in {"1", "d", "draw", "x"}:
        return 1
    if raw in {"2", "a", "away"}:
        return 2
    raise ValueError(f"Unsupported 1X2 outcome: {outcome!r}")


def fit_temperature_platt_1x2(
    train_probs: Sequence[tuple[float, float, float]],
    train_outcomes: Sequence[int | str],
    *,
    fit_draw_intercept: bool = True,
) -> dict[str, float]:
    """Fit (temperature, draw_bias) by minimizing 1X2 cross-entropy on train_probs only."""
    if len(train_probs) != len(train_outcomes):
        raise ValueError("train_probs and train_outcomes must have equal length.")
    if not train_probs:
        return {"temperature": 1.0, "draw_bias": 0.0}

    targets = [_coerce_outcome_index(y) for y in train_outcomes]
    log_probs = [
        (
            math.log(max(float(p[0]), _EPS)),
            math.log(max(float(p[1]), _EPS)),
            math.log(max(float(p[2]), _EPS)),
        )
        for p in train_probs
    ]

    def _loss(temp: float, bias: float) -> float:
        inv_t = 1.0 / temp
        total = 0.0
        for (l0, l1, l2), y in zip(log_probs, targets):
            z0 = l0 * inv_t
            z1 = l1 * inv_t + bias
            z2 = l2 * inv_t
            m = max(z0, z1, z2)
            e0 = math.exp(z0 - m)
            e1 = math.exp(z1 - m)
            e2 = math.exp(z2 - m)
            denom = e0 + e1 + e2
            p_y = (e0 if y == 0 else (e1 if y == 1 else e2)) / denom
            total -= math.log(max(p_y, _EPS))
        return total / len(targets)

    best_t = 1.0
    best_b = 0.0
    best_l = _loss(best_t, best_b)

    t_lo, t_hi = 0.4, 2.5
    b_lo, b_hi = (-0.8, 0.8) if fit_draw_intercept else (0.0, 0.0)

    for _round in range(3):
        t_steps = 15
        b_steps = 17 if fit_draw_intercept else 1
        t_grid = [t_lo + (t_hi - t_lo) * i / (t_steps - 1) for i in range(t_steps)]
        b_grid = (
            [b_lo + (b_hi - b_lo) * j / (b_steps - 1) for j in range(b_steps)]
            if fit_draw_intercept
            else [0.0]
        )
        for t in t_grid:
            for b in b_grid:
                cur = _loss(t, b)
                if cur < best_l:
                    best_l = cur
                    best_t = t
                    best_b = b
        t_span = (t_hi - t_lo) * 0.25
        t_lo = max(0.25, best_t - t_span)
        t_hi = min(4.0, best_t + t_span)
        if fit_draw_intercept:
            b_span = (b_hi - b_lo) * 0.25
            b_lo = max(-2.0, best_b - b_span)
            b_hi = min(2.0, best_b + b_span)

    return {"temperature": float(best_t), "draw_bias": float(best_b)}
