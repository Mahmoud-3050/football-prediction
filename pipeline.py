"""One request, one match.

The handler awaits this path and does not start a second fixture. No call here
runs unless a user named the home team, the away team, and the competition.
"""

from __future__ import annotations

import logging
from typing import Any

from data_fetcher import (
    TODAY_MATCH_COMPETITIONS,
    UPCOMING_STATUSES,
    DataFetcher,
    classify_fixtures,
    explain_fixture,
    extract_last_updated,
    fixture_is_predictable,
    meetings_in_history,
    normalize_label,
    parse_fixture,
    resolve_competition,
    resolve_team,
    team_from_fixture_side,
    teams_from_payload,
    unknown_competition_reason,
)
from eligibility import FREE_TIER_DELAY_WARNING, evaluate
from models import (
    AnalysisSnapshot,
    CheckResult,
    Competition,
    Fixture,
    FixtureResolution,
    ModelError,
    PipelineResult,
    ProviderError,
    Team,
)
from predictor import Predictor
from score_model import estimate_score
from storage import Storage, utc_now

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, fetcher: DataFetcher, predictor: Predictor, store: Storage) -> None:
        self.fetcher = fetcher
        self.predictor = predictor
        self.store = store

    async def execute(
        self,
        *,
        chat_id: int | None,
        user_id: int | None,
        home: str,
        away: str,
        competition: str,
    ) -> PipelineResult:
        raw_home = home.strip()
        raw_away = away.strip()
        raw_competition = competition.strip()
        run_id = self.store.create_run(
            chat_id=chat_id,
            user_id=user_id,
            raw_home=raw_home,
            raw_away=raw_away,
            raw_competition=raw_competition,
        )
        if not raw_home or not raw_away or not raw_competition:
            return self._finish(
                run_id,
                status="failed",
                stage="input",
                reason="Home, away, and competition are all required.",
            )
        if normalize_label(raw_home) == normalize_label(raw_away):
            return self._finish(
                run_id,
                status="failed",
                stage="input",
                reason="Home and away are the same club.",
            )

        competition_obj = resolve_competition(raw_competition)
        if competition_obj is None:
            return self._finish(
                run_id,
                status="abstained",
                stage="competition",
                reason=unknown_competition_reason(raw_competition),
            )

        try:
            async with self.fetcher.request_scope():
                return await self._analyze(
                    run_id,
                    raw_home=raw_home,
                    raw_away=raw_away,
                    raw_competition=raw_competition,
                    competition=competition_obj,
                )
        except ProviderError as exc:
            logger.info("provider stop for run %s: %s", run_id, exc.reason)
            return self._finish(run_id, status="failed", stage="provider", reason=exc.reason)

    async def execute_by_match_id(
        self,
        *,
        chat_id: int | None,
        user_id: int | None,
        match_id: int,
        source: str = "match_id",
    ) -> PipelineResult:
        """Analyze one listed fixture by its Football-Data.org match id."""

        run_id = self.store.create_run(
            chat_id=chat_id,
            user_id=user_id,
            raw_home=f"#{match_id}",
            raw_away="",
            raw_competition=source,
        )
        if match_id <= 0:
            return self._finish(
                run_id,
                status="failed",
                stage="input",
                reason="Match id must be a positive Football-Data.org id.",
            )
        try:
            async with self.fetcher.request_scope():
                return await self._analyze_match_id(run_id, match_id=match_id)
        except ProviderError as exc:
            logger.info("provider stop for run %s: %s", run_id, exc.reason)
            return self._finish(run_id, status="failed", stage="provider", reason=exc.reason)

    async def _analyze_match_id(self, run_id: int, *, match_id: int) -> PipelineResult:
        payload = await self.fetcher.get_match(match_id)
        try:
            fixture = parse_fixture(payload)
        except (KeyError, TypeError, ValueError):
            return self._finish(
                run_id,
                status="abstained",
                stage="fixture",
                reason=f"Match #{match_id} could not be read from the provider.",
            )

        if fixture.competition_code not in TODAY_MATCH_COMPETITIONS:
            supported = ", ".join(TODAY_MATCH_COMPETITIONS)
            return self._finish(
                run_id,
                status="abstained",
                stage="competition",
                reason=(
                    f"Match #{match_id} is in {fixture.competition_name or fixture.competition_code}. "
                    f"/predict <id> only covers today's list competitions ({supported})."
                ),
            )

        competition = Competition(
            code=fixture.competition_code,
            name=fixture.competition_name or fixture.competition_code,
        )
        home = team_from_fixture_side(fixture.home_team_id, fixture.home_team_name)
        away = team_from_fixture_side(fixture.away_team_id, fixture.away_team_name)
        return await self._after_known_fixture(
            run_id,
            raw_home=home.name,
            raw_away=away.name,
            raw_competition=competition.name,
            competition=competition,
            home=home,
            away=away,
            fixture=fixture,
            fixture_payload_stamp=extract_last_updated(payload),
        )

    async def _analyze(
        self,
        run_id: int,
        *,
        raw_home: str,
        raw_away: str,
        raw_competition: str,
        competition: Any,
    ) -> PipelineResult:
        teams_payload = await self.fetcher.get_teams(competition.code)
        teams = teams_from_payload(teams_payload)
        home_team = resolve_team(raw_home, teams, competition.name)
        if home_team.team is None:
            return self._finish(run_id, status="abstained", stage="teams", reason=home_team.reason)
        away_team = resolve_team(raw_away, teams, competition.name)
        if away_team.team is None:
            return self._finish(run_id, status="abstained", stage="teams", reason=away_team.reason)
        home = home_team.team
        away = away_team.team
        if home.id == away.id:
            return self._finish(
                run_id,
                status="abstained",
                stage="teams",
                reason=f"Home and away both resolved to {home.name}.",
            )

        try:
            return await self._after_teams(
                run_id,
                raw_home=raw_home,
                raw_away=raw_away,
                raw_competition=raw_competition,
                competition=competition,
                home=home,
                away=away,
            )
        except ProviderError as exc:
            self.store.save_snapshot(
                run_id,
                _snapshot(
                    raw_home=raw_home,
                    raw_away=raw_away,
                    raw_competition=raw_competition,
                    competition_code=competition.code,
                    competition_name=competition.name,
                    home=home,
                    away=away,
                    fixture=None,
                    standings_rows={},
                    last_five={"home": [], "away": []},
                    h2h=[],
                    derived={},
                    checks=[
                        CheckResult(
                            "availability",
                            "fail",
                            "The provider stopped this request after the clubs were identified.",
                        )
                    ],
                    freshness={},
                    limitations=[],
                    warnings=[FREE_TIER_DELAY_WARNING],
                    include_half_time=False,
                ),
            )
            return self._finish(
                run_id,
                status="failed",
                stage="provider",
                reason=exc.reason,
                snapshot_saved=True,
                competition_name=competition.name,
                home_name=home.name,
                away_name=away.name,
                warnings=[FREE_TIER_DELAY_WARNING],
            )

    async def _after_teams(
        self,
        run_id: int,
        *,
        raw_home: str,
        raw_away: str,
        raw_competition: str,
        competition: Any,
        home: Team,
        away: Team,
    ) -> PipelineResult:
        fixture_payload = await self.fetcher.get_team_matches(home.id, competition.code)
        resolution = classify_fixtures(
            fixture_payload.get("matches") or [],
            competition_code=competition.code,
            home_id=home.id,
            away_id=away.id,
        )
        if resolution.kind != "upcoming" or resolution.fixture is None:
            reason = explain_fixture(
                resolution,
                competition_name=competition.name,
                requested_home=home.name,
                requested_away=away.name,
            )
            checks = _fixture_checks(resolution.kind)
            snapshot = _snapshot(
                raw_home=raw_home,
                raw_away=raw_away,
                raw_competition=raw_competition,
                competition_code=competition.code,
                competition_name=competition.name,
                home=home,
                away=away,
                fixture=resolution.fixture,
                standings_rows={},
                last_five={"home": [], "away": []},
                h2h=[],
                derived={},
                checks=checks,
                freshness={"fixtures": extract_last_updated(fixture_payload)},
                limitations=[],
                warnings=[FREE_TIER_DELAY_WARNING],
                include_half_time=False,
            )
            self.store.save_snapshot(run_id, snapshot)
            return self._finish(
                run_id,
                status="abstained",
                stage="fixture",
                reason=reason,
                snapshot_saved=True,
                competition_name=competition.name,
                home_name=home.name,
                away_name=away.name,
                kickoff=resolution.fixture.utc_date if resolution.fixture else None,
                warnings=snapshot.warnings,
            )

        return await self._after_known_fixture(
            run_id,
            raw_home=raw_home,
            raw_away=raw_away,
            raw_competition=raw_competition,
            competition=competition,
            home=home,
            away=away,
            fixture=resolution.fixture,
            fixture_payload_stamp=extract_last_updated(fixture_payload),
        )

    async def _after_known_fixture(
        self,
        run_id: int,
        *,
        raw_home: str,
        raw_away: str,
        raw_competition: str,
        competition: Competition,
        home: Team,
        away: Team,
        fixture: Fixture,
        fixture_payload_stamp: str | None,
    ) -> PipelineResult:
        if not fixture_is_predictable(fixture):
            if fixture.status == "FINISHED":
                kind = "finished"
            elif fixture.status in UPCOMING_STATUSES:
                kind = "started"
            else:
                kind = "unavailable"
            reason = explain_fixture(
                FixtureResolution(kind, fixture),
                competition_name=competition.name,
                requested_home=home.name,
                requested_away=away.name,
            )
            snapshot = _snapshot(
                raw_home=raw_home,
                raw_away=raw_away,
                raw_competition=raw_competition,
                competition_code=competition.code,
                competition_name=competition.name,
                home=home,
                away=away,
                fixture=fixture,
                standings_rows={},
                last_five={"home": [], "away": []},
                h2h=[],
                derived={},
                checks=_fixture_checks(kind),
                freshness={"fixtures": fixture_payload_stamp or fixture.last_updated},
                limitations=[],
                warnings=[FREE_TIER_DELAY_WARNING],
                include_half_time=False,
            )
            self.store.save_snapshot(run_id, snapshot)
            return self._finish(
                run_id,
                status="abstained",
                stage="fixture",
                reason=reason,
                snapshot_saved=True,
                competition_name=competition.name,
                home_name=home.name,
                away_name=away.name,
                kickoff=fixture.utc_date,
                warnings=snapshot.warnings,
            )

        standings_payload = await self.fetcher.get_standings(competition.code)
        competition_finished = await self.fetcher.get_competition_finished_matches(
            competition.code
        )
        home_form = await self.fetcher.get_finished_matches(home.id, competition.code)
        away_form = await self.fetcher.get_finished_matches(away.id, competition.code)
        h2h = meetings_in_history([home_form, away_form], home.id, away.id)
        h2h_payload: dict[str, Any] | None = {"matches": h2h} if h2h else None
        if not h2h:
            h2h_payload = await self.fetcher.get_head_to_head(fixture.id)
            h2h = meetings_in_history([h2h_payload], home.id, away.id)

        decision = evaluate(
            competition=competition,
            home=home,
            away=away,
            fixture=fixture,
            standings_payload=standings_payload,
            home_finished_payload=home_form,
            away_finished_payload=away_form,
            h2h_matches=h2h,
            h2h_payload=h2h_payload if h2h else None,
        )
        include_half_time = decision.include_half_time
        derived = {}
        if decision.home_facts is not None and decision.away_facts is not None:
            derived = {
                "home": decision.home_facts.public(include_half_time=include_half_time),
                "away": decision.away_facts.public(include_half_time=include_half_time),
            }
        standings_rows = {}
        if decision.home_row is not None:
            standings_rows["home"] = decision.home_row.public()
        if decision.away_row is not None:
            standings_rows["away"] = decision.away_row.public()
        limitations = list(decision.limitations)
        estimate = None
        if decision.eligible:
            estimate = estimate_score(
                competition_finished_payload=competition_finished,
                home_finished_payload=home_form,
                away_finished_payload=away_form,
                home_team_id=home.id,
                away_team_id=away.id,
                include_half_time=include_half_time,
                kickoff=fixture.utc_date,
                prior_mean_mode="last_season",
                half_time_share_mode="fixed_0_45",
            )
            derived["score_model"] = estimate.public()
            for note in estimate.limitations:
                if note not in limitations:
                    limitations.append(note)
        snapshot = _snapshot(
            raw_home=raw_home,
            raw_away=raw_away,
            raw_competition=raw_competition,
            competition_code=competition.code,
            competition_name=competition.name,
            home=home,
            away=away,
            fixture=fixture,
            standings_rows=standings_rows,
            last_five={"home": decision.home_window, "away": decision.away_window},
            h2h=h2h,
            derived=derived,
            checks=decision.checks,
            freshness=decision.freshness,
            limitations=limitations,
            warnings=decision.warnings,
            include_half_time=include_half_time,
        )
        snapshot_id = self.store.save_snapshot(run_id, snapshot)
        if not decision.eligible or estimate is None:
            return self._finish(
                run_id,
                status="abstained",
                stage=decision.blocking_stage,
                reason=decision.blocking_reason,
                snapshot_saved=True,
                competition_name=competition.name,
                home_name=home.name,
                away_name=away.name,
                kickoff=fixture.utc_date,
                warnings=decision.warnings,
            )

        try:
            produced = await self.predictor.predict(
                snapshot,
                estimate,
                limitations=limitations,
            )
        except ModelError as exc:
            return self._finish(
                run_id,
                status="failed",
                stage="model",
                reason=exc.reason,
                snapshot_saved=True,
                competition_name=competition.name,
                home_name=home.name,
                away_name=away.name,
                kickoff=fixture.utc_date,
                limitations=limitations,
                warnings=decision.warnings,
            )

        self.store.save_version(run_id, snapshot_id, produced)
        status = "partial" if produced.limitations else "completed"
        return self._finish(
            run_id,
            status=status,
            stage=None,
            reason=None,
            snapshot_saved=True,
            competition_name=competition.name,
            home_name=home.name,
            away_name=away.name,
            kickoff=fixture.utc_date,
            half_time_prediction=produced.prediction.half_time_prediction,
            half_time_score=produced.prediction.half_time_score,
            half_time_probabilities=produced.half_time_probabilities,
            full_time_score=produced.prediction.full_time_score,
            markets=produced.markets,
            confidence=produced.prediction.confidence_percentage,
            reasoning=produced.prediction.tactical_reasoning,
            reasoning_ar=produced.prediction.tactical_reasoning_ar,
            model_id=produced.model_id,
            limitations=produced.limitations,
            warnings=decision.warnings,
            include_half_time=include_half_time,
            probabilities=produced.probabilities,
            top_scores=produced.top_scores,
        )

    def _finish(self, run_id: int, **fields: Any) -> PipelineResult:
        self.store.update_run(
            run_id,
            status=fields["status"],
            stage=fields.get("stage"),
            reason=fields.get("reason"),
        )
        return PipelineResult(run_id=run_id, **fields)


