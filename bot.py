"""Telegram commands: /start, /today, /matches, and /predict.

/today and /matches list fixtures without predicting them.
/predict analyzes one named match or one listed match id.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from config import Config
from data_fetcher import format_match_list, fixtures_from_payload
from models import Fixture, PipelineResult, ProviderError
from pipeline import Pipeline
from timeutil import format_kickoff_cairo, format_kickoff_short_cairo

logger = logging.getLogger(__name__)

COMMAND_RE = re.compile(r"^/predict(?:@\w+)?\s+(?P<body>.+)$", re.IGNORECASE | re.DOTALL)
MATCH_ID_RE = re.compile(r"^#?(?P<match_id>\d+)$")
DAY_RE = re.compile(r"^(?P<day>\d{2})-(?P<month>\d{2})-(?P<year>\d{4})$")
PREDICT_CALLBACK_RE = re.compile(r"^p:(?P<match_id>\d+)$")
VS_RE = re.compile(r"\bvs\b", re.IGNORECASE)
BUTTONS_PER_MESSAGE = 6
BUTTON_LABEL_MAX = 60

START_TEXT = (
    "Football match estimates on request.\n\n"
    "/today — today's fixtures (no predictions)\n"
    "/matches 24-05-2026 — fixtures for that day (DD-MM-YYYY)\n"
    "/predict Home vs Away | Competition\n"
    "/predict <id> — predict a match from /today or /matches\n\n"
    "Examples:\n"
    "/matches 24-05-2026\n"
    "/predict Arsenal vs Chelsea | Premier League\n"
    "/predict #560638\n\n"
    "On /today and /matches, tap a match button to predict it.\n"
    "Fixture lists cover Premier League, UEFA Champions League, Primera Division, "
    "Serie A, Bundesliga, and Ligue 1.\n"
    "Kickoff times and /matches days use Cairo time.\n"
    "If a match is finished or the data is missing, the bot abstains.\n\n"
    "A result is a statistical estimate, not a betting tip."
)


@dataclass(frozen=True)
class ParsedCommand:
    home: str
    away: str
    competition: str


@dataclass(frozen=True)
class ParsedMatchId:
    match_id: int


@dataclass(frozen=True)
class ParseFailure:
    reason: str


@dataclass(frozen=True)
class ParsedDay:
    display: str
    api_date: str


def parse_day_argument(value: str) -> ParsedDay | ParseFailure:
    """Accept DD-MM-YYYY and convert to Football-Data's YYYY-MM-DD."""

    cleaned = value.strip()
    match = DAY_RE.fullmatch(cleaned)
    if match is None:
        return ParseFailure("Use the date format DD-MM-YYYY. Example: /matches 24-05-2026")
    day = int(match.group("day"))
    month = int(match.group("month"))
    year = int(match.group("year"))
    try:
        date(year, month, day)
    except ValueError:
        return ParseFailure(
            f"{cleaned} is not a valid calendar date. Use DD-MM-YYYY, for example 24-05-2026."
        )
    return ParsedDay(display=cleaned, api_date=f"{year:04d}-{month:02d}-{day:02d}")


def parse_predict_command(text: str) -> ParsedCommand | ParsedMatchId | ParseFailure:
    """Split one command. A bare numeric id is accepted from /today."""

    stripped = text.strip()
    match = COMMAND_RE.match(stripped)
    if match is None:
        return ParseFailure(
            "Send /predict Home vs Away | Competition, or /predict <id> from /today."
        )
    body = match.group("body").strip()
    id_match = MATCH_ID_RE.fullmatch(body)
    if id_match is not None:
        return ParsedMatchId(match_id=int(id_match.group("match_id")))
    if "|" not in body:
        return ParseFailure(
            "Competition is required. Use /predict Home vs Away | Competition, "
            "or /predict <id> from /today."
        )
    teams_part, competition = body.split("|", 1)
    competition = competition.strip()
    if VS_RE.search(teams_part) is None:
        return ParseFailure("Use vs between the home club and the away club.")
    home, away = VS_RE.split(teams_part, maxsplit=1)
    home, away = home.strip(), away.strip()
    if not home or not away or not competition:
        return ParseFailure("Home, away, and competition are all required.")
    if _same_label(home, away):
        return ParseFailure("Home and away are the same club.")
    return ParsedCommand(home=home, away=away, competition=competition)


def render_report(result: PipelineResult, *, markdown: bool = True) -> str:
    if result.status in {"completed", "partial"} and result.full_time_score and result.markets:
        return _success(result, markdown=markdown)
    return _failure(result, markdown=markdown)


