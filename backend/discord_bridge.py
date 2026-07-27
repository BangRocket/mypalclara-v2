"""Discord DM bridge for the shared MyPalClara conversation."""

import asyncio
import logging
import os
from collections.abc import Callable

import discord

from backend import auth, chat_store, conversation

log = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000
TranscriptCallback = Callable[[str, str], None]
StatusCallback = Callable[[str], None]


def _split_for_discord(content: str, prefix: str = "") -> list[str]:
    """Split text into Discord-safe messages while preserving a first prefix."""
    remaining = content.strip() or "(empty message)"
    chunks: list[str] = []
    current_prefix = prefix

    while remaining:
        budget = DISCORD_MESSAGE_LIMIT - len(current_prefix)
        if budget <= 0:
            raise ValueError("Discord message prefix is too long")
        if len(remaining) <= budget:
            body = remaining
        else:
            split_at = remaining.rfind("\n", 0, budget + 1)
            if split_at < budget // 2:
                split_at = remaining.rfind(" ", 0, budget + 1)
            if split_at < budget // 2:
                split_at = budget
            body = remaining[:split_at].rstrip()
        chunks.append(current_prefix + body)
        remaining = remaining[len(body) :].lstrip()
        current_prefix = "↳ "

    return chunks


class DiscordBridge(discord.Client):
    """Mirror console exchanges to one Discord user and process their DMs."""

    def __init__(
        self,
        on_transcript: TranscriptCallback,
        on_status: StatusCallback,
    ) -> None:
        intents = discord.Intents.none()
        intents.dm_messages = True
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self._token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        self._discord_user_id: int | None = None
        self._on_transcript = on_transcript
        self._on_status = on_status
        self._delivery_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._token)

    async def start_bridge(self) -> None:
        """Connect to Discord and remain active until the client is closed."""
        if not self.configured:
            self._on_status("off")
            return
        self._on_status("connecting")
        try:
            self._discord_user_id = await asyncio.to_thread(auth.get_discord_user_id)
            if self._discord_user_id is None:
                self._on_status("unlinked")
                return
            await self.start(self._token)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._on_status("error")
            log.exception("Discord bridge stopped unexpectedly")

    async def close_bridge(self) -> None:
        if not self.is_closed():
            await self.close()

    async def on_ready(self) -> None:
        log.info("Discord bridge connected as %s", self.user)
        self._on_status("on")
        await self.flush_outbox()

    async def on_disconnect(self) -> None:
        self._on_status("connecting")

    async def on_message(self, message: discord.Message) -> None:
        """Process DMs from the configured user through the shared agent."""
        if (
            message.author.bot
            or message.guild is not None
            or message.author.id != self._discord_user_id
        ):
            return

        external_id = str(message.id)
        if chat_store.has_external_message("discord", external_id):
            return

        parts = [message.content.strip()]
        parts.extend(attachment.url for attachment in message.attachments)
        user_text = "\n".join(part for part in parts if part)
        if not user_text:
            return

        self._on_transcript("user", user_text)
        try:
            reply = await conversation.get_reply(
                user_text,
                source="discord",
                external_id=external_id,
            )
        except conversation.DuplicateMessageError:
            return
        except Exception:
            self._on_status("error")
            log.exception("Failed to process Discord message %s", external_id)
            return

        self._on_transcript("assistant", reply)
        self._enqueue(reply)
        await self.flush_outbox()

    async def mirror_console_exchange(self, user_text: str, reply: str) -> None:
        """Queue both sides of a console exchange for Discord DM delivery."""
        if not self.configured:
            return
        self._enqueue(user_text, prefix="🖥️ **Console · You**\n")
        self._enqueue(reply, prefix="🖥️ **Console · Clara**\n")
        await self.flush_outbox()

    def _enqueue(self, content: str, prefix: str = "") -> None:
        user_id = conversation.current_user_id()
        for chunk in _split_for_discord(content, prefix=prefix):
            chat_store.enqueue_discord_message(user_id, chunk)

    async def flush_outbox(self) -> None:
        """Deliver queued messages in order when Discord is connected."""
        if not self.configured or self._discord_user_id is None or not self.is_ready():
            return
        async with self._delivery_lock:
            user_id = conversation.current_user_id()
            try:
                recipient = self.get_user(self._discord_user_id)
                if recipient is None:
                    recipient = await self.fetch_user(self._discord_user_id)
                for item in chat_store.list_pending_discord_messages(user_id):
                    await recipient.send(
                        item.content,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    chat_store.mark_discord_message_sent(item.id)
                self._on_status("on")
            except Exception:
                self._on_status("error")
                log.exception("Failed to flush Discord outbox")
