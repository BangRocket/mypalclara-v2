"""Terminal tools for the agent.

Shell command execution and file system operations, ported from v1's
terminal_tool. Commands run in a module-level working directory that
``change_directory`` updates, executions are recorded in a bounded history,
and a regex blocklist rejects dangerous commands (rm -rf /, mkfs, fork
bombs, raw-disk writes). File operations are capped at 10 MB.
"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.tools import tool

MAX_FILE_SIZE = 10 * 1024 * 1024
DEFAULT_TIMEOUT = 30
MAX_HISTORY = 50

DANGEROUS_PATTERNS = [
    r"rm\s+(-[rfRv]+\s+)*(/|/\*|\*/)",  # rm -rf / or rm -rf /*
    r"rm\s+(-[rfRv]+\s+)+\.\.",  # rm -rf ..
    r"mkfs\s",  # mkfs commands
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",  # fork bomb
    r"dd\s+.*of=/dev/(sd|hd|nvme)",  # dd to raw disk
    r">\s*/dev/(sd|hd|nvme)",  # redirect to raw disk
    r"chmod\s+(-R\s+)?777\s+/",  # chmod 777 /
    r"chown\s+(-R\s+)?.*\s+/\s*$",  # chown /
]

_DANGEROUS_COMPILED = [re.compile(p, re.IGNORECASE) for p in DANGEROUS_PATTERNS]


def _is_dangerous_command(command: str) -> tuple[bool, str]:
    for pattern in _DANGEROUS_COMPILED:
        if pattern.search(command):
            return True, f"Command matches dangerous pattern: {pattern.pattern}"
    return False, ""


@dataclass
class CommandHistoryEntry:
    """A single command execution record."""

    command: str
    timestamp: datetime
    exit_code: int | None
    duration_ms: int
    stdout_preview: str
    stderr_preview: str
    success: bool


_command_history: list[CommandHistoryEntry] = []
_current_directory: Path = Path.cwd()


def _add_to_history(entry: CommandHistoryEntry) -> None:
    global _command_history
    _command_history.append(entry)
    if len(_command_history) > MAX_HISTORY:
        _command_history = _command_history[-MAX_HISTORY:]


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _resolve(path: str) -> Path:
    if not os.path.isabs(path):
        return (_current_directory / path).resolve()
    return Path(path).resolve()


@tool
async def run_command(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Execute a shell command and return its combined stdout/stderr.

    Args:
        command: The command to execute.
        timeout: Timeout in seconds (default 30). Dangerous commands
            (rm -rf /, mkfs, fork bombs) are blocked for security.
    """
    if not command:
        return "Error: command is required"

    if not isinstance(timeout, int) or timeout < 1:
        timeout = DEFAULT_TIMEOUT

    is_dangerous, reason = _is_dangerous_command(command)
    if is_dangerous:
        return f"Error: Command blocked for security reasons. {reason}"

    start_time = time.time()

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_current_directory),
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            _add_to_history(
                CommandHistoryEntry(
                    command=command,
                    timestamp=datetime.now(UTC),
                    exit_code=None,
                    duration_ms=int((time.time() - start_time) * 1000),
                    stdout_preview="",
                    stderr_preview="",
                    success=False,
                )
            )
            return f"Error: Command timed out after {timeout} seconds"

        duration_ms = int((time.time() - start_time) * 1000)
        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")

        _add_to_history(
            CommandHistoryEntry(
                command=command,
                timestamp=datetime.now(UTC),
                exit_code=process.returncode,
                duration_ms=duration_ms,
                stdout_preview=stdout_str[:500],
                stderr_preview=stderr_str[:500],
                success=process.returncode == 0,
            )
        )

        result_parts = []
        if stdout_str:
            result_parts.append(stdout_str)
        if stderr_str:
            if result_parts:
                result_parts.append(f"\n--- stderr ---\n{stderr_str}")
            else:
                result_parts.append(stderr_str)

        result = "".join(result_parts) if result_parts else "(no output)"

        if process.returncode != 0:
            result = f"{result}\n\n[Exit code: {process.returncode}]"

        return result

    except Exception as e:
        _add_to_history(
            CommandHistoryEntry(
                command=command,
                timestamp=datetime.now(UTC),
                exit_code=None,
                duration_ms=int((time.time() - start_time) * 1000),
                stdout_preview="",
                stderr_preview=str(e)[:500],
                success=False,
            )
        )
        return f"Error executing command: {e}"


@tool
async def get_command_history(count: int = 10) -> str:
    """Retrieve recent command execution history with timestamps and status.

    Args:
        count: Number of entries to retrieve (default 10, max 50).
    """
    if not isinstance(count, int) or count < 1:
        count = 10
    count = min(count, MAX_HISTORY)

    if not _command_history:
        return "No command history."

    entries = _command_history[-count:]
    lines = ["**Command History:**"]

    for i, entry in enumerate(entries, 1):
        status = "✅" if entry.success else "❌"
        ts = entry.timestamp.strftime("%H:%M:%S")
        cmd = entry.command[:60] + ("..." if len(entry.command) > 60 else "")
        lines.append(f"{i}. {status} `{cmd}` ({ts}, {entry.duration_ms}ms)")

    return "\n".join(lines)


