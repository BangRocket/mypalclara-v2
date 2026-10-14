"""Long-term memory tools backed by the active memory provider.

Gives the agent direct access to the user's durable memories: semantic
search, listing, and explicit add/update/delete. Every tool degrades
gracefully — when no provider is reachable or an operation errors,
the tool returns a friendly message instead of raising.
"""

import logging

from langchain_core.tools import tool

from backend import memory as memory_backend

log = logging.getLogger(__name__)

_UNAVAILABLE = (
    "Long-term memory is currently unavailable (the memory server could not "
    "be reached). Let the user know and continue without it."
)

_MAX_CONTENT = 300
_LIST_CAP = 50


def _truncate(content: str) -> str:
    if len(content) > _MAX_CONTENT:
        return content[:_MAX_CONTENT] + "..."
    return content


@tool
async def memory_search(query: str, limit: int = 10) -> str:
    """Semantically search the user's long-term memories. Use this to recall
    durable facts about the user (preferences, projects, people, past
    decisions) that may not be in the current conversation."""
    if not query:
        return "Error: No search query provided."
    try:
        memories = await memory_backend.search(query=query, limit=limit)
    except Exception as exc:
        log.warning("Memory search failed: %s", exc)
        return _UNAVAILABLE
    if not memories:
        return f"No memories found matching '{query}'."
    lines = [f"Found {len(memories)} matching memory/memories:"]
    for memory in memories:
        lines.append(
            f"- [{memory.score:.2f}] `{memory.id}`: {_truncate(memory.content)}"
        )
    return "\n".join(lines)


@tool
async def memory_list(limit: int = 20) -> str:
    """List the user's stored long-term memories, most relevant first. Use
    this to show the user what is remembered about them."""
    limit = min(limit, _LIST_CAP)
    try:
        memories = await memory_backend.get_all(limit=limit)
    except Exception as exc:
        log.warning("Memory list failed: %s", exc)
        return _UNAVAILABLE
    if not memories:
        return "No memories stored for this user."
    lines = [f"Memories ({len(memories)}):"]
    for memory in memories:
        lines.append(f"- `{memory.id}` ({memory.memory_type}): {_truncate(memory.content)}")
    return "\n".join(lines)


@tool
async def memory_add(content: str) -> str:
    """Store a durable fact about the user in long-term memory (e.g. a
    preference, name, project detail). Use when the user asks you to remember
    something or states a fact worth keeping across conversations."""
    if not content:
        return "Error: No content provided."
    try:
        result = await memory_backend.add(content)
    except Exception as exc:
        log.warning("Memory add failed: %s", exc)
        return _UNAVAILABLE
    ids = ", ".join(f"`{memory_id}`" for memory_id in result.ids) or "pending id"
    action = "queued" if result.queued else "stored"
    return f"Memory {action} via {result.provider} ({ids}): {_truncate(content)}"


@tool
async def memory_update(memory_id: str, content: str) -> str:
    """Update the content of an existing long-term memory by its id. Use to
    correct or refresh a fact that has changed. Get ids from memory_search
    or memory_list."""
    if not memory_id or not content:
        return "Error: memory_id and content are required."
    try:
        memory = await memory_backend.update(memory_id, content)
    except Exception as exc:
        log.warning("Memory update failed: %s", exc)
        return _UNAVAILABLE
    return f"Memory `{memory.id}` updated: {_truncate(memory.content)}"


@tool
async def memory_delete(memory_id: str) -> str:
    """Delete a long-term memory by its id. Use when the user asks to forget
    something. Get ids from memory_search or memory_list."""
    if not memory_id:
        return "Error: memory_id is required."
    try:
        await memory_backend.delete(memory_id)
    except Exception as exc:
        log.warning("Memory delete failed: %s", exc)
        return _UNAVAILABLE
    return f"Memory `{memory_id}` deleted."


TOOLS = [memory_search, memory_list, memory_add, memory_update, memory_delete]
