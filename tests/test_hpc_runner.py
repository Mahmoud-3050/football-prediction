"""Tests for SharedMemory dataset sharing and streaming metric accumulation."""

from __future__ import annotations

from multiprocessing import shared_memory

import numpy as np

from domain.features import encode_matches
from laboratory.lab_runner import shared_encoded_dataset
from laboratory.metrics import StreamingMetricsAccumulator
from laboratory.snapshot import MatchRecord


def test_shared_encoded_dataset_roundtrip_and_cleanup() -> None:
    import os

    records = [
        MatchRecord("m1", "2025", "2025-08-10T15:00:00Z", "1", "A", "2", "B", 2, 1, 1, 0),
        MatchRecord("m2", "2025", "2025-08-12T15:00:00Z", "2", "B", "1", "A", 0, 3, 0, 1),
    ]
    ds = encode_matches(records)
    shm_names: list[str] = []
    tmp_dir: str | None = None
    with shared_encoded_dataset(ds) as meta:
        backend = meta["backend"]
        if backend == "shm":
            for name, (shm_name, shape, dtype_str) in meta["arrays"].items():
                shm_names.append(shm_name)
                attached = shared_memory.SharedMemory(name=shm_name)
                try:
                    arr = np.ndarray(shape, dtype=np.dtype(dtype_str), buffer=attached.buf)
                    assert np.array_equal(arr, getattr(ds, name))
                finally:
                    attached.close()
        else:
            tmp_dir = meta["dir"]
            for name, (fpath, _shape, _dtype_str) in meta["arrays"].items():
                arr = np.load(fpath, mmap_mode="r")
                assert np.array_equal(arr, getattr(ds, name))

    for shm_name in shm_names:
        try:
            s = shared_memory.SharedMemory(name=shm_name)
            s.close()
            s.unlink()
            raise AssertionError(f"SharedMemory {shm_name} was not unlinked!")
        except FileNotFoundError:
            pass
    if tmp_dir is not None:
        assert not os.path.exists(tmp_dir)


def test_streaming_metrics_accumulator() -> None:
    acc = StreamingMetricsAccumulator()
    acc.add_evaluation(
        {
            "full_time_1x2": {"status": "scored", "log_loss": 0.8, "brier": 0.4, "correct": True},
            "exact_score": {"status": "scored", "log_loss": 2.0, "correct": False},
        }
    )
    acc.add_evaluation(
        {
            "full_time_1x2": {"status": "scored", "log_loss": 1.2, "brier": 0.6, "correct": False},
            "exact_score": {"status": "scored", "log_loss": 2.4, "correct": True},
        }
    )
    res = acc.finalize()
    assert res["n_matches"] == 2
    assert abs(res["full_time_1x2_log_loss"] - 1.0) < 1e-12
    assert abs(res["full_time_1x2_brier"] - 0.5) < 1e-12
    assert abs(res["full_time_1x2_accuracy"] - 0.5) < 1e-12
    assert abs(res["exact_score_hit_rate"] - 0.5) < 1e-12
