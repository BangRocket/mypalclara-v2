import sys
import unittest
from unittest.mock import AsyncMock, patch

from frontend.ui import ChatApp, Message


class MessagePresentationTests(unittest.TestCase):
    def test_discord_messages_are_visibly_flagged(self) -> None:
        message = Message("hello", "user", source="discord")

        self.assertTrue(message.has_class("discord"))
        self.assertEqual(str(message.render()), "Discord · hello")

    def test_message_content_can_be_updated_in_place(self) -> None:
        message = Message("Thinking…", "ai")

        message.update("Finished")

        self.assertEqual(str(message.render()), "Finished")


class ChatHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_history_keeps_its_source_flag(self) -> None:
        history = [
            {"role": "user", "content": "from DM", "source": "discord"},
            {"role": "assistant", "content": "DM reply", "source": "discord"},
        ]

        async def no_events():
            await __import__("asyncio").Event().wait()
            yield {}

        with (
            patch("frontend.ui.auth.load_session", return_value={"name": "Tester"}),
            patch(
                "frontend.ui.server_client.get_health",
                new=AsyncMock(return_value={"memory": "on", "discord": "on"}),
            ),
            patch(
                "frontend.ui.server_client.get_history",
                new=AsyncMock(return_value=history),
            ),
            patch("frontend.ui.server_client.listen_events", side_effect=no_events),
        ):
            app = ChatApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                messages = list(app.query(Message))

        self.assertTrue(messages[0].has_class("discord"))
        self.assertTrue(messages[1].has_class("discord"))
        self.assertEqual(str(messages[0].render()), "Discord · from DM")

    async def test_server_event_from_discord_is_rendered_live(self) -> None:
        async def events():
            yield {
                "type": "chat_message",
                "role": "user",
                "content": "live DM",
                "source": "discord",
                "client_id": None,
            }

        with (
            patch("frontend.ui.auth.load_session", return_value={"name": "Tester"}),
            patch(
                "frontend.ui.server_client.get_health",
                new=AsyncMock(return_value={"memory": "on", "discord": "on"}),
            ),
            patch(
                "frontend.ui.server_client.get_history",
                new=AsyncMock(return_value=[]),
            ),
            patch("frontend.ui.server_client.listen_events", side_effect=events),
        ):
            app = ChatApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                messages = list(app.query(Message))

        self.assertEqual(str(messages[-1].render()), "Discord · live DM")


class ChatResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_comes_from_local_server(self) -> None:
        app = ChatApp()
        placeholder = unittest.mock.Mock()
        app.add_message = unittest.mock.Mock(return_value=placeholder)
        with patch(
            "frontend.ui.server_client.send_chat",
            new=AsyncMock(return_value="server reply"),
        ) as send_chat:
            await app.respond("hello")

        send_chat.assert_awaited_once_with("hello", app._client_id)
        app.add_message.assert_called_once_with("ai", "Thinking…")
        placeholder.update.assert_called_once_with("server reply")

    async def test_server_error_replaces_placeholder(self) -> None:
        app = ChatApp()
        placeholder = unittest.mock.Mock()
        app.add_message = unittest.mock.Mock(return_value=placeholder)
        with patch(
            "frontend.ui.server_client.send_chat",
            new=AsyncMock(side_effect=RuntimeError("offline")),
        ):
            await app.respond("hello")

        app.add_message.assert_called_once_with("ai", "Thinking…")
        placeholder.update.assert_called_once_with("Something went wrong: offline")


class CommandTests(unittest.TestCase):
    def test_link_discord_opens_clerk_profile(self) -> None:
        from frontend import ui

        with (
            patch.object(sys, "argv", ["mypalclara", "--link-discord"]),
            patch.object(ui.auth, "open_discord_linking") as open_linking,
        ):
            ui.main()
        open_linking.assert_called_once_with()
