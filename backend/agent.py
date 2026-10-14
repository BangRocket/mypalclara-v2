"""LangChain-backed chat agent for the front end.

Configuration comes from the .env file at the project root:
CHAT_MODEL, ANTHROPIC_BASE_URL, and ANTHROPIC_API_KEY.

A single thread_id is kept for the process so the conversation
has memory across messages. Long-term semantic memory comes from the
configured provider via backend/memory.py; the agent works without it if no
provider is available.
"""

import asyncio
import os
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, dynamic_prompt
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import InMemorySaver

from backend import memory, palace
from backend.tools import load_tools

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

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


class AgentContext(TypedDict, total=False):
    """Ephemeral context used to build a single system prompt per request."""

    memory_context: str


def _read_markdown(path: Path) -> str:
    """Read a prompt fragment and identify its source in the final prompt."""
    return f"# {path.name}\n\n{path.read_text(encoding='utf-8').strip()}"


def _build_system_prompt() -> str:
    """Load SOUL.md as the personality and other workspace files as context."""
    personality_path = WORKSPACE_DIR / PRIMARY_PERSONALITY_FILE
    if not personality_path.is_file():
        raise FileNotFoundError(
            f"Primary personality file not found: {personality_path}"
        )

    workspace_paths = {path.name: path for path in WORKSPACE_DIR.glob("*.md")}
    workspace_paths.pop(PRIMARY_PERSONALITY_FILE, None)
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


@dynamic_prompt
def _system_prompt(request: ModelRequest[AgentContext]) -> str:
    """Merge workspace instructions and retrieved memory into one prompt."""
    context = request.runtime.context or {}
    memory_context = (context.get("memory_context") or "").strip()
    if not memory_context:
        return BASE_SYSTEM_PROMPT
    return (
        BASE_SYSTEM_PROMPT
        + "\n\n# Relevant Long-Term Memory\n\n"
        + "Use these memories naturally when relevant; do not recite them.\n\n"
        + memory_context
    )


model = init_chat_model(
    model=os.environ["CHAT_MODEL"],
    model_provider="anthropic",
    base_url=os.environ["ANTHROPIC_BASE_URL"],
    api_key=os.environ["ANTHROPIC_API_KEY"],
)

agent = create_agent(
    model=model,
    tools=load_tools(),
    middleware=[_system_prompt],
    context_schema=AgentContext,
    checkpointer=InMemorySaver(),
)

config = {"configurable": {"thread_id": "frontend-session"}}

_history_seeded = False
_reply_lock = asyncio.Lock()


async def _seed_history() -> None:
    """One-time per process: load the ongoing Palace session's recent
    messages into the checkpointer so the conversation continues where the
    last app run left off. No-op when Palace is down (empty history)."""
    global _history_seeded
    if _history_seeded:
        return
    _history_seeded = True
    history = await palace.get_recent_messages()
    if history:
        await agent.aupdate_state(config, {"messages": history})


async def get_reply(user_text: str) -> str:
    """Send a user message to the agent and return its reply text."""
    async with _reply_lock:
        await _seed_history()
        await palace.record("user", user_text)

        memory_context = await memory.get_context(user_text)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_text}]},
            config=config,
            context={"memory_context": memory_context or ""},
        )
        content = result["messages"][-1].content
        if isinstance(content, list):  # content blocks -> join text parts
            reply = "".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        else:
            reply = content

        await palace.record("assistant", reply)
        asyncio.create_task(memory.ingest(user_text, reply))
        return reply


async def reset_conversation() -> None:
    """Move subsequent model calls to a fresh in-memory agent thread."""
    global _history_seeded
    async with _reply_lock:
        config["configurable"]["thread_id"] = str(uuid4())
        _history_seeded = False
