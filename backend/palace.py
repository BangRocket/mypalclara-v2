"""MyPalace memory integration for the chat backend.

Talks to a Palace server (see docker-compose.yml) for session/message
persistence, semantic memory context, and smart ingestion. Every function
degrades gracefully: if Palace is unreachable or errors, these become
no-ops and the chat keeps working without memory.

One Palace session is kept per user in the MyPalClara application database so
conversation history continues across app restarts.
``reset_session`` starts a fresh conversation.

Config comes from .env: PALACE_URL, PALACE_API_KEY, PALACE_USER_ID.
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from mypalace_client import PalaceClient

from backend import session_store

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger(__name__)

_LEGACY_SESSION_STATE_FILE = (
    Path(__file__).resolve().parent.parent / ".palace-session.json"
)

_client: PalaceClient | None = None
_session_id: str | None = None
_healthy: bool | None = None


def _get_client() -> PalaceClient:
    global _client
    if _client is None:
        _client = PalaceClient(
            base_url=os.environ.get("PALACE_URL", "http://localhost:8000"),
            api_key=os.environ.get("PALACE_API_KEY") or None,
        )
    return _client


def _user_id() -> str:
    """Clerk user id when logged in; PALACE_USER_ID from .env otherwise
    (fallback for scripts/tests running outside the login-gated TUI)."""
    from backend import auth

    return auth.current_user_id() or os.environ.get("PALACE_USER_ID", "local-user")


def _load_session_state(user_id: str) -> dict | None:
    state = session_store.load("palace", user_id)
    if state is not None:
        return state

    # One-time migration from versions that cached the active session on disk.
    try:
        state = json.loads(_LEGACY_SESSION_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    legacy_user_id = state.get("user_id")
    if legacy_user_id and state.get("session_id"):
        session_store.save("palace", legacy_user_id, state)
        _LEGACY_SESSION_STATE_FILE.unlink(missing_ok=True)
    return state if legacy_user_id == user_id else None


def _save_session_state(user_id: str, session_id: str) -> None:
    session_store.save(
        "palace", user_id, {"user_id": user_id, "session_id": session_id}
    )


async def available() -> bool:
    """Health-check Palace. Result is cached per process; a recheck can be
    forced by calling ``reset_health``."""
    global _healthy
    if _healthy is None:
        try:
            await _get_client().health()
            _healthy = True
        except Exception as exc:
            log.warning("Palace unavailable: %s", exc)
            _healthy = False
    return _healthy


def reset_health() -> None:
    global _healthy
    _healthy = None


async def ensure_session() -> str | None:
    """Return the user's ongoing Palace session id, reusing the one cached
    on disk from previous app runs (verified to still exist remotely).
    Creates a new session when there is none or it was deleted."""
    global _session_id
    if _session_id is not None:
        return _session_id
    if not await available():
        return None

    user_id = _user_id()
    state = _load_session_state(user_id)
    if state and state.get("user_id") == user_id and state.get("session_id"):
        try:
            await _get_client().get_session(state["session_id"])
            _session_id = state["session_id"]
            return _session_id
        except Exception:
            log.warning("Cached Palace session %s is gone; creating a new one",
                        state["session_id"])
    try:
        session = await _get_client().create_session(user_id=user_id, title="textual chat")
        _session_id = session.id
        _save_session_state(user_id, _session_id)
    except Exception as exc:
        log.warning("Palace session creation failed: %s", exc)
        return None
    return _session_id


async def reset_session() -> None:
    """Drop the cached session so the next message starts a fresh
    conversation. The old session's messages stay in Palace."""
    global _session_id
    _session_id = None
    user_id = _user_id()
    session_store.delete("palace", user_id)
    session_store.delete("conversation", user_id)


async def record(role: str, content: str) -> None:
    """Append a message to the Palace session. No-op when unavailable."""
    session_id = await ensure_session()
    if session_id is None:
        return
    try:
        await _get_client().add_message(
            session_id=session_id, user_id=_user_id(), role=role, content=content
        )
    except Exception as exc:
        log.warning("Palace record failed: %s", exc)


async def get_recent_messages(limit: int = 20) -> list[dict]:
    """Last ``limit`` messages of the ongoing session as
    ``[{"role": ..., "content": ...}]`` — used to seed the agent so the
    conversation continues across app restarts. Empty when unavailable."""
    session_id = await ensure_session()
    if session_id is None:
        return []
    try:
        session = await _get_client().get_session(session_id)
    except Exception as exc:
        log.warning("Palace history fetch failed: %s", exc)
        return []
    messages = [
        {"role": m.role, "content": m.content}
        for m in (session.messages or [])[-limit:]
        if m.role in ("user", "assistant") and m.content
    ]
    return messages


async def get_context(query: str, max_memories: int = 5) -> str | None:
    """Relevant long-term memories for a user message, formatted for prompt
    injection. None when Palace is unavailable or nothing is relevant."""
    session_id = await ensure_session()
    if session_id is None:
        return None
    try:
        context = await _get_client().assemble_context(
            user_id=_user_id(),
            query=query,
            session_id=session_id,
            max_memories=max_memories,
            max_messages=0,  # recent chat is seeded from get_recent_messages
        )
    except Exception as exc:
        log.warning("Palace context failed: %s", exc)
        return None
    lines = [
        f"- {m.get('content', '')}" for m in context.memories if m.get("content")
    ]
    return "\n".join(lines) or None


async def ingest(user_text: str, ai_text: str) -> None:
    """Fire-and-forget smart ingestion: LLM extracts durable facts from the
    exchange into long-term memory (dedup + auto-supersede server-side)."""
    if not await available():
        return
    try:
        await _get_client().add(
            messages=[
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": ai_text},
            ],
            user_id=_user_id(),
            source="textual-chat",
            infer=True,
        )
    except Exception as exc:
        log.warning("Palace ingest failed: %s", exc)
