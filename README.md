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

## Session commands

```sh
uv run mypalclara --new-chat
uv run mypalclara --logout
```
