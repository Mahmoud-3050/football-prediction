"""Per-competition score_model live stacks.

Leagues read from ``configs/live/{league}_stack.json``. Falls back to DEFAULT_LIVE_STACK.
Lab promotions write atomically to these files with version tracking.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_LIVE_STACK: dict[str, Any] = {
    "k": 6.0,
    "prior_mean": "last_season",
    "dependence": "independent",
    "half_time_share_mode": "fixed_0_45",
    "ratio_mode": "one_pass",
    "model_id": "bot-score-model-live",
    "decay_half_life_days": None,
    "draw_boost": 1.0,
    "draw_weights": None,
    "draw_pick_threshold": None,
    "draw_pick_margin": None,
    "draw_tightness_scale": None,
    "draw_tightness_ref": 1.0,
    # Neutral no-ops: keep live scoring bit-identical unless a stack file overrides.
    "goal_cap": None,
    "goal_cap_slope": 0.5,
    "nb_alpha": 0.0,
    "promoted_attack_prior": None,
    "promoted_defence_prior": None,
    "code_version": "score_model_v1",
}

_STACK_KEYS = (
    "k",
    "prior_mean",
    "dependence",
    "half_time_share_mode",
    "ratio_mode",
    "model_id",
    "decay_half_life_days",
    "draw_boost",
    "draw_weights",
    "draw_pick_threshold",
    "draw_pick_margin",
    "draw_tightness_scale",
    "draw_tightness_ref",
    "goal_cap",
    "goal_cap_slope",
    "nb_alpha",
    "promoted_attack_prior",
    "promoted_defence_prior",
    "code_version",
    "selected_experiment_id",
)


def _live_config_path(competition: str) -> Path:
    """Path to live config for a competition."""
    return Path("configs/live") / f"{competition.lower()}_stack.json"


def _lab_config_path(competition: str) -> Path:
    """Path to lab config for a competition."""
    return Path("configs/lab") / f"{competition.lower()}_stack.json"


def live_stack_for(
    competition: str,
    *,
    database_path: Path | None = None,
) -> dict[str, Any]:
    """Return the live score_model stack for one competition code.

    Reads from configs/live/{code}_stack.json, falls back to DEFAULT_LIVE_STACK.
    For SPL, also checks the legacy data/spl_stack.json path for backward compat.
    """
    code = (competition or "PL").upper()
    stack = dict(DEFAULT_LIVE_STACK)

    path = _live_config_path(code)
    if not path.is_file() and code == "SPL":
        # Legacy fallback for SPL
        from db_paths import resolve_database_path
        legacy_root = (database_path or resolve_database_path()).parent
        path = legacy_root / "spl_stack.json"

    if not path.is_file():
        return stack

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return stack

    if not isinstance(payload, dict):
        return stack

    return _merge_stack(stack, payload)


def save_live_stack(
    competition: str,
    stack: Mapping[str, Any],
    *,
    meta: Mapping[str, Any] | None = None,
) -> Path:
    """Persist a live stack atomically with version tracking."""
    path = _live_config_path(competition)
    path.parent.mkdir(parents=True, exist_ok=True)

    body = _merge_stack(DEFAULT_LIVE_STACK, stack)
    if meta:
        body["_meta"] = dict(meta)

    # Atomic write: temp file + os.replace
    import os
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=f".{path.name}.tmp.",
        delete=False,
    ) as tmp:
        json.dump(body, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)

    os.replace(tmp_path, path)
    return path


def draw_calibration_kwargs(stack: Mapping[str, Any]) -> dict[str, Any]:
    """Keyword args for ``estimate_score`` / ``price_from_snapshot`` / ``run_score_model``.

    Includes draw controls plus optional calibration knobs (``goal_cap``, ``nb_alpha``,
    promoted priors). Defaults are no-ops matching ``score_model_v1``.
    """
    weights = stack.get("draw_weights")
    return {
        "draw_boost": float(stack.get("draw_boost") or 1.0),
        "draw_weights": None if weights is None else list(weights),
        "draw_pick_threshold": _optional_float(stack.get("draw_pick_threshold")),
        "draw_pick_margin": _optional_float(stack.get("draw_pick_margin")),
        "draw_tightness_scale": _optional_float(stack.get("draw_tightness_scale")),
        "draw_tightness_ref": float(stack.get("draw_tightness_ref") or 1.0),
        "goal_cap": _optional_nonneg_float(stack.get("goal_cap")),
        "goal_cap_slope": float(
            stack["goal_cap_slope"]
            if stack.get("goal_cap_slope") is not None
            else DEFAULT_LIVE_STACK["goal_cap_slope"]
        ),
        "nb_alpha": float(stack.get("nb_alpha") or 0.0),
        "promoted_attack_prior": _optional_positive_float(
            stack.get("promoted_attack_prior")
        ),
        "promoted_defence_prior": _optional_positive_float(
            stack.get("promoted_defence_prior")
        ),
    }


def _merge_stack(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key in _STACK_KEYS:
        if key not in overlay:
            continue
        value = overlay[key]
        if key in {"k", "draw_boost", "draw_tightness_ref"}:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if key == "draw_boost" and number <= 0:
                continue
            if key == "draw_tightness_ref" and number <= 0:
                continue
            merged[key] = number
        elif key in {"draw_pick_threshold", "draw_pick_margin", "draw_tightness_scale"}:
            if value is None or value == "":
                merged[key] = None
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(number) or number < 0:
                continue
            merged[key] = number
        elif key == "draw_weights":
            parsed = _parse_draw_weights(value)
            if parsed is not None or value is None:
                merged[key] = parsed
        elif key == "decay_half_life_days":
            if value is None or value == "":
                merged[key] = None
            else:
                try:
                    merged[key] = float(value)
                except (TypeError, ValueError):
                    continue
        elif key == "goal_cap":
            if value is None or value == "":
                merged[key] = None
                continue
            number = _optional_nonneg_float(value)
            if number is not None:
                merged[key] = number
        elif key == "goal_cap_slope":
            number = _optional_nonneg_float(value)
            if number is not None:
                merged[key] = number
        elif key == "nb_alpha":
            number = _optional_nonneg_float(value)
            if number is not None:
                merged[key] = number
        elif key in {"promoted_attack_prior", "promoted_defence_prior"}:
            if value is None or value == "":
                merged[key] = None
                continue
            number = _optional_positive_float(value)
            if number is not None:
                merged[key] = number
        elif key in {
            "prior_mean",
            "dependence",
            "half_time_share_mode",
            "ratio_mode",
            "model_id",
            "code_version",
            "selected_experiment_id",
        }:
            if isinstance(value, str) and value.strip():
                merged[key] = value.strip()
    return merged


def _parse_draw_weights(value: Any) -> list[float] | None:
    if value is None:
        return None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    weights: list[float] = []
    for item in value:
        try:
            number = float(item)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number <= 0:
            return None
        weights.append(number)
    return weights or None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _optional_nonneg_float(value: Any) -> float | None:
    number = _optional_float(value)
    if number is None or number < 0:
        return None
    return number


def _optional_positive_float(value: Any) -> float | None:
    number = _optional_float(value)
    if number is None or number <= 0:
        return None
    return number


# Back-compat: SPL still reads from data/spl_stack.json
def spl_stack_path(*, database_path: Path | None = None) -> Path:
    """JSON beside the shared database (``data/spl_stack.json`` by default)."""
    from db_paths import resolve_database_path
    root = (database_path or resolve_database_path()).parent
    return root / "spl_stack.json"


def save_spl_stack(
    stack: Mapping[str, Any],
    *,
    database_path: Path | None = None,
    meta: Mapping[str, Any] | None = None,
) -> Path:
    """Persist the promoted SPL stack for live bot + laboratory pricing (legacy path)."""
    path = spl_stack_path(database_path=database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _merge_stack(DEFAULT_LIVE_STACK, stack)
    if meta:
        body["_meta"] = dict(meta)
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
