import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import build_application, predict, render_report
from config import Config, ConfigError, load_config, normalize_gemini_model
from conftest import Harness
from data_fetcher import DataFetcher, RateLimiter
from models import Markets, PipelineResult


def test_missing_keys_fail_without_a_provider_call() -> None:
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        load_config({"BOT_TOKEN": "123", "FOOTBALL_API_KEY": "abc", "GEMINI_API_KEY": "  "})


def test_model_defaults_to_gemini_3_5_flash() -> None:
    config = load_config(
        {
            "BOT_TOKEN": "123456:token",
            "FOOTBALL_API_KEY": "fd",
            "GEMINI_API_KEY": "gem",
        }
    )
    assert config.gemini_model == "gemini-3.5-flash"
    assert config.gemini_fallback_models == ("gemini-3.8-flash", "gemini-3.5-flash-lite")
    assert config.database_path == Path("data/football.db")
    assert config.cache_dir == Path("data/cache")
    assert config.pre_round_enabled is False
    assert config.pre_round_interval_seconds == 3600


def test_display_model_names_become_api_ids() -> None:
    base = {
        "BOT_TOKEN": "123456:token",
        "FOOTBALL_API_KEY": "fd",
        "GEMINI_API_KEY": "gem",
    }
    display = load_config({**base, "GEMINI_MODEL": "gemini-Gemini 3.8 Flash"})
    prefixed = load_config({**base, "GEMINI_MODEL": "models/gemini-3.8-flash"})
    assert display.gemini_model == "gemini-3.8-flash"
    assert prefixed.gemini_model == "gemini-3.8-flash"
    assert normalize_gemini_model("Gemini 2.5 Flash-Lite") == "gemini-2.5-flash-lite"


def _with_event_loop(callback):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return callback()
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_only_start_and_predict_are_registered() -> None:
    def run() -> None:
        config = load_config(
            {
                "BOT_TOKEN": "123456789:AAH1234567890abcdefghijklmnopqrstuv",
                "FOOTBALL_API_KEY": "fd",
                "GEMINI_API_KEY": "gem",
                "DATABASE_PATH": "data/football.db",
                "PRE_ROUND_ENABLED": "0",
            }
        )
        application = build_application(config, pipeline=SimpleNamespace())
        commands = []
        for handlers in application.handlers.values():
            for handler in handlers:
                commands.extend(getattr(handler, "commands", set()))
        assert set(commands) == {"start", "today", "matches", "predict"}
        assert application.job_queue is None
        assert application.bot_data["pre_round_enabled"] is False

    _with_event_loop(run)


def test_pre_round_can_be_enabled_on_the_application() -> None:
    def run() -> None:
        config = load_config(
            {
                "BOT_TOKEN": "123456789:AAH1234567890abcdefghijklmnopqrstuv",
                "FOOTBALL_API_KEY": "fd",
                "GEMINI_API_KEY": "gem",
                "DATABASE_PATH": "data/football.db",
                "PRE_ROUND_ENABLED": "1",
                "PRE_ROUND_INTERVAL_SECONDS": "120",
            }
        )
        application = build_application(config, pipeline=SimpleNamespace())
        assert application.bot_data["pre_round_enabled"] is True
        assert application.bot_data["pre_round_interval_seconds"] == 120.0
        assert application.job_queue is None

    _with_event_loop(run)


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


def test_report_shows_distribution_layout_with_gemini_interpretation() -> None:
    shown = PipelineResult(
        run_id=5,
        status="completed",
        stage=None,
        reason=None,
        full_time_score="2-1",
        markets=Markets("Home", "Yes", "Over", result_probability=48),
        confidence=14,
        reasoning="Home attack edge in the snapshot.",
        reasoning_ar="تفوق هجومي للمضيف في اللقطة.",
        include_half_time=True,
        half_time_prediction="Team A",
        half_time_score="1-0",
        half_time_probabilities={"home": 0.369, "draw": 0.339, "away": 0.292},
        home_name="Arsenal",
        away_name="Chelsea",
        competition_name="Premier League",
        probabilities={
            "home": 0.456,
            "draw": 0.211,
            "away": 0.333,
            "over_25": 0.729,
            "over_35": 0.524,
        },
        top_scores=[
            (2, 1, 0.083),
            (1, 1, 0.081),
            (2, 2, 0.072),
            (1, 2, 0.070),
            (3, 1, 0.056),
            (3, 2, 0.049),
            (2, 0, 0.048),
            (1, 0, 0.046),
        ],
    )
    text = render_report(shown, markdown=False)
    assert "Home  █████░░░░░  45.6%" in text
    assert "Most likely score 2–1" in text
    assert "2–1  8.3%" in text
    assert "1–0  4.6%" in text
    assert "Half time" in text
    assert "Most likely 1–0" in text
    assert "Over 2.5 72.9%" in text
    assert "Over 3.5 52.4%" in text
    assert "Interpretation" in text
    assert "Home attack edge in the snapshot." in text
    assert "التفسير" in text
    assert "تفوق هجومي للمضيف في اللقطة." in text
    assert "BTTS:" not in text
    assert "Markets from the score distribution" not in text


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
        context = SimpleNamespace(
            bot_data={"pipeline": opened, "active_chats": {5}},
        )
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
        await predict(
            SimpleNamespace(message=message),
            SimpleNamespace(bot_data={"pipeline": pipeline, "active_chats": {5}}),
        )
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
    assert "Most likely score" in report
    assert "Interpretation" in report
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
    banned = ("JobQueue", "run_daily", "run_repeating", "google.generativeai")
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text, f"{path.name} contains {word}"
    assert "get_todays_matches" in (root / "data_fetcher.py").read_text(encoding="utf-8")
    bot_text = (root / "bot.py").read_text(encoding="utf-8")
    assert 'CommandHandler("today"' in bot_text
    assert 'CommandHandler("matches"' in bot_text
    assert "CallbackQueryHandler" in bot_text
    assert "InlineKeyboardButton" in bot_text
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
