import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from backend import chat_store
from scripts.backfill_discord import (
    build_memory_jobs,
    build_writes,
    ingest_memory_job,
    ingest_memory_jobs,
    load_export,
    split_into_sessions,
)


class DiscordBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.directory = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write_export(self, messages: list[dict], *, dm: bool = True) -> Path:
        path = self.directory / "chat.json"
        path.write_text(
            json.dumps(
                {
                    "guild": {"name": "Direct Messages" if dm else "Test Guild"},
                    "channel": {
                        "id": "channel-1",
                        "name": "clara",
                        "type": "DirectTextChat" if dm else "GuildTextChat",
                    },
                    "messages": messages,
                }
            )
        )
        return path

    @staticmethod
    def _message(
        message_id: str,
        content: str,
        timestamp: str,
        author: str = "stairmaster401",
        **extra,
    ) -> dict:
        return {
            "id": message_id,
            "content": content,
            "timestamp": timestamp,
            "author": {"name": author, "isBot": author == "MyPalClara"},
            "attachments": [],
            "stickers": [],
            **extra,
        }

    def test_decisions_roles_and_attachment_only_messages_are_preserved(self) -> None:
        path = self._write_export(
            [
                self._message("1", "remove me", "2026-01-01T10:00:00Z"),
                self._message("2", "hello", "2026-01-01T10:01:00Z"),
                self._message(
                    "3",
                    "",
                    "2026-01-01T10:02:00Z",
                    author="MyPalClara",
                    attachments=[{"url": "https://cdn.example/file.txt"}],
                ),
            ]
        )
        path.with_name("chat_decisions.json").write_text(
            json.dumps({"1": {"status": "remove"}, "2": {"status": "keep"}})
        )

        export = load_export(path)

        self.assertTrue(export.decisions_found)
        self.assertEqual(export.removed_messages, 1)
        self.assertEqual(
            [message.role for message in export.messages], ["user", "assistant"]
        )
        self.assertEqual(export.messages[0].author_name, "Josh")
        self.assertEqual(export.messages[1].content, "https://cdn.example/file.txt")

    def test_group_messages_receive_canonical_speaker_labels(self) -> None:
        path = self._write_export(
            [self._message("1", "hello", "2026-01-01T10:00:00Z")],
            dm=False,
        )

        export = load_export(path)

        self.assertEqual(export.messages[0].content, "[Josh] hello")

    def test_session_splitting_and_writes_are_deterministic(self) -> None:
        path = self._write_export(
            [
                self._message("1", "first", "2026-01-01T10:00:00Z"),
                self._message("2", "second", "2026-01-01T10:05:00Z"),
                self._message("3", "later", "2026-01-01T11:00:00Z"),
            ]
        )
        export = load_export(path)

        sessions = split_into_sessions(
            export.messages, gap_minutes=30, max_messages=100
        )
        writes = build_writes(
            export,
            user_id="user_123",
            gap_minutes=30,
            max_session_messages=100,
        )

        self.assertEqual([len(session) for session in sessions], [2, 1])
        self.assertEqual(writes[0].conversation_id, "discord:channel-1:1")
        self.assertEqual(writes[1].conversation_id, "discord:channel-1:1")
        self.assertEqual(writes[2].conversation_id, "discord:channel-1:3")
        self.assertEqual(writes[0].external_id, "1")
        self.assertEqual(writes[0].source, "discord")
        self.assertEqual(writes[0].created_at, datetime(2026, 1, 1, 10, tzinfo=UTC))

    def test_batch_storage_retains_timestamp_and_counts_duplicates(self) -> None:
        created_at = datetime(2026, 1, 1, 10, tzinfo=UTC)
        messages = [
            chat_store.ChatMessageWrite(
                user_id="user_123",
                conversation_id="discord:channel-1:1",
                role="user",
                content="hello",
                source="discord",
                external_id="1",
                created_at=created_at,
            ),
            chat_store.ChatMessageWrite(
                user_id="user_123",
                conversation_id="discord:channel-1:1",
                role="assistant",
                content="hi",
                source="discord",
                external_id="2",
                created_at=created_at,
            ),
        ]
        inserted = Mock()
        inserted.fetchone.return_value = (1,)
        duplicate = Mock()
        duplicate.fetchone.return_value = None
        connection = MagicMock()
        connection.execute.side_effect = [inserted, duplicate]
        context = MagicMock()
        context.__enter__.return_value = connection

        with patch("backend.chat_store._connect", return_value=context):
            count = chat_store.record_messages(messages)

        self.assertEqual(count, 1)
        self.assertEqual(connection.execute.call_count, 2)
        self.assertEqual(connection.execute.call_args_list[0].args[1][-1], created_at)


class DiscordMemoryBackfillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        path = Path(self._temp.name) / "chat.json"
        path.write_text(
            json.dumps(
                {
                    "guild": {"name": "Direct Messages"},
                    "channel": {
                        "id": "channel-1",
                        "name": "clara",
                        "type": "DirectTextChat",
                    },
                    "messages": [
                        {
                            "id": str(index),
                            "content": f"message {index}",
                            "timestamp": f"2026-01-01T10:0{index}:00Z",
                            "author": {
                                "name": (
                                    "stairmaster401"
                                    if index % 2
                                    else "MyPalClara"
                                )
                            },
                            "attachments": [],
                        }
                        for index in range(1, 5)
                    ],
                }
            )
        )
        export = load_export(path)
        self.jobs = build_memory_jobs(
            export,
            gap_minutes=30,
            max_session_messages=2,
            min_session_messages=2,
        )

    def tearDown(self) -> None:
        self._temp.cleanup()

    async def test_palace_and_mem0_receive_inferred_session_payloads(self) -> None:
        palace_add = AsyncMock(return_value=[])
        client = SimpleNamespace(add=palace_add)
        mem0_add = AsyncMock(return_value={"event_id": "event-1"})

        with patch("scripts.backfill_discord.palace._get_client", return_value=client):
            await ingest_memory_job(
                self.jobs[0], provider="palace", user_id="user-1"
            )
        with patch("scripts.backfill_discord.mem0.add", new=mem0_add):
            await ingest_memory_job(
                self.jobs[0], provider="mem0", user_id="user-1"
            )

        palace_kwargs = palace_add.await_args.kwargs
        self.assertTrue(palace_kwargs["infer"])
        self.assertEqual(palace_kwargs["user_id"], "user-1")
        self.assertEqual(palace_kwargs["source"], "discord-backfill")
        self.assertEqual(len(palace_kwargs["messages"]), 2)
        mem0_kwargs = mem0_add.await_args.kwargs
        self.assertTrue(mem0_kwargs["infer"])
        self.assertEqual(mem0_kwargs["user_id"], "user-1")
        self.assertTrue(mem0_kwargs["metadata"]["backfill"])

    async def test_completed_memory_sessions_are_checkpointed_and_skipped(self) -> None:
        ingest = AsyncMock(return_value=[])
        save = Mock()
        with (
            patch(
                "scripts.backfill_discord.session_store.load_namespace",
                return_value={self.jobs[0].fingerprint: {"completed": True}},
            ),
            patch("scripts.backfill_discord.session_store.save", new=save),
            patch(
                "scripts.backfill_discord.ingest_memory_job", new=ingest
            ),
        ):
            completed, skipped, errors = await ingest_memory_jobs(
                self.jobs,
                provider="palace",
                user_id="user-1",
                concurrency=2,
                reingest=False,
            )

        self.assertEqual((completed, skipped, errors), (1, 1, 0))
        ingest.assert_awaited_once()
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
