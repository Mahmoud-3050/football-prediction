"""Offline tests for Clean Holdout + Paired Bootstrap 95% CI + Promotion Audit Log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laboratory.lab_promotion import (
    PromotionGuardrails,
    PromotionRejectedError,
    compute_paired_bootstrap_ci,
    get_promotion_audit_log,
    promote_best_config,
    promote_experiment_run,
    rollback_promotion,
)
from laboratory.store import complete_lab_experiment, connect, create_lab_experiment


def _seed_completed_experiment(
    db_path: Path,
    *,
    exp_id: str,
    competition: str = "PL",
    tuning_seasons: list[int] | None = None,
    holdout_seasons: list[int] | None = None,
) -> None:
    conn = connect(db_path)
    try:
        cfg = {
            "experiment_id": exp_id,
            "competition": competition,
            "tuning_seasons": tuning_seasons if tuning_seasons is not None else [2023, 2024],
            "holdout_seasons": holdout_seasons if holdout_seasons is not None else [2025],
            "primary_metric": "log_loss",
            "code_version": "score_model_v1",
        }
        create_lab_experiment(conn, exp_id, competition, cfg)
        complete_lab_experiment(
            conn,
            exp_id,
            best_config={"k": 10.0, "model_id": f"{exp_id}-m1", "draw_boost": 1.0},
            best_metrics={"full_time_1x2_log_loss": 0.98, "n_matches": 100},
        )
    finally:
        conn.close()


def test_paired_bootstrap_ci_deterministic_and_strict() -> None:
    # Candidate strictly better (lower loss by 0.05) -> ci_upper < 0 -> passes_gate=True
    cand = [0.85] * 40
    base = [0.90] * 40
    r1 = compute_paired_bootstrap_ci(cand, base, n_resamples=500, seed=42)
    r2 = compute_paired_bootstrap_ci(cand, base, n_resamples=500, seed=42)
    assert r1 == r2
    assert r1.mean_diff < 0.0
    assert r1.ci_upper < 0.0
    assert r1.passes_gate is True

    # Candidate noisy around 0 -> CI includes 0 -> passes_gate=False
    cand_noisy = [0.88, 0.92] * 20
    base_noisy = [0.90] * 40
    r_noisy = compute_paired_bootstrap_ci(cand_noisy, base_noisy, n_resamples=500, seed=42)
    assert r_noisy.ci_upper >= 0.0
    assert r_noisy.passes_gate is False


def test_promotion_rejects_spl_and_tuning_only_and_failed_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "lab.db"

    # 1. SPL is blocked unless allow_spl=True
    _seed_completed_experiment(db_path, exp_id="EXP-SPL-1", competition="SPL")
    with pytest.raises(PromotionRejectedError, match="SPL"):
        promote_experiment_run(
            db_path,
            "EXP-SPL-1",
            competition="SPL",
            holdout_candidate_losses=[0.8] * 40,
            holdout_baseline_losses=[0.9] * 40,
        )

    # 2. Tuning-only (empty holdout_seasons) is blocked
    _seed_completed_experiment(db_path, exp_id="EXP-TUNE-ONLY", competition="PL", holdout_seasons=[])
    with pytest.raises(PromotionRejectedError, match="clean Holdout"):
        promote_experiment_run(
            db_path,
            "EXP-TUNE-ONLY",
            competition="PL",
            holdout_candidate_losses=[0.8] * 40,
            holdout_baseline_losses=[0.9] * 40,
        )

    # 3. Failed bootstrap CI is blocked and recorded in lab_promotions audit log
    _seed_completed_experiment(db_path, exp_id="EXP-NOISY", competition="PL")
    with pytest.raises(PromotionRejectedError, match="Bootstrap 95% CI gate failed"):
        promote_experiment_run(
            db_path,
            "EXP-NOISY",
            competition="PL",
            holdout_candidate_losses=[0.88, 0.93] * 20,
            holdout_baseline_losses=[0.90] * 40,
        )

    audit = get_promotion_audit_log(db_path, competition="PL")
    assert any(r["experiment_id"] == "EXP-NOISY" and r["status"] == "rejected" for r in audit)

    # Backward-compatible promote_best_config returns blocked PromotionResult
    res = promote_best_config(
        db_path,
        "EXP-NOISY",
        competition="PL",
        holdout_candidate_losses=[0.90] * 40,
        holdout_baseline_losses=[0.90] * 40,
    )
    assert res.success is False
    assert res.promotion_type == "blocked"


def test_promotion_passes_clean_holdout_writes_atomically_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "configs" / "live").mkdir(parents=True)
    initial_live = {"k": 6.0, "code_version": "score_model_v1", "model_id": "live-v1"}
    (tmp_path / "configs" / "live" / "pl_stack.json").write_text(json.dumps(initial_live))

    db_path = tmp_path / "lab.db"
    _seed_completed_experiment(db_path, exp_id="EXP-WIN-1", competition="PL")

    res = promote_experiment_run(
        db_path,
        "EXP-WIN-1",
        competition="PL",
        guardrails=PromotionGuardrails(min_holdout_matches=30, n_bootstrap_resamples=500),
        holdout_candidate_losses=[0.82] * 35,
        holdout_baseline_losses=[0.90] * 35,
    )
    assert res.success is True
    assert res.promotion_type == "config_only"
    assert res.promotion_id is not None

    promoted_live = json.loads((tmp_path / "configs" / "live" / "pl_stack.json").read_text())
    assert promoted_live["k"] == 10.0

    audit = get_promotion_audit_log(db_path, competition="PL")
    assert audit[0]["status"] == "active"
    assert audit[0]["promotion_id"] == res.promotion_id

    rb = rollback_promotion(db_path, "PL", promotion_id=res.promotion_id)
    assert rb.success is True
    restored = json.loads((tmp_path / "configs" / "live" / "pl_stack.json").read_text())
    assert restored["k"] == 6.0


def test_promotion_anti_holdout_pollution_guardrail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "configs" / "live").mkdir(parents=True)
    (tmp_path / "configs" / "live" / "pl_stack.json").write_text(
        json.dumps({"k": 6.0, "code_version": "score_model_v1", "model_id": "live-v1"})
    )

    db_path = tmp_path / "lab.db"
    guardrails = PromotionGuardrails(
        min_holdout_matches=30,
        max_holdout_candidates=2,
        n_bootstrap_resamples=300,
    )

    for idx in (1, 2):
        exp_id = f"EXP-POLL-{idx}"
        _seed_completed_experiment(db_path, exp_id=exp_id, competition="PL", holdout_seasons=[2025])
        res = promote_experiment_run(
            db_path,
            exp_id,
            competition="PL",
            guardrails=guardrails,
            holdout_candidate_losses=[0.82] * 35,
            holdout_baseline_losses=[0.90] * 35,
            reason=f"Candidate {idx}",
        )
        assert res.success is True

    _seed_completed_experiment(db_path, exp_id="EXP-POLL-3", competition="PL", holdout_seasons=[2025])
    with pytest.raises(PromotionRejectedError, match="Holdout-Pollution"):
        promote_experiment_run(
            db_path,
            "EXP-POLL-3",
            competition="PL",
            guardrails=guardrails,
            holdout_candidate_losses=[0.82] * 35,
            holdout_baseline_losses=[0.90] * 35,
            reason="Candidate 3",
        )


def test_promotion_embeds_promotion_meta_and_audit_log_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "configs" / "live").mkdir(parents=True)
    (tmp_path / "configs" / "live" / "pl_stack.json").write_text(
        json.dumps({"k": 6.0, "code_version": "score_model_v1", "model_id": "live-v1"})
    )

    db_path = tmp_path / "lab.db"
    _seed_completed_experiment(db_path, exp_id="EXP-META-1", competition="PL")

    res = promote_experiment_run(
        db_path,
        "EXP-META-1",
        competition="PL",
        guardrails=PromotionGuardrails(min_holdout_matches=30, n_bootstrap_resamples=300),
        holdout_candidate_losses=[0.80] * 35,
        holdout_baseline_losses=[0.90] * 35,
        reason="Verified clean holdout improvement",
    )
    assert res.success is True

    promoted = json.loads((tmp_path / "configs" / "live" / "pl_stack.json").read_text())
    meta = promoted["promotion_meta"]
    assert meta["experiment_id"] == "EXP-META-1"
    assert meta["n_holdout_matches"] == 35
    assert meta["mean_diff"] < 0.0
    assert meta["ci_upper"] < 0.0
    assert meta["reason"] == "Verified clean holdout improvement"

    audit = get_promotion_audit_log(db_path, competition="PL")
    assert len(audit) == 1
    row = audit[0]
    assert row["experiment_id"] == "EXP-META-1"
    assert row["reason"] == "Verified clean holdout improvement"
    assert row["n_holdout_matches"] == 35
    assert row["mean_diff"] == pytest.approx(meta["mean_diff"])
    assert row["ci_lower"] == pytest.approx(meta["ci_lower"])
    assert row["ci_upper"] == pytest.approx(meta["ci_upper"])
    assert row["backup_path"]
