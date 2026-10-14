"""Long-running localhost service that owns the agent and Discord gateway."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from backend import conversation, memory, palace
from backend.discord_bridge import DiscordBridge

log = logging.getLogger(__name__)

SERVER_HOST = os.environ.get("MYPALCLARA_SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("MYPALCLARA_SERVER_PORT", "8765"))


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    client_id: str = Field(min_length=1, max_length=100)


class EventHub:
    """Fan live transcript and status events out to connected consoles."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def broadcast(self, event: dict) -> None:
        async with self._lock:
            connections = list(self._connections)
        disconnected = []
        for websocket in connections:
            try:
                await websocket.send_json(event)
            except Exception:  # noqa: BLE001 - a dead client must not break the hub
                disconnected.append(websocket)
        if disconnected:
            async with self._lock:
                for websocket in disconnected:
                    self._connections.discard(websocket)


class ServerRuntime:
    """Own the one Discord client and bridge its events to console clients."""

    def __init__(self) -> None:
        self.hub = EventHub()
        self.discord: DiscordBridge | None = None
        self.discord_task: asyncio.Task | None = None
        self.discord_status = "off"

    async def start(self) -> None:
        await self.start_discord()

    async def stop(self) -> None:
        if self.discord is not None:
            await self.discord.close_bridge()
        if self.discord_task is not None:
            with suppress(asyncio.CancelledError):
                await self.discord_task
        self.discord = None
        self.discord_task = None

    async def start_discord(self) -> None:
        if self.discord_task is not None and not self.discord_task.done():
            return
        if self.discord is not None:
            await self.discord.close_bridge()
        self.discord = DiscordBridge(
            on_transcript=self._on_discord_transcript,
            on_status=self._on_discord_status,
        )
        self.discord_task = asyncio.create_task(
            self.discord.start_bridge(), name="discord-bridge"
        )

    async def stop_discord(self) -> None:
        if self.discord is not None:
            await self.discord.close_bridge()
        if self.discord_task is not None:
            with suppress(asyncio.CancelledError):
                await self.discord_task
        self.discord = None
        self.discord_task = None
        self._on_discord_status("off")

    async def mirror_console_exchange(self, user_text: str, reply: str) -> None:
        if self.discord is not None:
            await self.discord.mirror_console_exchange(user_text, reply)

    def _on_discord_transcript(self, role: str, content: str) -> None:
        asyncio.create_task(
            self.hub.broadcast(
                {
                    "type": "chat_message",
                    "role": role,
                    "content": content,
                    "source": "discord",
                    "client_id": None,
                }
            )
        )

    def _on_discord_status(self, status: str) -> None:
        self.discord_status = status
        asyncio.create_task(
            self.hub.broadcast({"type": "discord_status", "status": status})
        )


runtime = ServerRuntime()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await runtime.start()
    yield
    await runtime.stop()


app = FastAPI(title="MyPalClara Server", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    memory_provider = await memory.provider_name()
    return {
        "status": "ok",
        "memory": "on" if memory_provider else "off",
        "memory_provider": memory_provider,
        "discord": runtime.discord_status,
    }


@app.get("/v1/history")
async def history(limit: int = 100) -> dict:
    limit = min(max(limit, 1), 500)
    messages = conversation.get_history(limit=limit)
    if not messages and await palace.available():
        conversation.import_legacy_history(await palace.get_recent_messages())
        messages = conversation.get_history(limit=limit)
    return {
        "messages": [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "source": message.source,
                "created_at": message.created_at.isoformat(),
            }
            for message in messages
        ]
    }


@app.post("/v1/chat")
async def chat(request: ChatRequest) -> dict:
    content = request.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="Message cannot be blank")

    await runtime.hub.broadcast(
        {
            "type": "chat_message",
            "role": "user",
            "content": content,
            "source": "console",
            "client_id": request.client_id,
        }
    )
    try:
        reply = await conversation.get_reply(content, source="console")
    except Exception as exc:
        log.exception("Console chat request failed")
        raise HTTPException(status_code=500, detail="Agent request failed") from exc

    await runtime.hub.broadcast(
        {
            "type": "chat_message",
            "role": "assistant",
            "content": reply,
            "source": "console",
            "client_id": request.client_id,
        }
    )
    try:
        await runtime.mirror_console_exchange(content, reply)
    except Exception:
        log.exception("Failed to queue console exchange for Discord")
    return {"reply": reply}


@app.post("/v1/session/reset")
async def reset_session() -> dict:
    await palace.reset_session()
    from backend.agent import reset_conversation

    await reset_conversation()
    await runtime.hub.broadcast({"type": "session_reset"})
    return {"reset": True}


@app.post("/v1/discord/reconnect")
async def reconnect_discord() -> dict:
    await runtime.start_discord()
    return {"discord": runtime.discord_status}


@app.post("/v1/discord/stop")
async def stop_discord() -> dict:
    await runtime.stop_discord()
    return {"discord": "off"}


@app.websocket("/v1/events")
async def events(websocket: WebSocket) -> None:
    await runtime.hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await runtime.hub.disconnect(websocket)


def main() -> None:
    """Run the long-lived local service."""
    uvicorn.run(
        "backend.server:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level=os.environ.get("MYPALCLARA_SERVER_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
