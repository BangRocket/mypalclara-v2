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

from backend import auth, palace


class Message(Static):
    """A single chat message bubble."""

    def __init__(self, text: str, sender: str) -> None:
        super().__init__(text)
        self.add_class("message")
        self.add_class("user" if sender == "user" else "ai")


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

    #chat-input {
        dock: bottom;
        margin: 0 1 1 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="chat-log")
        yield Input(placeholder="Type a message and press Enter...", id="chat-input")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#chat-input", Input).focus()
        memory_on = await palace.available()
        session = auth.load_session() or {}
        who = session.get("name") or session.get("email") or "?"
        self.sub_title = f"{who} • memory: {'on' if memory_on else 'off'}"
        history = await palace.get_recent_messages() if memory_on else []
        for m in history:
            self.add_message("user" if m["role"] == "user" else "ai", m["content"])
        if history:
            self.add_message("ai", f"Welcome back, {who} — continuing where we left off.")
        else:
            greeting = f"Hi {who}! I'm Clara. Ask me anything."
            if not memory_on:
                greeting += " (Memory service is offline — I won't remember past chats.)"
            self.add_message("ai", greeting)

    def add_message(self, sender: str, text: str) -> None:
        log = self.query_one("#chat-log", VerticalScroll)
        log.mount(Message(text, sender))
        log.scroll_end(animate=False)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()
        self.add_message("user", text)
        self.run_worker(self.respond(text), exclusive=True)

    async def respond(self, user_text: str) -> None:
        try:
            from backend.agent import get_reply

            reply = await get_reply(user_text)
        except Exception as exc:
            reply = f"Something went wrong: {exc}"
        self.add_message("ai", reply)


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
    args = parser.parse_args()

    if args.logout:
        auth.logout()
        print("Logged out.")
        return
    if args.new_chat:
        asyncio.run(palace.reset_session())
        print("Next launch starts a fresh conversation (old history is kept in Palace).")
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
