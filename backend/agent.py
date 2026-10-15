"""Copilot SDK-backed chat agent for every chat surface.

Configuration comes from the .env file at the project root: CHAT_MODEL,
ANTHROPIC_BASE_URL and ANTHROPIC_API_KEY select the model through BYOK;
REPLY_TIMEOUT (seconds, default 300) bounds one turn.

Conversation state lives in a persistent SDK session per user (see
backend/runtime.py), so a restart resumes where the last turn left off.
Long-term memory comes from the configured provider via backend/memory.py
and is injected per turn by a prompt hook; the agent works without it if no
provider is available.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import TypedDict

from copilot import ToolSet
from copilot.session import PermissionHandler
from dotenv import load_dotenv

from backend import memory, palace, runtime
from backend.copilot_tools import to_copilot_tools
from backend.tools import load_tools

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = BACKEND_DIR / "workspace"
PRIMARY_PERSONALITY_FILE = "SOUL.md"
SUPPLEMENTAL_FILE_ORDER = (
    "AGENTS.md",
    "IDENTITY.md",
    "USER.md",
    "MEMORY.md",
    "TOOLS.md",
)
# Files Clara rewrites during conversations. Sessions now live for weeks and
# the system message is fixed at session creation, so these are injected
# fresh every turn by the prompt hook instead.
DYNAMIC_FILES = ("PERSONA_STATE.md",)
MEMORY_HEADER = (
    "# Relevant Long-Term Memory\n\n"
    "Use these memories naturally when relevant; do not recite them.\n\n"
)
DEFAULT_REPLY_TIMEOUT = 300.0


class AgentContext(TypedDict, total=False):
    """Kept for import compatibility; context is now built by the prompt hook."""

    memory_context: str


def _read_markdown(path: Path) -> str:
    """Read a prompt fragment and identify its source in the final prompt."""
    return f"# {path.name}\n\n{path.read_text(encoding='utf-8').strip()}"


def _build_system_prompt() -> str:
    """Load SOUL.md as the personality and static workspace files as context."""
    personality_path = WORKSPACE_DIR / PRIMARY_PERSONALITY_FILE
    if not personality_path.is_file():
        raise FileNotFoundError(
            f"Primary personality file not found: {personality_path}"
        )

    workspace_paths = {path.name: path for path in WORKSPACE_DIR.glob("*.md")}
    workspace_paths.pop(PRIMARY_PERSONALITY_FILE, None)
    for filename in DYNAMIC_FILES:
        workspace_paths.pop(filename, None)
    supplemental_paths = [
        workspace_paths.pop(filename)
        for filename in SUPPLEMENTAL_FILE_ORDER
        if filename in workspace_paths
    ]
    supplemental_paths.extend(
        workspace_paths[filename] for filename in sorted(workspace_paths)
    )

    sections = ["# Primary Personality\n\n" + _read_markdown(personality_path)]
    if supplemental_paths:
        sections.append(
            "# Supplemental Workspace Context\n\n"
            + "\n\n".join(_read_markdown(path) for path in supplemental_paths)
        )
    return "\n\n".join(sections)


BASE_SYSTEM_PROMPT = _build_system_prompt()


def _dynamic_context() -> str:
    """Read the workspace files Clara maintains herself, as they are now."""
    parts = []
    for filename in DYNAMIC_FILES:
        path = WORKSPACE_DIR / filename
        if path.is_file():
            parts.append(_read_markdown(path))
    return "\n\n".join(parts)


class _InjectedState:
    """What this session's history already carries, so it is not resent.

    The SDK folds ``additionalContext`` into the stored user message, so the
    model sees every earlier turn's injected context again until compaction.
    Injecting only what is new keeps that growth to genuinely new facts.
    In-process only: after a restart, context is injected once more.
    """

    def __init__(self) -> None:
        self.memory_lines: set[str] = set()
        self.dynamic: str | None = None


_injected: dict[str, _InjectedState] = {}


async def build_turn_context(prompt: str, session_id: str = "") -> str | None:
    """Assemble the per-turn context the session has not already seen."""
    state = _injected.setdefault(session_id, _InjectedState())
    sections = []

    dynamic = _dynamic_context()
    if dynamic and dynamic != state.dynamic:
        sections.append(dynamic)
        state.dynamic = dynamic

    try:
        memory_context = await memory.get_context(prompt)
    except Exception:  # noqa: BLE001 - memory must never block a reply
        log.exception("[agent] Memory context failed")
        memory_context = None
    if memory_context:
        new_lines = [
            line
            for line in memory_context.strip().splitlines()
            if line.strip() and line.strip() not in state.memory_lines
        ]
        state.memory_lines.update(line.strip() for line in new_lines)
        if new_lines:
            sections.append(MEMORY_HEADER + "\n".join(new_lines))

    return "\n\n".join(sections) or None


async def _on_user_prompt_submitted(input_data, invocation):
    context = await build_turn_context(
        input_data.get("prompt") or "", invocation.get("session_id", "")
    )
    return {"additionalContext": context} if context else None


def _reply_timeout() -> float:
    try:
        return float(os.environ.get("REPLY_TIMEOUT", DEFAULT_REPLY_TIMEOUT))
    except ValueError:
        return DEFAULT_REPLY_TIMEOUT


_copilot_tools = None


def _tools():
    global _copilot_tools
    if _copilot_tools is None:
        _copilot_tools = to_copilot_tools(load_tools())
    return _copilot_tools


def session_config() -> dict:
    """Build the SDK session options. Read lazily so imports never need .env."""
    return dict(
        model=os.environ["CHAT_MODEL"],
        provider={
            "type": "anthropic",
            "base_url": os.environ["ANTHROPIC_BASE_URL"],
            "api_key": os.environ["ANTHROPIC_API_KEY"],
        },
        system_message={
            "mode": "customize",
            "sections": {
                "identity": {"action": "replace", "content": BASE_SYSTEM_PROMPT},
                "tone": {"action": "remove"},
                "code_change_rules": {"action": "remove"},
                "environment_context": {"action": "remove"},
            },
        },
        tools=_tools(),
        # Empty mode requires an explicit allowlist: Clara's own tools only.
        # SDK built-ins (shell, file edit) are a later phase behind a guard.
        available_tools=ToolSet().add_custom("*"),
        hooks={"on_user_prompt_submitted": _on_user_prompt_submitted},
        on_permission_request=PermissionHandler.approve_all,
    )


_reply_lock = asyncio.Lock()


def _reply_text(event) -> str:
    data = getattr(event, "data", None)
    content = getattr(data, "content", None) if data is not None else None
    return content or ""


async def get_reply(user_text: str) -> str:
    """Send a user message to the agent and return its reply text."""
    async with _reply_lock:
        await palace.record("user", user_text)

        session = await runtime.session_for(memory.user_id(), session_config)
        event = await session.send_and_wait(user_text, timeout=_reply_timeout())
        reply = _reply_text(event)

        await palace.record("assistant", reply)
        asyncio.create_task(memory.ingest(user_text, reply))
        return reply


async def reset_conversation() -> None:
    """Move subsequent turns to a fresh SDK session."""
    async with _reply_lock:
        await runtime.rotate(memory.user_id())


async def shutdown() -> None:
    """Disconnect sessions and stop the SDK client."""
    await runtime.stop()
