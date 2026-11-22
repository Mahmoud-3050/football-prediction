"""Tests for lab_experiment module."""

from __future__ import annotations

import json

import pytest

from pathlib import Path

from laboratory.lab_experiment import (
    ExperimentConfig,
    ExperimentConfigError,
    expand_param_grid,
    generate_model_configs,
    make_model_id,
    merge_configs,
    metric_sort_value,
    parse_experiment_launch_json,
    pick_best_run,
    resolve_primary_metric,
)
from laboratory.lab_runner import _aggregate_metrics
from laboratory.store import connect


def test_experiment_config_defaults() -> None:
    """Test ExperimentConfig with defaults."""
    config = ExperimentConfig(
        experiment_id="EXP-TEST-001",
        competition="PL",
        seasons=(2024, 2025),
    )
    assert config.experiment_id == "EXP-TEST-001"
    assert config.competition == "PL"
    assert config.seasons == (2024, 2025)
    assert config.primary_metric == "log_loss"
    assert config.min_matches == 100
    assert config.random_seed == 42


def test_experiment_config_from_stored_dict_strips_batch_id() -> None:
    """Dashboard stores batch_id; loading must ignore unknown keys."""
    config = ExperimentConfig.from_stored_dict(
        {
            "experiment_id": "EXP-BATCH-PL",
            "competition": "PL",
            "seasons": [2024, 2025],
            "batch_id": "BATCH-123",
            "base_config": {"k": 6.0},
            "param_grid": {"k": [6.0], "draw_boost": [1.0]},
            "primary_metric": "log_loss",
            "min_matches": 50,
        }
    )
    assert config.experiment_id == "EXP-BATCH-PL"
    assert config.seasons == (2024, 2025)
    assert config.tuning_seasons == (2024,)
    assert config.holdout_seasons == (2025,)
    assert config.primary_metric_key() == ("full_time_1x2_log_loss", False)


def test_primary_metric_key_accuracy_higher_better() -> None:
    config = ExperimentConfig(
        experiment_id="EXP-ACC",
        competition="PL",
        seasons=(2025,),
        primary_metric="full_time_1x2_accuracy",
    )
    assert config.primary_metric_key() == ("full_time_1x2_accuracy", True)


def test_resolve_primary_metric_maps_ui_labels() -> None:
    assert resolve_primary_metric("log_loss") == ("full_time_1x2_log_loss", False)
    assert resolve_primary_metric("brier") == ("full_time_1x2_brier", False)
    assert resolve_primary_metric("accuracy") == ("full_time_1x2_accuracy", True)
    assert resolve_primary_metric("exact_score_log_loss") == ("exact_score_log_loss", False)


def test_primary_metric_key_exact_score_log_loss() -> None:
    config = ExperimentConfig(
        experiment_id="EXP-EXACT-LL",
        competition="PL",
        seasons=(2025,),
        primary_metric="exact_score_log_loss",
    )
    assert config.primary_metric_key() == ("exact_score_log_loss", False)


def test_metric_sort_value_exact_score_nested_fallback() -> None:
    metrics = {"exact_score": {"hit": 0.1, "log_loss": 2.5}}
    assert metric_sort_value(metrics, "exact_score_log_loss", higher_is_better=False) == 2.5
    assert metric_sort_value(metrics, "exact_score_hit_rate", higher_is_better=True) == 0.1


def test_parse_experiment_launch_json_exact_score_log_loss() -> None:
    spec = parse_experiment_launch_json(
        json.dumps(
            {
                "seasons": [2025],
                "competition": "PL",
                "primary_metric": "exact_score_log_loss",
            }
        )
    )
    assert spec.primary_metric == "exact_score_log_loss"


def test_metric_sort_value_missing_sorts_last() -> None:
    assert metric_sort_value({}, "full_time_1x2_log_loss", higher_is_better=False) == float(
        "inf"
    )
    assert metric_sort_value({}, "full_time_1x2_accuracy", higher_is_better=True) == float(
        "-inf"
    )
    assert (
        metric_sort_value(
            {"full_time_1x2_log_loss": 1.05},
            "full_time_1x2_log_loss",
            higher_is_better=False,
        )
        == 1.05
    )


