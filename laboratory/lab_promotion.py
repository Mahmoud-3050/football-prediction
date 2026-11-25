"""Lab Promotion — Clean Holdout + Paired Bootstrap 95% CI + Audit Log & Rollback.

Config-only wins: atomic live config update with rollback.
Code+config wins: PR bundle export for human review.
"""

from __future__ import annotations

import json
import math
import os
import random
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from laboratory.store import connect, get_lab_experiment
from modeling.league_stacks import DEFAULT_LIVE_STACK, _merge_stack

# Scoring + annotation keys that must survive promote / rollback validation.
_PROMOTED_STACK_ANNOTATIONS = ("selected_experiment_id",)


class PromotionRejectedError(ValueError):
    """Raised when a promotion attempt fails Holdout, Bootstrap CI, or SPL guardrails."""


@dataclass(frozen=True)
class BootstrapResult:
    """Deterministic paired bootstrap confidence interval result."""

    mean_diff: float
    ci_lower: float
    ci_upper: float
    n_matches: int
    n_resamples: int
    confidence: float
    passes_gate: bool


def compute_paired_bootstrap_ci(
    candidate_losses: Sequence[float],
    baseline_losses: Sequence[float],
    *,
    n_resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 42,
) -> BootstrapResult:
    """Compute paired bootstrap CI for ``d_i = candidate_loss_i - baseline_loss_i``.

    Negative ``mean_diff`` means candidate has lower (better) loss than baseline.
    ``passes_gate`` is True iff ``ci_upper < 0.0`` (95% CI lies strictly below zero).
    """
    n = len(candidate_losses)
    if n == 0 or n != len(baseline_losses):
        raise ValueError(
            f"candidate_losses (len={n}) and baseline_losses (len={len(baseline_losses)}) "
            "must be non-empty and equal length."
        )
    if n_resamples <= 0:
        raise ValueError("n_resamples must be positive.")
    if not (0.0 < confidence < 1.0):
        raise ValueError("confidence must be in (0, 1).")

    diffs = [float(c) - float(b) for c, b in zip(candidate_losses, baseline_losses)]
    for d in diffs:
        if not math.isfinite(d):
            raise ValueError("All loss differences must be finite.")

    mean_diff = sum(diffs) / n
    rng = random.Random(seed)
    boot_means: list[float] = []
    for _ in range(n_resamples):
        total = 0.0
        for _ in range(n):
            total += diffs[rng.randrange(n)]
        boot_means.append(total / n)
    boot_means.sort()

    alpha = 1.0 - confidence
    lo_idx = max(0, min(n_resamples - 1, int(math.floor((alpha / 2.0) * (n_resamples - 1)))))
    hi_idx = max(0, min(n_resamples - 1, int(math.ceil((1.0 - alpha / 2.0) * (n_resamples - 1)))))
    ci_lower = boot_means[lo_idx]
    ci_upper = boot_means[hi_idx]
    return BootstrapResult(
        mean_diff=mean_diff,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        n_matches=n,
        n_resamples=n_resamples,
        confidence=confidence,
        passes_gate=(ci_upper < 0.0),
    )


@dataclass(frozen=True)
class PromotionGuardrails:
    """Guardrails that must pass for promotion."""

    min_holdout_matches: int = 30
    max_ece_degradation: float = 0.01
    max_brier_degradation: float = 0.005
    n_bootstrap_resamples: int = 2000
    confidence: float = 0.95
    bootstrap_seed: int = 42


@dataclass(frozen=True)
class PromotionResult:
    """Result of a promotion attempt."""

    success: bool
    promotion_type: str  # "config_only" | "code_pr_bundle" | "blocked"
    promotion_id: str | None
    message: str
    evidence: dict[str, Any] | None = None
    bundle_path: str | None = None


@dataclass(frozen=True)
class RollbackResult:
    """Result of a rollback."""

    success: bool
    promotion_id: str
    message: str


def _get_live_config_path(competition: str) -> Path:
    return Path("configs/live") / f"{competition.lower()}_stack.json"


def _get_lab_config_path(competition: str) -> Path:
    return Path("configs/lab") / f"{competition.lower()}_stack.json"


def _read_live_code_version(competition: str) -> str | None:
    path = _get_live_config_path(competition)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            config = json.load(f)
        return config.get("code_version")
    except Exception:
        return None


