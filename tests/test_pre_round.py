from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from conftest import ARSENAL, CHELSEA, Harness, make_match
from data_fetcher import fixture_in_prospective_window, parse_fixture
from pre_round import (
    cairo_days_covering,
    run_pre_round_once,
    select_prospective_fixtures,
)
from storage import Storage


def test_selects_only_fixtures_inside_the_lead_window() -> None:
    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    inside = parse_fixture(
        make_match(
            10,
            ARSENAL,
            CHELSEA,
            status="TIMED",
            home_goals=None,
            away_goals=None,
            half_home=None,
            half_away=None,
            when=(now + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    outside = parse_fixture(
        make_match(
            11,
            ARSENAL,
            CHELSEA,
            status="TIMED",
            home_goals=None,
            away_goals=None,
            half_home=None,
            half_away=None,
            when=(now + timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    started = parse_fixture(
        make_match(
            12,
            ARSENAL,
            CHELSEA,
            status="IN_PLAY",
            home_goals=None,
            away_goals=None,
            half_home=None,
            half_away=None,
            when=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    selected = select_prospective_fixtures(
        [outside, inside, started], now=now, lead_days=1
    )
    assert [fixture.id for fixture in selected] == [10]
    assert fixture_in_prospective_window(inside, now=now, lead_days=1)
    assert not fixture_in_prospective_window(outside, now=now, lead_days=1)


def test_cairo_days_covering_spans_the_window() -> None:
    start = datetime(2026, 11, 10, 22, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    days = cairo_days_covering(start, end)
    assert len(days) >= 2


def test_pre_round_reprices_even_when_a_prediction_exists(tmp_path: Path) -> None:
    async def scenario() -> None:
        holder = Harness(tmp_path)
        kickoff = (datetime.now(timezone.utc) + timedelta(hours=12)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        holder.api.fixtures = [
            make_match(
                50,
                ARSENAL,
                CHELSEA,
                status="SCHEDULED",
                home_goals=None,
                away_goals=None,
                half_home=None,
                half_away=None,
                when=kickoff,
            )
        ]
        await holder.execute()
        assert holder.pipeline is not None
        pipeline = holder.pipeline
        pipeline.store = Storage(tmp_path / "pre-round.db")

        async def fake_day(_day):
            return {"matches": holder.api.fixtures}

        pipeline.fetcher.get_matches_for_cairo_day = fake_day  # type: ignore[method-assign]
        pipeline.execute_by_match_id = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(status="completed", stage=None, reason=None)
        )
        # Pretend a prior prediction already exists; the job must still price.
        pipeline.store.has_prediction_for_match = lambda _match_id: True  # type: ignore[method-assign]

        first = await run_pre_round_once(pipeline, lead_days=1)
        second = await run_pre_round_once(pipeline, lead_days=1)
        assert first["candidates"] == 1
        assert first["priced"] == 1
        assert second["priced"] == 1
        assert pipeline.execute_by_match_id.await_count == 2
        assert pipeline.execute_by_match_id.await_args.kwargs["source"] == "pre-round"

    asyncio.run(scenario())
