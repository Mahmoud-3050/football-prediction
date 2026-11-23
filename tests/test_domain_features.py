"""Unit tests for domain entities and zero-copy feature engineering."""

from __future__ import annotations

import numpy as np

from domain.entities import ModelConfig
from domain.features import (
    compute_standings_blend_means,
    compute_time_decay_weights,
    encode_matches,
    soft_cap_goals_vec,
)
from laboratory.snapshot import MatchRecord


def test_encode_matches_zero_copy_slice_view() -> None:
    records = [
        MatchRecord("m2", "2025", "2025-08-12T15:00:00Z", "2", "B", "1", "A", 1, 2, 0, 1),
        MatchRecord("m1", "2025", "2025-08-10T15:00:00Z", "1", "A", "2", "B", 2, 0, 1, 0),
        MatchRecord("m3", "2025", "2025-08-15T15:00:00Z", "1", "A", "2", "B", None, None, None, None),
    ]
    ds = encode_matches(records)
    assert ds.n_matches == 2
    assert ds.match_ids == ("m1", "m2")
    assert ds.ft_home.dtype == np.int16
    assert ds.home_idx.dtype == np.int32
    assert ds.kickoff_days.dtype == np.float64

    view = ds.slice_view(1)
    assert view.n_matches == 1
    assert view.home_idx.base is ds.home_idx
    assert view.ft_home.base is ds.ft_home
    assert view.kickoff_days.base is ds.kickoff_days


def test_vectorized_feature_helpers() -> None:
    goals = np.array([0, 2, 5], dtype=np.int16)
    capped = soft_cap_goals_vec(goals, cap=3.0, slope=0.5)
    assert np.allclose(capped, [0.0, 2.0, 4.0])

    days = np.array([100.0, 190.0], dtype=np.float64)
    weights = compute_time_decay_weights(days, target_day=190.0, half_life_days=90.0)
    assert np.allclose(weights, [0.5, 1.0])

    cfg = ModelConfig.from_mapping({"k": 8.0, "dependence": "dixon_coles", "standings_blend": 0.2})
    assert cfg.k == 8.0
    assert cfg.dependence == "dixon_coles"
    assert cfg.standings_blend == 0.2

    records = [
        MatchRecord(f"m{i}", "2025", f"2025-08-{10+i:02d}T15:00:00Z", "1", "A", "2", "B", 2, 0, 1, 0)
        for i in range(6)
    ]
    ds = encode_matches(records)
    means = compute_standings_blend_means(
        ds,
        ds.team_to_idx["1"],
        ds.team_to_idx["2"],
        base_means={"attack_home": 1.0, "defence_home": 1.0, "attack_away": 1.0, "defence_away": 1.0},
        standings_blend=0.2,
        standings_min_games=4,
        standings_full_games=19.0,
    )
    assert means["attack_home"] > 1.0
    assert means["attack_away"] < 1.0
