import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.tools import tool

from backend import agent, runtime
from backend.copilot_tools import to_copilot_tool


class TurnContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        agent._injected.clear()

    async def test_memory_lines_are_injected_once_per_session(self) -> None:
        memories = AsyncMock(side_effect=["- likes tea\n- has a cat", "- likes tea\n- moved to Ohio"])
        with (
            patch.object(agent.memory, "get_context", memories),
            patch.object(agent, "_dynamic_context", return_value=""),
        ):
            first = await agent.build_turn_context("q1", "s1")
            second = await agent.build_turn_context("q2", "s1")

        self.assertIn("- likes tea", first)
        self.assertIn("- has a cat", first)
        self.assertNotIn("- likes tea", second)
        self.assertIn("- moved to Ohio", second)

    async def test_sessions_track_injections_separately(self) -> None:
        with (
            patch.object(agent.memory, "get_context", AsyncMock(return_value="- likes tea")),
            patch.object(agent, "_dynamic_context", return_value=""),
        ):
            await agent.build_turn_context("q", "s1")
            fresh = await agent.build_turn_context("q", "s2")

        self.assertIn("- likes tea", fresh)

    async def test_persona_state_is_resent_only_when_it_changes(self) -> None:
        states = iter(["# PERSONA_STATE.md\n\nv1", "# PERSONA_STATE.md\n\nv1", "# PERSONA_STATE.md\n\nv2"])
        with (
            patch.object(agent.memory, "get_context", AsyncMock(return_value=None)),
            patch.object(agent, "_dynamic_context", side_effect=lambda: next(states)),
        ):
            first = await agent.build_turn_context("q", "s1")
            unchanged = await agent.build_turn_context("q", "s1")
            changed = await agent.build_turn_context("q", "s1")

        self.assertIn("v1", first)
        self.assertIsNone(unchanged)
        self.assertIn("v2", changed)

    async def test_memory_failure_does_not_block_the_turn(self) -> None:
        with (
            patch.object(agent.memory, "get_context", AsyncMock(side_effect=RuntimeError("down"))),
            patch.object(agent, "_dynamic_context", return_value=""),
        ):
            self.assertIsNone(await agent.build_turn_context("q", "s1"))

    async def test_hook_returns_additional_context(self) -> None:
        with patch.object(agent, "build_turn_context", AsyncMock(return_value="ctx")) as build:
            result = await agent._on_user_prompt_submitted({"prompt": "hi"}, {"session_id": "s9"})

        build.assert_awaited_once_with("hi", "s9")
        self.assertEqual(result, {"additionalContext": "ctx"})


class SessionConfigTests(unittest.TestCase):
    def test_config_uses_byok_and_allowlists_only_custom_tools(self) -> None:
        env = {"CHAT_MODEL": "m", "ANTHROPIC_BASE_URL": "https://proxy", "ANTHROPIC_API_KEY": "k"}
        with patch.dict("os.environ", env), patch.object(agent, "_tools", return_value=[]):
            config = agent.session_config()

        self.assertEqual(config["provider"], {"type": "anthropic", "base_url": "https://proxy", "api_key": "k"})
        self.assertEqual(config["available_tools"].to_list(), ["custom:*"])
        self.assertEqual(config["system_message"]["mode"], "customize")
        self.assertIn("SOUL.md", config["system_message"]["sections"]["identity"]["content"])
        self.assertNotIn("# PERSONA_STATE.md\n", agent.BASE_SYSTEM_PROMPT)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        runtime._sessions.clear()
        self.store: dict = {}
        self.patches = [
            patch.object(runtime.session_store, "load", side_effect=lambda ns, k: self.store.get((ns, k))),
            patch.object(runtime.session_store, "save", side_effect=lambda ns, k, v: self.store.__setitem__((ns, k), v)),
        ]
        for p in self.patches:
            p.start()
        self.copilot = MagicMock()
        self.copilot.create_session = AsyncMock(return_value=SimpleNamespace(name="created", disconnect=AsyncMock()))
        self.copilot.resume_session = AsyncMock(return_value=SimpleNamespace(name="resumed", disconnect=AsyncMock()))
        self.client_patch = patch.object(runtime, "client", AsyncMock(return_value=self.copilot))
        self.client_patch.start()

    def tearDown(self) -> None:
        self.client_patch.stop()
        for p in self.patches:
            p.stop()
        runtime._sessions.clear()

    async def test_new_user_gets_a_created_session_that_is_cached(self) -> None:
        self.copilot.get_session_metadata = AsyncMock(return_value=None)

        first = await runtime.session_for("u1", dict)
        second = await runtime.session_for("u1", dict)

        self.assertEqual(first.name, "created")
        self.assertIs(first, second)
        self.copilot.create_session.assert_awaited_once()

    async def test_known_session_id_is_resumed_after_restart(self) -> None:
        self.store[(runtime.SESSION_NAMESPACE, "u1")] = {"session_id": "clara-u1-abc"}
        self.copilot.get_session_metadata = AsyncMock(return_value=SimpleNamespace())

        session = await runtime.session_for("u1", dict)

        self.assertEqual(session.name, "resumed")
        self.copilot.resume_session.assert_awaited_once_with("clara-u1-abc")

    async def test_rotate_disconnects_and_mints_a_new_id(self) -> None:
        self.copilot.get_session_metadata = AsyncMock(return_value=None)
        session = await runtime.session_for("u1", dict)
        old_id = runtime.session_id_for("u1")

        new_id = await runtime.rotate("u1")

        session.disconnect.assert_awaited_once()
        self.assertNotEqual(old_id, new_id)
        self.assertEqual(runtime.session_id_for("u1"), new_id)

    def test_session_ids_are_filesystem_safe(self) -> None:
        self.assertRegex(runtime._new_session_id("user_2x/Ab:c"), r"^clara-[A-Za-z0-9_-]+$")


class ToolAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_preserves_schema_and_result(self) -> None:
        @tool
        async def add(a: int, b: int = 2) -> str:
            """Add two numbers."""
            return str(a + b)

        adapted = to_copilot_tool(add)
        result = await adapted.handler(SimpleNamespace(arguments={"a": 3}))

        self.assertEqual(adapted.name, "add")
        self.assertEqual(adapted.parameters["required"], ["a"])
        self.assertEqual(result.text_result_for_llm, "5")
        self.assertEqual(result.result_type, "success")

    async def test_adapter_reports_failures_to_the_model(self) -> None:
        @tool
        async def boom() -> str:
            """Always fails."""
            raise ValueError("nope")

        result = await to_copilot_tool(boom).handler(SimpleNamespace(arguments={}))

        self.assertEqual(result.result_type, "failure")
        self.assertIn("nope", result.text_result_for_llm)