def test_pick_best_run_enforces_min_matches() -> None:
    ranked = [
        {
            "model_id": "tiny-winner",
            "matches_evaluated": 10,
            "primary_metric_value": 0.9,
        },
        {
            "model_id": "solid",
            "matches_evaluated": 80,
            "primary_metric_value": 1.0,
        },
    ]
    assert pick_best_run(ranked, min_matches=50)["model_id"] == "solid"
    assert pick_best_run(ranked, min_matches=100) is None
    assert pick_best_run(ranked, min_matches=10)["model_id"] == "tiny-winner"


def test_parse_experiment_launch_json_applies_defaults() -> None:
    spec = parse_experiment_launch_json(
        json.dumps({"seasons": [2025], "competition": "PL"})
    )
    assert spec.competitions == ("PL",)
    assert spec.seasons == (2025,)
    assert spec.experiment_id is None
    assert spec.primary_metric == "log_loss"
    assert spec.min_matches == 50
    assert spec.base_config["k"] == 6.0
    assert spec.base_config["prior_mean"] == "last_season"
    assert spec.param_grid["k"] == [6.0]
    assert spec.param_grid["draw_boost"] == [1.0]


def test_parse_experiment_launch_json_nested_and_grid() -> None:
    payload = {
        "experiment_id": "EXP-JSON-1",
        "competitions": ["PL", "SPL"],
        "seasons": [2024, 2025],
        "primary_metric": "brier",
        "min_matches": 40,
        "base_config": {
            "k": 5.0,
            "dependence": "dixon_coles",
            "draw_pick_threshold": 0.28,
        },
        "param_grid": {"k": [5.0, 6.0], "draw_boost": [1.0, 1.2]},
    }
    spec = parse_experiment_launch_json(json.dumps(payload))
    assert spec.experiment_id == "EXP-JSON-1"
    assert spec.competitions == ("PL", "SPL")
    assert spec.primary_metric == "brier"
    assert spec.min_matches == 40
    assert spec.base_config["dependence"] == "dixon_coles"
    assert spec.base_config["draw_pick_threshold"] == 0.28
    assert spec.param_grid["k"] == [5.0, 6.0]
    assert "draw_pick_threshold" not in spec.param_grid


def test_parse_experiment_launch_json_errors() -> None:
    with pytest.raises(ExperimentConfigError, match="Invalid JSON"):
        parse_experiment_launch_json("{nope")
    with pytest.raises(ExperimentConfigError, match="seasons is required"):
        parse_experiment_launch_json('{"competition":"PL"}')
    with pytest.raises(ExperimentConfigError, match="Unsupported competition"):
        parse_experiment_launch_json('{"seasons":[2025],"competition":"XX"}')
    with pytest.raises(ExperimentConfigError, match="experiment_id"):
        parse_experiment_launch_json(
            '{"seasons":[2025],"competition":"PL","experiment_id":"bad id"}'
        )
    with pytest.raises(ExperimentConfigError, match="min_matches"):
        parse_experiment_launch_json(
            '{"seasons":[2025],"competition":"PL","min_matches":5}'
        )


def test_experiment_config_auto_split() -> None:
    """Test auto-split of seasons into tuning/holdout when not explicitly provided."""
    config = ExperimentConfig(
        experiment_id="EXP-TEST-002",
        competition="PL",
        seasons=(2022, 2023, 2024, 2025),
    )
    # 4 seasons → 3 tuning, 1 holdout (80/20)
    assert config.tuning_seasons == (2022, 2023, 2024)
    assert config.holdout_seasons == (2025,)
    assert config.all_seasons == (2022, 2023, 2024, 2025)


def test_experiment_config_explicit_split() -> None:
    """Test explicit tuning/holdout seasons."""
    config = ExperimentConfig(
        experiment_id="EXP-TEST-003",
        competition="PL",
        seasons=(),
        tuning_seasons=(2022, 2023, 2024),
        holdout_seasons=(2025,),
    )
    assert config.tuning_seasons == (2022, 2023, 2024)
    assert config.holdout_seasons == (2025,)
    assert config.all_seasons == (2022, 2023, 2024, 2025)


