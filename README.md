# MyPalClara

A Textual chat interface with a GitHub Copilot SDK agent, Clerk authentication, and
persistent memory through MyPalace with Mem0 fallback support.

## Run

Start the backing services, install the locked environment, and launch the UI:

```sh
cp .env.example .env
docker compose up -d
uv sync
uv run mypalclara
```

On Windows PowerShell, replace `cp` with `Copy-Item .env.example .env`. Everything
else works the same way, including `uv run` and the Discord backfill script, on
Linux, macOS, and Windows.

Fill in the required credentials in `.env` before starting the services. The
configuration file is ignored by Git.

## Agent runtime

The agent runs on the GitHub Copilot SDK in BYOK mode: `CHAT_MODEL`,
`ANTHROPIC_BASE_URL`, and `ANTHROPIC_API_KEY` select the model, so no Copilot
subscription is needed. Each user has one persistent SDK session, stored under
`COPILOT_HOME` (default `~/.mypalclara/copilot`), so a server restart resumes
the conversation where it left off. `--new-chat` starts a fresh SDK session;
the canonical transcript in PostgreSQL is unaffected. Long-term memory is
injected per turn by a prompt hook, sending only memories the session has not
already seen.

## Memory providers

`MEMORY_PROVIDER=auto` prefers MyPalace and falls back to the hosted Mem0 API
when Palace is disabled or its health check fails. Set `MEM0_API_KEY` to a
Mem0 Platform API key to enable that fallback. You can also select `palace`,
`mem0`, or `off` explicitly:

```env
MEMORY_PROVIDER=auto
PALACE_ENABLED=true
MEM0_ENABLED=true
MEM0_API_KEY=
```

The signed-in Clerk user ID scopes memories. `MEMORY_USER_ID` is the fallback
identity for local scripts or tests that run without a Clerk session.

`mypalclara` automatically starts a long-running server on `127.0.0.1:8765`.
The Textual console connects to that server and can be closed without stopping
the agent or Discord bridge. To run the server in the foreground or under a
service manager instead, use:

```sh
uv run mypalclara-server
```

Set `MYPALCLARA_SERVER_URL`, `MYPALCLARA_SERVER_HOST`, and
`MYPALCLARA_SERVER_PORT` to change the local endpoint. Detached-server output
is written to `mypalclara-server.log` in the operating system's temporary
directory by default; override it with `MYPALCLARA_SERVER_LOG`.

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

While the background server is running:

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
