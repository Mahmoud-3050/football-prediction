"""Unit tests for domain entities and zero-copy feature engineering."""

from __future__ import annotations

import numpy as np

from domain.entities import ModelConfig
from domain.features import (
    compute_match_stakes,
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


def test_compute_match_stakes_and_pricing() -> None:
    from datetime import datetime, timedelta, timezone
    from laboratory.score_model_adapter import price_from_snapshot
    from laboratory.snapshot import build_snapshot

    # Build an 8-team double round-robin (14 games per team) up to round 12 (~85% progress)
    base_dt = datetime(2025, 8, 10, 15, 0, tzinfo=timezone.utc)
    records = []
    mid = 1
    day = 0
    for rnd in range(2):
        for i in range(1, 9):
            for j in range(1, 9):
                if i == j:
                    continue
                if rnd == 1 and i > 4 and j > 4:
                    continue  # leave last few matches unplayed so progress ~0.85
                # Stronger teams (1,2) win most; mid-table (5) draws; bottom (7,8) lose
                if i <= 2 and j >= 5:
                    fh, fa = 2, 0
                elif j <= 2 and i >= 5:
                    fh, fa = 0, 2
                elif i in (4, 5) and j in (4, 5):
                    fh, fa = 1, 1
                else:
                    fh, fa = 1, 0
                ko = (base_dt + timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%SZ")
                records.append(MatchRecord(f"m{mid}", "2025", ko, str(i), f"T{i}", str(j), f"T{j}", fh, fa, 0, 0))
                mid += 1
                day += 1

    ds = encode_matches(records)
    # Zero blend is always 1.0, 1.0
    s0 = compute_match_stakes(ds, ds.team_to_idx["1"], ds.team_to_idx["2"], stakes_blend=0.0, stakes_min_games=6)
    assert s0.home_factor == 1.0 and s0.away_factor == 1.0

    # Active blend in late season produces valid bounded factors
    s1 = compute_match_stakes(ds, ds.team_to_idx["1"], ds.team_to_idx["2"], stakes_blend=0.25, stakes_min_games=6)
    assert s1.season_progress >= 0.70
    assert 0.75 <= s1.home_factor <= 1.25
    assert 0.75 <= s1.away_factor <= 1.25

    # Early season threshold keeps factors neutral
    s_early = compute_match_stakes(ds, ds.team_to_idx["1"], ds.team_to_idx["2"], stakes_blend=0.25, stakes_min_games=99)
    assert s_early.context_label == "early"
    assert s_early.home_factor == 1.0 and s_early.away_factor == 1.0

    # End-to-end adapter smoke test with stakes_blend > 0
    tgt = MatchRecord("tgt", "2025", (base_dt + timedelta(days=day + 5)).strftime("%Y-%m-%dT%H:%M:%SZ"), "1", "T1", "2", "T2", None, None, None, None)
    snap = build_snapshot(tgt, records)
    out_base = price_from_snapshot(snap, stakes_blend=0.0)
    out_stakes = price_from_snapshot(snap, stakes_blend=0.25, stakes_min_games=6)
    assert not out_stakes.excluded and out_stakes.body is not None
    assert abs(sum(out_stakes.body["full_time"]["result_1x2"][k] for k in ("home", "draw", "away")) - 1.0) < 1e-9
    assert out_base.body["lambda_home"] != out_stakes.body["lambda_home"]
