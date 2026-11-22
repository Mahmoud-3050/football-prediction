"""Lab Experiment Runner — Parallel, Resumable, Single-Writer.

Uses ProcessPoolExecutor with read-only snapshot caching.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from laboratory.lab_experiment import ExperimentConfig, make_model_id
from laboratory.score_model_adapter import SCORE_MODEL_ENGINE, price_from_snapshot
from laboratory.snapshot import MatchRecord, build_snapshot, priors_from_snapshot
from laboratory.store import (
    connect,
    insert_evaluation,
    insert_prediction,
    list_matches,
    record_run,
    save_snapshot,
)
from score_model import club_venue_ratios, prior_means_for_fixture
from laboratory.score_model_adapter import team_int_id

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/lab.db")
LIVE_DB_PATH = Path("data/lab.db")  # Same file for now; can be separate


def _commit_with_retry(
    connection: sqlite3.Connection,
    *,
    attempts: int = 8,
    base_sleep: float = 0.05,
) -> None:
    """Commit through transient ``database is locked`` (dashboard + workers)."""

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            connection.commit()
            return
        except sqlite3.OperationalError as exc:
            last = exc
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            time.sleep(base_sleep * (2**attempt))
    assert last is not None
    raise last


def _connect_lab_with_retry(
    path: Path,
    *,
    attempts: int = 8,
    base_sleep: float = 0.05,
) -> sqlite3.Connection:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            conn = connect(path)
            conn.execute("PRAGMA busy_timeout = 60000")
            return conn
        except sqlite3.OperationalError as exc:
            last = exc
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            time.sleep(base_sleep * (2**attempt))
    assert last is not None
    raise last


@dataclass(frozen=True)
class ExperimentResult:
    experiment_id: str
    competition: str
    completed_runs: int
    failed_runs: int
    best_model_id: str | None
    best_config: dict[str, Any] | None
    best_metrics: dict[str, Any] | None
    all_runs: list[dict[str, Any]]


@dataclass(frozen=True)
class _WorkerInput:
    """Input passed to worker process (must be picklable)."""
    competition: str
    seasons: tuple[int, ...]
    config: dict[str, Any]
    model_id: str
    experiment_id: str
    live_db_path: str  # Path string for read-only access
    lab_db_path: str = "data/lab.db"  # Path string for lab DB access


@dataclass(frozen=True)
class _WorkerOutput:
    """Result returned from worker process."""
    model_id: str
    status: str  # "completed" | "failed"
    metrics: dict[str, Any] | None
    matches_priced: int
    matches_evaluated: int
    error: str | None


@dataclass(frozen=True)
class _CachedTarget:
    """One finished match ready to price, with same-season priors only."""

    target: MatchRecord
    priors: list[MatchRecord]


def _history_season_keys(
    seasons: tuple[int, ...],
    *,
    include_previous: bool,
) -> tuple[str, ...]:
    """Season keys to load from live DB (optionally previous year for last_season)."""
    keys: set[str] = {str(int(s)) for s in seasons}
    if include_previous:
        for season in seasons:
            keys.add(str(int(season) - 1))
    return tuple(sorted(keys))


def _build_snapshot_cache(
    live_db_path: Path,
    competition: str,
    seasons: tuple[int, ...],
    *,
    include_previous_season: bool = False,
) -> tuple[dict[str, _CachedTarget], list[MatchRecord]]:
    """Build read-only snapshot cache from live.db.

    Returns ``(targets_by_id, all_loaded_matches)``. Targets are restricted to
    ``seasons``; ``all_loaded_matches`` may also include the previous season so
    ``prior_mean=last_season`` can fit club ratios without leakage into priors.
    """
    conn = sqlite3.connect(f"file:{live_db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    season_keys = _history_season_keys(seasons, include_previous=include_previous_season)
    target_season_keys = {str(int(s)) for s in seasons}
    placeholders = ",".join("?" for _ in season_keys)
    rows = conn.execute(
        f"""
        SELECT m.*, r.ft_home, r.ft_away, r.ht_home, r.ht_away, r.revision
        FROM matches m
        LEFT JOIN result_revisions r
          ON r.match_id = m.match_id
         AND r.revision = (
            SELECT MAX(revision) FROM result_revisions WHERE match_id = m.match_id
         )
        WHERE m.competition = ? AND m.season IN ({placeholders})
        ORDER BY m.kickoff, m.match_id
        """,
        (competition.upper(), *season_keys),
    ).fetchall()
    conn.close()

    all_matches: list[MatchRecord] = []
    for row in rows:
        all_matches.append(
            MatchRecord(
                match_id=row["match_id"],
                season=str(row["season"]),
                kickoff=row["kickoff"],
                home_id=row["home_id"],
                home_name=row["home_name"],
                away_id=row["away_id"],
                away_name=row["away_name"],
                ft_home=row["ft_home"],
                ft_away=row["ft_away"],
                ht_home=row["ht_home"],
                ht_away=row["ht_away"],
                competition=str(row["competition"] or competition.upper()),
            )
        )

    matches_by_season: dict[str, list[MatchRecord]] = {}
    for match in all_matches:
        matches_by_season.setdefault(str(match.season), []).append(match)

    cache: dict[str, _CachedTarget] = {}
    for target in all_matches:
        if str(target.season) not in target_season_keys:
            continue
        if target.ft_home is None or target.ft_away is None:
            continue
        season_matches = matches_by_season.get(str(target.season)) or []
        priors = [
            match
            for match in season_matches
            if match.match_id != target.match_id
            and match.kickoff < target.kickoff
            and match.ft_home is not None
            and match.ft_away is not None
        ]
        if not priors:
            continue
        cache[target.match_id] = _CachedTarget(target=target, priors=priors)

    return cache, all_matches


def _price_worker(input_data: _WorkerInput) -> _WorkerOutput:
    """Worker function: price all matches for one config in a separate process.

    Opens its own DB connections (read-only to live, write to lab).
    """
    live_db = Path(input_data.live_db_path)

    # Resolve model params from config
    k = float(input_data.config.get("k", 6.0))
    prior_mean = str(input_data.config.get("prior_mean", "one"))
    ratio_mode = str(input_data.config.get("ratio_mode", "one_pass"))
    dependence = str(input_data.config.get("dependence", "independent"))
    half_time_share_mode = str(input_data.config.get("half_time_share_mode", "club_shrunk"))
    draw_boost = float(input_data.config.get("draw_boost", 1.0))
    draw_weights = input_data.config.get("draw_weights")
    draw_pick_threshold = input_data.config.get("draw_pick_threshold")
    draw_pick_margin = input_data.config.get("draw_pick_margin")
    draw_tightness_scale = input_data.config.get("draw_tightness_scale")
    draw_tightness_ref = float(input_data.config.get("draw_tightness_ref", 1.0))
    decay_half_life_days = input_data.config.get("decay_half_life_days")
    goal_cap_raw = input_data.config.get("goal_cap")
    goal_cap = None if goal_cap_raw is None else float(goal_cap_raw)
    slope_raw = input_data.config.get("goal_cap_slope")
    goal_cap_slope = 0.5 if slope_raw is None else float(slope_raw)
    alpha_raw = input_data.config.get("nb_alpha")
    nb_alpha = 0.0 if alpha_raw is None else float(alpha_raw)
    promo_atk_raw = input_data.config.get("promoted_attack_prior")
    promoted_attack_prior = None if promo_atk_raw is None else float(promo_atk_raw)
    promo_def_raw = input_data.config.get("promoted_defence_prior")
    promoted_defence_prior = None if promo_def_raw is None else float(promo_def_raw)
    vk_raw = input_data.config.get("venue_k")
    venue_k = None if vk_raw is None else float(vk_raw)
    srd_raw = input_data.config.get("short_rest_days")
    short_rest_days = None if srd_raw is None else int(srd_raw)
    rest_attack_factor = float(input_data.config.get("rest_attack_factor", 0.94))
    rest_defence_factor = float(input_data.config.get("rest_defence_factor", 1.05))
    standings_blend = float(input_data.config.get("standings_blend", 0.0))
    standings_min_games = int(input_data.config.get("standings_min_games", 6))

    cache, all_history = _build_snapshot_cache(
        live_db,
        input_data.competition,
        input_data.seasons,
        include_previous_season=(prior_mean == "last_season"),
    )

    lab_conn = _connect_lab_with_retry(Path(input_data.lab_db_path))

    season_ratio_cache: dict[str, dict[str, dict[str, float]]] = {}
    if prior_mean == "last_season":
        season_ratio_cache = _last_season_ratio_cache(
            all_history,
            k=k,
            goal_cap=goal_cap,
            goal_cap_slope=goal_cap_slope,
        )

    priced = 0
    evaluated = 0
    pending_commits = 0

    try:
        for cached in cache.values():
            target = cached.target
            priors = cached.priors

            payload = build_snapshot(target, priors)
            if not payload["leakage_ok"]:
                continue

            means = None
            if prior_mean == "last_season":
                means = prior_means_for_fixture(
                    season_ratio_cache.get(str(target.season)) or {},
                    home_id=target.home_id,
                    away_id=target.away_id,
                    promoted_attack_prior=promoted_attack_prior,
                    promoted_defence_prior=promoted_defence_prior,
                )

            outcome = price_from_snapshot(
                payload,
                k=k,
                config_id=input_data.model_id,
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
                last_kickoff_by_team=_last_kickoff_by_team(all_history, target.kickoff) if short_rest_days is not None else None,
                standings_blend=standings_blend,
                standings_min_games=standings_min_games,
            )

            if outcome.excluded or outcome.body is None:
                continue

            priced += 1

            snapshot_id = save_snapshot(lab_conn, payload, experiment_id=input_data.experiment_id)
            prediction_id = insert_prediction(
                lab_conn,
                match_id=target.match_id,
                snapshot_id=snapshot_id,
                engine=SCORE_MODEL_ENGINE,
                model_id=input_data.model_id,
                body=outcome.body,
                experiment_id=input_data.experiment_id,
            )

            if target.ft_home is not None and target.ft_away is not None:
                from laboratory.evaluate import evaluate_prediction

                metrics = evaluate_prediction(
                    outcome.body,
                    {
                        "ft_home": target.ft_home,
                        "ft_away": target.ft_away,
                        "ht_home": target.ht_home,
                        "ht_away": target.ht_away,
                    },
                )
                evaluated += 1
                if prediction_id:
                    insert_evaluation(lab_conn, prediction_id, 1, metrics)

            pending_commits += 1
            if pending_commits >= 50:
                _commit_with_retry(lab_conn)
                pending_commits = 0

        if pending_commits:
            _commit_with_retry(lab_conn)

        return _WorkerOutput(
            model_id=input_data.model_id,
            status="completed",
            metrics=None,
            matches_priced=priced,
            matches_evaluated=evaluated,
            error=None,
        )

    except Exception as e:
        logger.exception("Worker failed for model %s", input_data.model_id)
        return _WorkerOutput(
            model_id=input_data.model_id,
            status="failed",
            metrics=None,
            matches_priced=0,
            matches_evaluated=0,
            error=str(e),
        )
    finally:
        lab_conn.close()


def _last_season_ratio_cache(
    history: list[MatchRecord],
    *,
    k: float,
    goal_cap: float | None = None,
    goal_cap_slope: float = 0.5,
) -> dict[str, dict[str, dict[str, float]]]:
    """Map current season -> club venue ratios from previous season only."""
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


def _aggregate_metrics(
    lab_db_path: Path,
    experiment_id: str,
    model_id: str,
) -> dict[str, Any]:
    """Compute aggregate metrics for a model from evaluations in lab DB."""
    conn = connect(lab_db_path)
    try:
        # Get all evaluations for this model
        rows = conn.execute(
            """
            SELECT e.metrics_json
            FROM evaluations e
            JOIN predictions p ON p.prediction_id = e.prediction_id
            WHERE p.experiment_id = ? AND p.model_id = ?
            """,
            (experiment_id, model_id),
        ).fetchall()

        if not rows:
            return {}

        # Aggregate
        total = len(rows)
        metrics_sum: dict[str, float] = {}
        metrics_count: dict[str, int] = {}

        for row in rows:
            metrics = json.loads(row["metrics_json"])
            for market, vals in metrics.items():
                if vals.get("status") != "scored":
                    continue
                if market not in metrics_sum:
                    metrics_sum[market] = 0.0
                    metrics_count[market] = 0
                if "log_loss" in vals:
                    key = f"{market}_log_loss"
                    metrics_sum[key] = metrics_sum.get(key, 0.0) + vals["log_loss"]
                    metrics_count[key] = metrics_count.get(key, 0) + 1
                if "brier" in vals:
                    key = f"{market}_brier"
                    metrics_sum[key] = metrics_sum.get(key, 0.0) + vals["brier"]
                    metrics_count[key] = metrics_count.get(key, 0) + 1
                if "correct" in vals:
                    key = f"{market}_accuracy"
                    metrics_sum[key] = metrics_sum.get(key, 0.0) + (1.0 if vals["correct"] else 0.0)
                    metrics_count[key] = metrics_count.get(key, 0) + 1

        # Compute means
        result: dict[str, Any] = {"n_matches": total}
        for key in metrics_sum:
            if metrics_count.get(key, 0) > 0:
                result[key] = metrics_sum[key] / metrics_count[key]

        # Nested market blocks for dashboard leaderboard / AI prompt.
        for market in (
            "exact_score",
            "half_time_1x2",
            "over_under_2_5",
            "over_under_3_5",
        ):
            block: dict[str, Any] = {}
            acc_key = f"{market}_accuracy"
            ll_key = f"{market}_log_loss"
            if acc_key in result:
                block["hit"] = result[acc_key]
                block["accuracy"] = result[acc_key]
                if market == "exact_score":
                    # Alias used by Exact Hit % readers / primary-metric tooling.
                    result["exact_score_hit_rate"] = result[acc_key]
                    block["hit_rate"] = result[acc_key]
                    block["correct"] = result[acc_key]
            if ll_key in result:
                block["log_loss"] = result[ll_key]
            if metrics_count.get(acc_key, 0) > 0:
                block["n"] = metrics_count[acc_key]
            if block:
                result[market] = block

        return result
    finally:
        conn.close()


def run_experiment(
    lab_db_path: Path = DEFAULT_DB_PATH,
    live_db_path: Path = LIVE_DB_PATH,
    config: ExperimentConfig | None = None,
    *,
    max_workers: int = 4,
    resume: bool = True,
) -> ExperimentResult:
    """Run a full experiment with parallel config evaluation.

    1. Register experiment in lab DB
    2. Generate all config combinations + deterministic model_ids
    3. Skip already-completed runs if resume=True
    4. Execute pending configs in ProcessPoolExecutor
    5. Aggregate metrics and find best config
    6. Mark experiment complete
    """
    if config is None:
        raise ValueError("ExperimentConfig required")

    # Connect to lab DB
    conn = connect(lab_db_path)

    # Register experiment
    from laboratory.store import create_lab_experiment, complete_lab_experiment
    create_lab_experiment(conn, config.experiment_id, config.competition, config.__dict__)
    conn.close()

    # Generate all model configs
    model_configs = []
    for combo in _expand_param_grid(config.param_grid):
        merged = _merge_configs(config.base_config, combo)
        model_id = make_model_id(config.experiment_id, merged)
        model_configs.append((merged, model_id))

    logger.info(
        "Experiment %s: %d configs to evaluate (%d workers)",
        config.experiment_id,
        len(model_configs),
        max_workers,
    )

    # Check for existing completed runs
    conn = connect(lab_db_path)
    existing = conn.execute(
        "SELECT model_id FROM lab_experiment_runs WHERE experiment_id = ? AND status = 'completed'",
        (config.experiment_id,),
    ).fetchall()
    conn.close()

    completed_model_ids = {row["model_id"] for row in existing}
    pending = [(c, mid) for c, mid in model_configs if mid not in completed_model_ids]

    logger.info("Resuming: %d completed, %d pending", len(completed_model_ids), len(pending))

    if not pending:
        # Nothing to do, just return existing results
        return _collect_results(config, lab_db_path)

    # Prepare worker inputs
    worker_inputs = [
        _WorkerInput(
            competition=config.competition,
            seasons=config.all_seasons,
            config=cfg,
            model_id=mid,
            experiment_id=config.experiment_id,
            live_db_path=str(live_db_path),
            lab_db_path=str(lab_db_path),
        )
        for cfg, mid in pending
    ]

    # Execute in parallel
    completed_runs = 0
    failed_runs = 0

    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_price_worker, inp): inp.model_id for inp in worker_inputs}

        for future in concurrent.futures.as_completed(futures):
            model_id = futures[future]
            try:
                output = future.result()
            except Exception as e:
                logger.exception("Worker crashed for %s", model_id)
                output = _WorkerOutput(
                    model_id=model_id,
                    status="failed",
                    metrics=None,
                    matches_priced=0,
                    matches_evaluated=0,
                    error=str(e),
                )

            # Aggregate metrics for completed runs
            metrics = None
            if output.status == "completed" and output.matches_evaluated > 0:
                metrics = _aggregate_metrics(lab_db_path, config.experiment_id, model_id)

            # Record run in DB (retry through WAL contention with dashboard readers)
            conn = _connect_lab_with_retry(lab_db_path)
            from laboratory.store import upsert_lab_experiment_run
            try:
                upsert_lab_experiment_run(
                    conn,
                    run_id=f"{config.experiment_id}:{model_id}",
                    experiment_id=config.experiment_id,
                    model_id=model_id,
                    config=next(cfg for cfg, mid in pending if mid == model_id),
                    status=output.status,
                    metrics=metrics,
                    matches_priced=output.matches_priced,
                    matches_evaluated=output.matches_evaluated,
                    error=output.error,
                )
                _commit_with_retry(conn)
            finally:
                conn.close()

            if output.status == "completed":
                completed_runs += 1
            else:
                failed_runs += 1

            logger.info(
                "Config %s: %s (priced=%d, evaluated=%d)",
                model_id,
                output.status,
                output.matches_priced,
                output.matches_evaluated,
            )

    # Aggregate metrics for all completed runs
    return _collect_results(config, lab_db_path, completed_runs, failed_runs)


def _collect_results(
    config: ExperimentConfig,
    lab_db_path: Path,
    completed_runs: int = 0,
    failed_runs: int = 0,
) -> ExperimentResult:
    """Collect and rank all experiment results."""
    conn = connect(lab_db_path)
    runs = conn.execute(
        """
        SELECT run_id, model_id, config_json, status, metrics_json,
               matches_priced, matches_evaluated, error
        FROM lab_experiment_runs
        WHERE experiment_id = ? AND status = 'completed'
        ORDER BY matches_evaluated DESC
        """,
        (config.experiment_id,),
    ).fetchall()
    conn.close()

    if not runs:
        return ExperimentResult(
            experiment_id=config.experiment_id,
            competition=config.competition,
            completed_runs=completed_runs,
            failed_runs=failed_runs,
            best_model_id=None,
            best_config=None,
            best_metrics=None,
            all_runs=[],
        )

    # Rank by dashboard primary metric (mapped to aggregate evaluation keys)
    from laboratory.lab_experiment import metric_sort_value, pick_best_run

    metric_key, higher_is_better = config.primary_metric_key()
    ranked: list[dict[str, Any]] = []
    for row in runs:
        metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
        primary_val = metric_sort_value(
            metrics, metric_key, higher_is_better=higher_is_better
        )
        ranked.append(
            {
                "model_id": row["model_id"],
                "config": json.loads(row["config_json"]),
                "status": row["status"],
                "metrics": metrics,
                "primary_metric_value": primary_val,
                "matches_priced": row["matches_priced"],
                "matches_evaluated": row["matches_evaluated"],
                "meets_min_matches": int(row["matches_evaluated"] or 0)
                >= max(0, int(config.min_matches)),
            }
        )

    ranked.sort(
        key=lambda r: r["primary_metric_value"],
        reverse=higher_is_better,
    )

    # Best config must clear min_matches; do not crown a tiny-sample winner.
    best = pick_best_run(ranked, min_matches=config.min_matches)

    return ExperimentResult(
        experiment_id=config.experiment_id,
        competition=config.competition,
        completed_runs=completed_runs,
        failed_runs=failed_runs,
        best_model_id=best["model_id"] if best else None,
        best_config=best["config"] if best else None,
        best_metrics=best["metrics"] if best else None,
        all_runs=ranked,
    )


def _expand_param_grid(param_grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
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


def _merge_configs(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Deep merge base config with overrides (explicit None overrides base)."""
    result = dict(base)
    for key, value in overrides.items():
        result[key] = value
    return result


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
