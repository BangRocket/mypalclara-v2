"""Long-term memory provider selection and normalized operations.

``MEMORY_PROVIDER=auto`` prefers MyPalace and falls back to Mem0 when Palace
is disabled or unavailable. Explicit ``palace``, ``mem0``, and ``off`` modes
are also supported.
"""

import logging
import os
from dataclasses import dataclass

from backend import mem0, palace

log = logging.getLogger(__name__)


class MemoryUnavailable(RuntimeError):
    """Raised when no configured memory provider is reachable."""


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    content: str
    score: float = 0.0
    memory_type: str = "memory"


@dataclass(frozen=True)
class MemoryWrite:
    provider: str
    ids: tuple[str, ...]
    queued: bool = False


def user_id() -> str:
    """Return the authenticated user, with a configurable local fallback."""
    from backend import auth

    return (
        auth.current_user_id()
        or os.environ.get("MEMORY_USER_ID")
        or os.environ.get("PALACE_USER_ID", "local-user")
    )


def _mode() -> str:
    mode = os.environ.get("MEMORY_PROVIDER", "auto").strip().lower()
    if mode not in {"auto", "palace", "mem0", "off", "none"}:
        log.warning("Unknown MEMORY_PROVIDER=%r; using auto", mode)
        return "auto"
    return mode


async def provider_name(for_user_id: str | None = None) -> str | None:
    """Return the active healthy provider name, or ``None``.

    ``for_user_id`` lets maintenance jobs health-check Mem0 for their explicit
    target identity instead of whichever interactive user is signed in.
    """
    mode = _mode()
    if mode in {"off", "none"}:
        return None
    if mode in {"auto", "palace"} and palace.enabled() and await palace.available():
        return "palace"
    if mode in {"auto", "mem0"} and await mem0.available(
        for_user_id or user_id()
    ):
        return "mem0"
    return None


async def available() -> bool:
    return await provider_name() is not None


def reset_health() -> None:
    palace.reset_health()
    mem0.reset_health()


def _record(value: object) -> MemoryRecord:
    if isinstance(value, dict):
        content = (
            value.get("memory")
            or value.get("text")
            or value.get("content")
            or ""
        )
        categories = value.get("categories") or []
        return MemoryRecord(
            id=str(value.get("id", "")),
            content=str(content),
            score=float(value.get("score") or 0.0),
            memory_type=str(categories[0] if categories else "memory"),
        )
    return MemoryRecord(
        id=str(getattr(value, "id", "")),
        content=str(getattr(value, "content", "")),
        score=float(getattr(value, "score", 0.0) or 0.0),
        memory_type=str(getattr(value, "memory_type", "memory")),
    )


async def search(query: str, limit: int = 10) -> list[MemoryRecord]:
    provider = await provider_name()
    if provider == "palace":
        values = await palace._get_client().search(
            query=query, user_id=user_id(), limit=limit
        )
    elif provider == "mem0":
        values = await mem0.search(query, user_id=user_id(), limit=limit)
    else:
        raise MemoryUnavailable("No memory provider is available")
    return [_record(value) for value in values]


async def get_all(limit: int = 20) -> list[MemoryRecord]:
    provider = await provider_name()
    if provider == "palace":
        values = await palace._get_client().get_all(user_id=user_id(), limit=limit)
    elif provider == "mem0":
        values = await mem0.get_all(user_id=user_id(), limit=limit)
    else:
        raise MemoryUnavailable("No memory provider is available")
    return [_record(value) for value in values]


async def add(content: str) -> MemoryWrite:
    provider = await provider_name()
    if provider == "palace":
        values = await palace._get_client().add(
            messages=[{"role": "user", "content": content}],
            user_id=user_id(),
            source="agent-tool",
            infer=False,
        )
        return MemoryWrite("palace", tuple(str(value.id) for value in values))
    if provider == "mem0":
        result = await mem0.add(
            [{"role": "user", "content": content}],
            user_id=user_id(),
            source="agent-tool",
            infer=False,
        )
        event_id = result.get("event_id")
        return MemoryWrite("mem0", (str(event_id),) if event_id else (), queued=True)
    raise MemoryUnavailable("No memory provider is available")


async def update(memory_id: str, content: str) -> MemoryRecord:
    provider = await provider_name()
    if provider == "palace":
        value = await palace._get_client().update(memory_id, content=content)
    elif provider == "mem0":
        value = await mem0.update(memory_id, content=content)
    else:
        raise MemoryUnavailable("No memory provider is available")
    return _record(value)


async def delete(memory_id: str) -> None:
    provider = await provider_name()
    if provider == "palace":
        await palace._get_client().delete(memory_id)
    elif provider == "mem0":
        await mem0.delete(memory_id)
    else:
        raise MemoryUnavailable("No memory provider is available")


async def get_context(query: str, max_memories: int = 5) -> str | None:
    """Return relevant memories formatted for system-prompt injection."""
    provider = await provider_name()
    if provider == "palace":
        return await palace.get_context(query, max_memories=max_memories)
    if provider == "mem0":
        try:
            memories = await mem0.search(query, user_id=user_id(), limit=max_memories)
        except Exception as exc:
            log.warning("Mem0 context failed: %s", exc)
            return None
        lines = [f"- {_record(value).content}" for value in memories]
        return "\n".join(line for line in lines if line != "- ") or None
    return None


async def ingest(user_text: str, ai_text: str) -> None:
    """Extract durable memories from an exchange using the active provider."""
    provider = await provider_name()
    if provider == "palace":
        await palace.ingest(user_text, ai_text)
    elif provider == "mem0":
        try:
            await mem0.add(
                [
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": ai_text},
                ],
                user_id=user_id(),
                source="textual-chat",
                infer=True,
            )
        except Exception as exc:
            log.warning("Mem0 ingest failed: %s", exc)
