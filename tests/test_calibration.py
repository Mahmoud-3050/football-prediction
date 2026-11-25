"""Offline tests for Reliability Diagrams, ECE, Platt/Temperature scaling, and Lab calibration."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from laboratory.calibration import (
    apply_1x2_calibration,
    compute_reliability_bins,
    fit_temperature_platt_1x2,
)
from laboratory.lab_experiment import ExperimentConfig
from laboratory.lab_runner import run_experiment
from laboratory.store import connect


def test_apply_1x2_calibration_identity() -> None:
    probs = (0.45, 0.27, 0.28)
    out = apply_1x2_calibration(probs, temperature=1.0, draw_bias=0.0)
    assert out == probs
    assert sum(out) == pytest.approx(1.0)


def test_compute_reliability_bins_accuracy_and_ece() -> None:
    probs = [0.15, 0.15, 0.85, 0.85]
    outcomes = [0, 0, 1, 1]
    res = compute_reliability_bins(probs, outcomes, n_bins=10)
    assert res["n"] == 4
    assert res["ece"] == pytest.approx(0.15)
    b1 = res["bins"][1]  # [0.1, 0.2)
    b8 = res["bins"][8]  # [0.8, 0.9)
    assert b1["count"] == 2
    assert b1["mean_predicted"] == pytest.approx(0.15)
    assert b1["empirical_frequency"] == pytest.approx(0.0)
    assert b8["count"] == 2
    assert b8["mean_predicted"] == pytest.approx(0.85)
    assert b8["empirical_frequency"] == pytest.approx(1.0)


def test_fit_temperature_platt_1x2_corrects_draw_underconfidence() -> None:
    # Synthetic dataset where draws are systematically under-predicted (p_draw=0.15, actual ~35%)
    train_probs = [(0.50, 0.15, 0.35)] * 100
    train_outcomes = [0] * 40 + [1] * 35 + [2] * 25

    fit = fit_temperature_platt_1x2(train_probs, train_outcomes, fit_draw_intercept=True)
    assert fit["draw_bias"] > 0.2

    test_probs = [(0.50, 0.15, 0.35)] * 60
    test_outcomes = [0] * 24 + [1] * 21 + [2] * 15

    def _log_loss(plist, ylist):
        return -sum(math.log(max(p[y], 1e-12)) for p, y in zip(plist, ylist)) / len(ylist)

    uncal_ll = _log_loss(test_probs, test_outcomes)
    cal_probs = [
        apply_1x2_calibration(p, temperature=fit["temperature"], draw_bias=fit["draw_bias"])
        for p in test_probs
    ]
    cal_ll = _log_loss(cal_probs, test_outcomes)

    uncal_draw_ece = compute_reliability_bins([p[1] for p in test_probs], [1 if y == 1 else 0 for y in test_outcomes])["ece"]
    cal_draw_ece = compute_reliability_bins([p[1] for p in cal_probs], [1 if y == 1 else 0 for y in test_outcomes])["ece"]

    assert cal_ll < uncal_ll
    assert cal_draw_ece < uncal_draw_ece


def test_lab_experiment_platt_fit_prior_season_and_reliability_logs(tmp_path: Path) -> None:
    live_db = tmp_path / "live.db"
    lab_db = tmp_path / "lab.db"
    conn = connect(live_db)
    try:
        # Seed 2 seasons: 2024 (24 matches for fitting) and 2025 (12 matches for out-of-sample eval)
        idx = 0
        for season, month_start in ((2024, 1), (2025, 1)):
            for day in range(1, 25 if season == 2024 else 13):
                idx += 1
                h = str((day % 4) + 1)
                a = str(((day + 1) % 4) + 1)
                hg = 1 if day % 3 == 0 else 2
                ag = 1 if day % 3 == 0 else 0
                kick = f"{season}-0{(day - 1) // 28 + month_start:01d}-{(day - 1) % 28 + 1:02d}T15:00:00Z"
                conn.execute(
                    """
                    INSERT INTO matches (match_id, dataset_version, competition, season, kickoff, home_id, home_name, away_id, away_name)
                    VALUES (?, 'v1', 'PL', ?, ?, ?, ?, ?, ?)
                    """,
                    (f"m{idx}", str(season), kick, h, f"Club {h}", a, f"Club {a}"),
                )
                conn.execute(
                    """
                    INSERT INTO result_revisions (match_id, revision, ft_home, ft_away, source)
                    VALUES (?, 1, ?, ?, 'test')
                    """,
                    (f"m{idx}", hg, ag),
                )
        conn.commit()
    finally:
        conn.close()

    cfg_boost = ExperimentConfig(
        experiment_id="EXP-CAL-BOOST",
        competition="PL",
        seasons=(2024, 2025),
        base_config={"k": 6.0, "prior_mean": "one", "draw_boost": 1.06},
        param_grid={"draw_boost": [1.06]},
        min_matches=1,
        calibration_mode="none",
    )
    res_boost = run_experiment(lab_db_path=lab_db, live_db_path=live_db, config=cfg_boost, max_workers=1)
    assert res_boost.completed_runs == 1
    assert res_boost.failed_runs == 0
    assert res_boost.best_metrics is not None
    assert "reliability_diagrams" in res_boost.best_metrics
    assert "ece" in res_boost.best_metrics

    cfg_platt = ExperimentConfig(
        experiment_id="EXP-CAL-PLATT",
        competition="PL",
        seasons=(2024, 2025),
        base_config={"k": 6.0, "prior_mean": "one", "draw_boost": 1.0},
        param_grid={"draw_boost": [1.0]},
        min_matches=1,
        calibration_mode="platt_fit_prior_season",
    )
    res_platt = run_experiment(lab_db_path=lab_db, live_db_path=live_db, config=cfg_platt, max_workers=1)
    assert res_platt.completed_runs == 1
    assert res_platt.failed_runs == 0
    assert res_platt.best_metrics is not None
    assert "reliability_diagrams" in res_platt.best_metrics
    assert res_platt.best_metrics["ece"]["ft_draw_ece"] is not None
