"""Copilot SDK runtime: one client per process, one live session per user.

The SDK session id is deliberately separate from ``conversation_id``. The
conversation id keys the canonical Postgres transcript (and the console's
history view), which survives a reset; the SDK session id is the model's
working context, which a reset rotates. It is persisted per user in
``session_store`` so a server restart resumes the same SDK session.

Config (.env):
    COPILOT_HOME   where the SDK keeps session state
                   (default: ~/.mypalclara/copilot)
"""

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from copilot import CopilotClient

from backend import session_store

log = logging.getLogger(__name__)

SESSION_NAMESPACE = "copilot_session"

SessionConfigFactory = Callable[[], dict[str, Any]]

_client: CopilotClient | None = None
_client_lock = asyncio.Lock()
_sessions: dict[str, Any] = {}
_session_lock = asyncio.Lock()


def copilot_home() -> Path:
    configured = os.environ.get("COPILOT_HOME")
    return Path(configured) if configured else Path.home() / ".mypalclara" / "copilot"


async def client() -> CopilotClient:
    """Return the process-wide client, starting it on first use."""
    global _client
    async with _client_lock:
        if _client is None:
            home = copilot_home()
            home.mkdir(parents=True, exist_ok=True)
            started = CopilotClient(mode="empty", base_directory=str(home))
            await started.start()
            _client = started
            log.info("[runtime] Copilot client started (state in %s)", home)
        return _client


def _new_session_id(user_id: str) -> str:
    safe_user = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in user_id)
    return f"clara-{safe_user}-{uuid4().hex[:12]}"


def session_id_for(user_id: str) -> str:
    """Return the user's persisted SDK session id, minting one if needed."""
    state = session_store.load(SESSION_NAMESPACE, user_id)
    if state and state.get("session_id"):
        return state["session_id"]
    session_id = _new_session_id(user_id)
    session_store.save(SESSION_NAMESPACE, user_id, {"session_id": session_id})
    return session_id


async def session_for(user_id: str, config: SessionConfigFactory):
    """Return the user's live session: cached, resumed from disk, or new."""
    async with _session_lock:
        session_id = session_id_for(user_id)
        cached = _sessions.get(session_id)
        if cached is not None:
            return cached

        copilot = await client()
        if await copilot.get_session_metadata(session_id) is not None:
            session = await copilot.resume_session(session_id, **config())
            log.info("[runtime] Resumed session %s", session_id)
        else:
            session = await copilot.create_session(session_id=session_id, **config())
            log.info("[runtime] Created session %s", session_id)
        _sessions[session_id] = session
        return session


async def rotate(user_id: str) -> str:
    """Start the user on a fresh SDK session; the old one stays on disk."""
    async with _session_lock:
        state = session_store.load(SESSION_NAMESPACE, user_id) or {}
        old_id = state.get("session_id")
        if old_id and old_id in _sessions:
            await _disconnect(_sessions.pop(old_id))
        new_id = _new_session_id(user_id)
        session_store.save(SESSION_NAMESPACE, user_id, {"session_id": new_id})
        return new_id


async def _disconnect(session) -> None:
    try:
        await session.disconnect()
    except Exception:  # noqa: BLE001 - shutdown must not fail on one session
        log.exception("[runtime] Failed to disconnect a session")


async def stop() -> None:
    """Disconnect every session and stop the client."""
    global _client
    async with _session_lock:
        for session in list(_sessions.values()):
            await _disconnect(session)
        _sessions.clear()
    async with _client_lock:
        if _client is not None:
            try:
                await _client.stop()
            finally:
                _client = None
