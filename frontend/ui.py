"""Textual client for the long-running local MyPalClara service.

Run with: uv run mypalclara
"""

import argparse
import asyncio
import sys
from uuid import uuid4

import httpx
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Input, Static

from backend import auth
from frontend import server_client


class Message(Static):
    """A single chat message bubble."""

    def __init__(self, text: str, sender: str, source: str = "console") -> None:
        label = "Discord · " if source == "discord" else ""
        super().__init__(label + text)
        self.add_class("message")
        self.add_class("user" if sender == "user" else "ai")
        self.add_class(source)


class ChatApp(App):
    TITLE = "MyPalClara"
    SUB_TITLE = "chat"

    CSS = """
    #chat-log {
        height: 1fr;
        padding: 1 2;
    }

    .message {
        width: auto;
        max-width: 70%;
        padding: 1 2;
        margin-bottom: 1;
        border: round $surface-lighten-2;
    }

    .message.user {
        align-horizontal: right;
        background: $primary-darken-2;
        color: $text;
    }

    .message.ai {
        align-horizontal: left;
        background: $surface-lighten-1;
    }

    .message.discord {
        border: round #5865f2;
    }

    #chat-input {
        dock: bottom;
        margin: 0 1 1 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._who = "?"
        self._memory_status = "off"
        self._memory_provider = "off"
        self._discord_status = "off"
        self._client_id = str(uuid4())

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="chat-log")
        yield Input(placeholder="Type a message and press Enter...", id="chat-input")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#chat-input", Input).focus()
        session = auth.load_session() or {}
        self._who = session.get("name") or session.get("email") or "?"
        try:
            health, history = await asyncio.gather(
                server_client.get_health(), server_client.get_history()
            )
        except Exception as exc:  # noqa: BLE001 - show startup errors in the UI
            self.add_message("ai", f"Unable to connect to the local server: {exc}")
            return

        self._memory_status = health.get("memory", "off")
        self._memory_provider = health.get("memory_provider") or self._memory_status
        self._discord_status = health.get("discord", "off")
        self._refresh_subtitle()

        for message in history:
            self.add_message(
                "user" if message["role"] == "user" else "ai",
                message["content"],
                source=message["source"],
            )
        if history:
            self.add_message(
                "ai",
                f"Welcome back, {self._who} — continuing where we left off.",
            )
        else:
            greeting = f"Hi {self._who}! I'm Clara. Ask me anything."
            if self._memory_status != "on":
                greeting += (
                    " (Memory service is offline — I won't remember past chats.)"
                )
            self.add_message("ai", greeting)

        self.run_worker(
            self._listen_for_server_events(),
            name="server-events",
            group="server",
            exit_on_error=False,
        )

    def add_message(
        self, sender: str, text: str, source: str = "console"
    ) -> Message:
        log = self.query_one("#chat-log", VerticalScroll)
        message = Message(text, sender, source=source)
        log.mount(message)
        log.scroll_end(animate=False)
        return message

    async def _listen_for_server_events(self) -> None:
        async for event in server_client.listen_events():
            event_type = event.get("type")
            if event_type == "chat_message":
                if event.get("client_id") == self._client_id:
                    continue
                sender = "user" if event.get("role") == "user" else "ai"
                self.add_message(
                    sender,
                    str(event.get("content", "")),
                    source=str(event.get("source", "console")),
                )
            elif event_type == "discord_status":
                self._discord_status = str(event.get("status", "off"))
                self._refresh_subtitle()
            elif event_type == "session_reset":
                await self._show_session_reset()

    async def _show_session_reset(self) -> None:
        await self.query_one("#chat-log", VerticalScroll).remove_children()
        self.add_message("ai", "A fresh conversation has started.")

    def _refresh_subtitle(self) -> None:
        self.sub_title = (
            f"{self._who} • memory: {self._memory_provider} "
            f"• discord: {self._discord_status}"
        )

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()
        self.add_message("user", text)
        self.run_worker(
            self.respond(text),
            name="console-response",
            group="console",
        )

    async def respond(self, user_text: str) -> None:
        placeholder = self.add_message("ai", "Thinking…")
        try:
            reply = await server_client.send_chat(user_text, self._client_id)
        except Exception as exc:  # noqa: BLE001 - keep the interactive UI alive
            reply = f"Something went wrong: {exc}"
        placeholder.update(reply)


def main() -> None:
    """Run a session command or launch the Textual chat interface."""
    parser = argparse.ArgumentParser(prog="mypalclara")
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--logout",
        action="store_true",
        help="revoke the current login and clear its stored session",
    )
    action.add_argument(
        "--new-chat",
        action="store_true",
        help="start a fresh conversation on the next launch",
    )
    action.add_argument(
        "--link-discord",
        action="store_true",
        help="open Clerk profile settings to link a Discord account",
    )
    args = parser.parse_args()

    if args.logout:
        if server_client.server_is_ready():
            try:
                asyncio.run(server_client.stop_discord())
            except (httpx.HTTPError, server_client.ServerError):
                pass
        auth.logout()
        print("Logged out.")
        return
    if args.new_chat:
        try:
            server_client.ensure_server()
            asyncio.run(server_client.reset_session())
        except (httpx.HTTPError, server_client.ServerError) as exc:
            sys.exit(f"Unable to start a new chat: {exc}")
        print(
            "Next launch starts a fresh conversation (old history is kept in Palace)."
        )
        return
    if args.link_discord:
        try:
            auth.open_discord_linking()
        except auth.AuthError as exc:
            sys.exit(f"Unable to link Discord: {exc}")
        print(
            "Clerk profile opened. Add Discord under connected accounts, then restart."
        )
        return

    session = auth.ensure_session()
    if session is None:
        print("Sign in with Clerk to start chatting. Opening your browser...")
        try:
            session = auth.login()
        except auth.AuthError as exc:
            sys.exit(f"Login failed: {exc}")
    print(f"Signed in as {session.get('email') or session['user_id']}")
    try:
        server_client.ensure_server()
        asyncio.run(server_client.reconnect_discord())
    except (httpx.HTTPError, server_client.ServerError) as exc:
        sys.exit(f"Unable to start the local server: {exc}")
    ChatApp().run()


if __name__ == "__main__":
    main()
