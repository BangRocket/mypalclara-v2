"""Canonical cross-interface chat transcript and Discord delivery outbox."""

from dataclasses import dataclass
from datetime import datetime

from backend import session_store

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('console', 'discord', 'legacy')),
    external_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source, external_id)
);

ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS conversation_id TEXT NOT NULL DEFAULT 'legacy';

CREATE INDEX IF NOT EXISTS chat_messages_conversation_created_idx
    ON chat_messages (user_id, conversation_id, created_at, id);

CREATE TABLE IF NOT EXISTS discord_outbox (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS discord_outbox_pending_idx
    ON discord_outbox (user_id, id)
    WHERE sent_at IS NULL
"""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    id: int
    role: str
    content: str
    source: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    id: int
    content: str


def _connect():
    connection = session_store.connect()
    connection.execute(_SCHEMA_SQL)
    return connection


def record_message(
    *,
    user_id: str,
    conversation_id: str,
    role: str,
    content: str,
    source: str,
    external_id: str | None = None,
) -> bool:
    """Store a transcript message, returning False for a duplicate external ID."""
    with _connect() as connection:
        row = connection.execute(
            """
            INSERT INTO chat_messages (
                user_id, conversation_id, role, content, source, external_id
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, external_id) DO NOTHING
            RETURNING id
            """,
            (user_id, conversation_id, role, content, source, external_id),
        ).fetchone()
    return row is not None


def has_external_message(source: str, external_id: str) -> bool:
    """Return whether a remote message was already processed."""
    with _connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM chat_messages WHERE source = %s AND external_id = %s",
            (source, external_id),
        ).fetchone()
    return row is not None


def list_messages(
    user_id: str, conversation_id: str, limit: int = 100
) -> list[ChatMessage]:
    """Return the newest transcript messages in chronological order."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT id, role, content, source, created_at
            FROM (
                SELECT id, role, content, source, created_at
                FROM chat_messages
                WHERE user_id = %s AND conversation_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
            ) AS recent
            ORDER BY created_at, id
            """,
            (user_id, conversation_id, limit),
        ).fetchall()
    return [ChatMessage(*row) for row in rows]


def enqueue_discord_message(user_id: str, content: str) -> int:
    """Persist a Discord DM for at-least-once delivery."""
    with _connect() as connection:
        row = connection.execute(
            """
            INSERT INTO discord_outbox (user_id, content)
            VALUES (%s, %s)
            RETURNING id
            """,
            (user_id, content),
        ).fetchone()
    return row[0]


def list_pending_discord_messages(
    user_id: str, limit: int = 100
) -> list[OutboxMessage]:
    """Return queued Discord messages in delivery order."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT id, content
            FROM discord_outbox
            WHERE user_id = %s AND sent_at IS NULL
            ORDER BY id
            LIMIT %s
            """,
            (user_id, limit),
        ).fetchall()
    return [OutboxMessage(*row) for row in rows]


def mark_discord_message_sent(message_id: int) -> None:
    """Mark an outbox item delivered."""
    with _connect() as connection:
        connection.execute(
            """
            UPDATE discord_outbox
            SET sent_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (message_id,),
        )
