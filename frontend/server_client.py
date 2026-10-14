"""HTTP/WebSocket client and launcher for the local MyPalClara service."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from dotenv import load_dotenv
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
SERVER_URL = os.environ.get("MYPALCLARA_SERVER_URL", "http://127.0.0.1:8765").rstrip(
    "/"
)
SERVER_LOCK_PATH = Path(tempfile.gettempdir()) / "mypalclara-server.lock"
SERVER_LOG_PATH = Path(
    os.environ.get(
        "MYPALCLARA_SERVER_LOG",
        str(Path(tempfile.gettempdir()) / "mypalclara-server.log"),
    )
)


class ServerError(RuntimeError):
    """Raised when the local service cannot satisfy a client request."""


def _lock_exclusive(lock_file) -> None:
    """Block until an exclusive lock on ``lock_file`` is acquired."""
    if sys.platform == "win32":
        lock_file.seek(0)
        while True:
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.1)
    else:
        fcntl.flock(lock_file, fcntl.LOCK_EX)


def _events_url() -> str:
    parsed = urlsplit(SERVER_URL)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, parsed.path + "/v1/events", "", ""))


def _response_error(response: httpx.Response) -> ServerError:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    return ServerError(detail or f"Server returned HTTP {response.status_code}")


async def get_health() -> dict:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(f"{SERVER_URL}/health")
    if response.is_error:
        raise _response_error(response)
    return response.json()


async def get_history(limit: int = 100) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{SERVER_URL}/v1/history", params={"limit": limit})
    if response.is_error:
        raise _response_error(response)
    return response.json()["messages"]


async def send_chat(content: str, client_id: str) -> str:
    async with httpx.AsyncClient(timeout=None) as client:
        response = await client.post(
            f"{SERVER_URL}/v1/chat",
            json={"content": content, "client_id": client_id},
        )
    if response.is_error:
        raise _response_error(response)
    return response.json()["reply"]


async def reconnect_discord() -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{SERVER_URL}/v1/discord/reconnect")
    if response.is_error:
        raise _response_error(response)
    return response.json()


async def stop_discord() -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{SERVER_URL}/v1/discord/stop")
    if response.is_error:
        raise _response_error(response)
    return response.json()


async def reset_session() -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{SERVER_URL}/v1/session/reset")
    if response.is_error:
        raise _response_error(response)
    return response.json()


async def listen_events() -> AsyncIterator[dict]:
    """Yield server events forever, reconnecting after transient failures."""
    while True:
        try:
            async with connect(
                _events_url(),
                open_timeout=5,
                ping_interval=20,
                ping_timeout=20,
            ) as websocket:
                async for raw_event in websocket:
                    try:
                        event = json.loads(raw_event)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(event, dict):
                        yield event
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError, WebSocketException):
            await asyncio.sleep(1)


def server_is_ready() -> bool:
    try:
        response = httpx.get(f"{SERVER_URL}/health", timeout=1)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def ensure_server(timeout: float = 15) -> None:
    """Start one detached local server when it isn't already reachable."""
    if server_is_ready():
        return

    SERVER_LOCK_PATH.touch(mode=0o600, exist_ok=True)
    with SERVER_LOCK_PATH.open("r+b") as lock_file:
        _lock_exclusive(lock_file)
        if server_is_ready():
            return

        SERVER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        detach_kwargs: dict = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        with SERVER_LOG_PATH.open("a", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                [sys.executable, "-m", "backend.server"],
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
                **detach_kwargs,
            )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if server_is_ready():
                return
            if process.poll() is not None:
                break
            time.sleep(0.1)

    raise ServerError(
        f"MyPalClara server did not start. See {SERVER_LOG_PATH} for diagnostic output."
    )