def test_make_model_id_deterministic() -> None:
    """Test that make_model_id is deterministic for same config."""
    config = {"k": 6.0, "draw_boost": 1.2, "dependence": "independent"}
    id1 = make_model_id("EXP-TEST", config)
    id2 = make_model_id("EXP-TEST", config)
    assert id1 == id2
    assert id1.startswith("EXP-TEST-")
    assert len(id1) == len("EXP-TEST-") + 10  # 10 hex chars


def test_make_model_id_different_for_different_configs() -> None:
    """Test that different configs produce different model_ids."""
    config1 = {"k": 6.0, "draw_boost": 1.0}
    config2 = {"k": 6.0, "draw_boost": 1.2}
    id1 = make_model_id("EXP-TEST", config1)
    id2 = make_model_id("EXP-TEST", config2)
    assert id1 != id2


def test_make_model_id_order_independent() -> None:
    """Test that key order doesn't affect model_id."""
    config1 = {"a": 1, "b": 2}
    config2 = {"b": 2, "a": 1}
    id1 = make_model_id("EXP-TEST", config1)
    id2 = make_model_id("EXP-TEST", config2)
    assert id1 == id2


def test_expand_param_grid_empty() -> None:
    """Test expand_param_grid with empty grid."""
    result = expand_param_grid({})
    assert result == [{}]


def test_expand_param_grid_single() -> None:
    """Test expand_param_grid with single parameter."""
    result = expand_param_grid({"k": [4.0, 6.0, 8.0]})
    assert len(result) == 3
    assert result == [{"k": 4.0}, {"k": 6.0}, {"k": 8.0}]


def test_expand_param_grid_multiple() -> None:
    """Test expand_param_grid with multiple parameters (cartesian product)."""
    result = expand_param_grid({
        "k": [4.0, 6.0],
        "draw_boost": [1.0, 1.2],
    })
    assert len(result) == 4
    expected = [
        {"k": 4.0, "draw_boost": 1.0},
        {"k": 4.0, "draw_boost": 1.2},
        {"k": 6.0, "draw_boost": 1.0},
        {"k": 6.0, "draw_boost": 1.2},
    ]
    for exp in expected:
        assert exp in result


def test_merge_configs_basic() -> None:
    """Test basic config merging."""
    base = {"k": 6.0, "draw_boost": 1.0}
    overrides = {"draw_boost": 1.2}
    result = merge_configs(base, overrides)
    assert result == {"k": 6.0, "draw_boost": 1.2}


def test_merge_configs_preserves_base() -> None:
    """Test that base config is not mutated."""
    base = {"k": 6.0}
    overrides = {"draw_boost": 1.2}
    result = merge_configs(base, overrides)
    assert "draw_boost" not in base
    assert result["k"] == 6.0
    assert result["draw_boost"] == 1.2


def test_merge_configs_none_values() -> None:
    """Explicit None in overrides must override non-null base values."""
    from laboratory.lab_runner import _merge_configs

    base = {"k": 6.0, "decay_half_life_days": 150.0}
    overrides = {"decay_half_life_days": None}
    assert merge_configs(base, overrides) == {"k": 6.0, "decay_half_life_days": None}
    assert _merge_configs(base, overrides) == {"k": 6.0, "decay_half_life_days": None}


def test_generate_model_configs_distinct_for_null_vs_value() -> None:
    base = {"k": 6.0, "decay_half_life_days": 150.0}
    param_grid = {"decay_half_life_days": [None, 150.0]}
    results = generate_model_configs(base, param_grid)
    assert len(results) == 2
    assert results[0][0]["decay_half_life_days"] is None
    assert results[1][0]["decay_half_life_days"] == 150.0
    assert results[0][1] != results[1][1]


