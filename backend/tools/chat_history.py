"""Chat history tools - search and read the stored transcript.

Provides LangChain tools over the canonical Postgres transcript in
``backend.chat_store``, scoped to the current signed-in user.
Tools: search_chat_history, get_chat_history.
"""

from langchain_core.tools import tool

from backend import chat_store, conversation

_MAX_CONTENT = 300


def _format(message: chat_store.ChatMessage) -> str:
    timestamp = message.created_at.strftime("%Y-%m-%d %H:%M")
    content = message.content
    if len(content) > _MAX_CONTENT:
        content = content[:_MAX_CONTENT] + "..."
    return f"[{timestamp}] ({message.source}) {message.role}: {content}"


@tool
async def search_chat_history(query: str, limit: int = 20) -> str:
    """Search the current user's past chat messages for a case-insensitive
    substring. Use this to recall what was discussed earlier, find links,
    decisions, or anything the user references from a past conversation.
    Returns matching messages newest first."""
    if not query:
        return "Error: No search query provided."
    limit = max(1, min(limit, 100))
    try:
        user_id = conversation.current_user_id()
        matches = chat_store.search_messages(user_id, query, limit=limit)
    except Exception as exc:
        return f"Chat history is currently unavailable: {exc}"
    if not matches:
        return f"No messages found matching '{query}'."
    result = f"Found {len(matches)} matching message(s):\n\n"
    result += "\n\n".join(_format(m) for m in matches)
    return result


@tool
async def get_chat_history(limit: int = 20) -> str:
    """Retrieve recent messages from the current conversation, in
    chronological order. Use this to see what was discussed earlier in this
    conversation beyond what is in the current context."""
    limit = max(1, min(limit, 100))
    try:
        user_id = conversation.current_user_id()
        conversation_id = conversation.current_conversation_id(user_id)
        messages = chat_store.list_messages(user_id, conversation_id, limit=limit)
    except Exception as exc:
        return f"Chat history is currently unavailable: {exc}"
    if not messages:
        return "No messages found in the current conversation."
    result = f"Chat history ({len(messages)} messages):\n\n"
    result += "\n\n".join(_format(m) for m in messages)
    return result


TOOLS = [search_chat_history, get_chat_history]