def _normalize_promoted_stack(config: Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
    raw = dict(config)
    normalized = _merge_stack(DEFAULT_LIVE_STACK, raw)
    for key in _PROMOTED_STACK_ANNOTATIONS:
        if key in raw and key not in normalized:
            value = raw[key]
            if isinstance(value, str) and value.strip():
                normalized[key] = value.strip()
    for key in ("lab_stack_version", "promoted_at", "source_experiment", "source_model_id", "_meta"):
        if key in raw:
            normalized[key] = raw[key]
    return normalized


def _write_live_config_atomic(
    competition: str,
    config: dict[str, Any],
    *,
    promotion_id: str,
    previous_config: dict[str, Any] | None,
) -> None:
    path = _get_live_config_path(competition)
    path.parent.mkdir(parents=True, exist_ok=True)

    config = _normalize_promoted_stack(config)
    config["lab_stack_version"] = promotion_id
    config["promoted_at"] = datetime.now(timezone.utc).isoformat()

    if previous_config:
        rollback_path = path.parent / f".{path.name}.rollback_{promotion_id}"
        with open(rollback_path, "w") as f:
            json.dump(previous_config, f, indent=2, sort_keys=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=f".{path.name}.tmp.",
        delete=False,
    ) as tmp:
        json.dump(config, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)

    os.replace(tmp_path, path)


def _export_pr_bundle(
    experiment_id: str,
    competition: str,
    winning_config: dict[str, Any],
    winning_model_id: str,
    evidence: dict[str, Any],
    lab_code_version: str,
) -> Path:
    bundle_dir = Path("promotions") / "pending_pr" / experiment_id
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)

    config_path = bundle_dir / f"{competition.lower()}_stack.json"
    config = _normalize_promoted_stack(winning_config)
    config["lab_stack_version"] = f"promotion-{experiment_id}"
    config["promoted_at"] = datetime.now(timezone.utc).isoformat()
    config["source_experiment"] = experiment_id
    config["source_model_id"] = winning_model_id
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2, sort_keys=True)

    evidence_path = bundle_dir / "promotion_evidence.json"
    with open(evidence_path, "w") as f:
        json.dump(evidence, f, indent=2, sort_keys=True)

    diff_path = bundle_dir / "model_changes.md"
    with open(diff_path, "w") as f:
        f.write(f"# Model Changes for {experiment_id}\n\n")
        f.write(f"**Competition**: {competition}\n")
        f.write(f"**Live Code Version**: {lab_code_version}\n")
        f.write(f"**Experiment Code Version**: {lab_code_version}\n")
        f.write(f"**Winning Model**: {winning_model_id}\n\n")
        f.write("## Config Changes\n\n```json\n")
        f.write(json.dumps(config, indent=2))
        f.write("\n```\n\n## Statistical Evidence\n\n| Metric | Value |\n|--------|-------|\n")
        for key, val in evidence.items():
            f.write(f"| {key} | {val} |\n")

    pr_path = bundle_dir / "PR_TEMPLATE.md"
    with open(pr_path, "w") as f:
        f.write(f"# Promotion PR: {experiment_id} -> {competition}\n\n")
        f.write(f"This PR promotes experiment **{experiment_id}** to live for **{competition}**.\n")

    return bundle_dir


