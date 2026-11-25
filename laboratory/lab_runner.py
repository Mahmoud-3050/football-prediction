"""Lab Experiment Runner — Parallel, Resumable, Single-Writer.

Uses ProcessPoolExecutor with read-only snapshot caching.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from multiprocessing import shared_memory
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from domain.entities import EncodedDataset, ModelConfig
from domain.features import encode_matches
from laboratory.lab_experiment import (
    ExperimentConfig,
    expand_param_grid,
    make_model_id,
    merge_configs,
)
from laboratory.metrics import StreamingMetricsAccumulator
from laboratory.repositories import (
    ExperimentRepository,
    MatchRepository,
    connect_managed,
    transaction,
)
from laboratory.score_model_adapter import SCORE_MODEL_ENGINE, price_from_snapshot
from laboratory.snapshot import MatchRecord, build_snapshot
from laboratory.store import connect
from modeling.score_model import club_venue_ratios, prior_means_for_fixture
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


def _init_worker() -> None:
    """Pin BLAS/OpenMP thread pools to 1 inside worker processes to avoid contention."""
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"


@contextmanager
def shared_encoded_dataset(dataset: EncodedDataset) -> Iterator[dict[str, Any]]:
    """Publish EncodedDataset NumPy arrays via SharedMemory (with memmap fallback)."""
    import shutil
    import tempfile

    specs = {
        "home_idx": dataset.home_idx,
        "away_idx": dataset.away_idx,
        "ft_home": dataset.ft_home,
        "ft_away": dataset.ft_away,
        "ht_home": dataset.ht_home,
        "ht_away": dataset.ht_away,
        "ht_usable": dataset.ht_usable,
        "kickoff_days": dataset.kickoff_days,
        "season_codes": dataset.season_codes,
    }
    blocks: list[shared_memory.SharedMemory] = []
    tmp_dir: str | None = None
    meta: dict[str, Any] = {"backend": "shm", "arrays": {}}
    try:
        try:
            for name, arr in specs.items():
                contig = np.ascontiguousarray(arr)
                size = max(1, contig.nbytes)
                shm = shared_memory.SharedMemory(create=True, size=size)
                blocks.append(shm)
                if contig.nbytes > 0:
                    buf = np.ndarray(contig.shape, dtype=contig.dtype, buffer=shm.buf)
                    buf[:] = contig
                meta["arrays"][name] = (shm.name, contig.shape, contig.dtype.str)
        except PermissionError:
            for shm in blocks:
                try:
                    shm.close()
                    shm.unlink()
                except Exception:
                    pass
            blocks.clear()
            tmp_dir = tempfile.mkdtemp(prefix="fp_shared_")
            meta = {"backend": "memmap", "dir": tmp_dir, "arrays": {}}
            for name, arr in specs.items():
                contig = np.ascontiguousarray(arr)
                fpath = os.path.join(tmp_dir, f"{name}.npy")
                np.save(fpath, contig, allow_pickle=False)
                meta["arrays"][name] = (fpath, contig.shape, contig.dtype.str)
        yield meta
    finally:
        for shm in blocks:
            try:
                shm.close()
            except Exception:
                pass
            try:
                shm.unlink()
            except Exception:
                pass
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


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
    persist_predictions: bool = True
    calibration_mode: str = "none"


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
    season_keys = _history_season_keys(seasons, include_previous=include_previous_season)
    target_season_keys = {str(int(s)) for s in seasons}
    with connect_managed(live_db_path, read_only=True) as conn:
        all_matches = MatchRepository.fetch_matches(
            conn,
            competition=competition.upper(),
            seasons=season_keys,
        )
        from dataclasses import replace
        from db.xg_store import attach_xg_to_matches

        attached = attach_xg_to_matches(
            conn,
            [
                {
                    "competition": m.competition,
                    "home_id": m.home_id,
                    "away_id": m.away_id,
                    "kickoff": m.kickoff,
                }
                for m in all_matches
            ],
        )
        all_matches = [
            replace(m, home_xg=att.get("home_xg"), away_xg=att.get("away_xg"))
            if att.get("home_xg") is not None and att.get("away_xg") is not None
            else m
            for m, att in zip(all_matches, attached)
        ]

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
    stakes_blend = float(input_data.config.get("stakes_blend", 0.0))
    stakes_min_games = int(input_data.config.get("stakes_min_games", 15))
    elo_weight = float(input_data.config.get("elo_weight", 0.0))
    elo_k = float(input_data.config.get("elo_k", 20.0))
    elo_home_adv = float(input_data.config.get("elo_home_adv", 55.0))
    xg_weight = float(input_data.config.get("xg_weight", 0.0))
    calib_temperature = float(input_data.config.get("calib_temperature", 1.0))
    calib_draw_bias = float(input_data.config.get("calib_draw_bias", 0.0))
    _cfg_cal = str(input_data.config.get("calibration_mode") or "").strip()
    calibration_mode = _cfg_cal if _cfg_cal else str(input_data.calibration_mode or "none").strip()

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
    acc = StreamingMetricsAccumulator()
    batch_snapshots: list[tuple[str, dict[str, Any]]] = []
    batch_predictions: list[dict[str, Any]] = []
    batch_evaluations: list[tuple[str, int, dict[str, Any]]] = []

    def _flush_batches() -> None:
        if not input_data.persist_predictions:
            return
        if not batch_snapshots and not batch_predictions and not batch_evaluations:
            return
        with transaction(lab_conn):
            ExperimentRepository.bulk_save_snapshots(lab_conn, batch_snapshots)
            ExperimentRepository.bulk_insert_predictions(lab_conn, batch_predictions)
            ExperimentRepository.bulk_insert_evaluations(lab_conn, batch_evaluations)
        batch_snapshots.clear()
        batch_predictions.clear()
        batch_evaluations.clear()

    try:
        from laboratory.calibration import compute_reliability_bins, fit_temperature_platt_1x2
        from laboratory.evaluate import evaluate_prediction

        ordered_targets = sorted(
            cache.values(),
            key=lambda c: (int(c.target.season), c.target.kickoff, c.target.match_id),
        )
        prior_season_raw: list[tuple[int, tuple[float, float, float], int]] = []
        fitted_by_season: dict[int, tuple[float, float]] = {}

        ph_list: list[float] = []
        yh_list: list[int] = []
        pd_list: list[float] = []
        yd_list: list[int] = []
        pa_list: list[float] = []
        ya_list: list[int] = []
        pou_list: list[float] = []
        you_list: list[int] = []

        for cached in ordered_targets:
            target = cached.target
            priors = cached.priors

            payload = build_snapshot(target, priors)
            if not payload["leakage_ok"]:
                continue

            cur_season = int(target.season)
            eff_draw_boost = draw_boost
            eff_temp = calib_temperature
            eff_bias = calib_draw_bias
            if calibration_mode == "platt_fit_prior_season":
                eff_draw_boost = 1.0
                if cur_season not in fitted_by_season:
                    train_rows = [
                        (probs, y) for s, probs, y in prior_season_raw if s < cur_season
                    ]
                    if len(train_rows) >= 20:
                        fit = fit_temperature_platt_1x2(
                            [p for p, _ in train_rows],
                            [y for _, y in train_rows],
                        )
                        fitted_by_season[cur_season] = (fit["temperature"], fit["draw_bias"])
                    else:
                        fitted_by_season[cur_season] = (1.0, 0.0)
                eff_temp, eff_bias = fitted_by_season[cur_season]

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
                draw_boost=eff_draw_boost,
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
                stakes_blend=stakes_blend,
                stakes_min_games=stakes_min_games,
                elo_weight=elo_weight,
                elo_k=elo_k,
                elo_home_adv=elo_home_adv,
                xg_weight=xg_weight,
                calib_temperature=eff_temp,
                calib_draw_bias=eff_bias,
            )

            if outcome.excluded or outcome.body is None:
                continue

            priced += 1
            if input_data.persist_predictions:
                snapshot_id = f"{input_data.experiment_id}:{payload['payload_hash']}"
                prediction_id = f"{input_data.experiment_id}:{snapshot_id}:{SCORE_MODEL_ENGINE}:{input_data.model_id}"
                batch_snapshots.append((input_data.experiment_id, payload))
                batch_predictions.append(
                    {
                        "experiment_id": input_data.experiment_id,
                        "match_id": target.match_id,
                        "snapshot_id": snapshot_id,
                        "engine": SCORE_MODEL_ENGINE,
                        "model_id": input_data.model_id,
                        "body": outcome.body,
                    }
                )

            if target.ft_home is not None and target.ft_away is not None:
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
                acc.add_evaluation(metrics)

                ft_block = outcome.body.get("full_time") or {}
                r1x2 = ft_block.get("result_1x2") or {}
                ou25_blk = ft_block.get("over_under_2_5") or {}
                p_h = float(r1x2.get("home", 0.0))
                p_d = float(r1x2.get("draw", 0.0))
                p_a = float(r1x2.get("away", 0.0))
                p_ou = ou25_blk.get("over")
                hg_val = int(target.ft_home)
                ag_val = int(target.ft_away)
                out_idx = 0 if hg_val > ag_val else (1 if hg_val == ag_val else 2)
                ph_list.append(p_h)
                yh_list.append(1 if out_idx == 0 else 0)
                pd_list.append(p_d)
                yd_list.append(1 if out_idx == 1 else 0)
                pa_list.append(p_a)
                ya_list.append(1 if out_idx == 2 else 0)
                if p_ou is not None:
                    pou_list.append(float(p_ou))
                    you_list.append(1 if (hg_val + ag_val) > 2 else 0)

                if calibration_mode == "platt_fit_prior_season":
                    if eff_temp == 1.0 and eff_bias == 0.0:
                        raw_t = (p_h, p_d, p_a)
                    else:
                        import math as _math
                        _eps = 1e-12
                        _z = [
                            eff_temp * _math.log(max(p_h, _eps)),
                            eff_temp * (_math.log(max(p_d, _eps)) - eff_bias),
                            eff_temp * _math.log(max(p_a, _eps)),
                        ]
                        _mz = max(_z)
                        _ez = [_math.exp(v - _mz) for v in _z]
                        _sz = sum(_ez)
                        raw_t = (_ez[0] / _sz, _ez[1] / _sz, _ez[2] / _sz)
                    prior_season_raw.append((cur_season, raw_t, out_idx))

                if input_data.persist_predictions:
                    batch_evaluations.append((prediction_id, 1, metrics))

            if input_data.persist_predictions and len(batch_predictions) >= 50:
                _flush_batches()

        _flush_batches()

        rel_diagrams = {}
        ece_summary = {}
        if ph_list:
            rel_home = compute_reliability_bins(ph_list, yh_list)
            rel_draw = compute_reliability_bins(pd_list, yd_list)
            rel_away = compute_reliability_bins(pa_list, ya_list)
            rel_diagrams = {
                "ft_home": rel_home,
                "ft_draw": rel_draw,
                "ft_away": rel_away,
            }
            ece_summary = {
                "ft_home_ece": rel_home["ece"],
                "ft_draw_ece": rel_draw["ece"],
                "ft_away_ece": rel_away["ece"],
                "ou25_ece": None,
            }
            if pou_list:
                rel_ou = compute_reliability_bins(pou_list, you_list)
                rel_diagrams["over_under_2_5"] = rel_ou
                ece_summary["ou25_ece"] = rel_ou["ece"]

        if not input_data.persist_predictions:
            final_metrics = acc.finalize()
            if rel_diagrams:
                final_metrics["ece"] = ece_summary
                final_metrics["reliability_diagrams"] = rel_diagrams
        else:
            final_metrics = (
                {"ece": ece_summary, "reliability_diagrams": rel_diagrams}
                if rel_diagrams
                else None
            )

        return _WorkerOutput(
            model_id=input_data.model_id,
            status="completed",
            metrics=final_metrics,
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
    with connect_managed(lab_db_path) as conn:
        metrics_rows = ExperimentRepository.fetch_evaluation_metrics_json(
            conn, experiment_id=experiment_id, model_id=model_id
        )

    if not metrics_rows:
        return {}

    acc = StreamingMetricsAccumulator()
    for raw_json in metrics_rows:
        acc.add_evaluation(json.loads(raw_json))
    return acc.finalize()


def run_experiment(
    lab_db_path: Path = DEFAULT_DB_PATH,
    live_db_path: Path = LIVE_DB_PATH,
    config: ExperimentConfig | None = None,
    *,
    max_workers: int = 4,
    resume: bool = True,
    persist_predictions: bool = True,
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
    for combo in expand_param_grid(config.param_grid):
        merged = merge_configs(config.base_config, combo)
        model_id = make_model_id(config.experiment_id, merged)
        model_configs.append((merged, model_id))

    logger.info(
        "Experiment %s: %d configs to evaluate (%d workers)",
        config.experiment_id,
        len(model_configs),
        max_workers,
    )

    # Check for existing completed runs
    with connect_managed(lab_db_path) as conn:
        completed_model_ids = ExperimentRepository.fetch_completed_model_ids(
            conn, experiment_id=config.experiment_id
        )
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
            persist_predictions=persist_predictions,
            calibration_mode=config.calibration_mode,
        )
        for cfg, mid in pending
    ]

    # Execute in parallel
    completed_runs = 0
    failed_runs = 0

    with concurrent.futures.ProcessPoolExecutor(
        max_workers=max_workers,
        initializer=_init_worker,
    ) as executor:
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
            extra_calib = output.metrics if (persist_predictions and isinstance(output.metrics, dict)) else None
            metrics = output.metrics if not persist_predictions else None
            if metrics is None and output.status == "completed" and output.matches_evaluated > 0:
                metrics = _aggregate_metrics(lab_db_path, config.experiment_id, model_id)
                if extra_calib:
                    if "ece" in extra_calib:
                        metrics["ece"] = extra_calib["ece"]
                    if "reliability_diagrams" in extra_calib:
                        metrics["reliability_diagrams"] = extra_calib["reliability_diagrams"]

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
    with connect_managed(lab_db_path) as conn:
        runs = ExperimentRepository.fetch_completed_runs(
            conn, experiment_id=config.experiment_id
        )

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


_expand_param_grid = expand_param_grid
_merge_configs = merge_configs


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
