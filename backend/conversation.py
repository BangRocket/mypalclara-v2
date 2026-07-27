"""Shared conversation flow for console and Discord chat surfaces."""

import os
from uuid import uuid4

from backend import auth, chat_store, session_store


class DuplicateMessageError(Exception):
    """Raised when Discord redelivers an already processed message."""


def current_user_id() -> str:
    """Return the signed-in user, with the script/test fallback."""
    return auth.current_user_id() or os.environ.get("PALACE_USER_ID", "local-user")


def current_conversation_id(user_id: str | None = None) -> str:
    """Return the app conversation ID, creating it on the first message."""
    user_id = user_id or current_user_id()
    state = session_store.load("conversation", user_id)
    if state and state.get("conversation_id"):
        return state["conversation_id"]
    conversation_id = str(uuid4())
    session_store.save("conversation", user_id, {"conversation_id": conversation_id})
    return conversation_id


async def get_reply(
    user_text: str,
    *,
    source: str = "console",
    external_id: str | None = None,
) -> str:
    """Record an incoming message, invoke the agent, and record its reply."""
    from backend.agent import get_reply as get_agent_reply

    user_id = current_user_id()
    conversation_id = current_conversation_id(user_id)
    inserted = chat_store.record_message(
        user_id=user_id,
        conversation_id=conversation_id,
        role="user",
        content=user_text,
        source=source,
        external_id=external_id,
    )
    if not inserted:
        raise DuplicateMessageError(external_id or "unknown")

    reply = await get_agent_reply(user_text)
    chat_store.record_message(
        user_id=user_id,
        conversation_id=conversation_id,
        role="assistant",
        content=reply,
        source=source,
    )
    return reply


def get_history(limit: int = 100) -> list[chat_store.ChatMessage]:
    """Return the current user's canonical cross-interface transcript."""
    user_id = current_user_id()
    return chat_store.list_messages(
        user_id, current_conversation_id(user_id), limit=limit
    )


def import_legacy_history(messages: list[dict]) -> None:
    """Seed the canonical transcript from Palace on the first upgraded run."""
    user_id = current_user_id()
    conversation_id = current_conversation_id(user_id)
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role in {"user", "assistant"} and content:
            chat_store.record_message(
                user_id=user_id,
                conversation_id=conversation_id,
                role=role,
                content=content,
                source="legacy",
            )
