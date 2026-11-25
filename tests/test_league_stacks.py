"""Per-league live score_model stacks."""

from __future__ import annotations

import json
from pathlib import Path

from modeling.league_stacks import (
    DEFAULT_LIVE_STACK,
    draw_calibration_kwargs,
    live_stack_for,
    save_live_stack,
    save_spl_stack,
)

# Promoted live stacks under configs/live/{code}_stack.json (lowercase paths).
_PROMOTED_LIVE = {
    "PL": {
        "k": 12.0,
        "draw_boost": 1.06,
        "goal_cap": 4.0,
        "promoted_attack_prior": 0.8,
        "promoted_defence_prior": 1.25,
        "elo_weight": 0.08,
        "elo_k": 24.0,
        "elo_home_adv": 55.0,
        "model_id": "bot-score-model-pl-v4",
    },
    "PD": {
        "k": 12.0,
        "draw_boost": 1.0,
        "goal_cap": 4.0,
        "promoted_attack_prior": 0.8,
        "promoted_defence_prior": 1.25,
        "model_id": "bot-score-model-pd-v3",
    },
    "SA": {
        "k": 12.0,
        "draw_boost": 1.0,
        "goal_cap": 4.0,
        "promoted_attack_prior": 0.8,
        "promoted_defence_prior": 1.25,
        "model_id": "bot-score-model-sa-v3",
    },
    "BL1": {
        "k": 12.0,
        "draw_boost": 1.06,
        "goal_cap": 4.0,
        "promoted_attack_prior": 0.8,
        "promoted_defence_prior": 1.25,
        "model_id": "bot-score-model-bl1-v3",
    },
    "FL1": {
        "k": 12.0,
        "draw_boost": 1.0,
        "goal_cap": 4.0,
        "promoted_attack_prior": 0.8,
        "promoted_defence_prior": 1.25,
        "model_id": "bot-score-model-fl1-v3",
    },
    "PPL": {
        "k": 8.0,
        "draw_boost": 1.06,
        "goal_cap": 4.0,
        "promoted_attack_prior": 0.8,
        "promoted_defence_prior": 1.25,
        "model_id": "bot-score-model-ppl-v3",
    },
    "DED": {
        "k": 8.0,
        "draw_boost": 1.06,
        "goal_cap": 4.0,
        "promoted_attack_prior": 0.8,
        "promoted_defence_prior": 1.25,
        "model_id": "bot-score-model-ded-v3",
    },
    "CL": {
        "k": 8.0,
        "draw_boost": 0.85,
        "goal_cap": None,
        "promoted_attack_prior": 0.85,
        "promoted_defence_prior": 1.15,
        "dependence": "dixon_coles",
        "nb_alpha": 0.1,
        "persistent_priors": True,
        "elo_blend": 0.5,
        "domestic_prior_weight": 0.4,
        "elo_weight": 0.08,
        "elo_k": 24.0,
        "elo_home_adv": 55.0,
        "model_id": "bot-score-model-cl-v5",
    },
}


def test_unknown_competition_falls_back_to_default_live_stack() -> None:
    stack = live_stack_for("ZZZ")
    assert stack == DEFAULT_LIVE_STACK
    assert stack["k"] == 6.0
    assert stack["goal_cap"] is None
    assert stack["promoted_attack_prior"] is None
    assert stack["model_id"] == "bot-score-model-live"


def test_promoted_live_stacks_resolve_exact_knobs() -> None:
    for code, expected in _PROMOTED_LIVE.items():
        stack = live_stack_for(code)
        for key, value in expected.items():
            assert stack[key] == value, (code, key, stack[key], value)
        assert stack["prior_mean"] == "last_season"
        assert stack["dependence"] == expected.get("dependence", "independent")
        assert stack["half_time_share_mode"] == "club_shrunk"
        assert stack["decay_half_life_days"] == 150.0
        assert stack["nb_alpha"] == expected.get("nb_alpha", 0.0)
        assert stack["code_version"] == "score_model_v1"


def test_spl_retains_legacy_draw_pick_stack() -> None:
    stack = live_stack_for("SPL")
    assert stack["dependence"] == "dixon_coles"
    assert stack["draw_pick_threshold"] == 0.28
    assert stack["draw_pick_margin"] == 0.05
    assert stack["draw_boost"] == 1.0
    # SPL must not inherit the new domestic goal-cap / promoted priors.
    assert stack["goal_cap"] is None
    assert stack["promoted_attack_prior"] is None
    assert stack["promoted_defence_prior"] is None


