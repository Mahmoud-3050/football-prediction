"""Shared data shapes for one on-demand prediction request.

Nothing in this module talks to Telegram, Football-Data, or Gemini.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class StageError(Exception):
    """A known stop, with the stage name that the user should see."""

    def __init__(self, stage: str, reason: str, status: str = "failed") -> None:
        self.stage = stage
        self.reason = reason
        self.status = status
        super().__init__(reason)


class ProviderError(StageError):
    def __init__(self, reason: str) -> None:
        super().__init__("provider", reason, status="failed")


class ModelError(StageError):
    def __init__(self, reason: str) -> None:
        super().__init__("model", reason, status="failed")


@dataclass(frozen=True)
class Competition:
    code: str
    name: str


@dataclass(frozen=True)
class Team:
    id: int
    name: str
    short_name: str | None
    tla: str | None


@dataclass
class Fixture:
    id: int
    status: str
    utc_date: str | None
    competition_code: str
    competition_name: str
    home_team_id: int
    home_team_name: str
    away_team_id: int
    away_team_name: str
    full_time_home: int | None
    full_time_away: int | None
    half_time_home: int | None
    half_time_away: int | None
    last_updated: str | None

    def score_text(self) -> str | None:
        if self.full_time_home is None or self.full_time_away is None:
            return None
        return f"{self.full_time_home}-{self.full_time_away}"


@dataclass(frozen=True)
class FixtureResolution:
    """How the requested home/away pair lines up with provider fixtures."""

    kind: str
    fixture: Fixture | None


@dataclass(frozen=True)
class TeamResolution:
    team: Team | None
    reason: str | None = None


@dataclass
class StandingRow:
    team_id: int
    team_name: str
    position: int | None
    points: int
    goals_for: int
    goals_against: int
    goal_difference: int
    raw: dict[str, Any]

    def public(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "team_name": self.team_name,
            "position": self.position,
            "points": self.points,
            "goals_for": self.goals_for,
            "goals_against": self.goals_against,
            "goal_difference": self.goal_difference,
            "raw": self.raw,
        }


@dataclass
class CheckResult:
    """One validation outcome that is always kept."""

    name: str
    status: str
    detail: str

    def public(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class TeamFacts:
    team_id: int
    team_name: str
    wins: int
    draws: int
    losses: int
    record: str
    results: list[str]
    same_competition_count: int
    window_size: int
    finished_with_full_time: int
    half_time_samples: int
    half_time_goals_scored: int | None
    half_time_goals_conceded: int | None
    second_half_goals_scored: int | None
    second_half_goals_conceded: int | None

    def public(self, *, include_half_time: bool) -> dict[str, Any]:
        payload = asdict(self)
        if not include_half_time:
            payload["half_time_goals_scored"] = None
            payload["half_time_goals_conceded"] = None
            payload["second_half_goals_scored"] = None
            payload["second_half_goals_conceded"] = None
        return payload


@dataclass
class AnalysisSnapshot:
    """Point-in-time input. Gemini sees this object and nothing fetched later."""

    request_home: str
    request_away: str
    request_competition: str
    competition_code: str | None
    competition_name: str | None
    home_team_id: int | None
    away_team_id: int | None
    home_team_name: str | None
    away_team_name: str | None
    match_id: int | None
    kickoff: str | None
    fixture_status: str | None
    standings_rows: dict[str, Any]
    last_five: dict[str, list[dict[str, Any]]]
    h2h: list[dict[str, Any]]
    derived: dict[str, Any]
    checks: list[CheckResult]
    freshness: dict[str, str | None]
    fetched_at: str
    limitations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    include_half_time: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "request": {
                "home": self.request_home,
                "away": self.request_away,
                "competition": self.request_competition,
            },
            "resolved": {
                "competition_code": self.competition_code,
                "competition_name": self.competition_name,
                "home_team_id": self.home_team_id,
                "home_team_name": self.home_team_name,
                "away_team_id": self.away_team_id,
                "away_team_name": self.away_team_name,
                "match_id": self.match_id,
                "kickoff": self.kickoff,
                "fixture_status": self.fixture_status,
            },
            "standings": self.standings_rows,
            "last_five": self.last_five,
            "head_to_head": self.h2h,
            "derived": self.derived,
            "checks": [check.public() for check in self.checks],
            "freshness": self.freshness,
            "fetched_at": self.fetched_at,
            "limitations": list(self.limitations),
            "warnings": list(self.warnings),
            "include_half_time": self.include_half_time,
        }


@dataclass(frozen=True)
class Markets:
    """Outcomes computed from a validated full-time scoreline."""

    result_1x2: str
    btts: str
    over_under_25: str


@dataclass(frozen=True)
class ScorePrediction:
    half_time_prediction: str | None
    half_time_score: str | None
    full_time_score: str
    confidence_percentage: int
    tactical_reasoning: str


@dataclass
class ProducedPrediction:
    prediction: ScorePrediction
    markets: Markets
    version_number: int
    model_id: str
    prompt_version: str
    schema_version: str
    limitations: list[str]

    def output(self) -> dict[str, Any]:
        return {
            "half_time_prediction": self.prediction.half_time_prediction,
            "half_time_score": self.prediction.half_time_score,
            "full_time_score": self.prediction.full_time_score,
            "confidence_percentage": self.prediction.confidence_percentage,
            "tactical_reasoning": self.prediction.tactical_reasoning,
            "markets": asdict(self.markets),
            "markets_derived_from_score": True,
        }


@dataclass
class PipelineResult:
    run_id: int
    status: str
    stage: str | None
    reason: str | None
    snapshot_saved: bool = False
    competition_name: str | None = None
    home_name: str | None = None
    away_name: str | None = None
    kickoff: str | None = None
    half_time_prediction: str | None = None
    half_time_score: str | None = None
    full_time_score: str | None = None
    markets: Markets | None = None
    confidence: int | None = None
    reasoning: str | None = None
    limitations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    include_half_time: bool = False
