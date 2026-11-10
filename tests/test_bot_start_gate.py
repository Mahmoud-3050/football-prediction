"""Telegram bot stays idle in a chat until /start."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import NEED_START_TEXT, build_application, start, today
from config import load_config


def _with_event_loop(callback):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return callback()
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_commands_require_start_then_work() -> None:
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
        context = SimpleNamespace(bot_data=application.bot_data, args=[])
        message = SimpleNamespace(
            reply_text=AsyncMock(),
            chat_id=42,
            text="/today",
            from_user=SimpleNamespace(id=7),
        )
        update = SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(id=42),
            effective_message=message,
        )

        async def flow() -> None:
            await today(update, context)
            message.reply_text.assert_awaited_with(NEED_START_TEXT)
            message.reply_text.reset_mock()
            await start(update, context)
            assert 42 in context.bot_data["active_chats"]
            assert "Bot enabled" in message.reply_text.await_args.args[0]

        asyncio.get_event_loop().run_until_complete(flow())

    _with_event_loop(run)
