"""Vectorized, zero-copy feature engineering over EncodedDataset."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

import numpy as np

from domain.entities import DEFAULT_CONSTANTS, EncodedDataset, ScoringConstants


def parse_kickoff_days(
    value: str | None,
    *,
    constants: ScoringConstants = DEFAULT_CONSTANTS,
) -> float:
    """Parse an ISO-8601 kickoff timestamp into UTC days since Unix epoch."""
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() / constants.seconds_per_day
    except ValueError:
        return 0.0


def encode_matches(
    records: Sequence[Any],
    *,
    constants: ScoringConstants = DEFAULT_CONSTANTS,
) -> EncodedDataset:
    """Encode chronological match records once into contiguous downcast NumPy arrays.

    Filters out unfinished matches (``ft_home is None or ft_away is None``) and
    sorts strictly by ``(kickoff, match_id)`` for deterministic zero-leakage slicing.
    """
    finished = [
        r for r in records if r.ft_home is not None and r.ft_away is not None
    ]
    finished.sort(key=lambda r: str(r.kickoff))

    team_to_idx: dict[str, int] = {}
    idx_to_team: list[str] = []

    def _idx(tid: str) -> int:
        key = str(tid)
        val = team_to_idx.get(key)
        if val is None:
            val = len(idx_to_team)
            team_to_idx[key] = val
            idx_to_team.append(key)
        return val

    n = len(finished)
    home_idx = np.empty(n, dtype=np.int32)
    away_idx = np.empty(n, dtype=np.int32)
    ft_home = np.empty(n, dtype=np.int16)
    ft_away = np.empty(n, dtype=np.int16)
    ht_home = np.full(n, -1, dtype=np.int16)
    ht_away = np.full(n, -1, dtype=np.int16)
    ht_usable = np.zeros(n, dtype=bool)
    kickoff_days = np.empty(n, dtype=np.float64)
    season_codes = np.empty(n, dtype=np.int32)
    match_ids: list[str] = []

    for i, m in enumerate(finished):
        home_idx[i] = _idx(str(m.home_id))
        away_idx[i] = _idx(str(m.away_id))
        fth = int(m.ft_home)
        fta = int(m.ft_away)
        ft_home[i] = fth
        ft_away[i] = fta
        if m.ht_home is not None and m.ht_away is not None:
            hth = int(m.ht_home)
            hta = int(m.ht_away)
            ht_home[i] = hth
            ht_away[i] = hta
            if hth <= fth and hta <= fta:
                ht_usable[i] = True
        kickoff_days[i] = parse_kickoff_days(str(m.kickoff), constants=constants)
        season_val = getattr(m, "season", None)
        try:
            season_codes[i] = int(season_val) if season_val is not None else 0
        except (TypeError, ValueError):
            season_codes[i] = 0
        match_ids.append(str(m.match_id))

    return EncodedDataset(
        n_teams=len(idx_to_team),
        team_to_idx=team_to_idx,
        idx_to_team=tuple(idx_to_team),
        home_idx=home_idx,
        away_idx=away_idx,
        ft_home=ft_home,
        ft_away=ft_away,
        ht_home=ht_home,
        ht_away=ht_away,
        ht_usable=ht_usable,
        kickoff_days=kickoff_days,
        season_codes=season_codes,
        match_ids=tuple(match_ids),
    )


def soft_cap_goals_vec(
    goals: np.ndarray,
    cap: float | None,
    slope: float = 0.5,
) -> np.ndarray:
    """Vectorized soft goal cap matching ``score_model._soft_cap_goals``."""
    g = goals.astype(np.float64, copy=False)
    if cap is None:
        return g
    return np.where(g <= cap, g, cap + slope * (g - cap))


def compute_time_decay_weights(
    prior_days: np.ndarray,
    target_day: float,
    half_life_days: float | None,
) -> np.ndarray:
    """Vectorized exponential time-decay weights ``0.5 ** (age_days / half_life)``."""
    n = prior_days.shape[0]
    if half_life_days is None:
        return np.ones(n, dtype=np.float64)
    age = np.maximum(0.0, target_day - prior_days)
    return np.power(0.5, age / float(half_life_days))


def compute_standings_blend_means(
    priors: EncodedDataset,
    home_team_idx: int,
    away_team_idx: int,
    *,
    base_means: dict[str, float],
    standings_blend: float,
    standings_min_games: int,
    standings_full_games: float,
    constants: ScoringConstants = DEFAULT_CONSTANTS,
) -> dict[str, float]:
    """Vectorized pre-kickoff standings blend using ``np.bincount``."""
    means = dict(base_means)
    if not standings_blend or standings_blend <= 0.0 or priors.n_matches == 0:
        return means

    n_teams = priors.n_teams
    h = priors.home_idx
    a = priors.away_idx
    fth = priors.ft_home
    fta = priors.ft_away

    games = np.bincount(h, minlength=n_teams) + np.bincount(a, minlength=n_teams)
    h_pts = np.where(fth > fta, 3, np.where(fth == fta, 1, 0))
    a_pts = np.where(fta > fth, 3, np.where(fta == fth, 1, 0))
    points = np.bincount(h, weights=h_pts, minlength=n_teams) + np.bincount(
        a, weights=a_pts, minlength=n_teams
    )

    total_games = float(np.sum(games))
    if total_games <= 0.0:
        return means
    avg_ppg = float(np.sum(points)) / total_games
    if avg_ppg <= 0.0:
        avg_ppg = 1.0

    def _blend_team(t_idx: int, att_key: str, def_key: str) -> None:
        if t_idx < 0 or t_idx >= n_teams:
            return
        g = int(games[t_idx])
        if g < standings_min_games:
            return
        ppg = float(points[t_idx]) / g
        ppg_ratio = max(
            constants.standings_ppg_min,
            min(constants.standings_ppg_max, ppg / avg_ppg),
        )
        w = standings_blend * min(1.0, g / max(1.0, standings_full_games))
        means[att_key] = (1.0 - w) * means.get(att_key, 1.0) + w * (
            ppg_ratio ** constants.standings_ppg_exponent
        )
        means[def_key] = (1.0 - w) * means.get(def_key, 1.0) + w * (
            ppg_ratio ** (-constants.standings_ppg_exponent)
        )

    _blend_team(home_team_idx, "attack_home", "defence_home")
    _blend_team(away_team_idx, "attack_away", "defence_away")
    return means
