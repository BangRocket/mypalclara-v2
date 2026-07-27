import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from frontend.ui import ChatApp, Message


class MessagePresentationTests(unittest.TestCase):
    def test_discord_messages_are_visibly_flagged(self) -> None:
        message = Message("hello", "user", source="discord")

        self.assertTrue(message.has_class("discord"))
        self.assertEqual(str(message.render()), "Discord · hello")


class ChatHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_history_keeps_its_source_flag(self) -> None:
        history = [
            SimpleNamespace(role="user", content="from DM", source="discord"),
            SimpleNamespace(role="assistant", content="DM reply", source="discord"),
        ]
        with (
            patch("frontend.ui.palace.available", new=AsyncMock(return_value=False)),
            patch("frontend.ui.auth.load_session", return_value={"name": "Tester"}),
            patch("frontend.ui.conversation.get_history", return_value=history),
        ):
            app = ChatApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                messages = list(app.query(Message))

        self.assertTrue(messages[0].has_class("discord"))
        self.assertTrue(messages[1].has_class("discord"))
        self.assertEqual(str(messages[0].render()), "Discord · from DM")


class CommandTests(unittest.TestCase):
    def test_link_discord_opens_clerk_profile(self) -> None:
        from frontend import ui

        with (
            patch.object(sys, "argv", ["mypalclara", "--link-discord"]),
            patch.object(ui.auth, "open_discord_linking") as open_linking,
        ):
            ui.main()
        open_linking.assert_called_once_with()
