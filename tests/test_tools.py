import asyncio
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.tools import (
    chat_history,
    files,
    load_tools,
    memory,
    obsidian,
    process,
    terminal,
    workspace,
)


def _message(message_id: int, role: str, content: str, source: str = "console"):
    return SimpleNamespace(
        id=message_id,
        role=role,
        content=content,
        source=source,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


class TerminalToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._original_directory = terminal._current_directory
        self._tmp = tempfile.TemporaryDirectory()
        terminal._current_directory = Path(self._tmp.name).resolve()
        terminal._command_history.clear()

    def tearDown(self) -> None:
        terminal._current_directory = self._original_directory
        terminal._command_history.clear()
        self._tmp.cleanup()

    async def test_run_command_returns_output(self) -> None:
        result = await terminal.run_command.ainvoke({"command": "echo hello"})

        self.assertIn("hello", result)

    async def test_dangerous_command_is_blocked(self) -> None:
        result = await terminal.run_command.ainvoke({"command": "rm -rf /"})

        self.assertIn("blocked", result.lower())

    async def test_change_directory_updates_current_directory(self) -> None:
        subdir = Path(self._tmp.name) / "sub"
        subdir.mkdir()

        result = await terminal.change_directory.ainvoke({"path": str(subdir)})

        self.assertIn("Changed directory", result)
        current = await terminal.get_current_directory.ainvoke({})
        self.assertEqual(current, str(subdir.resolve()))

    async def test_write_then_read_file_round_trip(self) -> None:
        written = await terminal.write_file.ainvoke(
            {"path": "notes.txt", "content": "hello world"}
        )
        self.assertIn("Wrote", written)

        content = await terminal.read_file.ainvoke({"path": "notes.txt"})

        self.assertEqual(content, "hello world")

    async def test_command_history_is_recorded(self) -> None:
        await terminal.run_command.ainvoke({"command": "echo one"})
        await terminal.run_command.ainvoke({"command": "echo two"})

        history = await terminal.get_command_history.ainvoke({"count": 10})

        self.assertIn("echo one", history)
        self.assertIn("echo two", history)

    async def test_invalid_write_mode_is_rejected(self) -> None:
        result = await terminal.write_file.ainvoke(
            {"path": "notes.txt", "content": "x", "mode": "replace"}
        )

        self.assertIn("mode must be", result)


class ProcessToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        for name in list(process._processes):
            await process.process_stop.ainvoke({"name": name, "force": True})

    async def test_quick_process_captures_output(self) -> None:
        started = await process.process_start.ainvoke(
            {"command": "echo done", "name": "test-echo"}
        )
        self.assertIn("Started process 'test-echo'", started)

        status = ""
        for _ in range(100):
            status = await process.process_status.ainvoke({"name": "test-echo"})
            if "exited" in status:
                break
            await asyncio.sleep(0.1)

        self.assertIn("exited", status)
        output = await process.process_output.ainvoke({"name": "test-echo"})
        self.assertIn("done", output)

    async def test_sleep_process_is_listed_then_stopped(self) -> None:
        await process.process_start.ainvoke(
            {"command": "sleep 60", "name": "test-sleep"}
        )

        listing = await process.process_list.ainvoke({})
        self.assertIn("test-sleep", listing)

        stopped = await process.process_stop.ainvoke({"name": "test-sleep"})
        self.assertIn("terminated", stopped)
        self.assertNotIn("test-sleep", process._processes)


class WorkspaceToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self._tmp.name)
        (self.workspace_dir / "SOUL.md").write_text("soul content", encoding="utf-8")
        self._dir_patch = patch.object(workspace, "WORKSPACE_DIR", self.workspace_dir)
        self._dir_patch.start()

    def tearDown(self) -> None:
        self._dir_patch.stop()
        self._tmp.cleanup()

    async def test_workspace_list_includes_soul(self) -> None:
        result = await workspace.workspace_list.ainvoke({})

        self.assertIn("SOUL.md", result)
        self.assertIn("read-only", result)

    async def test_workspace_read_reads_soul(self) -> None:
        result = await workspace.workspace_read.ainvoke({"filename": "SOUL.md"})

        self.assertIn("soul content", result)

    async def test_writing_soul_is_rejected(self) -> None:
        result = await workspace.workspace_write.ainvoke(
            {"filename": "SOUL.md", "content": "nope"}
        )
        self.assertIn("read-only", result)

        created = await workspace.workspace_create.ainvoke({"filename": "SOUL.md"})
        self.assertIn("reserved", created)

    async def test_create_append_read_round_trip(self) -> None:
        created = await workspace.workspace_create.ainvoke(
            {"filename": "NOTES.md", "content": "line one"}
        )
        self.assertIn("Created", created)

        duplicate = await workspace.workspace_create.ainvoke({"filename": "NOTES.md"})
        self.assertIn("already exists", duplicate)

        appended = await workspace.workspace_write.ainvoke(
            {"filename": "NOTES.md", "content": "line two", "mode": "append"}
        )
        self.assertIn("Appended", appended)

        content = await workspace.workspace_read.ainvoke({"filename": "NOTES.md"})
        self.assertIn("line one", content)
        self.assertIn("line two", content)

    async def test_path_traversal_is_rejected(self) -> None:
        for tool_obj, args in (
            (workspace.workspace_read, {"filename": "../x.md"}),
            (workspace.workspace_write, {"filename": "../x.md", "content": "x"}),
            (workspace.workspace_create, {"filename": "../x.md"}),
        ):
            result = await tool_obj.ainvoke(args)
            self.assertIn("Invalid filename", result)

    async def test_invalid_write_mode_is_rejected(self) -> None:
        (self.workspace_dir / "NOTES.md").write_text("original", encoding="utf-8")

        result = await workspace.workspace_write.ainvoke(
            {"filename": "NOTES.md", "content": "x", "mode": "replace"}
        )

        self.assertIn("mode must be", result)


class FilesToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        manager = files.LocalFileManager(Path(self._tmp.name))
        self._manager_patch = patch.object(files, "_manager", manager)
        self._user_patch = patch(
            "backend.conversation.current_user_id", return_value="test-user"
        )
        self._manager_patch.start()
        self._user_patch.start()

    def tearDown(self) -> None:
        self._user_patch.stop()
        self._manager_patch.stop()
        self._tmp.cleanup()

    async def test_save_list_read_delete_round_trip(self) -> None:
        saved = await files.save_to_local.ainvoke(
            {"filename": "notes.txt", "content": "persistent data"}
        )
        self.assertIn("Saved", saved)

        listing = await files.list_local_files.ainvoke({})
        self.assertIn("notes.txt", listing)

        content = await files.read_local_file.ainvoke({"filename": "notes.txt"})
        self.assertEqual(content, "persistent data")

        deleted = await files.delete_local_file.ainvoke({"filename": "notes.txt"})
        self.assertIn("Deleted", deleted)

        listing = await files.list_local_files.ainvoke({})
        self.assertNotIn("notes.txt", listing)

    async def test_filename_sanitization_rejects_traversal(self) -> None:
        saved = await files.save_to_local.ainvoke(
            {"filename": "../evil.txt", "content": "x"}
        )
        self.assertIn("Invalid filename", saved)

        self.assertFalse((Path(self._tmp.name) / "evil.txt").exists())
        user_dir = Path(self._tmp.name) / "test-user"
        self.assertFalse(user_dir.exists())

class ChatHistoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_formats_role_and_content(self) -> None:
        rows = [
            _message(1, "user", "hello there"),
            _message(2, "assistant", "hi back"),
        ]
        with (
            patch("backend.conversation.current_user_id", return_value="user-1"),
            patch("backend.chat_store.search_messages", return_value=rows),
        ):
            result = await chat_history.search_chat_history.ainvoke({"query": "hello"})

        self.assertIn("Found 2 matching message(s)", result)
        self.assertIn("user: hello there", result)
        self.assertIn("assistant: hi back", result)

    async def test_search_without_match_says_so(self) -> None:
        with (
            patch("backend.conversation.current_user_id", return_value="user-1"),
            patch("backend.chat_store.search_messages", return_value=[]),
        ):
            result = await chat_history.search_chat_history.ainvoke({"query": "nope"})

        self.assertIn("No messages found", result)

    async def test_get_history_formats_role_and_content(self) -> None:
        rows = [
            _message(1, "user", "first"),
            _message(2, "assistant", "second", source="discord"),
        ]
        with (
            patch("backend.conversation.current_user_id", return_value="user-1"),
            patch(
                "backend.conversation.current_conversation_id",
                return_value="conversation-1",
            ),
            patch("backend.chat_store.list_messages", return_value=rows),
        ):
            result = await chat_history.get_chat_history.ainvoke({"limit": 5})

        self.assertIn("Chat history (2 messages)", result)
        self.assertIn("user: first", result)
        self.assertIn("(discord) assistant: second", result)

    async def test_get_history_degrades_when_database_is_unavailable(self) -> None:
        with (
            patch("backend.conversation.current_user_id", return_value="user-1"),
            patch(
                "backend.conversation.current_conversation_id",
                side_effect=ConnectionError("database down"),
            ),
        ):
            result = await chat_history.get_chat_history.ainvoke({})

        self.assertIn("unavailable", result)


class MemoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_degrade_when_memory_is_unavailable(self) -> None:
        with patch(
            "backend.memory.provider_name",
            new=AsyncMock(return_value=None),
        ):
            for tool_obj, args in (
                (memory.memory_search, {"query": "anything"}),
                (memory.memory_list, {}),
                (memory.memory_add, {"content": "a fact"}),
                (memory.memory_update, {"memory_id": "mem-1", "content": "updated"}),
                (memory.memory_delete, {"memory_id": "mem-1"}),
            ):
                result = await tool_obj.ainvoke(args)
                self.assertIn("unavailable", result, tool_obj.name)


class ObsidianToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_uses_vault_first_and_exact_path(self) -> None:
        fake_process = SimpleNamespace(
            communicate=AsyncMock(return_value=(b"# Note\n", b"")),
            returncode=0,
        )
        with (
            patch.object(obsidian, "VAULT", "Notes"),
            patch.object(obsidian, "VAULT_PATH", ""),
            patch.object(obsidian, "_cli_path", return_value="/usr/bin/obsidian"),
            patch(
                "backend.tools.obsidian.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=fake_process),
            ) as create_process,
        ):
            result = await obsidian.obsidian_read.ainvoke(
                {"path": "Projects/Plan.md"}
            )

        self.assertEqual(result, "# Note")
        create_process.assert_awaited_once()
        args = create_process.await_args.args
        self.assertEqual(
            args[:4],
            (
                "/usr/bin/obsidian",
                "vault=Notes",
                "read",
                "path=Projects/Plan.md",
            ),
        )

    async def test_create_encodes_multiline_content(self) -> None:
        fake_process = SimpleNamespace(
            communicate=AsyncMock(return_value=(b"", b"")),
            returncode=0,
        )
        with (
            patch.object(obsidian, "VAULT", "Notes"),
            patch.object(obsidian, "VAULT_PATH", ""),
            patch.object(obsidian, "_cli_path", return_value="obsidian"),
            patch(
                "backend.tools.obsidian.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=fake_process),
            ) as create_process,
        ):
            result = await obsidian.obsidian_create.ainvoke(
                {"path": "New.md", "content": "# Title\n\nBody", "overwrite": True}
            )

        self.assertEqual(result, "Created New.md.")
        self.assertIn("content=# Title\\n\\nBody", create_process.await_args.args)
        self.assertIn("overwrite", create_process.await_args.args)

    async def test_path_traversal_is_rejected(self) -> None:
        with patch(
            "backend.tools.obsidian.asyncio.create_subprocess_exec",
            new=AsyncMock(),
        ) as create_process:
            result = await obsidian.obsidian_read.ainvoke({"path": "../Secrets.md"})

        self.assertIn("vault-relative", result)
        create_process.assert_not_awaited()

    async def test_cli_errors_are_friendly(self) -> None:
        fake_process = SimpleNamespace(
            communicate=AsyncMock(return_value=(b"", b"Vault not found.")),
            returncode=1,
        )
        with (
            patch.object(obsidian, "VAULT", "Missing"),
            patch.object(obsidian, "VAULT_PATH", ""),
            patch.object(obsidian, "_cli_path", return_value="obsidian"),
            patch(
                "backend.tools.obsidian.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=fake_process),
            ),
        ):
            result = await obsidian.obsidian_list_files.ainvoke({})

        self.assertEqual(result, "Obsidian CLI error: Vault not found.")

    def test_expected_tools_are_exposed(self) -> None:
        names = {tool_obj.name for tool_obj in obsidian.TOOLS}
        self.assertEqual(
            names,
            {
                "obsidian_list_files",
                "obsidian_list_folders",
                "obsidian_read",
                "obsidian_search",
                "obsidian_create",
                "obsidian_append",
                "obsidian_delete",
                "obsidian_open",
                "obsidian_tags",
                "obsidian_list_commands",
                "obsidian_execute_command",
                "obsidian_daily_read",
                "obsidian_daily_append",
            },
        )


class LoadToolsTests(unittest.TestCase):
    def test_load_tools_includes_core_tool_names(self) -> None:
        names = {tool_obj.name for tool_obj in load_tools()}

        expected = {
            "run_command",
            "get_command_history",
            "get_current_directory",
            "change_directory",
            "list_directory",
            "read_file",
            "write_file",
            "process_start",
            "process_status",
            "process_output",
            "process_stop",
            "process_list",
            "workspace_list",
            "workspace_read",
            "workspace_write",
            "workspace_create",
            "search_chat_history",
            "get_chat_history",
            "save_to_local",
            "list_local_files",
            "read_local_file",
            "delete_local_file",
            "memory_search",
            "memory_list",
            "memory_add",
            "memory_update",
            "memory_delete",
        }
        self.assertTrue(expected.issubset(names))

    def test_load_tools_omits_browser_tools_without_playwright(self) -> None:
        with patch.dict(sys.modules, {"playwright": None}):
            names = {tool_obj.name for tool_obj in load_tools()}

        self.assertFalse(any(name.startswith("browser_") for name in names))

    def test_load_tools_omits_obsidian_tools_without_cli(self) -> None:
        with patch("backend.tools.obsidian.cli_available", return_value=False):
            names = {tool_obj.name for tool_obj in load_tools()}

        self.assertFalse(any(name.startswith("obsidian_") for name in names))


if __name__ == "__main__":
    unittest.main()
