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

Discord exempts bot DMs from the Message Content privileged-intent restriction,
so this integration requests only the direct-message gateway intent.

## Session commands

```sh
uv run mypalclara --new-chat
uv run mypalclara --logout
```
