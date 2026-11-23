"""Streaming metric accumulators for walk-forward and HPC grid search.

Computes Log-Loss, Brier Score, and Accuracy without retaining per-match
probability matrices in memory.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from laboratory import LOG_LOSS_FLOOR


@dataclass
class StreamingMetricsAccumulator:
    """In-place streaming accumulator for evaluation metrics across a run."""

    n_matches: int = 0
    sums: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def add_evaluation(self, metrics: dict[str, Any]) -> None:
        self.n_matches += 1
        for market, vals in metrics.items():
            if vals.get("status") != "scored":
                continue
            if "log_loss" in vals:
                key = f"{market}_log_loss"
                self.sums[key] = self.sums.get(key, 0.0) + float(vals["log_loss"])
                self.counts[key] = self.counts.get(key, 0) + 1
            if "brier" in vals:
                key = f"{market}_brier"
                self.sums[key] = self.sums.get(key, 0.0) + float(vals["brier"])
                self.counts[key] = self.counts.get(key, 0) + 1
            if "correct" in vals:
                key = f"{market}_accuracy"
                self.sums[key] = self.sums.get(key, 0.0) + (1.0 if vals["correct"] else 0.0)
                self.counts[key] = self.counts.get(key, 0) + 1

    def finalize(self) -> dict[str, Any]:
        if self.n_matches == 0:
            return {}
        result: dict[str, Any] = {"n_matches": self.n_matches}
        for key, total in self.sums.items():
            cnt = self.counts.get(key, 0)
            if cnt > 0:
                result[key] = total / cnt
        for market in (
            "exact_score",
            "half_time_1x2",
            "over_under_2_5",
            "over_under_3_5",
        ):
            block: dict[str, Any] = {}
            acc_key = f"{market}_accuracy"
            ll_key = f"{market}_log_loss"
            if acc_key in result:
                block["hit"] = result[acc_key]
                block["accuracy"] = result[acc_key]
                if market == "exact_score":
                    result["exact_score_hit_rate"] = result[acc_key]
                    block["hit_rate"] = result[acc_key]
                    block["correct"] = result[acc_key]
            if ll_key in result:
                block["log_loss"] = result[ll_key]
            if self.counts.get(acc_key, 0) > 0:
                block["n"] = self.counts[acc_key]
            if block:
                result[market] = block
        return result


def log_loss(prob: float) -> float:
    return -math.log(max(float(prob), LOG_LOSS_FLOOR))
