"""Phase 2 Golden Master, Mathematical Invariants & Zero-Leakage Tests."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from laboratory.evaluate import evaluate_prediction
from laboratory.score_model_adapter import price_from_snapshot
from laboratory.snapshot import MatchRecord, build_snapshot, leakage_status

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden_master_baseline.json"


def _build_synthetic_league() -> list[MatchRecord]:
    teams = [(str(i), f"Team {i}") for i in range(1, 9)]
    base_dt = datetime(2024, 8, 10, 15, 0, tzinfo=timezone.utc)
    matches: list[MatchRecord] = []
    mid = 1
    for season_idx, season in enumerate(("2024", "2025")):
        season_start = base_dt + timedelta(days=365 * season_idx)
        day_offset = 0
        for round_no in range(2):
            for i in range(len(teams)):
                for j in range(len(teams)):
                    if i == j:
                        continue
                    ko = (season_start + timedelta(days=day_offset)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    h_goals = (i + round_no + season_idx) % 4
                    a_goals = (j + round_no) % 3
                    ht_h = min(h_goals, (i + j) % 2)
                    ht_a = min(a_goals, (j + 1) % 2)
                    matches.append(
                        MatchRecord(
                            match_id=f"m-{mid}",
                            season=season,
                            kickoff=ko,
                            home_id=teams[i][0],
                            home_name=teams[i][1],
                            away_id=teams[j][0],
                            away_name=teams[j][1],
                            ft_home=h_goals,
                            ft_away=a_goals,
                            ht_home=ht_h,
                            ht_away=ht_a,
                            competition="PL",
                        )
                    )
                    mid += 1
                    day_offset += 2
    return matches


def test_golden_master_equivalence() -> None:
    """Ensure refactored pricing & evaluation match the pre-refactoring Golden Master."""
    data = json.loads(FIXTURE_PATH.read_text())
    matches = _build_synthetic_league()
    by_id = {m.match_id: m for m in matches}

    for case in data["cases"]:
        tgt = by_id[case["target_match_id"]]
        priors = [m for m in matches if m.season == tgt.season and m.kickoff < tgt.kickoff]
        payload = build_snapshot(tgt, priors)
        out = price_from_snapshot(payload, **case["params"])
        assert not out.excluded and out.body is not None, case["config_name"]

        ev = evaluate_prediction(
            out.body,
            {
                "ft_home": tgt.ft_home,
                "ft_away": tgt.ft_away,
                "ht_home": tgt.ht_home,
                "ht_away": tgt.ht_away,
            },
        )
        ft = out.body["full_time"]
        assert abs(out.body["lambda_home"] - case["lambda_home"]) < 1e-12
        assert abs(out.body["lambda_away"] - case["lambda_away"]) < 1e-12
        if case["rho"] is None:
            assert out.body["bot_public"].get("rho") is None
        else:
            assert abs(out.body["bot_public"]["rho"] - case["rho"]) < 1e-12
        for k in ("home", "draw", "away"):
            assert abs(ft["result_1x2"][k] - case["result_1x2"][k]) < 1e-12
        assert ft["result_1x2"]["most_likely"] == case["result_1x2"]["most_likely"]
        assert ft["exact_score_most_likely"] == case["exact_score_most_likely"]
        assert abs(ev["full_time_1x2"]["log_loss"] - case["ft_1x2_log_loss"]) < 1e-12
        assert abs(ev["full_time_1x2"]["brier"] - case["ft_1x2_brier"]) < 1e-12
        assert abs(ev["exact_score"]["log_loss"] - case["exact_log_loss"]) < 1e-12


def test_mathematical_invariants() -> None:
    """Probability distributions sum to 1.0, contain no NaN/Inf, and abstain on <3 priors."""
    matches = _build_synthetic_league()
    tgt = [m for m in matches if m.season == "2025"][-1]
    priors = [m for m in matches if m.season == tgt.season and m.kickoff < tgt.kickoff]
    payload = build_snapshot(tgt, priors)

    for kwargs in (
        {"dependence": "independent", "ratio_mode": "one_pass"},
        {"dependence": "independent", "ratio_mode": "joint"},
        {"dependence": "dixon_coles", "ratio_mode": "one_pass"},
        {"nb_alpha": 0.15, "draw_boost": 1.2},
    ):
        out = price_from_snapshot(payload, **kwargs)
        assert not out.excluded and out.body is not None
        ft = out.body["full_time"]
        r = ft["result_1x2"]
        s_1x2 = r["home"] + r["draw"] + r["away"]
        assert abs(s_1x2 - 1.0) < 1e-9
        for v in (out.body["lambda_home"], out.body["lambda_away"], r["home"], r["draw"], r["away"]):
            assert math.isfinite(v) and v >= 0.0
        cell_sum = sum(c["probability"] for c in ft["exact_scores"])
        assert abs(cell_sum - 1.0) < 1e-9

    # Abstention when < 3 scored matches for a club
    thin_payload = build_snapshot(tgt, priors[:2])
    thin_out = price_from_snapshot(thin_payload)
    assert thin_out.excluded is True
    assert thin_out.body is None


def test_zero_data_leakage_invariance() -> None:
    """Future matches or mutated future scores must never alter pre-kickoff predictions."""
    matches = _build_synthetic_league()
    s25 = [m for m in matches if m.season == "2025"]
    tgt = s25[len(s25) // 2]

    strict_priors = [m for m in s25 if m.kickoff < tgt.kickoff]
    p1 = build_snapshot(tgt, strict_priors)
    out1 = price_from_snapshot(p1, ratio_mode="joint", dependence="dixon_coles")

    # Pass full season including target & future matches with extreme scores
    mutated = [
        MatchRecord(
            match_id=m.match_id,
            season=m.season,
            kickoff=m.kickoff,
            home_id=m.home_id,
            home_name=m.home_name,
            away_id=m.away_id,
            away_name=m.away_name,
            ft_home=99 if m.kickoff >= tgt.kickoff else m.ft_home,
            ft_away=0 if m.kickoff >= tgt.kickoff else m.ft_away,
            ht_home=m.ht_home,
            ht_away=m.ht_away,
            competition=m.competition,
        )
        for m in s25
    ]
    p2 = build_snapshot(tgt, mutated)
    out2 = price_from_snapshot(p2, ratio_mode="joint", dependence="dixon_coles")

    assert p1["payload_hash"] == p2["payload_hash"]
    assert out1.body == out2.body

    # Verify leakage_status catches injected future prior
    bad = dict(p1)
    bad["prior_matches"] = list(p1["prior_matches"]) + [
        {
            "match_id": "future-1",
            "kickoff": tgt.kickoff,
            "home_id": "1",
            "away_id": "2",
            "ft_home": 5,
            "ft_away": 5,
        }
    ]
    ok, reason = leakage_status(bad, tgt.match_id)
    assert ok is False and reason is not None
