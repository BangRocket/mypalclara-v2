"""Async client for the hosted Mem0 memory API.

The V3 API handles add, search, and list operations. Single-memory updates
and deletes remain on V1. Configuration comes from ``MEM0_API_KEY`` and the
optional ``MEM0_BASE_URL`` setting.
"""

import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger(__name__)

_healthy: bool | None = None


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def enabled() -> bool:
    """Return whether Mem0 is enabled and has credentials configured."""
    return _env_flag("MEM0_ENABLED", True) and bool(os.environ.get("MEM0_API_KEY"))


def _base_url() -> str:
    return os.environ.get("MEM0_BASE_URL", "https://api.mem0.ai").rstrip("/")


async def _request(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    api_key = os.environ.get("MEM0_API_KEY")
    if not api_key:
        raise RuntimeError("MEM0_API_KEY is not configured")

    async with httpx.AsyncClient(
        base_url=_base_url(),
        headers={"Authorization": f"Token {api_key}"},
        timeout=httpx.Timeout(10.0),
    ) as client:
        response = await client.request(method, path, json=json, params=params)
        response.raise_for_status()
        return response.json() if response.content else {}


async def available(user_id: str) -> bool:
    """Verify that Mem0 is configured and accepts authenticated requests."""
    global _healthy
    if not enabled():
        return False
    if _healthy is None:
        try:
            await get_all(user_id=user_id, limit=1)
            _healthy = True
        except Exception as exc:
            log.warning("Mem0 unavailable: %s", exc)
            _healthy = False
    return _healthy


def reset_health() -> None:
    global _healthy
    _healthy = None


async def search(query: str, *, user_id: str, limit: int = 10) -> list[dict]:
    limit = min(max(limit, 1), 1000)
    response = await _request(
        "POST",
        "/v3/memories/search/",
        json={
            "query": query,
            "filters": {"user_id": user_id},
            "top_k": limit,
        },
    )
    return response.get("results", [])


async def get_all(*, user_id: str, limit: int = 20) -> list[dict]:
    limit = min(max(limit, 1), 200)
    response = await _request(
        "POST",
        "/v3/memories/",
        params={"page": 1, "page_size": limit},
        json={"filters": {"user_id": user_id}},
    )
    return response.get("results", [])


async def add(
    messages: list[dict[str, str]],
    *,
    user_id: str,
    source: str,
    infer: bool = True,
    metadata: dict[str, Any] | None = None,
) -> dict:
    memory_metadata = dict(metadata or {})
    memory_metadata["source"] = source
    return await _request(
        "POST",
        "/v3/memories/add/",
        json={
            "messages": messages,
            "user_id": user_id,
            "metadata": memory_metadata,
            "infer": infer,
        },
    )


async def update(memory_id: str, *, content: str) -> dict:
    encoded_id = quote(memory_id, safe="")
    return await _request(
        "PUT", f"/v1/memories/{encoded_id}", json={"text": content}
    )


async def delete(memory_id: str) -> None:
    encoded_id = quote(memory_id, safe="")
    await _request("DELETE", f"/v1/memories/{encoded_id}")
