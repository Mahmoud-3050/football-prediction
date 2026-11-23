"""Isolated Champions League (CL) Track B feature builders.

All functions strictly respect pre-kickoff cutoffs (``kickoff < target.kickoff``)
to guarantee zero future leakage. They are designed exclusively for CL
experiments without altering domestic league behavior.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from laboratory.snapshot import MatchRecord

DOMESTIC_LEAGUE_WEIGHTS: dict[str, float] = {
    "PL": 1.08,
    "PD": 1.05,
    "BL1": 1.03,
    "SA": 1.02,
    "FL1": 0.98,
    "PPL": 0.93,
    "DED": 0.91,
}

KNOCKOUT_STAGES = frozenset({"PLAYOFFS", "LAST_16", "QUARTER_FINALS", "SEMI_FINALS"})


def _parse_dt(iso_str: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def compute_cl_elo_timeline(
    history: list[MatchRecord],
    *,
    k_factor: float = 24.0,
    home_adv: float = 55.0,
    season_regression: float = 0.75,
) -> dict[str, tuple[float, float]]:
    """Return pre-kickoff (elo_home, elo_away) for every CL match_id.

    Walks forward chronologically; resets partial regression toward 1500 at
    each new season boundary. Zero future leakage.
    """
    ordered = sorted(
        (m for m in history if m.competition == "CL"),
        key=lambda m: (m.kickoff, m.match_id),
    )
    ratings: dict[str, float] = {}
    pre_match: dict[str, tuple[float, float]] = {}
    current_season: str | None = None

    for m in ordered:
        if current_season is None:
            current_season = m.season
        elif m.season != current_season:
            for tid in list(ratings):
                ratings[tid] = 1500.0 + season_regression * (ratings[tid] - 1500.0)
            current_season = m.season

        rh = ratings.get(m.home_id, 1500.0)
        ra = ratings.get(m.away_id, 1500.0)
        pre_match[m.match_id] = (rh, ra)

        if m.ft_home is None or m.ft_away is None:
            continue
        expected_h = 1.0 / (1.0 + 10.0 ** ((ra - (rh + home_adv)) / 400.0))
        if m.ft_home > m.ft_away:
            actual_h = 1.0
        elif m.ft_home < m.ft_away:
            actual_h = 0.0
        else:
            actual_h = 0.5
        margin = abs(m.ft_home - m.ft_away)
        mov = 1.0 + 0.25 * max(0, margin - 1)
        delta = k_factor * mov * (actual_h - expected_h)
        ratings[m.home_id] = rh + delta
        ratings[m.away_id] = ra - delta

    return pre_match


def build_persistent_cl_priors(
    history: list[MatchRecord],
    target: MatchRecord,
    *,
    k: float = 8.0,
    season_decay: float = 0.60,
    default_atk: float = 0.85,
    default_def: float = 1.15,
) -> dict[str, float]:
    """Decayed multi-season CL attack/defence priors across all seasons < target.season."""
    target_season = int(target.season)
    prior_matches = [
        m
        for m in history
        if m.competition == "CL"
        and int(m.season) < target_season
        and m.ft_home is not None
        and m.ft_away is not None
    ]
    if not prior_matches:
        return {
            "attack_home": default_atk,
            "defence_home": default_def,
            "attack_away": default_atk,
            "defence_away": default_def,
        }

    # Season-level baseline goals
    season_avg: dict[str, tuple[float, float]] = {}
    for s in {m.season for m in prior_matches}:
        sub = [m for m in prior_matches if m.season == s]
        season_avg[s] = (
            sum(m.ft_home for m in sub) / len(sub),
            sum(m.ft_away for m in sub) / len(sub),
        )

    def _club_ratios(club_id: str) -> tuple[float, float, float, float]:
        att_h_num = att_h_den = 0.0
        def_h_num = def_h_den = 0.0
        att_a_num = att_a_den = 0.0
        def_a_num = def_a_den = 0.0
        for m in prior_matches:
            age = target_season - int(m.season) - 1
            w = season_decay ** max(0, age)
            lh, la = season_avg[m.season]
            if m.home_id == club_id and lh > 0 and la > 0:
                att_h_num += w * (m.ft_home / lh)
                att_h_den += w
                def_h_num += w * (m.ft_away / la)
                def_h_den += w
            elif m.away_id == club_id and lh > 0 and la > 0:
                att_a_num += w * (m.ft_away / la)
                att_a_den += w
                def_a_num += w * (m.ft_home / lh)
                def_a_den += w

        def _shrink(num: float, den: float, prior: float) -> float:
            if den <= 0:
                return prior
            raw = num / den
            return (den * raw + k * prior) / (den + k)

        return (
            _shrink(att_h_num, att_h_den, default_atk),
            _shrink(def_h_num, def_h_den, default_def),
            _shrink(att_a_num, att_a_den, default_atk),
            _shrink(def_a_num, def_a_den, default_def),
        )

    h_ah, h_dh, _, _ = _club_ratios(target.home_id)
    _, _, a_aa, a_da = _club_ratios(target.away_id)
    return {
        "attack_home": h_ah,
        "defence_home": h_dh,
        "attack_away": a_aa,
        "defence_away": a_da,
    }


def load_all_competitions_data(
    db_path: str = "data/football.db",
) -> tuple[dict[str, list[tuple[str, str, int, int, bool]]], dict[str, tuple[float, float]]]:
    """Load domestic league finished matches (for domestic prior & rest days).

    Returns:
      - ``team_matches``: team_id -> sorted list of (kickoff, competition, gf, ga, is_home)
      - ``league_avgs``: competition -> (avg_home_goals, avg_away_goals)
    """
    from pathlib import Path
    from laboratory.repositories import MatchRepository, connect_managed

    with connect_managed(Path(db_path), read_only=True) as conn:
        records = MatchRepository.fetch_finished_before(conn)

    team_matches: dict[str, list[tuple[str, str, int, int, bool]]] = {}
    comp_sums: dict[str, list[float]] = {}
    for m in records:
        if m.ft_home is None or m.ft_away is None:
            continue
        fth = int(m.ft_home)
        fta = int(m.ft_away)
        team_matches.setdefault(m.home_id, []).append((m.kickoff, m.competition, fth, fta, True))
        team_matches.setdefault(m.away_id, []).append((m.kickoff, m.competition, fta, fth, False))
        if m.competition in DOMESTIC_LEAGUE_WEIGHTS:
            s = comp_sums.setdefault(m.competition, [0.0, 0.0, 0.0])
            s[0] += fth
            s[1] += fta
            s[2] += 1.0

    league_avgs = {
        comp: (vals[0] / vals[2], vals[1] / vals[2])
        for comp, vals in comp_sums.items()
        if vals[2] > 0
    }
    return team_matches, league_avgs


def domestic_team_prior(
    team_id: str,
    cutoff_kickoff: str,
    team_matches: dict[str, list[tuple[str, str, int, int, bool]]],
    league_avgs: dict[str, tuple[float, float]],
    *,
    window_days: int = 365,
    k_dom: float = 10.0,
) -> tuple[float, float] | None:
    """Compute (attack_ratio, defence_ratio) from pre-cutoff domestic matches."""
    cutoff_dt = _parse_dt(cutoff_kickoff)
    if cutoff_dt is None:
        return None
    matches = team_matches.get(str(team_id)) or []
    att_sum = def_sum = n_dom = 0.0
    for ko, comp, gf, ga, is_home in matches:
        if ko >= cutoff_kickoff:
            break
        if comp not in DOMESTIC_LEAGUE_WEIGHTS:
            continue
        dt = _parse_dt(ko)
        if dt is None or (cutoff_dt - dt).total_seconds() / 86400.0 > window_days:
            continue
        lh, la = league_avgs.get(comp, (1.5, 1.2))
        base_gf = lh if is_home else la
        base_ga = la if is_home else lh
        lg_w = DOMESTIC_LEAGUE_WEIGHTS[comp]
        att_sum += (gf / max(0.5, base_gf)) * lg_w
        def_sum += (ga / max(0.5, base_ga)) / lg_w
        n_dom += 1.0

    if n_dom < 3:
        return None
    atk = (n_dom * (att_sum / n_dom) + k_dom * 1.0) / (n_dom + k_dom)
    dfn = (n_dom * (def_sum / n_dom) + k_dom * 1.0) / (n_dom + k_dom)
    return atk, dfn


def last_kickoff_map_all_comps(
    team_matches: dict[str, list[tuple[str, str, int, int, bool]]],
    target: MatchRecord,
) -> dict[int, str]:
    """Pre-kickoff last match timestamp across ALL competitions for home & away."""
    out: dict[int, str] = {}
    for tid_str in (target.home_id, target.away_id):
        last_ko: str | None = None
        for ko, _comp, _gf, _ga, _ih in team_matches.get(tid_str, ()):
            if ko >= target.kickoff:
                break
            last_ko = ko
        if last_ko is not None:
            try:
                out[int(tid_str)] = last_ko
            except ValueError:
                pass
    return out


def is_second_leg_match(
    history: list[MatchRecord],
    target: MatchRecord,
    stage: str,
) -> bool:
    """True when target is in a two-legged knockout stage and reverse leg already played."""
    if stage not in KNOCKOUT_STAGES:
        return False
    return any(
        m.competition == "CL"
        and m.season == target.season
        and m.kickoff < target.kickoff
        and m.home_id == target.away_id
        and m.away_id == target.home_id
        for m in history
    )


def resolve_cl_live_prior_means(
    *,
    home_id: int | str,
    away_id: int | str,
    kickoff: str,
    k: float = 8.0,
    persistent_priors: bool = True,
    elo_blend: float = 0.5,
    domestic_prior_weight: float = 0.4,
    default_atk: float = 0.85,
    default_def: float = 1.15,
    db_path: str | None = None,
) -> dict[str, float] | None:
    """Compute pre-kickoff CL prior_means (Persistent CL + Elo + Domestic Prior) from SQLite.

    Returns ``None`` if the database is unavailable or contains no prior CL matches
    (allowing ``estimate_score`` to fall back cleanly to its standard path).
    """
    import math
    from pathlib import Path
    from db.db_paths import resolve_database_path

    path = Path(db_path) if db_path else resolve_database_path()
    if not path.is_file():
        return None

    from laboratory.repositories import MatchRepository, connect_managed

    try:
        with connect_managed(path, read_only=True) as conn:
            cl_all = MatchRepository.fetch_matches(conn, competition="CL")
    except sqlite3.Error:
        return None

    history = [m for m in cl_all if m.kickoff < kickoff]
    if not history:
        return None
    # Infer season year from kickoff (Aug+ => year, else year-1)
    dt = _parse_dt(kickoff)
    season_year = str(dt.year if (dt and dt.month >= 7) else (dt.year - 1 if dt else 2026))
    target = MatchRecord(
        match_id="__live__",
        season=season_year,
        kickoff=kickoff,
        home_id=str(home_id),
        home_name="",
        away_id=str(away_id),
        away_name="",
        ft_home=None,
        ft_away=None,
        ht_home=None,
        ht_away=None,
        competition="CL",
    )

    if persistent_priors:
        means = build_persistent_cl_priors(
            history, target, k=k, season_decay=0.60, default_atk=default_atk, default_def=default_def
        )
    else:
        means = {
            "attack_home": default_atk,
            "defence_home": default_def,
            "attack_away": default_atk,
            "defence_away": default_def,
        }

    if elo_blend > 0.0:
        elo_map = compute_cl_elo_timeline(history + [target])
        if target.match_id in elo_map:
            eh, ea = elo_map[target.match_id]
            h_factor = math.exp(0.35 * elo_blend * (eh - 1500.0) / 400.0)
            a_factor = math.exp(0.35 * elo_blend * (ea - 1500.0) / 400.0)
            means["attack_home"] *= h_factor
            means["defence_home"] /= h_factor
            means["attack_away"] *= a_factor
            means["defence_away"] /= a_factor

    if domestic_prior_weight > 0.0:
        try:
            team_matches, league_avgs = load_all_competitions_data(str(path))
            dh = domestic_team_prior(str(home_id), kickoff, team_matches, league_avgs)
            da = domestic_team_prior(str(away_id), kickoff, team_matches, league_avgs)
            if dh is not None:
                means["attack_home"] = (1.0 - domestic_prior_weight) * means["attack_home"] + domestic_prior_weight * dh[0]
                means["defence_home"] = (1.0 - domestic_prior_weight) * means["defence_home"] + domestic_prior_weight * dh[1]
            if da is not None:
                means["attack_away"] = (1.0 - domestic_prior_weight) * means["attack_away"] + domestic_prior_weight * da[0]
                means["defence_away"] = (1.0 - domestic_prior_weight) * means["defence_away"] + domestic_prior_weight * da[1]
        except sqlite3.Error:
            pass

    return means
