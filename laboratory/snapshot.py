"""Pre-kickoff snapshots. The target result never enters the payload."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from laboratory.poisson_engine import FinishedMatch


@dataclass(frozen=True)
class MatchRecord:
    match_id: str
    season: str
    kickoff: str
    home_id: str
    home_name: str
    away_id: str
    away_name: str
    ft_home: int | None
    ft_away: int | None
    ht_home: int | None
    ht_away: int | None
    competition: str = "PL"
    home_xg: float | None = None
    away_xg: float | None = None


def build_snapshot(target: MatchRecord, history: list[MatchRecord]) -> dict[str, Any]:
    """Build the engine payload for one match.

    `history` may contain the target and later matches. Only earlier matches
    in the same competition and season, with a full-time score, are copied in.
    """

    priors: list[dict[str, Any]] = []
    for match in history:
        if match.match_id == target.match_id:
            continue
        if match.competition != target.competition:
            continue
        if match.season != target.season or match.kickoff >= target.kickoff:
            continue
        if match.ft_home is None or match.ft_away is None:
            continue
        item = {
            "match_id": match.match_id,
            "kickoff": match.kickoff,
            "home_id": match.home_id,
            "home_name": match.home_name,
            "away_id": match.away_id,
            "away_name": match.away_name,
            "ft_home": match.ft_home,
            "ft_away": match.ft_away,
            "ht_home": match.ht_home,
            "ht_away": match.ht_away,
        }
        if match.home_xg is not None and match.away_xg is not None:
            item["home_xg"] = match.home_xg
            item["away_xg"] = match.away_xg
        priors.append(item)
    priors.sort(key=lambda item: (item["kickoff"], item["match_id"]))
    half_time_count = sum(
        1
        for item in priors
        if item["ht_home"] is not None
        and item["ht_away"] is not None
        and item["ht_home"] <= item["ft_home"]
        and item["ht_away"] <= item["ft_away"]
    )
    payload = {
        "competition": target.competition,
        "season": target.season,
        "match_id": target.match_id,
        "kickoff": target.kickoff,
        "cutoff": target.kickoff,
        "stage": "pre-kickoff",
        "home": {"id": target.home_id, "name": target.home_name},
        "away": {"id": target.away_id, "name": target.away_name},
        "prior_matches": priors,
        "coverage": {
            "prior_full_time_matches": len(priors),
            "prior_half_time_matches": half_time_count,
        },
    }
    leakage_ok, reason = leakage_status(payload, target.match_id)
    payload["leakage_ok"] = leakage_ok
    payload["leakage_reason"] = reason
    payload["payload_hash"] = hashlib.sha256(
        json.dumps(_hashable(payload), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def leakage_status(payload: dict[str, Any], target_match_id: str) -> tuple[bool, str | None]:
    if payload.get("cutoff") != payload.get("kickoff"):
        return False, "Cutoff is not the scheduled kickoff."
    if "ft_home" in payload or "ft_away" in payload or "score" in payload:
        return False, "Target score is present on the snapshot."
    forbidden = ("lambda_home", "lambda_away", "poisson", "gemini")
    for key in forbidden:
        if key in payload:
            return False, f"Engine field {key} is present on the snapshot."
    for prior in payload.get("prior_matches") or []:
        if prior["match_id"] == target_match_id:
            return False, "Target match is listed as a prior match."
        if prior["kickoff"] >= payload["cutoff"]:
            return False, "A prior match is not before the cutoff."
        if prior.get("ft_home") is None or prior.get("ft_away") is None:
            return False, "A prior match is missing a full-time score."
    return True, None


def priors_from_snapshot(payload: dict[str, Any]) -> list[FinishedMatch]:
    matches: list[FinishedMatch] = []
    for prior in payload["prior_matches"]:
        matches.append(
            FinishedMatch(
                match_id=prior["match_id"],
                season=payload["season"],
                kickoff=prior["kickoff"],
                home_id=prior["home_id"],
                away_id=prior["away_id"],
                ft_home=int(prior["ft_home"]),
                ft_away=int(prior["ft_away"]),
                ht_home=None if prior["ht_home"] is None else int(prior["ht_home"]),
                ht_away=None if prior["ht_away"] is None else int(prior["ht_away"]),
            )
        )
    return matches


def _hashable(payload: dict[str, Any]) -> dict[str, Any]:
    copied = dict(payload)
    copied.pop("payload_hash", None)
    return copied
