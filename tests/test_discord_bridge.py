import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from backend import conversation
from backend.discord_bridge import (
    DISCORD_MESSAGE_LIMIT,
    DiscordBridge,
    _split_for_discord,
)


class DiscordChunkingTests(unittest.TestCase):
    def test_long_messages_stay_within_discord_limit(self) -> None:
        content = "x" * 6500
        chunks = _split_for_discord(content)

        self.assertTrue(all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in chunks))
        self.assertEqual(
            "".join(chunk.removeprefix("↳ ") for chunk in chunks),
            content,
        )

    def test_prefix_is_applied_to_first_chunk(self) -> None:
        self.assertEqual(
            _split_for_discord("hello", "Console · You\n"),
            ["Console · You\nhello"],
        )


class DiscordInboundTests(unittest.IsolatedAsyncioTestCase):
    async def test_unlinked_clerk_user_does_not_connect_gateway(self) -> None:
        statuses = []
        bridge = DiscordBridge(lambda role, content: None, statuses.append)
        bridge._token = "token"
        bridge.start = AsyncMock()

        with patch(
            "backend.discord_bridge.auth.get_discord_user_id", return_value=None
        ):
            await bridge.start_bridge()

        self.assertEqual(statuses, ["connecting", "unlinked"])
        bridge.start.assert_not_awaited()
        await bridge.close_bridge()

    async def test_configured_user_dm_uses_shared_conversation(self) -> None:
        transcript = []
        bridge = DiscordBridge(
            on_transcript=lambda role, content: transcript.append((role, content)),
            on_status=lambda status: None,
        )
        bridge._discord_user_id = 42
        bridge._token = "token"
        bridge.flush_outbox = AsyncMock()
        bridge._enqueue = Mock()
        message = SimpleNamespace(
            id=123,
            author=SimpleNamespace(id=42, bot=False),
            guild=None,
            content="hello from Discord",
            attachments=[],
        )

        with (
            patch(
                "backend.discord_bridge.chat_store.has_external_message",
                return_value=False,
            ),
            patch(
                "backend.discord_bridge.conversation.get_reply",
                new=AsyncMock(return_value="hello back"),
            ) as get_reply,
        ):
            await bridge.on_message(message)

        get_reply.assert_awaited_once_with(
            "hello from Discord",
            source="discord",
            external_id="123",
        )
        self.assertEqual(
            transcript,
            [
                ("user", "hello from Discord"),
                ("assistant", "hello back"),
            ],
        )
        bridge._enqueue.assert_called_once_with("hello back")
        bridge.flush_outbox.assert_awaited_once_with()
        await bridge.close_bridge()

    async def test_other_users_and_guild_messages_are_ignored(self) -> None:
        bridge = DiscordBridge(lambda role, content: None, lambda status: None)
        bridge._discord_user_id = 42
        with patch(
            "backend.discord_bridge.conversation.get_reply", new=AsyncMock()
        ) as get_reply:
            for author_id, guild in ((41, None), (42, object())):
                message = SimpleNamespace(
                    id=123,
                    author=SimpleNamespace(id=author_id, bot=False),
                    guild=guild,
                    content="ignored",
                    attachments=[],
                )
                await bridge.on_message(message)
        get_reply.assert_not_awaited()
        await bridge.close_bridge()

    async def test_console_exchange_is_queued_then_flushed(self) -> None:
        bridge = DiscordBridge(lambda role, content: None, lambda status: None)
        bridge._discord_user_id = 42
        bridge._token = "token"
        bridge._enqueue = Mock()
        bridge.flush_outbox = AsyncMock()

        await bridge.mirror_console_exchange("question", "answer")

        self.assertEqual(
            bridge._enqueue.call_args_list,
            [
                unittest.mock.call("question", prefix="🖥️ **Console · You**\n"),
                unittest.mock.call("answer", prefix="🖥️ **Console · Clara**\n"),
            ],
        )
        bridge.flush_outbox.assert_awaited_once_with()
        await bridge.close_bridge()


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_is_stored_on_both_sides_of_exchange(self) -> None:
        fake_agent = ModuleType("backend.agent")
        fake_agent.get_reply = AsyncMock(return_value="reply")
        with (
            patch("backend.conversation.current_user_id", return_value="user-1"),
            patch(
                "backend.conversation.current_conversation_id",
                return_value="conversation-1",
            ),
            patch(
                "backend.conversation.chat_store.record_message",
                side_effect=[True, True],
            ) as record_message,
            patch.dict(sys.modules, {"backend.agent": fake_agent}),
        ):
            reply = await conversation.get_reply(
                "question", source="discord", external_id="message-1"
            )

        self.assertEqual(reply, "reply")
        self.assertEqual(record_message.call_args_list[0].kwargs["source"], "discord")
        self.assertEqual(record_message.call_args_list[1].kwargs["source"], "discord")

    async def test_duplicate_discord_message_is_not_answered_twice(self) -> None:
        fake_agent = ModuleType("backend.agent")
        fake_agent.get_reply = AsyncMock()
        with (
            patch("backend.conversation.current_user_id", return_value="user-1"),
            patch(
                "backend.conversation.current_conversation_id",
                return_value="conversation-1",
            ),
            patch("backend.conversation.chat_store.record_message", return_value=False),
            patch.dict(sys.modules, {"backend.agent": fake_agent}),
            self.assertRaises(conversation.DuplicateMessageError),
        ):
            await conversation.get_reply(
                "duplicate", source="discord", external_id="message-1"
            )
        fake_agent.get_reply.assert_not_awaited()
