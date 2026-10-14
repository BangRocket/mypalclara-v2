import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend import server


class ServerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start_patch = patch.object(server.runtime, "start", new=AsyncMock())
        self.stop_patch = patch.object(server.runtime, "stop", new=AsyncMock())
        self.start_patch.start()
        self.stop_patch.start()
        self.client = TestClient(server.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.stop_patch.stop()
        self.start_patch.stop()

    def test_health_reports_service_status(self) -> None:
        with patch(
            "backend.server.memory.provider_name",
            new=AsyncMock(return_value="mem0"),
        ):
            response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["memory"], "on")
        self.assertEqual(response.json()["memory_provider"], "mem0")

    def test_history_preserves_message_source(self) -> None:
        message = SimpleNamespace(
            id=7,
            role="user",
            content="from Discord",
            source="discord",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        with patch("backend.server.conversation.get_history", return_value=[message]):
            response = self.client.get("/v1/history")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"][0]["source"], "discord")

    def test_chat_uses_shared_conversation_and_mirrors_exchange(self) -> None:
        with (
            patch(
                "backend.server.conversation.get_reply",
                new=AsyncMock(return_value="hello back"),
            ) as get_reply,
            patch.object(
                server.runtime, "mirror_console_exchange", new=AsyncMock()
            ) as mirror,
            patch.object(server.runtime.hub, "broadcast", new=AsyncMock()) as broadcast,
        ):
            response = self.client.post(
                "/v1/chat", json={"content": "hello", "client_id": "console-1"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"reply": "hello back"})
        get_reply.assert_awaited_once_with("hello", source="console")
        mirror.assert_awaited_once_with("hello", "hello back")
        self.assertEqual(broadcast.await_count, 2)

    def test_blank_chat_is_rejected(self) -> None:
        response = self.client.post(
            "/v1/chat", json={"content": "   ", "client_id": "console-1"}
        )
        self.assertEqual(response.status_code, 422)

    def test_reset_clears_palace_and_agent_thread(self) -> None:
        with (
            patch(
                "backend.server.palace.reset_session", new=AsyncMock()
            ) as palace_reset,
            patch("backend.agent.reset_conversation", new=AsyncMock()) as agent_reset,
            patch.object(server.runtime.hub, "broadcast", new=AsyncMock()),
        ):
            response = self.client.post("/v1/session/reset")

        self.assertEqual(response.json(), {"reset": True})
        palace_reset.assert_awaited_once_with()
        agent_reset.assert_awaited_once_with()
