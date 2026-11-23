"""Tests for Strategy models, analytical gradients, warm-starting, and SQL isolation."""

from __future__ import annotations

import pathlib

import numpy as np

from domain.entities import ModelConfig
from domain.features import encode_matches
from laboratory.snapshot import MatchRecord
from modeling.dixon_coles import JointDixonColesMLEModel, dixon_coles_joint_nll_and_grad
from modeling.distributions import build_score_matrix_vec, market_1x2_from_matrix
from modeling.shrinkage import ShrinkageJointModel, ShrinkageOnePassModel


def _sample_dataset():
    records = []
    mid = 1
    for r in range(3):
        for i in range(1, 5):
            for j in range(1, 5):
                if i == j:
                    continue
                records.append(
                    MatchRecord(
                        match_id=f"m-{mid}",
                        season="2025",
                        kickoff=f"2025-09-{mid:02d}T15:00:00Z" if mid <= 30 else f"2025-10-{mid-30:02d}T15:00:00Z",
                        home_id=str(i),
                        home_name=f"T{i}",
                        away_id=str(j),
                        away_name=f"T{j}",
                        ft_home=(i + r) % 3,
                        ft_away=(j + r) % 2,
                        ht_home=0,
                        ht_away=0,
                    )
                )
                mid += 1
    return encode_matches(records)


def test_analytical_gradient_matches_finite_differences() -> None:
    ds = _sample_dataset()
    t = ds.n_teams
    w = np.ones(ds.n_matches, dtype=np.float64)
    gh = ds.ft_home.astype(np.float64)
    ga = ds.ft_away.astype(np.float64)
    m00 = (ds.ft_home == 0) & (ds.ft_away == 0)
    m01 = (ds.ft_home == 0) & (ds.ft_away == 1)
    m10 = (ds.ft_home == 1) & (ds.ft_away == 0)
    m11 = (ds.ft_home == 1) & (ds.ft_away == 1)

    rng = np.random.default_rng(42)
    theta = rng.normal(0.0, 0.15, size=2 * t + 2)
    theta[2 * t] = 0.2
    theta[2 * t + 1] = -0.05

    _, grad_analytic = dixon_coles_joint_nll_and_grad(
        theta, ds.home_idx, ds.away_idx, gh, ga, w,
        n_teams=t, m00=m00, m01=m01, m10=m10, m11=m11, l2_reg=1e-3,
    )

    eps = 1e-6
    grad_fd = np.zeros_like(theta)
    for idx in range(theta.shape[0]):
        tp = theta.copy()
        tm = theta.copy()
        tp[idx] += eps
        tm[idx] -= eps
        fp, _ = dixon_coles_joint_nll_and_grad(
            tp, ds.home_idx, ds.away_idx, gh, ga, w,
            n_teams=t, m00=m00, m01=m01, m10=m10, m11=m11, l2_reg=1e-3,
        )
        fm, _ = dixon_coles_joint_nll_and_grad(
            tm, ds.home_idx, ds.away_idx, gh, ga, w,
            n_teams=t, m00=m00, m01=m01, m10=m10, m11=m11, l2_reg=1e-3,
        )
        grad_fd[idx] = (fp - fm) / (2.0 * eps)

    rel_err = np.linalg.norm(grad_analytic - grad_fd) / max(1.0, np.linalg.norm(grad_fd))
    assert rel_err < 1e-6


def test_strategy_models_and_warm_start() -> None:
    ds = _sample_dataset()
    cfg = ModelConfig()
    for model in (ShrinkageOnePassModel(), ShrinkageJointModel()):
        st = model.fit(ds, config=cfg)
        lh, la = model.predict_lambdas(st, 0, 1, config=cfg)
        assert lh > 0.0 and la > 0.0
        mat = build_score_matrix_vec(lh, la)
        assert abs(float(np.sum(mat)) - 1.0) < 1e-9
        p_h, p_d, p_a = market_1x2_from_matrix(mat)
        assert abs((p_h + p_d + p_a) - 1.0) < 1e-9

    mle = JointDixonColesMLEModel(l2_reg=1e-3)
    st1, x1 = mle.fit_with_warm_start(ds.slice_view(ds.n_matches - 1))
    st2, _ = mle.fit_with_warm_start(ds, x0=x1)
    assert st1.extra["converged"] is True
    assert st2.extra["converged"] is True


def test_zero_sql_leakage_in_domain_and_modeling() -> None:
    root = pathlib.Path(__file__).resolve().parent.parent
    for folder in ("domain", "modeling"):
        for py_file in (root / folder).glob("*.py"):
            text = py_file.read_text()
            assert "sqlite3" not in text, f"SQL leakage found in {py_file}"