def test_parse_experiment_launch_json_calibration_knobs() -> None:
    payload = {
        "seasons": [2024, 2025],
        "base_config": {
            "goal_cap": 3.0,
            "goal_cap_slope": 0.4,
            "nb_alpha": 0.08,
            "promoted_attack_prior": 0.85,
            "promoted_defence_prior": 1.15,
        },
        "param_grid": {
            "decay_half_life_days": [None, 150],
            "goal_cap": [None, 3.0],
            "nb_alpha": [0.0, 0.08],
        },
    }
    spec = parse_experiment_launch_json(json.dumps(payload))
    assert spec.base_config["goal_cap"] == 3.0
    assert spec.base_config["goal_cap_slope"] == 0.4
    assert spec.base_config["nb_alpha"] == 0.08
    assert spec.base_config["promoted_attack_prior"] == 0.85
    assert spec.base_config["promoted_defence_prior"] == 1.15
    assert spec.param_grid["decay_half_life_days"] == [None, 150.0]
    assert spec.param_grid["goal_cap"] == [None, 3.0]
    assert spec.param_grid["nb_alpha"] == [0.0, 0.08]


def test_generate_model_configs() -> None:
    """Test generating all model configs with deterministic IDs."""
    base = {"k": 6.0, "prior_mean": "last_season"}
    param_grid = {"draw_boost": [1.0, 1.2], "k": [4.0, 6.0]}
    results = generate_model_configs(base, param_grid)
    assert len(results) == 4
    for config, model_id in results:
        assert model_id.startswith("exp-")
        assert config["prior_mean"] == "last_season"
        assert config["draw_boost"] in (1.0, 1.2)
        assert config["k"] in (4.0, 6.0)


def test_snapshot_cache_includes_last_match_and_previous_season(tmp_path) -> None:
    """Worker cache must price the last match and load prior season for last_season."""
    from pathlib import Path

    from laboratory.lab_runner import _build_snapshot_cache
    from laboratory.store import append_result, connect, upsert_match

    live_db = Path(tmp_path) / "live.db"
    conn = connect(live_db)
    fixtures = [
        ("prev1", "2024", "2024-08-10T15:00:00Z", "A", "B", 2, 1),
        ("prev2", "2024", "2024-08-17T15:00:00Z", "C", "D", 0, 0),
        ("cur1", "2025", "2025-08-10T15:00:00Z", "A", "B", 1, 0),
        ("cur2", "2025", "2025-08-17T15:00:00Z", "C", "D", 3, 1),
        ("cur3", "2025", "2025-08-24T15:00:00Z", "A", "C", 2, 2),
    ]
    for match_id, season, kickoff, home, away, ft_h, ft_a in fixtures:
        upsert_match(
            conn,
            {
                "match_id": match_id,
                "dataset_version": "t",
                "competition": "PL",
                "season": season,
                "kickoff": kickoff,
                "home_id": home,
                "home_name": home,
                "away_id": away,
                "away_name": away,
            },
        )
        append_result(
            conn,
            match_id,
            {"ft_home": ft_h, "ft_away": ft_a, "ht_home": 0, "ht_away": 0},
            "test",
        )
    conn.commit()
    conn.close()

    cache, history = _build_snapshot_cache(
        live_db,
        "PL",
        (2025,),
        include_previous_season=True,
    )
    assert set(cache) == {"cur2", "cur3"}  # cur1 has no same-season priors
    assert "cur3" in cache  # last match of season must be present
    assert {m.match_id for m in history} >= {"prev1", "prev2", "cur1", "cur2", "cur3"}
    assert cache["cur3"].target.match_id == "cur3"
    assert [p.match_id for p in cache["cur3"].priors] == ["cur1", "cur2"]