def _snapshot(
    *,
    raw_home: str,
    raw_away: str,
    raw_competition: str,
    competition_code: str,
    competition_name: str,
    home: Team,
    away: Team,
    fixture: Fixture | None,
    standings_rows: dict[str, Any],
    last_five: dict[str, list[dict[str, Any]]],
    h2h: list[dict[str, Any]],
    derived: dict[str, Any],
    checks: list[CheckResult],
    freshness: dict[str, str | None],
    limitations: list[str],
    warnings: list[str],
    include_half_time: bool,
) -> AnalysisSnapshot:
    return AnalysisSnapshot(
        request_home=raw_home,
        request_away=raw_away,
        request_competition=raw_competition,
        competition_code=competition_code,
        competition_name=competition_name,
        home_team_id=home.id,
        away_team_id=away.id,
        home_team_name=home.name,
        away_team_name=away.name,
        match_id=fixture.id if fixture else None,
        kickoff=fixture.utc_date if fixture else None,
        fixture_status=fixture.status if fixture else None,
        standings_rows=standings_rows,
        last_five=last_five,
        h2h=h2h,
        derived=derived,
        checks=checks,
        freshness=freshness,
        fetched_at=utc_now(),
        limitations=limitations,
        warnings=warnings,
        include_half_time=include_half_time,
    )


def _fixture_checks(kind: str) -> list[CheckResult]:
    if kind == "opposite":
        return [
            CheckResult(
                "consistency",
                "fail",
                "The only meeting is the opposite venue. Home and away were not swapped.",
            )
        ]
    if kind == "finished":
        return [
            CheckResult(
                "consistency",
                "pass",
                "The fixture matches the request and is already finished.",
            )
        ]
    if kind == "unavailable":
        return [
            CheckResult(
                "consistency",
                "pass",
                "The fixture matches the request but is not scheduled or timed.",
            )
        ]
    return [
        CheckResult(
            "consistency",
            "fail",
            "No fixture with the requested home team, away team, and competition was found.",
        )
    ]
