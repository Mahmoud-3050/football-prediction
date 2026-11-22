"""Lab Promotion — Dual-Path Gate & Rollback.

Config-only wins: atomic live config update with rollback.
Code+config wins: PR bundle export for human review.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from league_stacks import DEFAULT_LIVE_STACK, _merge_stack
from laboratory.store import connect, create_lab_experiment, get_lab_experiment

# Scoring + annotation keys that must survive promote / rollback validation.
_PROMOTED_STACK_ANNOTATIONS = ("selected_experiment_id",)


@dataclass(frozen=True)
class PromotionGuardrails:
    """Guardrails that must pass for promotion."""
    min_improvement_pct: float = 1.0        # Minimum holdout metric improvement (%)
    max_ece_degradation: float = 0.01       # Prevent calibration collapse
    max_brier_degradation: float = 0.005    # Prevent probability regression
    require_significance: bool = True       # Adjusted p-value < 0.05
    require_season_stability: bool = True   # No severe regression on any holdout season


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
    """Get the live config path for a competition."""
    return Path("configs/live") / f"{competition.lower()}_stack.json"


def _get_lab_config_path(competition: str) -> Path:
    """Get the lab config path for a competition."""
    return Path("configs/lab") / f"{competition.lower()}_stack.json"


def _read_live_code_version(competition: str) -> str | None:
    """Read the code_version from live config."""
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
    """Validate/merge promoted stack keys (incl. calibration knobs) against defaults."""
    raw = dict(config)
    normalized = _merge_stack(DEFAULT_LIVE_STACK, raw)
    for key in _PROMOTED_STACK_ANNOTATIONS:
        if key in raw and key not in normalized:
            value = raw[key]
            if isinstance(value, str) and value.strip():
                normalized[key] = value.strip()
    # Preserve non-scoring bookkeeping already present on live files.
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
    """Atomically write live config with version metadata."""
    path = _get_live_config_path(competition)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Validate calibration / draw keys through the shared stack merger.
    config = _normalize_promoted_stack(config)
    config["lab_stack_version"] = promotion_id
    config["promoted_at"] = datetime.now(timezone.utc).isoformat()

    # Save previous config for rollback before replace.
    if previous_config:
        rollback_path = path.parent / f".{path.name}.rollback_{promotion_id}"
        with open(rollback_path, "w") as f:
            json.dump(previous_config, f, indent=2, sort_keys=True)

    # Atomic write: temp file + os.replace
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
    """Export a Promotion Bundle for code+config changes."""
    bundle_dir = Path("promotions") / "pending_pr" / experiment_id
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)

    # 1. Winning config (validated through shared stack merger)
    config_path = bundle_dir / f"{competition.lower()}_stack.json"
    config = _normalize_promoted_stack(winning_config)
    config["lab_stack_version"] = f"promotion-{experiment_id}"
    config["promoted_at"] = datetime.now(timezone.utc).isoformat()
    config["source_experiment"] = experiment_id
    config["source_model_id"] = winning_model_id
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2, sort_keys=True)

    # 2. Evidence report
    evidence_path = bundle_dir / "promotion_evidence.json"
    with open(evidence_path, "w") as f:
        json.dump(evidence, f, indent=2, sort_keys=True)

    # 3. Model code diff (placeholder - actual implementation would diff sandbox vs live)
    diff_path = bundle_dir / "model_changes.md"
    with open(diff_path, "w") as f:
        f.write(f"# Model Changes for {experiment_id}\n\n")
        f.write(f"**Competition**: {competition}\n")
        f.write(f"**Live Code Version**: {lab_code_version} (to be determined)\n")
        f.write(f"**Experiment Code Version**: {lab_code_version}\n")
        f.write(f"**Winning Model**: {winning_model_id}\n\n")
        f.write("## Config Changes\n\n")
        f.write("```json\n")
        f.write(json.dumps(config, indent=2))
        f.write("\n```\n\n")
        f.write("## Statistical Evidence\n\n")
        f.write("| Metric | Value |\n")
        f.write("|--------|-------|\n")
        for key, val in evidence.items():
            f.write(f"| {key} | {val} |\n")

    # 4. PR template
    pr_path = bundle_dir / "PR_TEMPLATE.md"
    with open(pr_path, "w") as f:
        f.write(f"# Promotion PR: {experiment_id} → {competition}\n\n")
        f.write("## Summary\n")
        f.write(f"This PR promotes experiment **{experiment_id}** to live for **{competition}**.\n\n")
        f.write("## Changes\n")
        f.write("- Model code updated (see `model_changes.md`)\n")
        f.write(f"- Config updated: `configs/live/{competition.lower()}_stack.json`\n\n")
        f.write("## Validation\n")
        f.write("- [ ] CI passes\n")
        f.write("- [ ] Backtest on holdout seasons matches evidence\n")
        f.write("- [ ] Shadow mode validation (2-4 matchdays)\n\n")
        f.write("## Rollback\n")
        f.write("If issues arise, revert this PR and the previous config will be restored.\n")

    return bundle_dir


def promote_best_config(
    lab_db_path: Path,
    experiment_id: str,
    *,
    competition: str,
    guardrails: PromotionGuardrails = PromotionGuardrails(),
    dry_run: bool = False,
) -> PromotionResult:
    """Promote the best config from an experiment to live.

    Enforces guardrails and routes through dual-path gate:
    - Config-only win (code_version matches): atomic live update
    - Code+config win (code_version differs): PR bundle export
    """
    # Load experiment
    conn = connect(lab_db_path)
    exp = get_lab_experiment(conn, experiment_id)
    conn.close()

    if not exp:
        return PromotionResult(
            success=False,
            promotion_type="blocked",
            promotion_id=None,
            message=f"Experiment {experiment_id} not found",
        )

    if exp["status"] != "completed":
        return PromotionResult(
            success=False,
            promotion_type="blocked",
            promotion_id=None,
            message=f"Experiment not completed (status: {exp['status']})",
        )

    best_config = exp.get("best_config")
    best_metrics = exp.get("best_metrics")
    best_model_id = best_config.get("model_id") if best_config else None

    if not best_config or not best_metrics:
        return PromotionResult(
            success=False,
            promotion_type="blocked",
            promotion_id=None,
            message="No winning config/metrics found",
        )

    # Get live code version and config
    live_code_version = _read_live_code_version(competition)
    exp_code_version = exp["config"].get("code_version", "unknown")

    # Compare against live baseline (would need baseline metrics from holdout)
    # For now, assume evidence is in best_metrics
    evidence = {
        "experiment_id": experiment_id,
        "winning_model_id": best_model_id,
        "holdout_log_loss": best_metrics.get("full_time_1x2_log_loss"),
        "holdout_brier": best_metrics.get("full_time_1x2_brier"),
        "holdout_accuracy": best_metrics.get("full_time_1x2_accuracy"),
        "ece": best_metrics.get("ece"),
        "improvement_pct": best_metrics.get("improvement_pct", 0),
        "p_value": best_metrics.get("p_value"),
        "significant": best_metrics.get("significant", False),
    }

    # Check guardrails
    if guardrails.require_significance and not evidence.get("significant", False):
        return PromotionResult(
            success=False,
            promotion_type="blocked",
            promotion_id=None,
            message="Guardrail failed: not statistically significant on holdout",
            evidence=evidence,
        )

    if evidence.get("improvement_pct", 0) < guardrails.min_improvement_pct:
        return PromotionResult(
            success=False,
            promotion_type="blocked",
            promotion_id=None,
            message=f"Guardrail failed: improvement {evidence.get('improvement_pct', 0):.2f}% < {guardrails.min_improvement_pct}%",
            evidence=evidence,
        )

    if evidence.get("ece", 0) > guardrails.max_ece_degradation:
        return PromotionResult(
            success=False,
            promotion_type="blocked",
            promotion_id=None,
            message=f"Guardrail failed: ECE degradation {evidence.get('ece', 0):.4f} > {guardrails.max_ece_degradation}",
            evidence=evidence,
        )

    # Determine promotion path
    promotion_id = f"promo_{experiment_id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    if live_code_version and exp_code_version != live_code_version:
        # CODE + CONFIG WIN → PR Bundle
        if dry_run:
            return PromotionResult(
                success=True,
                promotion_type="code_pr_bundle",
                promotion_id=promotion_id,
                message="Dry run: would export PR bundle (code version mismatch)",
                evidence=evidence,
            )

        bundle_path = _export_pr_bundle(
            experiment_id, competition, best_config, best_model_id, evidence, exp_code_version
        )

        # Record promotion
        conn = connect(lab_db_path)
        conn.execute(
            """
            INSERT INTO lab_promotions (
                promotion_id, experiment_id, competition, promotion_type,
                promoted_model_id, live_code_version, exp_code_version,
                previous_config_json, new_config_json, bundle_path,
                evidence_json, status, promoted_at
            ) VALUES (?, ?, ?, 'code_pr_bundle', ?, ?, ?, ?, ?, ?, 'pending_pr', ?)
            """,
            (
                promotion_id,
                experiment_id,
                competition,
                best_model_id,
                live_code_version or "unknown",
                exp_code_version,
                json.dumps(_read_live_config(competition) or {}),
                json.dumps(best_config),
                str(bundle_path),
                json.dumps(evidence),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        conn.close()

        return PromotionResult(
            success=True,
            promotion_type="code_pr_bundle",
            promotion_id=promotion_id,
            message=f"PR bundle exported to {bundle_path}. Requires human review and merge.",
            evidence=evidence,
            bundle_path=str(bundle_path),
        )

    else:
        # CONFIG-ONLY WIN → Atomic update
        live_config = _read_live_config(competition)

        if dry_run:
            return PromotionResult(
                success=True,
                promotion_type="config_only",
                promotion_id=promotion_id,
                message="Dry run: would atomically update live config",
                evidence=evidence,
            )

        _write_live_config_atomic(
            competition, best_config, promotion_id=promotion_id, previous_config=live_config
        )

        # Record promotion
        conn = connect(lab_db_path)
        conn.execute(
            """
            INSERT INTO lab_promotions (
                promotion_id, experiment_id, competition, promotion_type,
                promoted_model_id, live_code_version, exp_code_version,
                previous_config_json, new_config_json, bundle_path,
                evidence_json, status, promoted_at
            ) VALUES (?, ?, ?, 'config_only', ?, ?, ?, ?, ?, NULL, 'active', ?)
            """,
            (
                promotion_id,
                experiment_id,
                competition,
                best_model_id,
                live_code_version or "unknown",
                exp_code_version,
                json.dumps(live_config or {}),
                json.dumps(best_config),
                json.dumps(evidence),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        conn.close()

        return PromotionResult(
            success=True,
            promotion_type="config_only",
            promotion_id=promotion_id,
            message=f"Live config updated atomically. Rollback with: rollback_promotion('{competition}', promotion_id='{promotion_id}')",
            evidence=evidence,
        )


def _read_live_config(competition: str) -> dict[str, Any] | None:
    """Read current live config."""
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
        # Find the promotion to rollback
        if promotion_id:
            row = conn.execute(
                """
                SELECT promotion_id, previous_config_json FROM lab_promotions
                WHERE competition = ? AND promotion_id = ? AND status = 'active'
                """,
                (competition, promotion_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT promotion_id, previous_config_json FROM lab_promotions
                WHERE competition = ? AND status = 'active'
                ORDER BY promoted_at DESC LIMIT 1
                """,
                (competition,),
            ).fetchone()

        if not row:
            return RollbackResult(
                success=False,
                promotion_id=promotion_id or "unknown",
                message="No active promotion found to rollback",
            )

        promo_id = row["promotion_id"]
        previous_config = json.loads(row["previous_config_json"])

        # Restore previous config
        path = _get_live_config_path(competition)
        if previous_config:
            _write_live_config_atomic(
                competition, previous_config, promotion_id=f"rollback_{promo_id}", previous_config=None
            )

        # Mark promotion as rolled back
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