"""PostgreSQL-backed storage for MyPalClara application session state.

Config (.env): MYPALCLARA_DATABASE_URL. The default matches the dedicated
database service in docker-compose.yml.
"""

import json
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DATABASE_URL = os.environ.get(
    "MYPALCLARA_DATABASE_URL",
    "postgresql://mypalclara:mypalclara@localhost:5444/mypalclara",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS app_session_state (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (namespace, key)
)
"""


def _connect() -> psycopg.Connection:
    connection = psycopg.connect(DATABASE_URL)
    connection.execute(_SCHEMA_SQL)
    return connection


def load(namespace: str, key: str) -> dict | None:
    """Load a JSON object, or None when the key does not exist."""
    with _connect() as connection:
        row = connection.execute(
            "SELECT value FROM app_session_state WHERE namespace = %s AND key = %s",
            (namespace, key),
        ).fetchone()
    return row[0] if row else None


def save(namespace: str, key: str, value: dict) -> None:
    """Insert or replace a JSON object."""
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO app_session_state (namespace, key, value)
            VALUES (%s, %s, %s::jsonb)
            ON CONFLICT (namespace, key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (namespace, key, json.dumps(value)),
        )


def delete(namespace: str, key: str) -> None:
    """Delete a stored object if it exists."""
    with _connect() as connection:
        connection.execute(
            "DELETE FROM app_session_state WHERE namespace = %s AND key = %s",
            (namespace, key),
        )
