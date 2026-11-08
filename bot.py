"""Telegram commands: /start and /predict Home vs Away | Competition.

A /predict update acknowledges that one match, then awaits its pipeline.
It does not offer a fixture list or start a second analysis.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from config import Config
from models import PipelineResult
from pipeline import Pipeline

logger = logging.getLogger(__name__)

COMMAND_RE = re.compile(r"^/predict(?:@\w+)?\s+(?P<body>.+)$", re.IGNORECASE | re.DOTALL)
VS_RE = re.compile(r"\bvs\b", re.IGNORECASE)

START_TEXT = (
    "This bot estimates one match when you name it.\n\n"
    "/predict Home vs Away | Competition\n\n"
    "Example:\n"
    "/predict Arsenal vs Chelsea | Premier League\n\n"
    "It only listens for commands. It does not scan the fixture list. "
    "Home and away stay in the order you wrote. If the match is finished "
    "or the data is missing, it abstains instead of guessing.\n\n"
    "A result is a statistical estimate, not a betting tip."
)


@dataclass(frozen=True)
class ParsedCommand:
    home: str
    away: str
    competition: str


@dataclass(frozen=True)
class ParseFailure:
    reason: str


def parse_predict_command(text: str) -> ParsedCommand | ParseFailure:
    """Split one command. No club or competition is inferred to fill a blank."""

    stripped = text.strip()
    match = COMMAND_RE.match(stripped)
    if match is None:
        return ParseFailure("Send /predict Home vs Away | Competition.")
    body = match.group("body").strip()
    if "|" not in body:
        return ParseFailure(
            "Competition is required. Use /predict Home vs Away | Competition."
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


def build_application(config: Config, pipeline: Pipeline) -> Application:
    application = Application.builder().token(config.bot_token).job_queue(None).build()
    application.bot_data["pipeline"] = pipeline
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("predict", predict))
    application.add_error_handler(on_error)
    return application


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(START_TEXT)


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
        pipeline.store.update_run(result.run_id, status="failed", stage="input", reason=parsed.reason)
        await message.reply_text(render_report(result, markdown=False))
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
            "Markets derived from the predicted scoreline:",
            f"1X2: {result.markets.result_1x2}",
            f"BTTS: {result.markets.btts}",
            f"Over/Under 2.5: {result.markets.over_under_25}",
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


def _half_time_name(result: PipelineResult) -> str:
    if result.half_time_prediction == "Team A":
        return result.home_name or "Team A"
    if result.half_time_prediction == "Team B":
        return result.away_name or "Team B"
    return "Draw"


def _kickoff(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace("T", " ").replace("Z", " UTC")


def _same_label(left: str, right: str) -> bool:
    return re.sub(r"\s+", " ", left.strip().casefold()) == re.sub(r"\s+", " ", right.strip().casefold())


def _plain(text: str, markdown: bool) -> str:
    if not markdown:
        return text
    return re.sub(r"([_*`\[])", r"\\\1", text)


def _em(text: str, markdown: bool, *, italic: bool = False) -> str:
    if not markdown:
        return text
    marker = "_" if italic else "*"
    return f"{marker}{_plain(text, True)}{marker}"