def test_spl_uses_saved_stack_json(tmp_path: Path, monkeypatch) -> None:
    # Isolate from repo configs/live/* so peer leagues fall back to DEFAULT.
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "football.db"
    path = save_spl_stack(
        {
            "k": 10,
            "dependence": "dixon_coles",
            "prior_mean": "last_season",
            "half_time_share_mode": "fixed_0_45",
            "draw_boost": 1.5,
            "draw_weights": [1.3, 1.2, 1.0, 0.6],
            "draw_pick_threshold": 0.27,
            "draw_pick_margin": 0.05,
            "draw_tightness_scale": 1.0,
            "draw_tightness_ref": 1.0,
        },
        database_path=db,
        meta={"selected_experiment_id": "BOT-SPL-chal-k10-dc"},
    )
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["k"] == 10.0
    assert payload["dependence"] == "dixon_coles"
    assert payload["draw_boost"] == 1.5
    assert payload["draw_weights"] == [1.3, 1.2, 1.0, 0.6]
    assert payload["draw_pick_threshold"] == 0.27
    # Neutral calibration defaults survive merge on legacy SPL writes.
    assert payload["goal_cap"] is None
    assert payload["nb_alpha"] == 0.0
    stack = live_stack_for("SPL", database_path=db)
    assert stack["k"] == 10.0
    assert stack["dependence"] == "dixon_coles"
    assert stack["draw_boost"] == 1.5
    assert stack["draw_weights"] == [1.3, 1.2, 1.0, 0.6]
    assert stack["draw_pick_margin"] == 0.05
    assert stack["draw_tightness_scale"] == 1.0
    # Peer leagues stay on the default live stack when no live file exists.
    assert live_stack_for("PD", database_path=db) == DEFAULT_LIVE_STACK
    assert live_stack_for("PD", database_path=db)["goal_cap"] is None


def test_draw_calibration_kwargs_from_stack(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "football.db"
    save_spl_stack(
        {
            "draw_boost": 1.1,
            "draw_weights": [1.2, 1.0],
            "draw_pick_threshold": 0.3,
            "draw_pick_margin": 0.04,
            "draw_tightness_scale": 0.8,
        },
        database_path=db,
    )
    kwargs = draw_calibration_kwargs(live_stack_for("SPL", database_path=db))
    assert kwargs["draw_boost"] == 1.1
    assert kwargs["draw_weights"] == [1.2, 1.0]
    assert kwargs["draw_pick_threshold"] == 0.3
    assert kwargs["draw_pick_margin"] == 0.04
    assert kwargs["draw_tightness_scale"] == 0.8
    assert kwargs["goal_cap"] is None
    assert kwargs["goal_cap_slope"] == 0.5
    assert kwargs["nb_alpha"] == 0.0
    assert kwargs["promoted_attack_prior"] is None
    assert kwargs["promoted_defence_prior"] is None


def test_live_stack_passes_calibration_knobs(tmp_path: Path, monkeypatch) -> None:
    live_dir = tmp_path / "configs" / "live"
    live_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    path = save_live_stack(
        "PL",
        {
            "k": 12.0,
            "prior_mean": "last_season",
            "dependence": "independent",
            "half_time_share_mode": "club_shrunk",
            "ratio_mode": "one_pass",
            "model_id": "bot-score-model-pl-v3",
            "decay_half_life_days": 150.0,
            "draw_boost": 1.06,
            "goal_cap": 4.0,
            "goal_cap_slope": 0.5,
            "nb_alpha": 0.0,
            "promoted_attack_prior": 0.80,
            "promoted_defence_prior": 1.25,
            "selected_experiment_id": "EXP-PL-FINAL-HOLD-21",
            "code_version": "score_model_v1",
        },
    )
    assert path.resolve() == (live_dir / "pl_stack.json").resolve()
    stack = live_stack_for("PL")
    assert stack["k"] == 12.0
    assert stack["goal_cap"] == 4.0
    assert stack["promoted_attack_prior"] == 0.8
    assert stack["promoted_defence_prior"] == 1.25
    assert stack["selected_experiment_id"] == "EXP-PL-FINAL-HOLD-21"
    kwargs = draw_calibration_kwargs(stack)
    assert kwargs["goal_cap"] == 4.0
    assert kwargs["goal_cap_slope"] == 0.5
    assert kwargs["nb_alpha"] == 0.0
    assert kwargs["promoted_attack_prior"] == 0.8
    assert kwargs["promoted_defence_prior"] == 1.25
    # Other leagues still get neutral defaults without a stack file in this cwd.
    peer = live_stack_for("BL1")
    assert peer == DEFAULT_LIVE_STACK
    assert peer["goal_cap"] is None
