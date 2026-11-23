"""Immutable domain value objects, centralized constants, and contiguous NumPy datasets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np


@dataclass(frozen=True)
class ScoringConstants:
    """Centralized mathematical and domain constants (no magic numbers in logic)."""

    k: float = 6.0
    tail_tolerance: float = 0.001
    max_grid_goals: int = 25
    min_scored_matches: int = 3
    min_half_time_samples: int = 3
    joint_ratio_iterations: int = 25
    min_rho_training_matches: int = 30
    min_rho_earlier_matches: int = 6
    rho_max_goals: int = 10
    default_half_time_share: float = 0.45
    eps_clip: float = 1e-12
    eps_strength: float = 1e-9
    standings_ppg_min: float = 0.65
    standings_ppg_max: float = 1.45
    standings_ppg_exponent: float = 0.35
    elo_scale: float = 0.35
    elo_base: float = 1500.0
    elo_divisor: float = 400.0
    cl_season_decay: float = 0.60
    seconds_per_day: float = 86400.0
    rho_grid: tuple[float, ...] = tuple(
        round(-0.20 + 0.01 * step, 2) for step in range(0, 36)
    )


DEFAULT_CONSTANTS = ScoringConstants()


@dataclass(frozen=True)
class ModelConfig:
    """Immutable hyperparameter configuration for pricing a match or running an experiment."""

    k: float = 6.0
    prior_mean: str = "one"
    ratio_mode: str = "one_pass"
    dependence: str = "independent"
    dc_solver: str = "grid"  # "grid" (exact Golden Master) | "analytic_1d" | "mle_lbfgs"
    half_time_share_mode: str = "club_shrunk"
    decay_half_life_days: float | None = None
    draw_boost: float = 1.0
    draw_weights: tuple[float, ...] | None = None
    draw_pick_threshold: float | None = None
    draw_pick_margin: float | None = None
    draw_tightness_scale: float | None = None
    draw_tightness_ref: float = 1.0
    goal_cap: float | None = None
    goal_cap_slope: float = 0.5
    nb_alpha: float = 0.0
    promoted_attack_prior: float | None = None
    promoted_defence_prior: float | None = None
    venue_k: float | None = None
    short_rest_days: int | None = None
    rest_attack_factor: float = 0.94
    rest_defence_factor: float = 1.05
    standings_blend: float = 0.0
    standings_min_games: int = 6
    standings_full_games: float = 19.0
    neutral_venue: bool = False
    tail_tolerance: float = 0.001

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ModelConfig:
        dw = raw.get("draw_weights")
        return cls(
            k=float(raw.get("k", 6.0)),
            prior_mean=str(raw.get("prior_mean", "one")),
            ratio_mode=str(raw.get("ratio_mode", "one_pass")),
            dependence=str(raw.get("dependence", "independent")),
            dc_solver=str(raw.get("dc_solver", "grid")),
            half_time_share_mode=str(raw.get("half_time_share_mode", "club_shrunk")),
            decay_half_life_days=(
                None
                if raw.get("decay_half_life_days") is None
                else float(raw["decay_half_life_days"])
            ),
            draw_boost=float(raw.get("draw_boost", 1.0)),
            draw_weights=None if dw is None else tuple(float(x) for x in dw),
            draw_pick_threshold=(
                None
                if raw.get("draw_pick_threshold") is None
                else float(raw["draw_pick_threshold"])
            ),
            draw_pick_margin=(
                None
                if raw.get("draw_pick_margin") is None
                else float(raw["draw_pick_margin"])
            ),
            draw_tightness_scale=(
                None
                if raw.get("draw_tightness_scale") is None
                else float(raw["draw_tightness_scale"])
            ),
            draw_tightness_ref=float(raw.get("draw_tightness_ref", 1.0)),
            goal_cap=None if raw.get("goal_cap") is None else float(raw["goal_cap"]),
            goal_cap_slope=(
                0.5 if raw.get("goal_cap_slope") is None else float(raw["goal_cap_slope"])
            ),
            nb_alpha=0.0 if raw.get("nb_alpha") is None else float(raw["nb_alpha"]),
            promoted_attack_prior=(
                None
                if raw.get("promoted_attack_prior") is None
                else float(raw["promoted_attack_prior"])
            ),
            promoted_defence_prior=(
                None
                if raw.get("promoted_defence_prior") is None
                else float(raw["promoted_defence_prior"])
            ),
            venue_k=None if raw.get("venue_k") is None else float(raw["venue_k"]),
            short_rest_days=(
                None
                if raw.get("short_rest_days") is None
                else int(raw["short_rest_days"])
            ),
            rest_attack_factor=float(raw.get("rest_attack_factor", 0.94)),
            rest_defence_factor=float(raw.get("rest_defence_factor", 1.05)),
            standings_blend=float(raw.get("standings_blend", 0.0)),
            standings_min_games=int(raw.get("standings_min_games", 6)),
            standings_full_games=float(raw.get("standings_full_games", 19.0)),
            neutral_venue=bool(raw.get("neutral_venue", False)),
            tail_tolerance=float(raw.get("tail_tolerance", 0.001)),
        )


@dataclass(frozen=True)
class EncodedDataset:
    """Contiguous NumPy representation of chronologically sorted finished matches.

    Supports zero-copy walk-forward windowing via ``slice_view(end_idx)``.
    """

    n_teams: int
    team_to_idx: dict[str, int]
    idx_to_team: tuple[str, ...]
    home_idx: np.ndarray  # int32[N]
    away_idx: np.ndarray  # int32[N]
    ft_home: np.ndarray  # int16[N]
    ft_away: np.ndarray  # int16[N]
    ht_home: np.ndarray  # int16[N] (-1 when missing)
    ht_away: np.ndarray  # int16[N] (-1 when missing)
    ht_usable: np.ndarray  # bool[N]
    kickoff_days: np.ndarray  # float64[N] (UTC days since epoch)
    season_codes: np.ndarray  # int32[N]
    match_ids: tuple[str, ...]

    @property
    def n_matches(self) -> int:
        return int(self.home_idx.shape[0])

    def slice_view(self, end_idx: int) -> EncodedDataset:
        """Return a zero-copy NumPy view of matches ``[:end_idx]``."""
        return EncodedDataset(
            n_teams=self.n_teams,
            team_to_idx=self.team_to_idx,
            idx_to_team=self.idx_to_team,
            home_idx=self.home_idx[:end_idx],
            away_idx=self.away_idx[:end_idx],
            ft_home=self.ft_home[:end_idx],
            ft_away=self.ft_away[:end_idx],
            ht_home=self.ht_home[:end_idx],
            ht_away=self.ht_away[:end_idx],
            ht_usable=self.ht_usable[:end_idx],
            kickoff_days=self.kickoff_days[:end_idx],
            season_codes=self.season_codes[:end_idx],
            match_ids=self.match_ids[:end_idx],
        )


@dataclass(frozen=True)
class FittedModelState:
    """Immutable output of ``BaseScoreModel.fit``."""

    league_home: float
    league_away: float
    attack_home: np.ndarray  # float64[T]
    defence_home: np.ndarray  # float64[T]
    attack_away: np.ndarray  # float64[T]
    defence_away: np.ndarray  # float64[T]
    rho: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)
