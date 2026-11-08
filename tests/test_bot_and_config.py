import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import build_application, predict, render_report
from config import Config, ConfigError, load_config
from conftest import Harness
from data_fetcher import DataFetcher, RateLimiter
from models import Markets, PipelineResult


def test_missing_keys_fail_without_a_provider_call() -> None:
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        load_config({"BOT_TOKEN": "123", "FOOTBALL_API_KEY": "abc", "GEMINI_API_KEY": "  "})


def test_model_defaults_to_gemini_2_5_flash() -> None:
    config = load_config(
        {
            "BOT_TOKEN": "123456:token",
            "FOOTBALL_API_KEY": "fd",
            "GEMINI_API_KEY": "gem",
        }
    )
    assert config.gemini_model == "gemini-2.5-flash"
    assert config.database_path == Path("data/predictions.db")
    assert config.cache_dir == Path("data/cache")


def test_only_start_and_predict_are_registered() -> None:
    config = load_config(
        {
            "BOT_TOKEN": "123456789:AAH1234567890abcdefghijklmnopqrstuv",
            "FOOTBALL_API_KEY": "fd",
            "GEMINI_API_KEY": "gem",
            "DATABASE_PATH": "data/predictions.db",
        }
    )
    application = build_application(config, pipeline=SimpleNamespace())
    commands = []
    for handlers in application.handlers.values():
        for handler in handlers:
            commands.extend(getattr(handler, "commands", set()))
    assert set(commands) == {"start", "predict"}
    assert application.job_queue is None


def test_report_escapes_markdown_and_hides_ineligible_half_time() -> None:
    hidden = PipelineResult(
        run_id=4,
        status="partial",
        stage=None,
        reason=None,
        full_time_score="1-0",
        markets=Markets("Home", "No", "Under"),
        confidence=20,
        reasoning="line_with_underscore",
        include_half_time=False,
        home_name="Arsenal",
        away_name="Chelsea",
        competition_name="Premier League",
    )
    text = render_report(hidden)
    assert "Half-time:" not in text
    assert "line\\_with\\_underscore" in text
    failure = render_report(
        PipelineResult(run_id=4, status="abstained", stage="fixture", reason="stopped"),
        markdown=False,
    )
    assert failure.startswith("Stage: fixture")
    assert "No score was estimated." in failure


def test_predict_rejects_a_bad_command_before_the_provider(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path)
        # Build the pipeline by executing nothing: open it through a rejected command.
        from bot import predict as predict_command

        message = SimpleNamespace(
            text="/predict Arsenal vs Chelsea",
            chat_id=5,
            from_user=SimpleNamespace(id=9),
            reply_text=AsyncMock(),
        )
        # The handler needs a pipeline. Construct one with the harness client by running
        # a local execute wrapper's pipeline after a dummy open.
        opened = await _open(holder)
        context = SimpleNamespace(bot_data={"pipeline": opened})
        await predict_command(SimpleNamespace(message=message), context)
        return message, holder

    message, holder = asyncio.run(run())
    message.reply_text.assert_awaited()
    assert "Competition is required" in message.reply_text.await_args.args[0]
    assert holder.api.calls == []


def test_predict_acknowledges_one_match_and_does_not_swap_it(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path)
        pipeline = await _open(holder)
        message = SimpleNamespace(
            text="/predict Arsenal vs Chelsea | Premier League",
            chat_id=5,
            from_user=SimpleNamespace(id=9),
            reply_text=AsyncMock(),
        )
        await predict(SimpleNamespace(message=message), SimpleNamespace(bot_data={"pipeline": pipeline}))
        result = await pipeline.execute(
            chat_id=5,
            user_id=9,
            home="Chelsea",
            away="Arsenal",
            competition="Premier League",
        )
        return message, holder, result

    message, holder, swapped = asyncio.run(run())
    assert message.reply_text.await_count == 2
    assert "Only this match is analyzed." in message.reply_text.await_args_list[0].args[0]
    assert "Arsenal vs Chelsea" in message.reply_text.await_args_list[0].args[0]
    report = message.reply_text.await_args_list[1].args[0]
    assert "1X2: Home" in report
    # The explicit swapped command is a different request and must not reuse the first fixture.
    assert swapped.stage == "fixture"
    assert "not swapped" in swapped.reason


def test_requirements_and_sources_stay_on_demand() -> None:
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "google-genai" in requirements
    assert "google-generativeai" not in requirements
    assert "python-telegram-bot" in requirements
    assert "httpx" in requirements
    banned = ("today_matches", "JobQueue", "run_daily", "run_repeating", "InlineKeyboard", "google.generativeai")
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text, f"{path.name} contains {word}"
    main_text = (root / "main.py").read_text(encoding="utf-8")
    assert "run_polling" in main_text
    assert "get_teams" not in main_text
    assert "generate_content" not in main_text


def test_in_process_limit_waits_before_the_ninth_call() -> None:
    clock = {"now": 0.0}
    slept: list[float] = []

    async def scenario() -> None:
        async def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock["now"] += seconds

        limiter = RateLimiter(max_calls=2, period_seconds=60, clock=lambda: clock["now"], sleep=sleep)
        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()

    asyncio.run(scenario())
    assert slept == [60.0]


def test_team_cache_is_written_only_when_a_request_reads_it(tmp_path) -> None:
    async def run():
        holder = Harness(tmp_path)
        from conftest import Api

        api = Api()
        import httpx

        transport = httpx.MockTransport(api.handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://api.football-data.org") as client:
            fetcher = DataFetcher("fd-test-key", tmp_path / "cache", client=client, sleep=api.sleep)
            async with fetcher.request_scope():
                await fetcher.get_teams("PL")
                await fetcher.get_teams("PL")
        return api

    api = asyncio.run(run())
    assert [call["path"] for call in api.calls] == ["/v4/competitions/PL/teams"]
    assert list((tmp_path / "cache").glob("teams_*.json"))


async def _open(holder: Harness):
    import httpx

    from data_fetcher import DataFetcher
    from pipeline import Pipeline
    from predictor import Predictor
    from storage import Storage

    holder._client_cm = httpx.AsyncClient(
        transport=httpx.MockTransport(holder.api.handler),
        base_url="https://api.football-data.org",
    )
    client = await holder._client_cm.__aenter__()
    fetcher = DataFetcher("fd-test-key", holder.tmp_path / "cache", client=client, sleep=holder.api.sleep)

    async def generate(prompt: str, schema: dict) -> str:
        holder.prompts.append(prompt)
        holder.schemas.append(schema)
        item = holder.scripts.pop(0)
        return item

    predictor = Predictor("gemini-test-key", "gemini-2.5-flash", generate=generate)
    holder.store = Storage(holder.tmp_path / "predictions.db")
    holder.pipeline = Pipeline(fetcher, predictor, holder.store)
    return holder.pipeline