def _record_promotion_audit(
    lab_db_path: Path,
    *,
    promotion_id: str,
    experiment_id: str,
    competition: str,
    promotion_type: str,
    promoted_model_id: str | None,
    live_code_version: str,
    exp_code_version: str,
    previous_config: dict[str, Any] | None,
    new_config: dict[str, Any] | None,
    bundle_path: str | None,
    evidence: dict[str, Any],
    status: str,
    rejection_reason: str | None = None,
) -> None:
    conn = connect(lab_db_path)
    try:
        conn.execute(
            """
            INSERT INTO lab_promotions (
                promotion_id, experiment_id, competition, promotion_type,
                promoted_model_id, live_code_version, exp_code_version,
                previous_config_json, new_config_json, bundle_path,
                evidence_json, status, rejection_reason, promoted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                promotion_id,
                experiment_id,
                competition,
                promotion_type,
                promoted_model_id,
                live_code_version,
                exp_code_version,
                json.dumps(previous_config or {}),
                json.dumps(new_config or {}),
                bundle_path,
                json.dumps(evidence),
                status,
                rejection_reason,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _load_matched_holdout_losses(
    lab_db_path: Path,
    *,
    candidate_experiment_id: str,
    candidate_model_id: str,
    baseline_experiment_id: str,
    baseline_model_id: str,
    holdout_seasons: Sequence[int],
    metric_key: str = "full_time_1x2",
) -> tuple[list[float], list[float], list[str]]:
    """Load 1-to-1 matched per-fixture Holdout losses from evaluations table."""
    season_strs = [str(int(s)) for s in holdout_seasons]
    if not season_strs:
        return [], [], []
    marks = ",".join("?" for _ in season_strs)
    conn = connect(lab_db_path)
    try:
        def _fetch(exp_id: str, mod_id: str) -> dict[str, float]:
            rows = conn.execute(
                f"""
                SELECT p.match_id, e.metrics_json
                FROM evaluations e
                JOIN predictions p ON p.prediction_id = e.prediction_id
                JOIN matches m ON m.match_id = p.match_id
                WHERE p.experiment_id = ? AND p.model_id = ? AND m.season IN ({marks})
                ORDER BY m.kickoff, p.match_id
                """,
                (exp_id, mod_id, *season_strs),
            ).fetchall()
            out: dict[str, float] = {}
            for r in rows:
                metrics = json.loads(r["metrics_json"])
                block = metrics.get(metric_key) or {}
                if block.get("status") == "scored" and "log_loss" in block:
                    out[str(r["match_id"])] = float(block["log_loss"])
            return out

        cand_map = _fetch(candidate_experiment_id, candidate_model_id)
        base_map = _fetch(baseline_experiment_id, baseline_model_id)
    finally:
        conn.close()

    common_ids = [mid for mid in cand_map if mid in base_map]
    return (
        [cand_map[mid] for mid in common_ids],
        [base_map[mid] for mid in common_ids],
        common_ids,
    )


def promote_experiment_run(
    lab_db_path: Path,
    experiment_id: str,
    *,
    competition: str,
    guardrails: PromotionGuardrails = PromotionGuardrails(),
    allow_spl: bool = False,
    holdout_candidate_losses: Sequence[float] | None = None,
    holdout_baseline_losses: Sequence[float] | None = None,
    baseline_experiment_id: str | None = None,
    baseline_model_id: str | None = None,
    brier_degradation: float = 0.0,
    ece_degradation: float = 0.0,
    dry_run: bool = False,
) -> PromotionResult:
    """Enforce Clean Holdout + Paired Bootstrap 95% CI + SPL isolation before promoting."""
    comp_upper = (competition or "").strip().upper()
    promo_stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    promotion_id = f"promo_{experiment_id}_{promo_stamp}"

    # 1. Never touch SPL automatically
    if comp_upper == "SPL" and not allow_spl:
        raise PromotionRejectedError(
            "Automatic promotion for SPL is blocked; pass allow_spl=True explicitly to preserve SPL isolation."
        )

    conn = connect(lab_db_path)
    exp = get_lab_experiment(conn, experiment_id)
    conn.close()

    if not exp:
        raise PromotionRejectedError(f"Experiment {experiment_id} not found.")
    if exp["status"] != "completed":
        raise PromotionRejectedError(f"Experiment {experiment_id} not completed (status: {exp['status']}).")

    best_config = exp.get("best_config")
    best_metrics = exp.get("best_metrics")
    if not best_config or not best_metrics:
        raise PromotionRejectedError("No winning config/metrics found on experiment.")

    best_model_id = str(best_config.get("model_id") or best_metrics.get("model_id") or experiment_id)
    exp_cfg = exp.get("config") or {}
    tuning_seasons = tuple(int(s) for s in (exp_cfg.get("tuning_seasons") or ()))
    holdout_seasons = tuple(int(s) for s in (exp_cfg.get("holdout_seasons") or ()))

    live_code_version = _read_live_code_version(comp_upper) or "unknown"
    exp_code_version = str(exp_cfg.get("code_version") or "score_model_v1")
    live_config = _read_live_config(comp_upper)

    def _reject(reason: str, evidence: dict[str, Any] | None = None) -> None:
        if not dry_run:
            _record_promotion_audit(
                lab_db_path,
                promotion_id=promotion_id,
                experiment_id=experiment_id,
                competition=comp_upper,
                promotion_type="blocked",
                promoted_model_id=best_model_id,
                live_code_version=live_code_version,
                exp_code_version=exp_code_version,
                previous_config=live_config,
                new_config=best_config,
                bundle_path=None,
                evidence=evidence or {},
                status="rejected",
                rejection_reason=reason,
            )
        raise PromotionRejectedError(reason)

    # 2. Mandatory Clean Holdout Evaluation (disjoint from tuning_seasons)
    if not holdout_seasons or (set(tuning_seasons) & set(holdout_seasons)):
        _reject(
            "Tuning-only results cannot be promoted without a clean Holdout evaluation."
        )

    cand_losses: list[float]
    base_losses: list[float]
    if holdout_candidate_losses is not None and holdout_baseline_losses is not None:
        cand_losses = [float(x) for x in holdout_candidate_losses]
        base_losses = [float(x) for x in holdout_baseline_losses]
    elif baseline_experiment_id and baseline_model_id:
        primary = str(exp_cfg.get("primary_metric") or "log_loss")
        metric_key = "exact_score" if primary == "exact_score_log_loss" else "full_time_1x2"
        cand_losses, base_losses, _ = _load_matched_holdout_losses(
            lab_db_path,
            candidate_experiment_id=experiment_id,
            candidate_model_id=best_model_id,
            baseline_experiment_id=baseline_experiment_id,
            baseline_model_id=baseline_model_id,
            holdout_seasons=holdout_seasons,
            metric_key=metric_key,
        )
    else:
        _reject(
            "Tuning-only results cannot be promoted without a clean Holdout evaluation."
        )

    if len(cand_losses) != len(base_losses) or len(cand_losses) < guardrails.min_holdout_matches:
        _reject(
            f"Clean Holdout sample too small or mismatched: n={len(cand_losses)} "
            f"(baseline={len(base_losses)}, required>={guardrails.min_holdout_matches})."
        )

    # 3. Mandatory Paired Bootstrap 95% CI Gate (ci_upper < 0.0)
    boot = compute_paired_bootstrap_ci(
        cand_losses,
        base_losses,
        n_resamples=guardrails.n_bootstrap_resamples,
        confidence=guardrails.confidence,
        seed=guardrails.bootstrap_seed,
    )
    evidence = {
        "experiment_id": experiment_id,
        "winning_model_id": best_model_id,
        "tuning_seasons": list(tuning_seasons),
        "holdout_seasons": list(holdout_seasons),
        "n_holdout_matches": boot.n_matches,
        "bootstrap_mean_diff": boot.mean_diff,
        "bootstrap_ci_lower": boot.ci_lower,
        "bootstrap_ci_upper": boot.ci_upper,
        "bootstrap_confidence": boot.confidence,
        "bootstrap_passes": boot.passes_gate,
        "brier_degradation": brier_degradation,
        "ece_degradation": ece_degradation,
    }
    if not boot.passes_gate:
        _reject(
            f"Bootstrap 95% CI gate failed: mean_diff={boot.mean_diff:+.4f}, "
            f"CI=[{boot.ci_lower:+.4f}, {boot.ci_upper:+.4f}], n={boot.n_matches} "
            "(ci_upper must be < 0.0).",
            evidence=evidence,
        )

    # 4. Anti-degradation checks
    if brier_degradation > guardrails.max_brier_degradation:
        _reject(
            f"Guardrail failed: Brier degradation {brier_degradation:.4f} > {guardrails.max_brier_degradation}",
            evidence=evidence,
        )
    if ece_degradation > guardrails.max_ece_degradation:
        _reject(
            f"Guardrail failed: ECE degradation {ece_degradation:.4f} > {guardrails.max_ece_degradation}",
            evidence=evidence,
        )

    # 5. Dual-path promotion (config_only atomic write vs code_pr_bundle)
    if live_code_version != "unknown" and exp_code_version != live_code_version:
        if dry_run:
            return PromotionResult(
                success=True,
                promotion_type="code_pr_bundle",
                promotion_id=promotion_id,
                message="Dry run: would export PR bundle (code version mismatch)",
                evidence=evidence,
            )
        bundle_path = _export_pr_bundle(
            experiment_id, comp_upper, best_config, best_model_id, evidence, exp_code_version
        )
        _record_promotion_audit(
            lab_db_path,
            promotion_id=promotion_id,
            experiment_id=experiment_id,
            competition=comp_upper,
            promotion_type="code_pr_bundle",
            promoted_model_id=best_model_id,
            live_code_version=live_code_version,
            exp_code_version=exp_code_version,
            previous_config=live_config,
            new_config=best_config,
            bundle_path=str(bundle_path),
            evidence=evidence,
            status="pending_pr",
        )
        return PromotionResult(
            success=True,
            promotion_type="code_pr_bundle",
            promotion_id=promotion_id,
            message=f"PR bundle exported to {bundle_path}. Requires human review and merge.",
            evidence=evidence,
            bundle_path=str(bundle_path),
        )

    if dry_run:
        return PromotionResult(
            success=True,
            promotion_type="config_only",
            promotion_id=promotion_id,
            message="Dry run: would atomically update live config",
            evidence=evidence,
        )

    _write_live_config_atomic(
        comp_upper, best_config, promotion_id=promotion_id, previous_config=live_config
    )
    _record_promotion_audit(
        lab_db_path,
        promotion_id=promotion_id,
        experiment_id=experiment_id,
        competition=comp_upper,
        promotion_type="config_only",
        promoted_model_id=best_model_id,
        live_code_version=live_code_version,
        exp_code_version=exp_code_version,
        previous_config=live_config,
        new_config=best_config,
        bundle_path=None,
        evidence=evidence,
        status="active",
    )
    return PromotionResult(
        success=True,
        promotion_type="config_only",
        promotion_id=promotion_id,
        message=f"Live config updated atomically. Rollback with: rollback_promotion('{comp_upper}', promotion_id='{promotion_id}')",
        evidence=evidence,
    )


def promote_best_config(
    lab_db_path: Path,
    experiment_id: str,
    *,
    competition: str,
    guardrails: PromotionGuardrails = PromotionGuardrails(),
    dry_run: bool = False,
    **kwargs: Any,
) -> PromotionResult:
    """Backward-compatible wrapper returning blocked PromotionResult on PromotionRejectedError."""
    try:
        return promote_experiment_run(
            lab_db_path,
            experiment_id,
            competition=competition,
            guardrails=guardrails,
            dry_run=dry_run,
            **kwargs,
        )
    except PromotionRejectedError as exc:
        return PromotionResult(
            success=False,
            promotion_type="blocked",
            promotion_id=None,
            message=str(exc),
        )


def get_promotion_audit_log(
    lab_db_path: Path,
    *,
    competition: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read promotion audit history (active, pending_pr, rejected, rolled_back)."""
    conn = connect(lab_db_path)
    try:
        sql = "SELECT * FROM lab_promotions"
        params: list[Any] = []
        if competition:
            sql += " WHERE competition = ?"
            params.append(competition.upper())
        sql += " ORDER BY promoted_at DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _read_live_config(competition: str) -> dict[str, Any] | None:
    path = _get_live_config_path(competition)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def rollback_promotion(
    lab_db_path: Path,
    competition: str,
    *,
    promotion_id: str | None = None,
) -> RollbackResult:
    """Rollback the most recent (or specific) promotion for a competition."""
    conn = connect(lab_db_path)
    try:
        if promotion_id:
            row = conn.execute(
                """
                SELECT promotion_id, previous_config_json FROM lab_promotions
                WHERE competition = ? AND promotion_id = ? AND status = 'active'
                """,
                (competition.upper(), promotion_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT promotion_id, previous_config_json FROM lab_promotions
                WHERE competition = ? AND status = 'active'
                ORDER BY promoted_at DESC LIMIT 1
                """,
                (competition.upper(),),
            ).fetchone()

        if not row:
            return RollbackResult(
                success=False,
                promotion_id=promotion_id or "unknown",
                message="No active promotion found to rollback",
            )

        promo_id = row["promotion_id"]
        previous_config = json.loads(row["previous_config_json"])

        if previous_config:
            _write_live_config_atomic(
                competition.upper(),
                previous_config,
                promotion_id=f"rollback_{promo_id}",
                previous_config=None,
            )

        conn.execute(
            "UPDATE lab_promotions SET status = 'rolled_back', rolled_back_at = ? WHERE promotion_id = ?",
            (datetime.now(timezone.utc).isoformat(), promo_id),
        )
        conn.commit()

        return RollbackResult(
            success=True,
            promotion_id=promo_id,
            message=f"Rolled back promotion {promo_id}. Live config restored.",
        )
    finally:
        conn.close()
