"""Lab Experiment Definition & Deterministic Identity.

Frozen configs, Tuning vs Holdout season splits, SHA-256 model_id generation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, fields
from typing import Any

# Dashboard / YAML launch defaults (omit a key → these apply).
DEFAULT_BASE_CONFIG: dict[str, Any] = {
    "k": 6.0,
    "prior_mean": "last_season",
    "dependence": "independent",
    "half_time_share_mode": "fixed_0_45",
    "ratio_mode": "one_pass",
    "draw_boost": 1.0,
}

ALLOWED_PRIOR_MEAN = frozenset({"one", "last_season"})
ALLOWED_DEPENDENCE = frozenset({"independent", "dixon_coles"})
ALLOWED_HALF_TIME = frozenset({"fixed_0_45", "fixed_0_50", "club_shrunk", "league_only"})
ALLOWED_RATIO_MODE = frozenset({"one_pass", "joint"})
ALLOWED_PRIMARY_METRIC = frozenset(
    {
        "log_loss",
        "brier",
        "full_time_1x2_accuracy",
        "accuracy",
        "exact_score_log_loss",
    }
)
_EXPERIMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ExperimentConfigError(ValueError):
    """Invalid dashboard/YAML/JSON experiment launch payload."""


def _as_season_tuple(value: Any) -> tuple[int, ...]:
    """Coerce JSON/list season payloads to a sorted-safe int tuple."""
    if value is None:
        return ()
    if isinstance(value, tuple):
        items = value
    elif isinstance(value, list):
        items = tuple(value)
    else:
        items = (value,)
    out: list[int] = []
    for item in items:
        out.append(int(item))
    return tuple(out)


@dataclass(frozen=True)
class ExperimentConfig:
    """Immutable experiment configuration with explicit Tuning/Holdout splits."""

    experiment_id: str
    competition: str
    seasons: tuple[int, ...]  # Legacy: all seasons (deprecated, use tuning/holdout)
    tuning_seasons: tuple[int, ...] = field(default_factory=tuple)
    holdout_seasons: tuple[int, ...] = field(default_factory=tuple)
    base_config: dict[str, Any] = field(default_factory=dict)
    param_grid: dict[str, list[Any]] = field(default_factory=dict)
    primary_metric: str = "log_loss"
    min_matches: int = 100
    random_seed: int = 42

    def __post_init__(self) -> None:
        object.__setattr__(self, "seasons", _as_season_tuple(self.seasons))
        object.__setattr__(self, "tuning_seasons", _as_season_tuple(self.tuning_seasons))
        object.__setattr__(self, "holdout_seasons", _as_season_tuple(self.holdout_seasons))
        # Back-compat: if only `seasons` provided, split 80/20 by year
        if self.seasons and not self.tuning_seasons and not self.holdout_seasons:
            sorted_seasons = tuple(sorted(self.seasons))
            split_idx = max(1, int(len(sorted_seasons) * 0.8))
            object.__setattr__(self, "tuning_seasons", sorted_seasons[:split_idx])
            object.__setattr__(self, "holdout_seasons", sorted_seasons[split_idx:])

    @classmethod
    def from_stored_dict(cls, data: dict[str, Any]) -> ExperimentConfig:
        """Load a config from dashboard/SQLite JSON, ignoring unknown keys (e.g. batch_id)."""
        known = {f.name for f in fields(cls)}
        payload = {key: value for key, value in data.items() if key in known}
        return cls(**payload)

    @property
    def all_seasons(self) -> tuple[int, ...]:
        """All seasons used (tuning + holdout, deduplicated, sorted)."""
        return tuple(sorted(set(self.tuning_seasons) | set(self.holdout_seasons)))

    def primary_metric_key(self) -> tuple[str, bool]:
        """Map dashboard primary_metric to aggregate key and sort direction.

        Returns ``(metrics_key, higher_is_better)``.
        """
        return resolve_primary_metric(self.primary_metric)


def resolve_primary_metric(name: str | None) -> tuple[str, bool]:
    """Map a UI/YAML primary_metric name to ``(metrics_json key, higher_is_better)``."""

    label = (name or "log_loss").strip()
    if label == "log_loss":
        return "full_time_1x2_log_loss", False
    if label == "brier":
        return "full_time_1x2_brier", False
    if label in {"full_time_1x2_accuracy", "accuracy"}:
        return "full_time_1x2_accuracy", True
    if label == "exact_score_log_loss":
        return "exact_score_log_loss", False
    return label, label.endswith("accuracy") or label.endswith("hit_rate")


def metric_value(metrics: dict[str, Any], metric_key: str) -> float | None:
    """Read a flat metric key, with nested ``exact_score.log_loss`` fallback."""

    raw = metrics.get(metric_key)
    if raw is not None:
        return float(raw)
    if metric_key == "exact_score_log_loss":
        nested = metrics.get("exact_score")
        if isinstance(nested, dict) and nested.get("log_loss") is not None:
            return float(nested["log_loss"])
    if metric_key in {"exact_score_accuracy", "exact_score_hit_rate"}:
        nested = metrics.get("exact_score")
        if isinstance(nested, dict):
            for key in ("hit", "accuracy", "hit_rate", "correct"):
                val = nested.get(key)
                if val is None or isinstance(val, bool):
                    continue
                return float(val)
        alt = metrics.get("exact_score_hit_rate" if metric_key.endswith("accuracy") else "exact_score_accuracy")
        if alt is not None:
            return float(alt)
    return None


def metric_sort_value(
    metrics: dict[str, Any] | None,
    metric_key: str,
    *,
    higher_is_better: bool,
) -> float:
    """Value used for ranking; missing metrics sort last for the chosen direction."""

    if not metrics:
        return float("-inf") if higher_is_better else float("inf")
    value = metric_value(metrics, metric_key)
    if value is None:
        return float("-inf") if higher_is_better else float("inf")
    return value


def pick_best_run(
    ranked_runs: list[dict[str, Any]],
    *,
    min_matches: int,
) -> dict[str, Any] | None:
    """First run in an already-sorted list that meets ``min_matches`` evaluated.

    ``ranked_runs`` entries must include ``matches_evaluated``. Returns ``None``
    when no run clears the floor (best is left unset rather than picking a tiny sample).
    """

    floor = max(0, int(min_matches))
    for run in ranked_runs:
        if int(run.get("matches_evaluated") or 0) >= floor:
            return run
    return None


def make_model_id(experiment_id: str, config: dict[str, Any]) -> str:
    """Generate deterministic SHA-256 model_id from experiment + config (first 10 hex chars)."""
    payload = {"config": config}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]
    return f"{experiment_id}-{digest}"


def expand_param_grid(param_grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Expand a parameter grid into all combinations (cartesian product)."""
    if not param_grid:
        return [{}]

    keys = list(param_grid.keys())
    values = list(param_grid.values())

    def _product(idx: int) -> list[dict[str, Any]]:
        if idx == len(keys):
            return [{}]
        rest = _product(idx + 1)
        result: list[dict[str, Any]] = []
        for value in values[idx]:
            for combo in rest:
                combo = dict(combo)
                combo[keys[idx]] = value
                result.append(combo)
        return result

    return _product(0)


