# MyPalClara

A Textual chat interface with a LangChain agent, Clerk authentication, and
persistent memory through MyPalace.

## Run

Start the backing services, install the locked environment, and launch the UI:

```sh
cp .env.example .env
docker compose up -d
uv sync
uv run mypalclara
```

Fill in the required credentials in `.env` before starting the services. The
configuration file is ignored by Git.

## Discord DM bridge

Create a bot in the Discord Developer Portal and copy its token into
`DISCORD_BOT_TOKEN`. Enable Discord as a social connection in the Clerk
Dashboard, then link it to the signed-in Clerk user:

```sh
uv run mypalclara --link-discord
```

The bridge reads the verified Discord provider user ID from that user's Clerk
external accounts. No Discord user ID is stored in `.env`. The user must be able
to DM the bot. Server messages and DMs from every other user are ignored.

While the UI is running:

- DMs to the bot use the same agent conversation and appear in console history
  with a Discord label and border.
- Both sides of a console exchange are mirrored to the configured user's DMs.
- Outgoing DMs are queued in PostgreSQL and retried after Discord reconnects.
- Discord message IDs are recorded so reconnects cannot generate duplicate
  agent replies.

The console subscribes to the server over WebSocket, so Discord DMs appear
live when the console is open and remain in PostgreSQL history when it is not.

Discord exempts bot DMs from the Message Content privileged-intent restriction,
so this integration requests only the direct-message gateway intent.

### Backfill exported Discord history

DiscordChatExporter JSON files can be imported into the canonical transcript.
Companion `*_decisions.json` files from the legacy chat cleaner are applied
automatically, original timestamps are retained, and Discord message IDs make
repeated runs safe. Add `--ingest-memory` to also run each historical session
through the active memory provider with inference enabled. This follows
`MEMORY_PROVIDER`, including Palace's automatic fallback to Mem0, and stores a
per-user/per-provider checkpoint after every completed session.

Preview an import first, then target the appropriate Clerk user:

```sh
uv run python scripts/backfill_discord.py \
  --chats-dir /path/to/old/mypalclara/chats \
  --dry-run

uv run python scripts/backfill_discord.py \
  --chats-dir /path/to/old/mypalclara/chats \
  --user-id user_abc123 \
  --ingest-memory
```

Memory inference can incur provider usage costs. Interrupted runs resume from
their PostgreSQL checkpoints; `--memory-only` resumes memory without scanning
the transcript for duplicates, while `--reingest-memory` intentionally ignores
the checkpoints. To populate both providers, run `--memory-only` once with
`MEMORY_PROVIDER=palace` and once with `MEMORY_PROVIDER=mem0`; their checkpoints
are independent. Run `uv run python scripts/backfill_discord.py --help` for
single-file import, custom bot/name mappings, concurrency, and session-splitting
options.

## Session commands

```sh
uv run mypalclara --new-chat
uv run mypalclara --logout
```
