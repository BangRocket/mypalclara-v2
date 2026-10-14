"""Obsidian vault tools backed by the official desktop CLI.

The CLI is optional and is only registered by ``load_tools`` when its binary
is available. Commands use ``create_subprocess_exec`` rather than a shell so
note paths and content cannot be interpreted as shell syntax.

Configuration:

``OBSIDIAN_CLI``
    CLI binary name or absolute path (default: ``obsidian``).
``OBSIDIAN_VAULT``
    Optional vault name or ID. Passed as the CLI's first ``vault=`` argument.
``OBSIDIAN_VAULT_PATH``
    Optional local vault directory used as the subprocess working directory.
``OBSIDIAN_CLI_TIMEOUT``
    Command timeout in seconds (default: 30).
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path, PurePosixPath

from dotenv import load_dotenv
from langchain_core.tools import tool

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

log = logging.getLogger(__name__)

CLI_COMMAND = os.environ.get("OBSIDIAN_CLI", "obsidian").strip() or "obsidian"
VAULT = os.environ.get("OBSIDIAN_VAULT", "").strip()
VAULT_PATH = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
try:
    CLI_TIMEOUT = max(1, int(os.environ.get("OBSIDIAN_CLI_TIMEOUT", "30")))
except ValueError:
    CLI_TIMEOUT = 30
MAX_OUTPUT = 50_000


def _cli_path() -> str | None:
    """Resolve the configured CLI binary without invoking it."""
    configured = Path(CLI_COMMAND).expanduser()
    if configured.is_absolute():
        return (
            str(configured)
            if configured.is_file() and os.access(configured, os.X_OK)
            else None
        )
    return shutil.which(CLI_COMMAND)


def cli_available() -> bool:
    """Return whether the configured Obsidian CLI exists on PATH."""
    return _cli_path() is not None


def _safe_vault_path(path: str, *, allow_empty: bool = False) -> str | None:
    """Validate an exact vault-relative path used by Obsidian's CLI."""
    path = path.strip()
    if not path:
        return "" if allow_empty else None
    if path.startswith(("/", "~")) or "\\" in path:
        return None
    parts = PurePosixPath(path).parts
    if any(part in {".", ".."} for part in parts):
        return None
    return path