def merge_configs(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Deep merge base config with overrides (explicit None overrides base)."""
    result = dict(base)
    for key, value in overrides.items():
        result[key] = value
    return result


def generate_model_configs(
    base_config: dict[str, Any],
    param_grid: dict[str, list[Any]],
) -> list[tuple[dict[str, Any], str]]:
    """Generate all model configs from base + param_grid with deterministic model_ids.

    Returns list of (config_dict, model_id) tuples.
    """
    combos = expand_param_grid(param_grid)
    results: list[tuple[dict[str, Any], str]] = []
    for combo in combos:
        config = merge_configs(base_config, combo)
        # Generate deterministic ID from the full config
        model_id = make_model_id("exp", config)
        results.append((config, model_id))
    return results


@dataclass(frozen=True)
class ExperimentLaunchSpec:
    """Normalized payload ready for ``create_lab_experiment`` + ``lab-run``."""

    experiment_id: str | None
    competitions: tuple[str, ...]
    seasons: tuple[int, ...]
    base_config: dict[str, Any]
    param_grid: dict[str, list[Any]]
    primary_metric: str
    min_matches: int


def _require_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ExperimentConfigError(f"{label} must be a JSON object.")
    return dict(value)


def _as_float(value: Any, *, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ExperimentConfigError(f"{label} must be a number.") from exc
    if not (number == number):  # NaN
        raise ExperimentConfigError(f"{label} must be a finite number.")
    return number


def _as_optional_float(value: Any, *, label: str) -> float | None:
    if value is None or value == "":
        return None
    return _as_float(value, label=label)


def _as_optional_float_list(value: Any, *, label: str) -> list[float | None]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return [
            None if p.lower() in {"none", "null"} else _as_float(p, label=label)
            for p in parts
        ]
    if not isinstance(value, list):
        return [_as_optional_float(value, label=label)]
    if not value:
        raise ExperimentConfigError(f"{label} must be a non-empty list.")
    return [_as_optional_float(item, label=label) for item in value]


def _as_float_list(value: Any, *, label: str) -> list[float]:
    if value is None:
        return []
    if isinstance(value, (int, float, str)) and not isinstance(value, bool):
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",") if p.strip()]
            return [_as_float(p, label=label) for p in parts]
        return [_as_float(value, label=label)]
    if not isinstance(value, list) or not value:
        raise ExperimentConfigError(f"{label} must be a non-empty list of numbers.")
    return [_as_float(item, label=label) for item in value]


def _normalize_competitions(
    data: dict[str, Any],
    *,
    allowed: frozenset[str],
) -> tuple[str, ...]:
    raw = data.get("competitions", data.get("competition"))
    if raw is None or raw == "":
        codes = ["PL"]
    elif isinstance(raw, str):
        codes = [part.strip().upper() for part in raw.replace(";", ",").split(",") if part.strip()]
    elif isinstance(raw, list):
        codes = [str(item).strip().upper() for item in raw if str(item).strip()]
    else:
        raise ExperimentConfigError("competition/competitions must be a string or list.")
    if not codes:
        raise ExperimentConfigError("At least one competition is required.")
    unique: list[str] = []
    for code in codes:
        if code not in allowed:
            raise ExperimentConfigError(
                f"Unsupported competition code: {code}. Allowed: {', '.join(sorted(allowed))}."
            )
        if code not in unique:
            unique.append(code)
    return tuple(unique)


def _normalize_seasons(data: dict[str, Any]) -> tuple[int, ...]:
    raw = data.get("seasons")
    if raw is None or raw == "" or raw == []:
        raise ExperimentConfigError("seasons is required (e.g. [2024, 2025]).")
    try:
        seasons = _as_season_tuple(raw)
    except (TypeError, ValueError) as exc:
        raise ExperimentConfigError("seasons must be integers (year starts).") from exc
    if not seasons:
        raise ExperimentConfigError("At least one season is required.")
    # Preserve first-seen order while de-duplicating.
    unique: list[int] = []
    for season in seasons:
        if season < 1990 or season > 2100:
            raise ExperimentConfigError(f"Season out of range: {season}.")
        if season not in unique:
            unique.append(season)
    return tuple(unique)


def _normalize_base_config(raw: dict[str, Any], flat: dict[str, Any]) -> dict[str, Any]:
    """Merge defaults ← nested base_config ← flat root knobs."""

    nested = _require_mapping(raw.get("base_config"), label="base_config")
    merged = dict(DEFAULT_BASE_CONFIG)
    merged.update(nested)

    # Flat shortcuts at root (dashboard JSON convenience).
    for key in (
        "k",
        "prior_mean",
        "dependence",
        "half_time_share_mode",
        "ratio_mode",
        "draw_boost",
        "draw_weights",
        "draw_pick_threshold",
        "draw_pick_margin",
        "draw_tightness_scale",
        "draw_tightness_ref",
        "decay_half_life_days",
        "goal_cap",
        "elo_weight",
        "elo_k",
        "elo_home_adv",
        "goal_cap_slope",
        "nb_alpha",
        "promoted_attack_prior",
        "promoted_defence_prior",
        "venue_k",
        "short_rest_days",
        "rest_attack_factor",
        "rest_defence_factor",
        "standings_blend",
        "standings_min_games",
        "stakes_blend",
        "stakes_min_games",
        "elo_weight",
        "elo_k",
        "elo_home_adv",
    ):
        if key in flat and flat[key] is not None:
            merged[key] = flat[key]

    k = _as_float(merged.get("k", 6.0), label="k")
    if k < 0:
        raise ExperimentConfigError("k must be >= 0.")
    merged["k"] = k

    prior_mean = str(merged.get("prior_mean") or "last_season").strip()
    if prior_mean not in ALLOWED_PRIOR_MEAN:
        raise ExperimentConfigError(
            f"prior_mean must be one of: {', '.join(sorted(ALLOWED_PRIOR_MEAN))}."
        )
    merged["prior_mean"] = prior_mean

    dependence = str(merged.get("dependence") or "independent").strip()
    if dependence not in ALLOWED_DEPENDENCE:
        raise ExperimentConfigError(
            f"dependence must be one of: {', '.join(sorted(ALLOWED_DEPENDENCE))}."
        )
    merged["dependence"] = dependence

    ht = str(merged.get("half_time_share_mode") or "fixed_0_45").strip()
    if ht not in ALLOWED_HALF_TIME:
        raise ExperimentConfigError(
            f"half_time_share_mode must be one of: {', '.join(sorted(ALLOWED_HALF_TIME))}."
        )
    merged["half_time_share_mode"] = ht

    ratio_mode = str(merged.get("ratio_mode") or "one_pass").strip()
    if ratio_mode not in ALLOWED_RATIO_MODE:
        raise ExperimentConfigError(
            f"ratio_mode must be one of: {', '.join(sorted(ALLOWED_RATIO_MODE))}."
        )
    merged["ratio_mode"] = ratio_mode

    draw_boost = _as_float(merged.get("draw_boost", 1.0), label="draw_boost")
    if draw_boost <= 0:
        raise ExperimentConfigError("draw_boost must be > 0.")
    merged["draw_boost"] = draw_boost

    if "draw_weights" in merged and merged["draw_weights"] is not None:
        weights = merged["draw_weights"]
        if isinstance(weights, str):
            weights = [w.strip() for w in weights.split(",") if w.strip()]
        if not isinstance(weights, list) or not weights:
            raise ExperimentConfigError("draw_weights must be a non-empty list of positive numbers.")
        parsed_weights = [_as_float(w, label="draw_weights") for w in weights]
        if any(w <= 0 for w in parsed_weights):
            raise ExperimentConfigError("draw_weights must be positive.")
        merged["draw_weights"] = parsed_weights
    else:
        merged.pop("draw_weights", None)

    for opt_key in (
        "draw_pick_threshold",
        "draw_pick_margin",
        "draw_tightness_scale",
        "decay_half_life_days",
        "goal_cap",
        "elo_weight",
        "elo_k",
        "elo_home_adv",
        "promoted_attack_prior",
        "promoted_defence_prior",
        "venue_k",
        "short_rest_days",
        "rest_attack_factor",
        "rest_defence_factor",
        "standings_blend",
        "standings_min_games",
        "stakes_blend",
        "stakes_min_games",
        "elo_weight",
        "elo_k",
        "elo_home_adv",
    ):
        if opt_key in merged:
            val = _as_optional_float(merged.get(opt_key), label=opt_key)
            if val is None:
                merged.pop(opt_key, None)
            else:
                if val < 0 and opt_key not in {
                    "decay_half_life_days",
                    "promoted_attack_prior",
                    "promoted_defence_prior",
                }:
                    raise ExperimentConfigError(f"{opt_key} must be >= 0.")
                if opt_key in {
                    "decay_half_life_days",
                    "promoted_attack_prior",
                    "promoted_defence_prior",
                } and val <= 0:
                    raise ExperimentConfigError(f"{opt_key} must be > 0 when set.")
                merged[opt_key] = val

    if "goal_cap_slope" in merged and merged["goal_cap_slope"] is not None:
        slope = _as_float(merged["goal_cap_slope"], label="goal_cap_slope")
        if slope < 0 or slope > 1:
            raise ExperimentConfigError("goal_cap_slope must be between 0 and 1.")
        merged["goal_cap_slope"] = slope
    elif "goal_cap_slope" in merged:
        merged.pop("goal_cap_slope", None)

    if "nb_alpha" in merged and merged["nb_alpha"] is not None:
        alpha = _as_float(merged["nb_alpha"], label="nb_alpha")
        if alpha < 0:
            raise ExperimentConfigError("nb_alpha must be >= 0.")
        merged["nb_alpha"] = alpha
    elif "nb_alpha" in merged:
        merged.pop("nb_alpha", None)

    if "draw_tightness_ref" in merged and merged["draw_tightness_ref"] is not None:
        merged["draw_tightness_ref"] = _as_float(
            merged["draw_tightness_ref"], label="draw_tightness_ref"
        )
    elif "draw_tightness_ref" in merged:
        merged.pop("draw_tightness_ref", None)

    return merged


def _normalize_param_grid(
    raw: dict[str, Any],
    flat: dict[str, Any],
    *,
    base_config: dict[str, Any],
) -> dict[str, list[Any]]:
    nested = _require_mapping(raw.get("param_grid"), label="param_grid")
    grid: dict[str, list[Any]] = {}
    for key, value in nested.items():
        if value is None:
            continue
        if not isinstance(value, list):
            # Allow scalar → single-point grid.
            value = [value]
        if not value:
            raise ExperimentConfigError(f"param_grid.{key} must be a non-empty list.")
        grid[str(key)] = list(value)

    # Flat grid shortcuts.
    if "grid_k" in flat and flat["grid_k"] is not None and flat["grid_k"] != "":
        grid["k"] = _as_float_list(flat["grid_k"], label="grid_k")
    if "grid_draw_boost" in flat and flat["grid_draw_boost"] is not None and flat["grid_draw_boost"] != "":
        grid["draw_boost"] = _as_float_list(flat["grid_draw_boost"], label="grid_draw_boost")

    # Ensure searchable dims always present (match dashboard form behaviour).
    if "k" not in grid:
        grid["k"] = [float(base_config["k"])]
    else:
        grid["k"] = _as_float_list(grid["k"], label="param_grid.k")
        if any(v < 0 for v in grid["k"]):
            raise ExperimentConfigError("param_grid.k values must be >= 0.")

    if "draw_boost" not in grid:
        grid["draw_boost"] = [float(base_config["draw_boost"])]
    else:
        grid["draw_boost"] = _as_float_list(grid["draw_boost"], label="param_grid.draw_boost")
        if any(v <= 0 for v in grid["draw_boost"]):
            raise ExperimentConfigError("param_grid.draw_boost values must be > 0.")

    # Validate optional grid lists that mirror base knobs.
    for key in ("draw_tightness_ref", "nb_alpha", "goal_cap_slope"):
        if key in grid:
            grid[key] = _as_float_list(grid[key], label=f"param_grid.{key}")
            if key == "nb_alpha" and any(v < 0 for v in grid[key]):
                raise ExperimentConfigError("param_grid.nb_alpha values must be >= 0.")
            if key == "goal_cap_slope" and any(v < 0 or v > 1 for v in grid[key]):
                raise ExperimentConfigError("param_grid.goal_cap_slope values must be between 0 and 1.")

    for key in (
        "draw_pick_threshold",
        "draw_pick_margin",
        "draw_tightness_scale",
        "decay_half_life_days",
        "goal_cap",
        "elo_weight",
        "elo_k",
        "elo_home_adv",
        "promoted_attack_prior",
        "promoted_defence_prior",
    ):
        if key in grid:
            grid[key] = _as_optional_float_list(grid[key], label=f"param_grid.{key}")

    if "draw_weights" in grid:
        weights_points = grid["draw_weights"]
        normalized_points: list[Any] = []
        for point in weights_points:
            if isinstance(point, str):
                point = [w.strip() for w in point.split(",") if w.strip()]
            if not isinstance(point, list) or not point:
                raise ExperimentConfigError("param_grid.draw_weights entries must be number lists.")
            parsed = [_as_float(w, label="param_grid.draw_weights") for w in point]
            if any(w <= 0 for w in parsed):
                raise ExperimentConfigError("param_grid.draw_weights must be positive.")
            normalized_points.append(parsed)
        grid["draw_weights"] = normalized_points

    return grid


def parse_experiment_launch_json(
    raw: str,
    *,
    allowed_competitions: frozenset[str] | None = None,
) -> ExperimentLaunchSpec:
    """Parse dashboard JSON textarea / YAML-equivalent object into a launch spec.

    Omitted keys receive dashboard defaults. ``seasons`` is required.
    Accepts either nested ``base_config``/``param_grid`` (YAML shape) or flat
    form-like keys (``k``, ``grid_k``, ``draw_boost``, …).
    """

    text = (raw or "").strip()
    if not text:
        raise ExperimentConfigError("JSON config is empty.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExperimentConfigError(f"Invalid JSON: {exc.msg} (line {exc.lineno}).") from exc
    if not isinstance(data, dict):
        raise ExperimentConfigError("JSON root must be an object.")

    from laboratory.football_data import COMPETITION_NAMES

    allowed = allowed_competitions or frozenset(COMPETITION_NAMES.keys())
    competitions = _normalize_competitions(data, allowed=allowed)
    seasons = _normalize_seasons(data)

    experiment_id = data.get("experiment_id")
    if experiment_id is None or experiment_id == "":
        slug: str | None = None
    else:
        slug = str(experiment_id).strip()
        if not _EXPERIMENT_ID_RE.match(slug):
            raise ExperimentConfigError(
                "experiment_id may only contain letters, numbers, underscores, and hyphens."
            )

    primary_metric = str(data.get("primary_metric") or "log_loss").strip()
    if primary_metric not in ALLOWED_PRIMARY_METRIC:
        raise ExperimentConfigError(
            f"primary_metric must be one of: {', '.join(sorted(ALLOWED_PRIMARY_METRIC))}."
        )
    if primary_metric == "accuracy":
        primary_metric = "full_time_1x2_accuracy"

    try:
        min_matches = int(data.get("min_matches", 50))
    except (TypeError, ValueError) as exc:
        raise ExperimentConfigError("min_matches must be an integer.") from exc
    if min_matches < 10:
        raise ExperimentConfigError("min_matches must be >= 10.")

    # Keys that belong only to launch metadata / grids — not copied into base_config.
    reserved = {
        "experiment_id",
        "competition",
        "competitions",
        "seasons",
        "tuning_seasons",
        "holdout_seasons",
        "base_config",
        "param_grid",
        "primary_metric",
        "min_matches",
        "random_seed",
        "batch_id",
        "grid_k",
        "grid_draw_boost",
    }
    flat = {key: value for key, value in data.items() if key not in reserved}
    base_config = _normalize_base_config(data, flat)
    param_grid = _normalize_param_grid(data, data, base_config=base_config)

    return ExperimentLaunchSpec(
        experiment_id=slug,
        competitions=competitions,
        seasons=seasons,
        base_config=base_config,
        param_grid=param_grid,
        primary_metric=primary_metric,
        min_matches=min_matches,
    )


def example_experiment_launch_json() -> str:
    """Pretty sample shown in the dashboard textarea placeholder."""

    sample = {
        "experiment_id": "EXP-CUSTOM-001",
        "competitions": ["PL"],
        "seasons": [2024, 2025],
        "primary_metric": "log_loss",
        "min_matches": 50,
        "base_config": {
            "k": 6.0,
            "prior_mean": "last_season",
            "dependence": "independent",
            "half_time_share_mode": "fixed_0_45",
            "ratio_mode": "one_pass",
            "draw_boost": 1.0,
        },
        "param_grid": {
            "k": [5.0, 6.0, 7.0],
            "draw_boost": [1.0, 1.1, 1.2],
        },
    }
    return json.dumps(sample, indent=2)