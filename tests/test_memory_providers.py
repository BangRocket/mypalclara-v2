import os
import unittest
from unittest.mock import AsyncMock, patch

from backend import mem0, memory, palace


class Mem0ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_uses_v3_filters_and_normalizes_limit(self) -> None:
        request = AsyncMock(
            return_value={
                "results": [
                    {"id": "mem-1", "memory": "Likes tea", "score": 0.91}
                ]
            }
        )
        with patch.object(mem0, "_request", request):
            results = await mem0.search("drink", user_id="user-1", limit=5000)

        self.assertEqual(results[0]["memory"], "Likes tea")
        request.assert_awaited_once_with(
            "POST",
            "/v3/memories/search/",
            json={
                "query": "drink",
                "filters": {"user_id": "user-1"},
                "top_k": 1000,
            },
        )

    async def test_add_uses_v3_async_pipeline(self) -> None:
        request = AsyncMock(return_value={"status": "PENDING", "event_id": "evt-1"})
        messages = [{"role": "user", "content": "Likes tea"}]
        with patch.object(mem0, "_request", request):
            result = await mem0.add(
                messages,
                user_id="user-1",
                source="agent-tool",
                infer=False,
            )

        self.assertEqual(result["event_id"], "evt-1")
        request.assert_awaited_once_with(
            "POST",
            "/v3/memories/add/",
            json={
                "messages": messages,
                "user_id": "user-1",
                "metadata": {"source": "agent-tool"},
                "infer": False,
            },
        )

    async def test_add_merges_backfill_metadata_with_source(self) -> None:
        request = AsyncMock(return_value={"status": "PENDING"})
        with patch.object(mem0, "_request", request):
            await mem0.add(
                [{"role": "user", "content": "Likes tea"}],
                user_id="user-1",
                source="discord-backfill",
                metadata={"channel_id": "123", "backfill": True},
            )

        metadata = request.await_args.kwargs["json"]["metadata"]
        self.assertEqual(
            metadata,
            {
                "source": "discord-backfill",
                "channel_id": "123",
                "backfill": True,
            },
        )

    async def test_health_requires_api_key(self) -> None:
        with patch.dict(
            os.environ,
            {"MEM0_API_KEY": "", "MEM0_ENABLED": "true"},
        ):
            mem0.reset_health()
            self.assertFalse(await mem0.available("user-1"))


class MemoryProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_explicit_mem0_disables_palace_session_calls(self) -> None:
        with patch.dict(
            os.environ,
            {"MEMORY_PROVIDER": "mem0", "PALACE_ENABLED": "true"},
        ):
            self.assertFalse(palace.enabled())

    async def test_auto_falls_back_to_mem0(self) -> None:
        with (
            patch.dict(os.environ, {"MEMORY_PROVIDER": "auto"}),
            patch.object(memory.palace, "enabled", return_value=True),
            patch.object(
                memory.palace, "available", new=AsyncMock(return_value=False)
            ),
            patch.object(memory.mem0, "available", new=AsyncMock(return_value=True)),
            patch.object(memory, "user_id", return_value="user-1"),
        ):
            self.assertEqual(await memory.provider_name(), "mem0")

    async def test_provider_health_uses_explicit_maintenance_user(self) -> None:
        mem0_available = AsyncMock(return_value=True)
        with (
            patch.dict(os.environ, {"MEMORY_PROVIDER": "mem0"}),
            patch.object(memory.mem0, "available", new=mem0_available),
        ):
            self.assertEqual(await memory.provider_name("backfill-user"), "mem0")

        mem0_available.assert_awaited_once_with("backfill-user")

    async def test_disabled_palace_is_skipped(self) -> None:
        palace_available = AsyncMock(return_value=True)
        with (
            patch.dict(os.environ, {"MEMORY_PROVIDER": "auto"}),
            patch.object(memory.palace, "enabled", return_value=False),
            patch.object(memory.palace, "available", palace_available),
            patch.object(memory.mem0, "available", new=AsyncMock(return_value=True)),
            patch.object(memory, "user_id", return_value="user-1"),
        ):
            self.assertEqual(await memory.provider_name(), "mem0")
        palace_available.assert_not_awaited()

    async def test_mem0_results_are_normalized_for_tools_and_context(self) -> None:
        values = [
            {
                "id": "mem-1",
                "memory": "Likes tea",
                "score": 0.91,
                "categories": ["preferences"],
            }
        ]
        with (
            patch.object(memory, "provider_name", new=AsyncMock(return_value="mem0")),
            patch.object(memory, "user_id", return_value="user-1"),
            patch.object(memory.mem0, "search", new=AsyncMock(return_value=values)),
        ):
            records = await memory.search("drink")
            context = await memory.get_context("drink")

        self.assertEqual(records[0].content, "Likes tea")
        self.assertEqual(records[0].memory_type, "preferences")
        self.assertEqual(context, "- Likes tea")

    async def test_mem0_explicit_add_reports_queued_event(self) -> None:
        with (
            patch.object(memory, "provider_name", new=AsyncMock(return_value="mem0")),
            patch.object(memory, "user_id", return_value="user-1"),
            patch.object(
                memory.mem0,
                "add",
                new=AsyncMock(return_value={"event_id": "evt-1"}),
            ),
        ):
            result = await memory.add("Likes tea")

        self.assertEqual(result.provider, "mem0")
        self.assertEqual(result.ids, ("evt-1",))
        self.assertTrue(result.queued)