def build_predict_button_pages(
    fixtures: list[Fixture],
    *,
    buttons_per_message: int = BUTTONS_PER_MESSAGE,
) -> list[InlineKeyboardMarkup]:
    """One tap target per match. callback_data is p:<Football-Data match id>."""

    if not fixtures:
        return []
    pages: list[InlineKeyboardMarkup] = []
    for start in range(0, len(fixtures), buttons_per_message):
        chunk = fixtures[start : start + buttons_per_message]
        rows = [
            [InlineKeyboardButton(text=_button_label(fixture), callback_data=f"p:{fixture.id}")]
            for fixture in chunk
        ]
        pages.append(InlineKeyboardMarkup(rows))
    return pages


def _button_label(fixture: Fixture) -> str:
    kickoff = format_kickoff_short_cairo(fixture.utc_date)
    label = f"{kickoff} | {fixture.home_team_name} vs {fixture.away_team_name}"
    if len(label) <= BUTTON_LABEL_MAX:
        return label
    return label[: BUTTON_LABEL_MAX - 1] + "…"


def build_application(config: Config, pipeline: Pipeline) -> Application:
    application = Application.builder().token(config.bot_token).job_queue(None).build()
    application.bot_data["pipeline"] = pipeline
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("today", today))
    application.add_handler(CommandHandler("matches", matches))
    application.add_handler(CommandHandler("predict", predict))
    application.add_handler(
        CallbackQueryHandler(predict_button, pattern=PREDICT_CALLBACK_RE.pattern)
    )
    application.add_error_handler(on_error)
    return application


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(START_TEXT)


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    from timeutil import cairo_today

    day = cairo_today()
    await _reply_match_list(
        message,
        context,
        loading="Loading today's fixtures (Cairo time). No predictions yet.",
        cairo_day=day,
        day_label="today",
    )


async def matches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    args = context.args or []
    if len(args) != 1:
        await message.reply_text("Use /matches DD-MM-YYYY. Example: /matches 24-05-2026")
        return
    parsed = parse_day_argument(args[0])
    if isinstance(parsed, ParseFailure):
        await message.reply_text(parsed.reason)
        return
    day = date.fromisoformat(parsed.api_date)
    await _reply_match_list(
        message,
        context,
        loading=f"Loading fixtures for {parsed.display} (Cairo time). No predictions yet.",
        cairo_day=day,
        day_label=parsed.display,
    )


async def _reply_match_list(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    loading: str,
    cairo_day: date,
    day_label: str,
) -> None:
    pipeline: Pipeline = context.bot_data["pipeline"]
    await message.reply_text(loading)
    try:
        async with pipeline.fetcher.request_scope():
            payload = await pipeline.fetcher.get_matches_for_cairo_day(cairo_day)
        fixtures = fixtures_from_payload(payload, cairo_day=cairo_day)
        pages = format_match_list(fixtures, day_label=day_label)
    except ProviderError as exc:
        await message.reply_text(f"Stage: provider\n{exc.reason}\nNo fixtures listed.")
        return
    except Exception:
        logger.exception("fixture list failed")
        await message.reply_text(
            "Stage: provider\nFixtures could not be loaded. The bot is still running."
        )
        return
    for page in pages:
        await message.reply_text(page)
    button_pages = build_predict_button_pages(fixtures)
    total_pages = len(button_pages)
    for index, keyboard in enumerate(button_pages, start=1):
        caption = "Tap a match to predict it:"
        if total_pages > 1:
            caption = f"Tap a match to predict it ({index}/{total_pages}):"
        await message.reply_text(caption, reply_markup=keyboard)


async def predict_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    match = PREDICT_CALLBACK_RE.fullmatch(query.data)
    if match is None:
        await query.answer()
        return
    match_id = int(match.group("match_id"))
    await query.answer()
    chat = query.message.chat if query.message is not None else None
    chat_id = chat.id if chat is not None else None
    user_id = query.from_user.id if query.from_user else None
    if query.message is not None:
        await query.message.reply_text(
            f"Checking match #{match_id}. Only this match is analyzed."
        )
    try:
        result = await _run_predict_by_id(context, chat_id=chat_id, user_id=user_id, match_id=match_id)
    except Exception:
        logger.exception("prediction button failed")
        if query.message is not None:
            await query.message.reply_text(
                "Stage: failed\nThe request could not be finished. The bot is still running."
            )
        return
    if query.message is not None:
        await _reply(query.message, result)


