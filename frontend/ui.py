"""Basic AI chat front end built with Textual.

Talks to the LangChain agent in backend/agent.py.

Run with: uv run mypalclara
"""

import argparse
import asyncio
import sys

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Input, Static

from backend import auth, conversation, palace
from backend.discord_bridge import DiscordBridge


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
        self._discord_status = "off"
        self.discord = DiscordBridge(
            on_transcript=self._on_discord_transcript,
            on_status=self._on_discord_status,
        )

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="chat-log")
        yield Input(placeholder="Type a message and press Enter...", id="chat-input")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#chat-input", Input).focus()
        memory_on = await palace.available()
        session = auth.load_session() or {}
        self._who = session.get("name") or session.get("email") or "?"
        self._memory_status = "on" if memory_on else "off"
        self._discord_status = "connecting" if self.discord.configured else "off"
        self._refresh_subtitle()

        history = conversation.get_history()
        if not history and memory_on:
            legacy_history = await palace.get_recent_messages()
            conversation.import_legacy_history(legacy_history)
            history = conversation.get_history()
        for message in history:
            self.add_message(
                "user" if message.role == "user" else "ai",
                message.content,
                source=message.source,
            )
        if history:
            self.add_message(
                "ai",
                f"Welcome back, {self._who} — continuing where we left off.",
            )
        else:
            greeting = f"Hi {self._who}! I'm Clara. Ask me anything."
            if not memory_on:
                greeting += (
                    " (Memory service is offline — I won't remember past chats.)"
                )
            self.add_message("ai", greeting)

        if self.discord.configured:
            self.run_worker(
                self.discord.start_bridge(),
                name="discord-bridge",
                group="discord",
                exit_on_error=False,
            )

    async def on_unmount(self) -> None:
        await self.discord.close_bridge()

    def add_message(self, sender: str, text: str, source: str = "console") -> None:
        log = self.query_one("#chat-log", VerticalScroll)
        log.mount(Message(text, sender, source=source))
        log.scroll_end(animate=False)

    def _on_discord_transcript(self, role: str, content: str) -> None:
        sender = "user" if role == "user" else "ai"
        self.call_later(self.add_message, sender, content, "discord")

    def _on_discord_status(self, status: str) -> None:
        self._discord_status = status
        self.call_later(self._refresh_subtitle)

    def _refresh_subtitle(self) -> None:
        self.sub_title = (
            f"{self._who} • memory: {self._memory_status} "
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
            exclusive=True,
        )

    async def respond(self, user_text: str) -> None:
        try:
            reply = await conversation.get_reply(user_text, source="console")
        except Exception as exc:  # noqa: BLE001 - keep the interactive UI alive
            reply = f"Something went wrong: {exc}"
            self.add_message("ai", reply)
            return
        self.add_message("ai", reply)
        await self.discord.mirror_console_exchange(user_text, reply)


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
        auth.logout()
        print("Logged out.")
        return
    if args.new_chat:
        asyncio.run(palace.reset_session())
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
    ChatApp().run()


if __name__ == "__main__":
    main()
