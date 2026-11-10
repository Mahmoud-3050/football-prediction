"""Walk-forward job. Poisson and score_model do not read Gemini. Results are scored after insert."""

from __future__ import annotations

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
from score_model import club_venue_ratios, prior_means_for_fixture

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

    competition = str(
        competition or BOT_EXPERIMENT_CONFIG.get("competition") or "PL"
    ).upper()
    rows = list_matches(connection, competition=competition)
    history = [_record(row) for row in rows]
    season_ratio_cache: dict[str, dict[str, dict[str, float]]] = {}
    if prior_mean == "last_season":
        season_ratio_cache = _last_season_ratio_cache(history, k=k)
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
        if prior_mean == "last_season":
            means = prior_means_for_fixture(
                season_ratio_cache.get(target.season) or {},
                home_id=target.home_id,
                away_id=target.away_id,
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
    return counts


def _last_season_ratio_cache(
    history: list[MatchRecord],
    *,
    k: float,
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
        season: club_venue_ratios(matches, k=k) for season, matches in by_season.items()
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