async def predict(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.text:
        return
    parsed = parse_predict_command(message.text)
    pipeline: Pipeline = context.bot_data["pipeline"]
    if isinstance(parsed, ParseFailure):
        result = await pipeline.execute(
            chat_id=message.chat_id,
            user_id=message.from_user.id if message.from_user else None,
            home="",
            away="",
            competition="",
        )
        result.reason = parsed.reason
        result.stage = "input"
        pipeline.store.update_run(
            result.run_id, status="failed", stage="input", reason=parsed.reason
        )
        await message.reply_text(render_report(result, markdown=False))
        return

    if isinstance(parsed, ParsedMatchId):
        await message.reply_text(
            f"Checking match #{parsed.match_id}. Only this match is analyzed."
        )
        try:
            result = await _run_predict_by_id(
                context,
                chat_id=message.chat_id,
                user_id=message.from_user.id if message.from_user else None,
                match_id=parsed.match_id,
            )
        except Exception:
            logger.exception("prediction by match id failed")
            await message.reply_text(
                "Stage: failed\nThe request could not be finished. The bot is still running."
            )
            return
        await _reply(message, result)
        return

    await message.reply_text(
        f"Checking {parsed.home} vs {parsed.away} in {parsed.competition}. "
        "Only this match is analyzed."
    )
    try:
        result = await pipeline.execute(
            chat_id=message.chat_id,
            user_id=message.from_user.id if message.from_user else None,
            home=parsed.home,
            away=parsed.away,
            competition=parsed.competition,
        )
    except Exception:
        logger.exception("prediction request failed")
        await message.reply_text(
            "Stage: failed\nThe request could not be finished. The bot is still running."
        )
        return
    await _reply(message, result)


async def _run_predict_by_id(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int | None,
    user_id: int | None,
    match_id: int,
) -> PipelineResult:
    pipeline: Pipeline = context.bot_data["pipeline"]
    return await pipeline.execute_by_match_id(
        chat_id=chat_id,
        user_id=user_id,
        match_id=match_id,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("telegram update failed", exc_info=context.error)


async def _reply(message: Any, result: PipelineResult) -> None:
    try:
        await message.reply_text(render_report(result), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        logger.exception("markdown reply failed")
        await message.reply_text(render_report(result, markdown=False))


def _success(result: PipelineResult, *, markdown: bool) -> str:
    assert result.markets is not None
    home = result.home_name or "Home"
    away = result.away_name or "Away"
    kickoff = _kickoff(result.kickoff) or "unknown"
    lines = [
        _em(result.competition_name or "Competition", markdown),
        _em(f"{home} vs {away}", markdown),
        f"Kickoff: {_plain(kickoff, markdown)}",
        "",
    ]
    if result.include_half_time and result.half_time_score and result.half_time_prediction:
        lines.append(
            "Half-time: "
            f"{_plain(_half_time_name(result), markdown)} {_plain(result.half_time_score, markdown)}"
        )
    lines.extend(
        [
            f"Full-time: {_plain(result.full_time_score or '', markdown)}",
            "",
            "Markets from the score distribution:",
            _market_line("1X2", result.markets.result_1x2, result.markets.result_probability),
            _market_line("BTTS", result.markets.btts, result.markets.btts_probability),
            _market_line(
                "Over/Under 2.5",
                result.markets.over_under_25,
                result.markets.over_under_probability,
            ),
            "",
            f"Confidence: {result.confidence}% for this scoreline",
            _plain(result.reasoning or "", markdown),
        ]
    )
    if result.limitations:
        lines.append("")
        lines.append("Limitations:")
        lines.extend(f"- {_plain(item, markdown)}" for item in result.limitations)
    if result.warnings:
        lines.append("")
        lines.extend(_plain(item, markdown) for item in result.warnings)
    lines.append("")
    lines.append(f"Run id: {result.run_id}")
    lines.append(_em("Statistical estimate, not a betting tip.", markdown, italic=True))
    return "\n".join(lines)


def _failure(result: PipelineResult, *, markdown: bool) -> str:
    stage = result.stage or "failed"
    reason = result.reason or "The request stopped."
    lines = [
        f"Stage: {_plain(stage, markdown)}",
        _plain(reason, markdown),
        "No score was estimated.",
        f"Run id: {result.run_id}",
        _em("Statistical estimate, not a betting tip.", markdown, italic=True),
    ]
    return "\n".join(lines)


def _market_line(label: str, choice: str, percent: int | None) -> str:
    if percent is None:
        return f"{label}: {choice}"
    return f"{label}: {choice} ({percent}%)"


def _half_time_name(result: PipelineResult) -> str:
    if result.half_time_prediction == "Team A":
        return result.home_name or "Team A"
    if result.half_time_prediction == "Team B":
        return result.away_name or "Team B"
    return "Draw"


def _kickoff(value: str | None) -> str | None:
    return format_kickoff_cairo(value)


def _same_label(left: str, right: str) -> bool:
    return re.sub(r"\s+", " ", left.strip().casefold()) == re.sub(
        r"\s+", " ", right.strip().casefold()
    )


def _plain(text: str, markdown: bool) -> str:
    if not markdown:
        return text
    return re.sub(r"([_*`\[])", r"\\\1", text)


def _em(text: str, markdown: bool, *, italic: bool = False) -> str:
    if not markdown:
        return text
    marker = "_" if italic else "*"
    return f"{marker}{_plain(text, True)}{marker}"