@tool
async def get_current_directory() -> str:
    """Get the current working directory used by terminal tools."""
    return str(_current_directory)


@tool
async def change_directory(path: str) -> str:
    """Change the current working directory used by terminal tools.

    Args:
        path: Path to change to (absolute or relative to the current directory).
    """
    global _current_directory

    if not path:
        return "Error: path is required"

    new_path = _resolve(path)

    if not new_path.exists():
        return f"Error: Directory does not exist: {new_path}"
    if not new_path.is_dir():
        return f"Error: Not a directory: {new_path}"

    _current_directory = new_path
    return f"Changed directory to: {_current_directory}"


@tool
async def list_directory(path: str = "") -> str:
    """List files and directories in the given path or the current directory.

    Args:
        path: Path to list (default: current directory).
    """
    target = _resolve(path) if path else _current_directory

    if not target.exists():
        return f"Error: Path does not exist: {target}"
    if not target.is_dir():
        return f"Error: Not a directory: {target}"

    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))

        lines = [f"**Contents of {target}:**"]
        for entry in entries:
            if entry.is_dir():
                lines.append(f"  📁 {entry.name}/")
            elif entry.is_symlink():
                lines.append(f"  🔗 {entry.name} -> {entry.resolve()}")
            else:
                lines.append(f"  📄 {entry.name} ({_format_size(entry.stat().st_size)})")

        if len(entries) == 0:
            lines.append("  (empty)")

        return "\n".join(lines)

    except PermissionError:
        return f"Error: Permission denied: {target}"
    except Exception as e:
        return f"Error listing directory: {e}"


@tool
async def read_file(
    path: str,
    start_row: int | None = None,
    end_row: int | None = None,
    as_json: bool = False,
) -> str:
    """Read file contents with optional row selection. Files over 10 MB are rejected.

    Args:
        path: Path to the file.
        start_row: Starting row (0-based, optional).
        end_row: Ending row (0-based, inclusive, optional).
        as_json: Parse and pretty-print as JSON (default false).
    """
    if not path:
        return "Error: path is required"

    file_path = _resolve(path)

    if not file_path.exists():
        return f"Error: File does not exist: {file_path}"
    if not file_path.is_file():
        return f"Error: Not a file: {file_path}"

    size = file_path.stat().st_size
    if size > MAX_FILE_SIZE:
        return f"Error: File too large ({_format_size(size)}). Maximum is {_format_size(MAX_FILE_SIZE)}."

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")

        if start_row is not None or end_row is not None:
            lines = content.splitlines(keepends=True)
            start = max(0, start_row if start_row is not None else 0)
            end = min(len(lines), (end_row + 1) if end_row is not None else len(lines))
            content = "".join(lines[start:end])

        if as_json:
            try:
                content = json.dumps(json.loads(content), indent=2)
            except json.JSONDecodeError as e:
                return f"Error parsing JSON: {e}"

        return content if content else "(empty file)"

    except Exception as e:
        return f"Error reading file: {e}"


@tool
async def write_file(path: str, content: str, mode: str = "overwrite") -> str:
    """Write content to a file, creating parent directories if needed.

    Args:
        path: Path to the file.
        content: Content to write.
        mode: "overwrite" (default) or "append".
    """
    if not path:
        return "Error: path is required"
    if content is None:
        return "Error: content is required"
    if mode not in {"append", "overwrite"}:
        return "Error: mode must be 'append' or 'overwrite'"

    file_path = _resolve(path)

    content_bytes = content.encode("utf-8")
    if len(content_bytes) > MAX_FILE_SIZE:
        return f"Error: Content too large ({_format_size(len(content_bytes))}). Maximum is {_format_size(MAX_FILE_SIZE)}."

    try:
        if mode == "append" and file_path.exists():
            resulting_size = file_path.stat().st_size + len(content_bytes)
            if resulting_size > MAX_FILE_SIZE:
                return f"Error: Resulting file would exceed {_format_size(MAX_FILE_SIZE)}."

        file_path.parent.mkdir(parents=True, exist_ok=True)

        if mode == "append":
            with file_path.open("a", encoding="utf-8") as f:
                f.write(content)
            action = "Appended to"
        else:
            file_path.write_text(content, encoding="utf-8")
            action = "Wrote"

        return f"{action} {_format_size(len(content_bytes))} to {file_path}"

    except Exception as e:
        return f"Error writing file: {e}"


TOOLS = [
    run_command,
    get_command_history,
    get_current_directory,
    change_directory,
    list_directory,
    read_file,
    write_file,
]
