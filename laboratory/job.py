"""Walk-forward job. Poisson and score_model do not read Gemini. Results are scored after insert."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from laboratory import (
    BOT_BACKTEST_SEASONS,
    BOT_EXPERIMENT_ID,
    EXPERIMENT_ID,
    POISSON_CONFIG_ID,
    SCORE_MODEL_CONFIG_ID,
)
from laboratory.evaluate import evaluate_prediction
from laboratory.poisson_engine import price_match
from laboratory.score_model_adapter import SCORE_MODEL_ENGINE, price_from_snapshot
from laboratory.snapshot import MatchRecord, build_snapshot, priors_from_snapshot
from laboratory.store import (
    insert_evaluation,
    insert_prediction,
    list_matches,
    record_run,
    save_snapshot,
)
from modeling.score_model import club_venue_ratios, prior_means_for_fixture

EXPERIMENT_CONFIG = {
    "experiment_id": EXPERIMENT_ID,
    "competition": "PL",
    "k": 6,
    "tail_tolerance": 0.001,
    "half_time": "HT-SHARE-1",
    "poisson_config_id": POISSON_CONFIG_ID,
    "gemini_model": "gemini-3.5-flash",
}

BOT_EXPERIMENT_CONFIG = {
    "experiment_id": BOT_EXPERIMENT_ID,
    "competition": "PL",
    "seasons": list(BOT_BACKTEST_SEASONS),
    "k": 6,
    "tail_tolerance": 0.001,
    "engine": SCORE_MODEL_ENGINE,
    "score_model_config_id": SCORE_MODEL_CONFIG_ID,
    "gemini": False,
}


def run_poisson(connection: sqlite3.Connection) -> dict[str, int]:
    competition = str(EXPERIMENT_CONFIG.get("competition") or "PL")
    rows = list_matches(connection, competition=competition)
    history = [_record(row) for row in rows]
    counts = {
        "priced": 0,
        "excluded": 0,
        "existing": 0,
        "skipped_unfinished": 0,
    }
    for row in rows:
        target = _record(row)
        if target.ft_home is None or target.ft_away is None:
            # Walk-forward stores predictions only for played matches.
            # Upcoming / in-progress fixtures wait for /predict or the 1-day pre-round job.
            counts["skipped_unfinished"] += 1
            continue
        payload = build_snapshot(target, history)
        snapshot_id = save_snapshot(connection, payload, experiment_id=EXPERIMENT_ID)
        if not payload["leakage_ok"]:
            record_run(
                connection,
                match_id=target.match_id,
                engine="poisson",
                status="failed",
                detail=payload["leakage_reason"] or "Leakage check failed.",
                experiment_id=EXPERIMENT_ID,
            )
            continue
        outcome = price_match(
            season=target.season,
            home_id=target.home_id,
            away_id=target.away_id,
            priors=priors_from_snapshot(payload),
        )
        if outcome.excluded or outcome.body is None:
            record_run(
                connection,
                match_id=target.match_id,
                engine="poisson",
                status="excluded",
                detail=outcome.reason or "Excluded.",
                experiment_id=EXPERIMENT_ID,
            )
            counts["excluded"] += 1
            continue
        prediction_id = insert_prediction(
            connection,
            match_id=target.match_id,
            snapshot_id=snapshot_id,
            engine="poisson",
            model_id=POISSON_CONFIG_ID,
            body=outcome.body,
            experiment_id=EXPERIMENT_ID,
        )
        if prediction_id is None:
            counts["existing"] += 1
            record_run(
                connection,
                match_id=target.match_id,
                engine="poisson",
                status="completed",
                detail="Prediction already stored.",
                experiment_id=EXPERIMENT_ID,
            )
            continue
        counts["priced"] += 1
        record_run(
            connection,
            match_id=target.match_id,
            engine="poisson",
            status="completed",
            detail="Priced from the pre-kickoff snapshot.",
            experiment_id=EXPERIMENT_ID,
        )
        if row["ft_home"] is not None and row["ft_away"] is not None:
            metrics = evaluate_prediction(
                outcome.body,
                {
                    "ft_home": row["ft_home"],
                    "ft_away": row["ft_away"],
                    "ht_home": row["ht_home"],
                    "ht_away": row["ht_away"],
                },
            )
            insert_evaluation(connection, prediction_id, int(row["revision"]), metrics)
    connection.commit()
    return counts


def run_score_model(
    connection: sqlite3.Connection,
    *,
    experiment_id: str = BOT_EXPERIMENT_ID,
    competition: str | None = None,
    k: float = 6.0,
    model_id: str = SCORE_MODEL_CONFIG_ID,
    decay_half_life_days: float | None = None,
    prior_mean: str = "one",
    ratio_mode: str = "one_pass",
    dependence: str = "independent",
    half_time_share_mode: str = "club_shrunk",
    draw_boost: float = 1.0,
    draw_weights: list[float] | tuple[float, ...] | None = None,
    draw_pick_threshold: float | None = None,
    draw_pick_margin: float | None = None,
    draw_tightness_scale: float | None = None,
    draw_tightness_ref: float = 1.0,
    goal_cap: float | None = None,
    goal_cap_slope: float = 0.5,
    nb_alpha: float = 0.0,
    promoted_attack_prior: float | None = None,
    promoted_defence_prior: float | None = None,
    venue_k: float | None = None,
    short_rest_days: int | None = None,
    rest_attack_factor: float = 0.94,
    rest_defence_factor: float = 1.05,
    standings_blend: float = 0.0,
    standings_min_games: int = 6,
    stakes_blend: float = 0.0,
    stakes_min_games: int = 15,
    elo_weight: float = 0.0,
    elo_k: float = 20.0,
    elo_home_adv: float = 55.0,
    xg_weight: float = 0.0,
    persistent_priors: bool = False,
    elo_blend: float = 0.0,
    domestic_prior_weight: float = 0.0,
    min_scored_matches: int | None = None,
) -> dict[str, int]:
    """Walk-forward using the Telegram bot scorer. No Gemini."""

    if prior_mean not in {"one", "last_season"}:
        raise ValueError("prior_mean must be 'one' or 'last_season'.")
    if ratio_mode not in {"one_pass", "joint"}:
        raise ValueError("ratio_mode must be 'one_pass' or 'joint'.")
    if dependence not in {"independent", "dixon_coles"}:
        raise ValueError("dependence must be 'independent' or 'dixon_coles'.")
    if half_time_share_mode not in {
        "club_shrunk",
        "league_only",
        "fixed_0_45",
        "fixed_0_50",
    }:
        raise ValueError(
            "half_time_share_mode must be 'club_shrunk', 'league_only', "
            "'fixed_0_45', or 'fixed_0_50'."
        )
    if draw_boost <= 0:
        raise ValueError("draw_boost must be positive.")

    competition = str(
        competition or BOT_EXPERIMENT_CONFIG.get("competition") or "PL"
    ).upper()
    rows = list_matches(connection, competition=competition)
    history = [_record(row) for row in rows]
    if xg_weight > 0.0:
        from dataclasses import replace
        from db.xg_store import attach_xg_to_matches

        attached = attach_xg_to_matches(
            connection,
            [
                {
                    "competition": m.competition,
                    "home_id": m.home_id,
                    "away_id": m.away_id,
                    "kickoff": m.kickoff,
                }
                for m in history
            ],
        )
        history = [
            replace(m, home_xg=att.get("home_xg"), away_xg=att.get("away_xg"))
            if att.get("home_xg") is not None and att.get("away_xg") is not None
            else m
            for m, att in zip(history, attached)
        ]
    season_ratio_cache: dict[str, dict[str, dict[str, float]]] = {}
    if prior_mean == "last_season":
        season_ratio_cache = _last_season_ratio_cache(
            history,
            k=k,
            goal_cap=goal_cap,
            goal_cap_slope=goal_cap_slope,
        )
    cl_track_b = competition == "CL" and (
        persistent_priors or elo_blend > 0.0 or domestic_prior_weight > 0.0
    )
    cl_elo_map: dict[str, tuple[float, float]] = {}
    cl_team_matches: dict[str, list[Any]] = {}
    cl_league_avgs: dict[str, tuple[float, float]] = {}
    if cl_track_b:
        from laboratory.cl_features import (
            compute_cl_elo_timeline,
            load_all_competitions_data,
        )

        if elo_blend > 0.0:
            cl_elo_map = compute_cl_elo_timeline(history)
        if domestic_prior_weight > 0.0:
            db_row = connection.execute("PRAGMA database_list").fetchone()
            db_file = db_row[2] if db_row and len(db_row) > 2 and db_row[2] else None
            if db_file:
                cl_team_matches, cl_league_avgs = load_all_competitions_data(db_file)
    effective_min_scored = (
        0 if (min_scored_matches is None and cl_track_b) else min_scored_matches
    )
    counts = {
        "competition": competition,
        "experiment_id": experiment_id,
        "priced": 0,
        "excluded": 0,
        "existing": 0,
        "failed": 0,
        "skipped_unfinished": 0,
    }
    for row in rows:
        target = _record(row)
        if target.ft_home is None or target.ft_away is None:
            # Walk-forward stores predictions only for played matches.
            # Upcoming / in-progress fixtures wait for /predict or the 1-day pre-round job.
            counts["skipped_unfinished"] += 1
            continue
        payload = build_snapshot(target, history)
        snapshot_id = save_snapshot(connection, payload, experiment_id=experiment_id)
        if not payload["leakage_ok"]:
            record_run(
                connection,
                match_id=target.match_id,
                engine=SCORE_MODEL_ENGINE,
                status="failed",
                detail=payload["leakage_reason"] or "Leakage check failed.",
                experiment_id=experiment_id,
            )
            counts["failed"] += 1
            continue
        means = None
        if cl_track_b:
            import math
            from laboratory.cl_features import (
                build_persistent_cl_priors,
                domestic_team_prior,
            )

            atk_def = float(promoted_attack_prior or 0.85)
            def_def = float(promoted_defence_prior or 1.15)
            if persistent_priors:
                means = build_persistent_cl_priors(
                    history,
                    target,
                    k=k,
                    season_decay=0.60,
                    default_atk=atk_def,
                    default_def=def_def,
                )
            elif prior_mean == "last_season":
                means = prior_means_for_fixture(
                    season_ratio_cache.get(target.season) or {},
                    home_id=target.home_id,
                    away_id=target.away_id,
                    promoted_attack_prior=promoted_attack_prior,
                    promoted_defence_prior=promoted_defence_prior,
                )
            else:
                means = {
                    "attack_home": atk_def,
                    "defence_home": def_def,
                    "attack_away": atk_def,
                    "defence_away": def_def,
                }
            if elo_blend > 0.0 and target.match_id in cl_elo_map:
                eh, ea = cl_elo_map[target.match_id]
                h_factor = math.exp(0.35 * elo_blend * (eh - 1500.0) / 400.0)
                a_factor = math.exp(0.35 * elo_blend * (ea - 1500.0) / 400.0)
                means["attack_home"] *= h_factor
                means["defence_home"] /= h_factor
                means["attack_away"] *= a_factor
                means["defence_away"] /= a_factor
            if domestic_prior_weight > 0.0 and cl_team_matches:
                dh = domestic_team_prior(
                    target.home_id, target.kickoff, cl_team_matches, cl_league_avgs
                )
                da = domestic_team_prior(
                    target.away_id, target.kickoff, cl_team_matches, cl_league_avgs
                )
                if dh is not None:
                    means["attack_home"] = (1.0 - domestic_prior_weight) * means[
                        "attack_home"
                    ] + domestic_prior_weight * dh[0]
                    means["defence_home"] = (1.0 - domestic_prior_weight) * means[
                        "defence_home"
                    ] + domestic_prior_weight * dh[1]
                if da is not None:
                    means["attack_away"] = (1.0 - domestic_prior_weight) * means[
                        "attack_away"
                    ] + domestic_prior_weight * da[0]
                    means["defence_away"] = (1.0 - domestic_prior_weight) * means[
                        "defence_away"
                    ] + domestic_prior_weight * da[1]
        elif prior_mean == "last_season":
            means = prior_means_for_fixture(
                season_ratio_cache.get(target.season) or {},
                home_id=target.home_id,
                away_id=target.away_id,
                promoted_attack_prior=promoted_attack_prior,
                promoted_defence_prior=promoted_defence_prior,
            )
        outcome = price_from_snapshot(
            payload,
            k=k,
            config_id=model_id,
            decay_half_life_days=decay_half_life_days,
            prior_means=means,
            ratio_mode=ratio_mode,
            dependence=dependence,
            half_time_share_mode=half_time_share_mode,
            draw_boost=draw_boost,
            draw_weights=draw_weights,
            draw_pick_threshold=draw_pick_threshold,
            draw_pick_margin=draw_pick_margin,
            draw_tightness_scale=draw_tightness_scale,
            draw_tightness_ref=draw_tightness_ref,
            goal_cap=goal_cap,
            goal_cap_slope=goal_cap_slope,
            nb_alpha=nb_alpha,
            promoted_attack_prior=promoted_attack_prior,
            promoted_defence_prior=promoted_defence_prior,
            venue_k=venue_k,
            short_rest_days=short_rest_days,
            rest_attack_factor=rest_attack_factor,
            rest_defence_factor=rest_defence_factor,
            target_kickoff=target.kickoff,
            last_kickoff_by_team=_last_kickoff_by_team(history, target.kickoff) if short_rest_days is not None else None,
            standings_blend=standings_blend,
            standings_min_games=standings_min_games,
            stakes_blend=stakes_blend,
            stakes_min_games=stakes_min_games,
            elo_weight=elo_weight,
            elo_k=elo_k,
            elo_home_adv=elo_home_adv,
            xg_weight=xg_weight,
            min_scored_matches=effective_min_scored,
        )
        if outcome.excluded or outcome.body is None:
            record_run(
                connection,
                match_id=target.match_id,
                engine=SCORE_MODEL_ENGINE,
                status="excluded",
                detail=outcome.reason or "Excluded.",
                experiment_id=experiment_id,
            )
            counts["excluded"] += 1
            continue
        prediction_id = insert_prediction(
            connection,
            match_id=target.match_id,
            snapshot_id=snapshot_id,
            engine=SCORE_MODEL_ENGINE,
            model_id=model_id,
            body=outcome.body,
            experiment_id=experiment_id,
        )
        if prediction_id is None:
            counts["existing"] += 1
            record_run(
                connection,
                match_id=target.match_id,
                engine=SCORE_MODEL_ENGINE,
                status="completed",
                detail="Prediction already stored.",
                experiment_id=experiment_id,
            )
            continue
        counts["priced"] += 1
        extras = []
        if decay_half_life_days is not None:
            extras.append(f"decay_half_life_days={decay_half_life_days}")
        if prior_mean != "one":
            extras.append(f"prior_mean={prior_mean}")
        if ratio_mode != "one_pass":
            extras.append(f"ratio_mode={ratio_mode}")
        if dependence != "independent":
            extras.append(f"dependence={dependence}")
        if half_time_share_mode != "club_shrunk":
            extras.append(f"half_time_share_mode={half_time_share_mode}")
        if abs(draw_boost - 1.0) > 1e-9:
            extras.append(f"draw_boost={draw_boost:g}")
        if draw_weights:
            extras.append(f"draw_weights={list(draw_weights)}")
        if draw_pick_threshold is not None or draw_pick_margin is not None:
            extras.append(
                f"draw_pick(thr={draw_pick_threshold},margin={draw_pick_margin})"
            )
        if draw_tightness_scale is not None:
            extras.append(f"draw_tightness_scale={draw_tightness_scale:g}")
        if goal_cap is not None:
            extras.append(f"goal_cap={goal_cap:g}@{goal_cap_slope:g}")
        if nb_alpha > 0:
            extras.append(f"nb_alpha={nb_alpha:g}")
        if promoted_attack_prior is not None or promoted_defence_prior is not None:
            extras.append(
                f"promoted_prior=({promoted_attack_prior},{promoted_defence_prior})"
            )
        detail = f"Priced with the Telegram bot score_model (k={k}"
        if extras:
            detail += ", " + ", ".join(extras)
        detail += ")."
        record_run(
            connection,
            match_id=target.match_id,
            engine=SCORE_MODEL_ENGINE,
            status="completed",
            detail=detail,
            experiment_id=experiment_id,
        )
        if row["ft_home"] is not None and row["ft_away"] is not None:
            metrics = evaluate_prediction(
                outcome.body,
                {
                    "ft_home": row["ft_home"],
                    "ft_away": row["ft_away"],
                    "ht_home": row["ht_home"],
                    "ht_away": row["ht_away"],
                },
            )
            insert_evaluation(connection, prediction_id, int(row["revision"]), metrics)
    connection.commit()
    try:
        from laboratory.store import checkpoint_wal

        checkpoint_wal(connection, mode="TRUNCATE")
    except sqlite3.OperationalError:
        # Another reader/writer may block TRUNCATE; PASSIVE still helps shrink pressure.
        try:
            from laboratory.store import checkpoint_wal

            checkpoint_wal(connection, mode="PASSIVE")
        except sqlite3.OperationalError:
            pass
    return counts


def price_one_score_model(
    connection: sqlite3.Connection,
    match_id: str,
    *,
    replace: bool = True,
) -> dict[str, Any]:
    """Price one stored match with the live bot score_model stack (no Gemini).

    Writes laboratory predictions the dashboard reads. Finished matches also get
    an evaluation when a full-time result exists. Upcoming fixtures are allowed.
    """

    from laboratory.store import delete_match_engine_outputs, ensure_experiment

    row = connection.execute(
        """
        SELECT m.match_id, m.competition, m.season, m.kickoff,
               m.home_id, m.home_name, m.away_id, m.away_name,
               r.ft_home, r.ft_away, r.ht_home, r.ht_away, r.revision
        FROM matches m
        LEFT JOIN result_revisions r
          ON r.match_id = m.match_id
         AND r.revision = (
            SELECT MAX(revision) FROM result_revisions WHERE match_id = m.match_id
         )
        WHERE m.match_id = ?
        """,
        (match_id,),
    ).fetchone()
    if row is None and match_id.startswith("fd-"):
        return price_one_score_model(
            connection,
            match_id.removeprefix("fd-"),
            replace=replace,
        )
    if row is None:
        return {"ok": False, "status": "missing", "detail": "Match not found in laboratory."}

    from modeling.league_stacks import draw_calibration_kwargs, live_stack_for

    competition = str(row["competition"] or "PL").upper()
    if competition == "PL":
        experiment_id = BOT_EXPERIMENT_ID
    else:
        experiment_id = f"BOT-{competition}-live"
    stack = live_stack_for(competition)
    model_id = str(stack.get("model_id") or "bot-score-model-live")
    k = float(stack["k"])
    prior_mean = str(stack["prior_mean"])
    dependence = str(stack["dependence"])
    half_time_share_mode = str(stack["half_time_share_mode"])
    ratio_mode = str(stack.get("ratio_mode") or "one_pass")
    decay = stack.get("decay_half_life_days")
    draw_kwargs = draw_calibration_kwargs(stack)
    draw_boost = float(draw_kwargs["draw_boost"])
    ensure_experiment(
        connection,
        {
            "experiment_id": experiment_id,
            "competition": competition,
            "k": k,
            "prior_mean": prior_mean,
            "dependence": dependence,
            "half_time_share_mode": half_time_share_mode,
            "draw_boost": draw_boost,
            "draw_weights": draw_kwargs.get("draw_weights"),
            "draw_pick_threshold": draw_kwargs.get("draw_pick_threshold"),
            "draw_pick_margin": draw_kwargs.get("draw_pick_margin"),
            "draw_tightness_scale": draw_kwargs.get("draw_tightness_scale"),
            "tail_tolerance": 0.001,
            "engine": SCORE_MODEL_ENGINE,
            "score_model_config_id": model_id,
            "gemini": False,
            "note": (
                f"Dashboard on-demand price ({competition} live stack: "
                f"k={k}, {prior_mean}, {dependence}, {half_time_share_mode}, "
                f"draw_boost={draw_boost:g})."
            ),
        },
    )

    history_rows = list_matches(connection, competition=competition)
    history = [_record(item) for item in history_rows]
    target = _record(row)
    season_ratio_cache: dict[str, dict[str, dict[str, float]]] = {}
    if prior_mean == "last_season":
        season_ratio_cache = _last_season_ratio_cache(
            history,
            k=k,
            goal_cap=draw_kwargs.get("goal_cap"),
            goal_cap_slope=float(draw_kwargs.get("goal_cap_slope") or 0.5),
        )
    payload = build_snapshot(target, history)
    snapshot_id = save_snapshot(connection, payload, experiment_id=experiment_id)
    if not payload["leakage_ok"]:
        record_run(
            connection,
            match_id=target.match_id,
            engine=SCORE_MODEL_ENGINE,
            status="failed",
            detail=payload["leakage_reason"] or "Leakage check failed.",
            experiment_id=experiment_id,
        )
        connection.commit()
        return {
            "ok": False,
            "status": "failed",
            "detail": payload["leakage_reason"] or "Leakage check failed.",
            "experiment_id": experiment_id,
            "match_id": target.match_id,
        }

    means = None
    if competition == "CL" and (
        stack.get("persistent_priors")
        or float(stack.get("elo_blend") or 0.0) > 0.0
        or float(stack.get("domestic_prior_weight") or 0.0) > 0.0
    ):
        from laboratory.cl_features import resolve_cl_live_prior_means

        db_row = connection.execute("PRAGMA database_list").fetchone()
        db_file = db_row[2] if db_row and len(db_row) > 2 and db_row[2] else None
        means = resolve_cl_live_prior_means(
            home_id=target.home_id,
            away_id=target.away_id,
            kickoff=target.kickoff,
            k=k,
            persistent_priors=bool(stack.get("persistent_priors")),
            elo_blend=float(stack.get("elo_blend") or 0.0),
            domestic_prior_weight=float(stack.get("domestic_prior_weight") or 0.0),
            default_atk=float(draw_kwargs.get("promoted_attack_prior") or 0.85),
            default_def=float(draw_kwargs.get("promoted_defence_prior") or 1.15),
            db_path=db_file,
        )
    if means is None and prior_mean == "last_season":
        means = prior_means_for_fixture(
            season_ratio_cache.get(target.season) or {},
            home_id=target.home_id,
            away_id=target.away_id,
            promoted_attack_prior=draw_kwargs.get("promoted_attack_prior"),
            promoted_defence_prior=draw_kwargs.get("promoted_defence_prior"),
        )
    cl_min_scored = (
        0
        if (
            competition == "CL"
            and (
                stack.get("persistent_priors")
                or float(stack.get("domestic_prior_weight") or 0.0) > 0.0
            )
        )
        else None
    )
    outcome = price_from_snapshot(
        payload,
        k=k,
        config_id=model_id,
        prior_means=means,
        ratio_mode=ratio_mode,
        dependence=dependence,
        half_time_share_mode=half_time_share_mode,
        decay_half_life_days=float(decay) if decay is not None else None,
        min_scored_matches=cl_min_scored,
        **draw_kwargs,
    )
    if outcome.excluded or outcome.body is None:
        record_run(
            connection,
            match_id=target.match_id,
            engine=SCORE_MODEL_ENGINE,
            status="excluded",
            detail=outcome.reason or "Excluded.",
            experiment_id=experiment_id,
        )
        connection.commit()
        return {
            "ok": False,
            "status": "excluded",
            "detail": outcome.reason or "Excluded.",
            "experiment_id": experiment_id,
            "match_id": target.match_id,
        }

    if replace:
        delete_match_engine_outputs(
            connection,
            match_id=target.match_id,
            experiment_id=experiment_id,
            engine=SCORE_MODEL_ENGINE,
        )
    prediction_id = insert_prediction(
        connection,
        match_id=target.match_id,
        snapshot_id=snapshot_id,
        engine=SCORE_MODEL_ENGINE,
        model_id=model_id,
        body=outcome.body,
        experiment_id=experiment_id,
    )
    if prediction_id is None:
        record_run(
            connection,
            match_id=target.match_id,
            engine=SCORE_MODEL_ENGINE,
            status="completed",
            detail="Prediction already stored.",
            experiment_id=experiment_id,
        )
        connection.commit()
        return {
            "ok": True,
            "status": "existing",
            "detail": "Prediction already stored.",
            "experiment_id": experiment_id,
            "match_id": target.match_id,
        }

    record_run(
        connection,
        match_id=target.match_id,
        engine=SCORE_MODEL_ENGINE,
        status="completed",
        detail="Priced from dashboard (live score_model stack).",
        experiment_id=experiment_id,
    )
    evaluated = False
    if row["ft_home"] is not None and row["ft_away"] is not None:
        metrics = evaluate_prediction(
            outcome.body,
            {
                "ft_home": row["ft_home"],
                "ft_away": row["ft_away"],
                "ht_home": row["ht_home"],
                "ht_away": row["ht_away"],
            },
        )
        insert_evaluation(connection, prediction_id, int(row["revision"] or 1), metrics)
        evaluated = True
    connection.commit()
    body = outcome.body
    pick = (
        (body.get("full_time") or {}).get("result_1x2") or {}
    ).get("most_likely")
    score = (body.get("full_time") or {}).get("exact_score_most_likely") or {}
    detail = "Stored score_model prediction."
    if pick:
        detail = f"Stored · FT {str(pick).replace('_', ' ').title()}"
        if score.get("home_goals") is not None:
            detail += f" · {score.get('home_goals')}–{score.get('away_goals')}"
    return {
        "ok": True,
        "status": "priced",
        "detail": detail,
        "experiment_id": experiment_id,
        "match_id": target.match_id,
        "prediction_id": prediction_id,
        "evaluated": evaluated,
    }


def bot_live_experiment_id(competition: str) -> str:
    """Live bot experiment id used by dashboard / sync pricing (no Gemini)."""

    code = str(competition or "PL").upper()
    if code == "PL":
        return BOT_EXPERIMENT_ID
    return f"BOT-{code}-live"


def find_score_model_prediction(
    connection: sqlite3.Connection,
    *,
    match_id: str,
    experiment_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT prediction_id, body_json, experiment_id, model_id
        FROM predictions
        WHERE match_id = ? AND experiment_id = ? AND engine = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (match_id, experiment_id, SCORE_MODEL_ENGINE),
    ).fetchone()


def evaluate_stored_score_model(
    connection: sqlite3.Connection,
    *,
    prediction_id: str,
    body_json: str | dict[str, Any],
    ft_home: int,
    ft_away: int,
    ht_home: int | None,
    ht_away: int | None,
    revision: int,
) -> str:
    """Score an existing prediction against a result revision.

    Returns ``evaluated``, ``already_evaluated``, or ``failed``.
    """

    evaluation_id = f"{prediction_id}:r{int(revision)}"
    existing = connection.execute(
        "SELECT 1 FROM evaluations WHERE evaluation_id = ?",
        (evaluation_id,),
    ).fetchone()
    if existing is not None:
        return "already_evaluated"
    try:
        body = json.loads(body_json) if isinstance(body_json, str) else body_json
        metrics = evaluate_prediction(
            body,
            {
                "ft_home": int(ft_home),
                "ft_away": int(ft_away),
                "ht_home": ht_home,
                "ht_away": ht_away,
            },
        )
        insert_evaluation(connection, prediction_id, int(revision), metrics)
    except Exception:
        return "failed"
    return "evaluated"


def price_or_evaluate_finished_in_window(
    connection: sqlite3.Connection,
    *,
    competitions: tuple[str, ...],
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    """For finished matches in ``[date_from, date_to]``: price if missing, else evaluate.

    Insert-only: never re-prices when a score_model prediction already exists for the
    live bot experiment. Existing predictions are compared to the latest FT result.
    No Gemini.
    """

    counts: dict[str, Any] = {
        "priced": 0,
        "evaluated": 0,
        "already_evaluated": 0,
        "excluded": 0,
        "failed": 0,
        "skipped_unfinished": 0,
        "by_competition": {},
    }
    codes = tuple(dict.fromkeys(c.upper() for c in competitions if c))
    if not codes:
        return counts

    marks = ",".join("?" for _ in codes)
    rows = list(
        connection.execute(
            f"""
            SELECT m.match_id, m.competition, m.kickoff,
                   r.ft_home, r.ft_away, r.ht_home, r.ht_away, r.revision
            FROM matches m
            JOIN result_revisions r
              ON r.match_id = m.match_id
             AND r.revision = (
                SELECT MAX(revision) FROM result_revisions WHERE match_id = m.match_id
             )
            WHERE m.competition IN ({marks})
              AND substr(m.kickoff, 1, 10) >= ?
              AND substr(m.kickoff, 1, 10) <= ?
              AND r.ft_home IS NOT NULL
              AND r.ft_away IS NOT NULL
            ORDER BY m.kickoff, m.match_id
            """,
            (*codes, date_from, date_to),
        )
    )

    per: dict[str, dict[str, int]] = {
        code: {
            "priced": 0,
            "evaluated": 0,
            "already_evaluated": 0,
            "excluded": 0,
            "failed": 0,
        }
        for code in codes
    }

    for row in rows:
        competition = str(row["competition"] or "PL").upper()
        match_id = str(row["match_id"])
        experiment_id = bot_live_experiment_id(competition)
        bucket = per.setdefault(
            competition,
            {
                "priced": 0,
                "evaluated": 0,
                "already_evaluated": 0,
                "excluded": 0,
                "failed": 0,
            },
        )
        existing = find_score_model_prediction(
            connection,
            match_id=match_id,
            experiment_id=experiment_id,
        )
        if existing is not None:
            status = evaluate_stored_score_model(
                connection,
                prediction_id=str(existing["prediction_id"]),
                body_json=str(existing["body_json"]),
                ft_home=int(row["ft_home"]),
                ft_away=int(row["ft_away"]),
                ht_home=row["ht_home"],
                ht_away=row["ht_away"],
                revision=int(row["revision"] or 1),
            )
            if status == "evaluated":
                counts["evaluated"] += 1
                bucket["evaluated"] += 1
            elif status == "already_evaluated":
                counts["already_evaluated"] += 1
                bucket["already_evaluated"] += 1
            else:
                counts["failed"] += 1
                bucket["failed"] += 1
            continue

        result = price_one_score_model(connection, match_id, replace=False)
        status = str(result.get("status") or "")
        if status == "priced":
            # New prediction (+ auto-eval when FT exists). Count as priced only.
            counts["priced"] += 1
            bucket["priced"] += 1
        elif status == "existing":
            # Race / alternate path: prediction appeared; compare now.
            again = find_score_model_prediction(
                connection,
                match_id=match_id,
                experiment_id=experiment_id,
            )
            if again is not None:
                ev = evaluate_stored_score_model(
                    connection,
                    prediction_id=str(again["prediction_id"]),
                    body_json=str(again["body_json"]),
                    ft_home=int(row["ft_home"]),
                    ft_away=int(row["ft_away"]),
                    ht_home=row["ht_home"],
                    ht_away=row["ht_away"],
                    revision=int(row["revision"] or 1),
                )
                if ev == "evaluated":
                    counts["evaluated"] += 1
                    bucket["evaluated"] += 1
                elif ev == "already_evaluated":
                    counts["already_evaluated"] += 1
                    bucket["already_evaluated"] += 1
                else:
                    counts["failed"] += 1
                    bucket["failed"] += 1
            else:
                counts["failed"] += 1
                bucket["failed"] += 1
        elif status == "excluded":
            counts["excluded"] += 1
            bucket["excluded"] += 1
        else:
            counts["failed"] += 1
            bucket["failed"] += 1

    connection.commit()
    counts["by_competition"] = per
    return counts


def _last_season_ratio_cache(
    history: list[MatchRecord],
    *,
    k: float,
    goal_cap: float | None = None,
    goal_cap_slope: float = 0.5,
) -> dict[str, dict[str, dict[str, float]]]:
    """Map current season -> club venue ratios computed from the previous season only."""

    by_season: dict[str, list[dict[str, Any]]] = {}
    for match in history:
        if match.ft_home is None or match.ft_away is None:
            continue
        by_season.setdefault(match.season, []).append(
            {
                "home_id": match.home_id,
                "away_id": match.away_id,
                "ft_home": match.ft_home,
                "ft_away": match.ft_away,
            }
        )
    ratios_by_source = {
        season: club_venue_ratios(
            matches,
            k=k,
            goal_cap=goal_cap,
            goal_cap_slope=goal_cap_slope,
        )
        for season, matches in by_season.items()
    }
    cache: dict[str, dict[str, dict[str, float]]] = {}
    for season in by_season:
        try:
            previous = str(int(season) - 1)
        except ValueError:
            continue
        cache[season] = ratios_by_source.get(previous) or {}
    return cache


def _record(row: sqlite3.Row) -> MatchRecord:
    keys = set(row.keys())
    return MatchRecord(
        match_id=row["match_id"],
        season=row["season"],
        kickoff=row["kickoff"],
        home_id=row["home_id"],
        home_name=row["home_name"],
        away_id=row["away_id"],
        away_name=row["away_name"],
        ft_home=row["ft_home"],
        ft_away=row["ft_away"],
        ht_home=row["ht_home"],
        ht_away=row["ht_away"],
        competition=str(row["competition"] if "competition" in keys else "PL"),
    )


def gemini_prompt(payload: dict[str, Any], *, half_time_required: bool) -> str:
    """Prompt for the independent Gemini engine. Priors only."""

    instruction = {
        "task": "Predict this Premier League match from the prior matches only.",
        "schema_version": "gemini-schema-exp-pl-001",
        "probabilities": "fractions from 0 to 1",
        "exact_scores": "include every home_goals and away_goals pair from 0 through 10",
        "half_time_required": half_time_required,
        "snapshot": payload,
        "forbidden": [
            "Do not use the result of this match.",
            "Do not use another model's probabilities or expected goals.",
        ],
    }
    return json_dumps(instruction)


def json_dumps(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, sort_keys=True)


def _last_kickoff_by_team(history, target_kickoff):
    from laboratory.score_model_adapter import team_int_id
    last = {}
    for m in history:
        if m.kickoff >= target_kickoff:
            continue
        if m.ft_home is None or m.ft_away is None:
            continue
        for tid in (m.home_id, m.away_id):
            key = team_int_id(str(tid))
            if key not in last or m.kickoff > last[key]:
                last[key] = m.kickoff
    return last