def test_aggregate_metrics_includes_exact_score_and_ou25(tmp_path: Path) -> None:
    """Completed-run aggregation must store Exact Score + O/U 2.5 flat and nested keys."""
    from laboratory.store import insert_evaluation, insert_prediction, upsert_match

    lab_db = tmp_path / "lab.db"
    conn = connect(lab_db)
    upsert_match(
        conn,
        {
            "match_id": "m1",
            "dataset_version": "t",
            "competition": "PL",
            "season": "2025",
            "kickoff": "2025-08-10T15:00:00Z",
            "home_id": "A",
            "home_name": "A",
            "away_id": "B",
            "away_name": "B",
        },
    )
    conn.execute(
        """
        INSERT INTO snapshots (snapshot_id, experiment_id, match_id, payload_json, leakage_ok)
        VALUES ('s1', 'EXP-AGG', 'm1', '{}', 1)
        """
    )
    prediction_id = insert_prediction(
        conn,
        experiment_id="EXP-AGG",
        match_id="m1",
        snapshot_id="s1",
        engine="score_model",
        model_id="mid-agg",
        body={},
    )
    assert prediction_id is not None
    insert_evaluation(
        conn,
        prediction_id,
        1,
        {
            "exact_score": {"status": "scored", "correct": True, "log_loss": 2.0},
            "over_under_2_5": {
                "status": "scored",
                "correct": False,
                "log_loss": 0.8,
                "brier": 0.4,
            },
            "full_time_1x2": {
                "status": "scored",
                "correct": True,
                "log_loss": 1.0,
                "brier": 0.2,
            },
        },
    )
    conn.commit()
    conn.close()

    agg = _aggregate_metrics(lab_db, "EXP-AGG", "mid-agg")
    assert agg["exact_score_accuracy"] == 1.0
    assert agg["exact_score_hit_rate"] == 1.0
    assert agg["exact_score_log_loss"] == 2.0
    assert agg["exact_score"]["hit"] == 1.0
    assert agg["exact_score"]["log_loss"] == 2.0
    assert agg["over_under_2_5_accuracy"] == 0.0
    assert agg["over_under_2_5_log_loss"] == 0.8
    assert agg["over_under_2_5"]["accuracy"] == 0.0
    assert agg["over_under_2_5"]["log_loss"] == 0.8


def test_merge_configs_explicit_none_overrides_base() -> None:
    """EXP-PL-EXACT-K-14 regression: null in param_grid must override base_config."""
    from laboratory.lab_runner import _merge_configs

    base = {"k": 6.0, "decay_half_life_days": 150.0, "goal_cap": 3.0}
    override = {"decay_half_life_days": None, "goal_cap": None}

    merged_exp = merge_configs(base, override)
    merged_run = _merge_configs(base, override)

    assert "decay_half_life_days" in merged_exp and merged_exp["decay_half_life_days"] is None
    assert "goal_cap" in merged_exp and merged_exp["goal_cap"] is None
    assert "decay_half_life_days" in merged_run and merged_run["decay_half_life_days"] is None
    assert "goal_cap" in merged_run and merged_run["goal_cap"] is None

    id_none = make_model_id("EXP-PL-EXACT-K-14", merge_configs(base, {"decay_half_life_days": None}))
    id_150 = make_model_id("EXP-PL-EXACT-K-14", merge_configs(base, {"decay_half_life_days": 150.0}))
    assert id_none != id_150


def test_parse_experiment_launch_json_calibration_knobs_and_null_grid() -> None:
    payload = {
        "seasons": [2024, 2025],
        "competition": "PL",
        "base_config": {
            "k": 14.0,
            "decay_half_life_days": 150.0,
            "goal_cap": 3.0,
            "goal_cap_slope": 0.5,
            "nb_alpha": 0.06,
            "promoted_attack_prior": 0.85,
            "promoted_defence_prior": 1.15,
        },
        "param_grid": {
            "decay_half_life_days": [None, 150],
            "goal_cap": [None, 3.0, 4.0],
            "nb_alpha": [0.0, 0.06],
        },
    }
    spec = parse_experiment_launch_json(json.dumps(payload))
    assert spec.base_config["goal_cap"] == 3.0
    assert spec.base_config["goal_cap_slope"] == 0.5
    assert spec.base_config["nb_alpha"] == 0.06
    assert spec.base_config["promoted_attack_prior"] == 0.85
    assert spec.base_config["promoted_defence_prior"] == 1.15
    assert spec.param_grid["decay_half_life_days"] == [None, 150.0]
    assert spec.param_grid["goal_cap"] == [None, 3.0, 4.0]
    assert spec.param_grid["nb_alpha"] == [0.0, 0.06]