def _cli_content(content: str) -> str:
    """Encode multiline content using the syntax documented by Obsidian."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", "\\n").replace("\t", "\\t")


async def _run_cli(*arguments: str, success_message: str = "") -> str:
    """Run one CLI command and return stdout or a friendly error string."""
    executable = _cli_path()
    if executable is None:
        return (
            f"Obsidian CLI is unavailable. Install or configure '{CLI_COMMAND}' "
            "and restart Clara."
        )

    command = [executable]
    if VAULT:
        command.append(f"vault={VAULT}")
    command.extend(arguments)

    cwd: str | None = None
    if VAULT_PATH:
        vault_dir = Path(VAULT_PATH).expanduser()
        if not vault_dir.is_dir():
            return f"Obsidian vault directory is unavailable: {vault_dir}"
        cwd = str(vault_dir)

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=max(1, CLI_TIMEOUT)
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return f"Obsidian CLI timed out after {CLI_TIMEOUT} seconds."
    except Exception as exc:
        log.warning("Obsidian CLI invocation failed: %s", exc)
        return f"Obsidian CLI is unavailable: {exc}"

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        detail = stderr_text or stdout_text or f"exit code {process.returncode}"
        return f"Obsidian CLI error: {detail[:MAX_OUTPUT]}"

    output = stdout_text or stderr_text
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + "\n... (truncated)"
    return output or success_message or "Obsidian command completed successfully."


@tool
async def obsidian_list_files(folder: str = "", extension: str = "md") -> str:
    """List files in the configured Obsidian vault.

    Args:
        folder: Optional vault-relative folder to search within.
        extension: Optional file extension filter (default: md).
    """
    safe_folder = _safe_vault_path(folder, allow_empty=True)
    if safe_folder is None:
        return "Error: folder must be a vault-relative path."
    extension = extension.lstrip(".")
    if extension and not extension.replace("-", "").isalnum():
        return "Error: extension contains invalid characters."
    arguments = ["files"]
    if safe_folder:
        arguments.append(f"folder={safe_folder}")
    if extension:
        arguments.append(f"ext={extension}")
    return await _run_cli(*arguments)


@tool
async def obsidian_list_folders(folder: str = "") -> str:
    """List folders in the configured Obsidian vault.

    Args:
        folder: Optional vault-relative parent folder.
    """
    safe_folder = _safe_vault_path(folder, allow_empty=True)
    if safe_folder is None:
        return "Error: folder must be a vault-relative path."
    arguments = ["folders"]
    if safe_folder:
        arguments.append(f"folder={safe_folder}")
    return await _run_cli(*arguments)


@tool
async def obsidian_read(path: str) -> str:
    """Read a note by its exact vault-relative path.

    Args:
        path: Exact path from the vault root, such as Projects/Plan.md.
    """
    safe_path = _safe_vault_path(path)
    if safe_path is None:
        return "Error: path must be a vault-relative path."
    return await _run_cli("read", f"path={safe_path}")


@tool
async def obsidian_search(query: str, folder: str = "", limit: int = 20) -> str:
    """Search note contents and return matching paths, lines, and excerpts.

    Args:
        query: Text to search for.
        folder: Optional vault-relative folder to limit the search.
        limit: Maximum matching files to return (default 20, max 100).
    """
    if not query.strip():
        return "Error: query is required."
    safe_folder = _safe_vault_path(folder, allow_empty=True)
    if safe_folder is None:
        return "Error: folder must be a vault-relative path."
    arguments = ["search:context", f"query={query}", f"limit={max(1, min(limit, 100))}"]
    if safe_folder:
        arguments.append(f"path={safe_folder}")
    return await _run_cli(*arguments)


@tool
async def obsidian_create(path: str, content: str = "", overwrite: bool = False) -> str:
    """Create a note, optionally replacing an existing note in overwrite mode.

    Args:
        path: Exact vault-relative destination path.
        content: Initial markdown content.
        overwrite: Replace an existing file when true (default false).
    """
    safe_path = _safe_vault_path(path)
    if safe_path is None:
        return "Error: path must be a vault-relative path."
    arguments = ["create", f"path={safe_path}", f"content={_cli_content(content)}"]
    if overwrite:
        arguments.append("overwrite")
    return await _run_cli(*arguments, success_message=f"Created {safe_path}.")


@tool
async def obsidian_append(path: str, content: str) -> str:
    """Append markdown content to a note, creating the note when needed.

    Args:
        path: Exact vault-relative note path.
        content: Markdown content to append.
    """
    safe_path = _safe_vault_path(path)
    if safe_path is None:
        return "Error: path must be a vault-relative path."
    if not content:
        return "Error: content is required."
    return await _run_cli(
        "append",
        f"path={safe_path}",
        f"content={_cli_content(content)}",
        success_message=f"Appended to {safe_path}.",
    )


@tool
async def obsidian_delete(path: str) -> str:
    """Move a note to the Obsidian trash. Permanent deletion is not exposed.

    Args:
        path: Exact vault-relative note path.
    """
    safe_path = _safe_vault_path(path)
    if safe_path is None:
        return "Error: path must be a vault-relative path."
    return await _run_cli(
        "delete", f"path={safe_path}", success_message=f"Moved {safe_path} to trash."
    )


@tool
async def obsidian_open(path: str) -> str:
    """Open a note in the user's Obsidian app.

    Args:
        path: Exact vault-relative note path.
    """
    safe_path = _safe_vault_path(path)
    if safe_path is None:
        return "Error: path must be a vault-relative path."
    return await _run_cli(
        "open", f"path={safe_path}", success_message=f"Opened {safe_path}."
    )


@tool
async def obsidian_tags() -> str:
    """List tags in the configured vault with their usage counts."""
    return await _run_cli("tags", "counts")


@tool
async def obsidian_list_commands(filter_prefix: str = "") -> str:
    """List Obsidian command IDs, optionally filtered by an ID prefix.

    Args:
        filter_prefix: Optional command ID prefix, such as editor:.
    """
    arguments = ["commands"]
    if filter_prefix:
        arguments.append(f"filter={filter_prefix}")
    return await _run_cli(*arguments)


@tool
async def obsidian_execute_command(command_id: str) -> str:
    """Execute an Obsidian command by ID. This may change the app or vault.

    Args:
        command_id: Command ID obtained from obsidian_list_commands.
    """
    if not command_id.strip():
        return "Error: command_id is required."
    return await _run_cli(
        "command",
        f"id={command_id}",
        success_message=f"Executed Obsidian command: {command_id}",
    )


@tool
async def obsidian_daily_read() -> str:
    """Read today's daily note from the configured vault."""
    return await _run_cli("daily:read")


@tool
async def obsidian_daily_append(content: str) -> str:
    """Append markdown content to today's daily note.

    Args:
        content: Markdown content to append.
    """
    if not content:
        return "Error: content is required."
    return await _run_cli(
        "daily:append",
        f"content={_cli_content(content)}",
        success_message="Appended to today's daily note.",
    )


TOOLS = [
    obsidian_list_files,
    obsidian_list_folders,
    obsidian_read,
    obsidian_search,
    obsidian_create,
    obsidian_append,
    obsidian_delete,
    obsidian_open,
    obsidian_tags,
    obsidian_list_commands,
    obsidian_execute_command,
    obsidian_daily_read,
    obsidian_daily_append,
]
