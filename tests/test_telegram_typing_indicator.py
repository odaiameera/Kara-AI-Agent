from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.platforms import telegram as telegram_adapter


class TypingIndicatorTests(unittest.IsolatedAsyncioTestCase):
    def _fake_context(self) -> MagicMock:
        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()
        return context

    async def test_refreshes_typing_action_repeatedly_while_block_runs(self) -> None:
        context = self._fake_context()
        with patch.object(telegram_adapter, "TYPING_REFRESH_SECONDS", 0.01):
            async with telegram_adapter._typing_indicator(context, 123):
                await asyncio.sleep(0.05)

        self.assertGreaterEqual(context.bot.send_chat_action.await_count, 2)
        context.bot.send_chat_action.assert_awaited_with(chat_id=123, action=telegram_adapter.ChatAction.TYPING)

    async def test_stops_refreshing_once_the_block_exits(self) -> None:
        context = self._fake_context()
        with patch.object(telegram_adapter, "TYPING_REFRESH_SECONDS", 0.01):
            async with telegram_adapter._typing_indicator(context, 123):
                await asyncio.sleep(0.03)
            count_at_exit = context.bot.send_chat_action.await_count
            await asyncio.sleep(0.05)

        self.assertEqual(context.bot.send_chat_action.await_count, count_at_exit)

    async def test_does_nothing_when_chat_id_is_none(self) -> None:
        context = self._fake_context()
        async with telegram_adapter._typing_indicator(context, None):
            await asyncio.sleep(0.02)

        context.bot.send_chat_action.assert_not_awaited()

    async def test_a_failed_send_does_not_break_the_refresh_loop(self) -> None:
        context = self._fake_context()
        context.bot.send_chat_action.side_effect = [RuntimeError("boom"), None, None]
        with patch.object(telegram_adapter, "TYPING_REFRESH_SECONDS", 0.01):
            async with telegram_adapter._typing_indicator(context, 123):
                await asyncio.sleep(0.05)

        self.assertGreaterEqual(context.bot.send_chat_action.await_count, 2)


if __name__ == "__main__":
    unittest.main()
