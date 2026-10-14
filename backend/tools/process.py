"""Process management tools for the agent.

Start, monitor, and stop named background processes, ported from v1's
process_tool. Each process runs via the shell with stdout/stderr merged
into a per-name ring buffer (last 1000 lines) filled by a reader task that
also enforces the process timeout. Stopping sends SIGTERM first and
escalates to SIGKILL after 5 seconds.
"""

import asyncio
import time
from dataclasses import dataclass, field

from langchain_core.tools import tool

OUTPUT_BUFFER_LINES = 1000
DEFAULT_TIMEOUT = 3600
STOP_GRACE_SECONDS = 5.0


@dataclass
class TrackedProcess:
    """A tracked background process."""

    pid: int
    command: str
    started_at: float
    process: asyncio.subprocess.Process
    output_buffer: list[str] = field(default_factory=list)
    cwd: str | None = None
    timeout: int = DEFAULT_TIMEOUT


_processes: dict[str, TrackedProcess] = {}
_output_readers: dict[str, asyncio.Task] = {}


async def _read_output(name: str, proc: asyncio.subprocess.Process, timeout: int) -> None:
    """Read process output into the ring buffer and enforce the timeout."""
    try:
        async with asyncio.timeout(timeout):
            while True:
                if proc.stdout is None:
                    break
                line = await proc.stdout.readline()
                if not line:
                    break
                if name in _processes:
                    buffer = _processes[name].output_buffer
                    buffer.append(line.decode(errors="replace"))
                    if len(buffer) > OUTPUT_BUFFER_LINES:
                        buffer.pop(0)
    except asyncio.TimeoutError:
        proc.terminate()
        if name in _processes:
            _processes[name].output_buffer.append(f"\n[Process timed out after {timeout}s]")
    except Exception as e:
        if name in _processes:
            _processes[name].output_buffer.append(f"\n[Error reading output: {e}]")
    finally:
        await proc.wait()


def _not_found(name: str) -> str:
    available = list(_processes.keys()) if _processes else "none"
    return f"Process '{name}' not found. Available: {available}"


@tool
async def process_start(
    command: str,
    name: str = "",
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Start a background process and track it by name. Use this for
    long-running commands like development servers, builds, or tests.

    Args:
        command: Shell command to execute.
        name: Name to identify this process (auto-generated if not provided).
        cwd: Working directory for the command.
        timeout: Timeout in seconds (default 3600 = 1 hour).
    """
    if not command:
        return "Error: command is required"

    if not name:
        name = f"proc_{int(time.time())}"

    if name in _processes:
        tp = _processes[name]
        if tp.process.returncode is None:
            return f"Error: Process '{name}' is already running (PID {tp.pid}). Use a different name or stop it first."

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
        )

        _processes[name] = TrackedProcess(
            pid=proc.pid,
            command=command,
            started_at=time.time(),
            process=proc,
            cwd=cwd,
            timeout=timeout,
        )

        _output_readers[name] = asyncio.create_task(_read_output(name, proc, timeout))

        return f"Started process '{name}' with PID {proc.pid}\nCommand: {command}"
    except Exception as e:
        return f"Error starting process: {e}"


@tool
async def process_status(name: str) -> str:
    """Get status of a tracked process: running state, elapsed time, and
    output line count.

    Args:
        name: Process name.
    """
    if not name:
        return "Error: name is required"

    if name not in _processes:
        return _not_found(name)

    tp = _processes[name]
    running = tp.process.returncode is None
    elapsed = int(time.time() - tp.started_at)
    status = "running" if running else f"exited (code {tp.process.returncode})"

    return f"""Process: {name}
PID: {tp.pid}
Command: {tp.command}
Working Dir: {tp.cwd or "(default)"}
Status: {status}
Elapsed: {elapsed}s
Output lines: {len(tp.output_buffer)}
Timeout: {tp.timeout}s"""


@tool
async def process_output(name: str, lines: int = 50) -> str:
    """Get recent output lines from a tracked process.

    Args:
        name: Process name.
        lines: Number of lines to return (default 50).
    """
    if not name:
        return "Error: name is required"

    if name not in _processes:
        return _not_found(name)

    output = _processes[name].output_buffer[-lines:]
    if not output:
        return "(no output yet)"

    return "".join(output)


@tool
async def process_stop(name: str, force: bool = False) -> str:
    """Stop a running process (SIGTERM, escalating to SIGKILL if it does
    not exit within 5 seconds).

    Args:
        name: Process name.
        force: Force kill (SIGKILL) instead of terminate (SIGTERM).
    """
    if not name:
        return "Error: name is required"

    if name not in _processes:
        return _not_found(name)

    tp = _processes[name]
    if tp.process.returncode is not None:
        del _processes[name]
        if name in _output_readers:
            _output_readers[name].cancel()
            del _output_readers[name]
        return f"Process '{name}' already finished (exit code {tp.process.returncode})."

    if force:
        tp.process.kill()
        action = "killed"
    else:
        tp.process.terminate()
        action = "terminated"

    try:
        await asyncio.wait_for(tp.process.wait(), timeout=STOP_GRACE_SECONDS)
    except asyncio.TimeoutError:
        if not force:
            tp.process.kill()
            await tp.process.wait()
            action = "force killed (did not respond to terminate)"

    del _processes[name]
    if name in _output_readers:
        _output_readers[name].cancel()
        del _output_readers[name]

    return f"Process '{name}' {action}."


@tool
async def process_list() -> str:
    """List all tracked processes with their status and elapsed time."""
    if not _processes:
        return "No active processes."

    lines = ["Active processes:"]
    for name, tp in _processes.items():
        running = "running" if tp.process.returncode is None else f"stopped ({tp.process.returncode})"
        elapsed = int(time.time() - tp.started_at)
        cmd_short = tp.command[:50] + ("..." if len(tp.command) > 50 else "")
        lines.append(f"  - {name}: PID {tp.pid}, {running}, {elapsed}s elapsed")
        lines.append(f"    Command: {cmd_short}")

    return "\n".join(lines)


TOOLS = [
    process_start,
    process_status,
    process_output,
    process_stop,
    process_list,
]
