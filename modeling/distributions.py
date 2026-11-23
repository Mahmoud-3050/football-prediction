"""Vectorized Poisson / Negative Binomial score matrices and market probabilities."""

from __future__ import annotations

import numpy as np
from scipy.special import gammaln

from domain.entities import DEFAULT_CONSTANTS


def build_score_matrix_vec(
    lambda_home: float,
    lambda_away: float,
    *,
    tail_tolerance: float = DEFAULT_CONSTANTS.tail_tolerance,
    max_grid_goals: int = DEFAULT_CONSTANTS.max_grid_goals,
    nb_alpha: float = 0.0,
    rho: float | None = None,
) -> np.ndarray:
    """Build normalized (G+1, G+1) score probability matrix using NumPy broadcasting."""
    lh = max(float(lambda_home), 0.0)
    la = max(float(lambda_away), 0.0)
    g_arr = np.arange(max_grid_goals + 1, dtype=np.float64)

    def _pmf_1d(expected: float) -> np.ndarray:
        if expected == 0.0:
            p = np.zeros(max_grid_goals + 1, dtype=np.float64)
            p[0] = 1.0
            return p
        if nb_alpha == 0.0:
            return np.exp(-expected + g_arr * np.log(expected) - gammaln(g_arr + 1.0))
        r = 1.0 / nb_alpha
        p_nb = r / (r + expected)
        log_p = (
            gammaln(g_arr + r)
            - gammaln(r)
            - gammaln(g_arr + 1.0)
            + r * np.log(p_nb)
            + g_arr * np.log(1.0 - p_nb)
        )
        return np.exp(log_p)

    h_full = _pmf_1d(lh)
    a_full = _pmf_1d(la)
    h_cum = np.cumsum(h_full)
    a_cum = np.cumsum(a_full)
    tail = 1.0 - (h_cum * a_cum)
    hits = np.flatnonzero(tail <= tail_tolerance)
    max_g = int(hits[0]) if hits.size > 0 else max_grid_goals

    mat = np.outer(h_full[: max_g + 1], a_full[: max_g + 1])
    if rho is not None and max_g >= 1:
        mat[0, 0] = max(0.0, mat[0, 0] * (1.0 - lh * la * rho))
        mat[0, 1] = max(0.0, mat[0, 1] * (1.0 + lh * rho))
        mat[1, 0] = max(0.0, mat[1, 0] * (1.0 + la * rho))
        mat[1, 1] = max(0.0, mat[1, 1] * (1.0 - rho))
    total = float(np.sum(mat))
    if total <= 0.0:
        return np.full((max_g + 1, max_g + 1), 1.0 / ((max_g + 1) ** 2), dtype=np.float64)
    return mat / total


def market_1x2_from_matrix(matrix: np.ndarray) -> tuple[float, float, float]:
    """Return (p_home, p_draw, p_away) from a normalized score matrix."""
    p_draw = float(np.trace(matrix))
    p_home = float(np.sum(np.tril(matrix, k=-1)))
    p_away = float(np.sum(np.triu(matrix, k=1)))
    return p_home, p_draw, p_away
