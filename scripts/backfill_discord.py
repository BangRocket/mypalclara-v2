#!/usr/bin/env python3
"""Backfill DiscordChatExporter JSON into MyPalClara v2.

The importer understands the companion ``*_decisions.json`` files produced by
the legacy chat-cleaner workflow. Messages marked ``remove`` are skipped;
without a decisions file, all non-empty messages are imported. Discord message
IDs provide idempotency, so the same export can be run more than once safely.
With ``--ingest-memory``, historical sessions are also sent through the active
Palace or Mem0 inference pipeline. Memory progress is checkpointed per provider
and user so interrupted runs resume safely.

Examples:
    # Preview every export under ./chats
    uv run python scripts/backfill_discord.py --dry-run

    # Import the legacy exports for a specific MyPalClara/Clerk user
    uv run python scripts/backfill_discord.py \
        --chats-dir /path/to/old/mypalclara/chats \
        --user-id user_abc123 \
        --ingest-memory

    # Import one export
    uv run python scripts/backfill_discord.py \
        --file "chats/Direct Messages - MyPalClara [123].json" \
        --user-id user_abc123
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import chat_store, mem0, memory, palace, session_store  # noqa: E402

log = logging.getLogger("discord_backfill")

DEFAULT_CLARA_BOTS = {"MyPalClara", "MyPalClarissa"}
DEFAULT_NAME_MAP = {
    "stairmaster401": "Josh",
    "cadacious": "cadacious",
    "MyPalClara": "Clara",
    "MyPalClarissa": "Clarissa",
}


@dataclass(frozen=True, slots=True)
class DiscordMessage:
    message_id: str
    role: str
    content: str
    author_name: str
    timestamp: datetime | None


@dataclass(frozen=True, slots=True)
class DiscordExport:
    path: Path
    channel_id: str
    channel_name: str
    guild_name: str
    is_dm: bool
    total_messages: int
    removed_messages: int
    empty_messages: int
    decisions_found: bool
    messages: tuple[DiscordMessage, ...]


@dataclass(frozen=True, slots=True)
class MemoryJob:
    """One historical Discord session to submit for memory inference."""

    fingerprint: str
    channel_id: str
    channel_name: str
    guild_name: str
    messages: tuple[DiscordMessage, ...]


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _message_content(raw: dict) -> str:
    """Combine message text and durable attachment/sticker references."""
    parts: list[str] = []
    content = raw.get("content")
    if isinstance(content, str) and content.strip():
        parts.append(content.strip())

    for attachment in raw.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        url = attachment.get("url")
        if isinstance(url, str) and url.strip():
            parts.append(url.strip())

    for sticker in raw.get("stickers") or []:
        if not isinstance(sticker, dict):
            continue
        value = sticker.get("url") or sticker.get("name")
        if isinstance(value, str) and value.strip():
            parts.append(f"[sticker: {value.strip()}]")

    return "\n".join(parts)


def load_export(
    path: Path,
    *,
    include_removed: bool = False,
    require_decisions: bool = False,
    clara_bots: set[str] | None = None,
    name_map: dict[str, str] | None = None,
    speaker_labels: bool = True,
) -> DiscordExport:
    """Load and normalize one DiscordChatExporter JSON document."""
    try:
        raw_export = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read {path}: {exc}") from exc
    if not isinstance(raw_export, dict) or not isinstance(
        raw_export.get("messages"), list
    ):
        raise ValueError(f"{path} is not a DiscordChatExporter JSON file")

    decisions_path = path.with_name(f"{path.stem}_decisions.json")
    decisions: dict = {}
    decisions_found = decisions_path.exists()
    if decisions_found and not include_removed:
        try:
            loaded_decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Unable to read {decisions_path}: {exc}") from exc
        if not isinstance(loaded_decisions, dict):
            raise ValueError(f"{decisions_path} must contain a JSON object")
        decisions = loaded_decisions
    elif require_decisions and not decisions_found:
        raise ValueError(f"Missing decisions file for {path}")

    guild = raw_export.get("guild") or {}
    channel = raw_export.get("channel") or {}
    guild_name = str(guild.get("name") or "unknown")
    channel_name = str(channel.get("name") or "unknown")
    channel_id = str(channel.get("id") or path.stem)
    is_dm = guild_name == "Direct Messages" or channel.get("type") == "DirectTextChat"
    bot_names = clara_bots or DEFAULT_CLARA_BOTS
    canonical_names = name_map or DEFAULT_NAME_MAP

    messages: list[DiscordMessage] = []
    removed = 0
    empty = 0
    raw_messages = raw_export["messages"]
    for raw in raw_messages:
        if not isinstance(raw, dict):
            empty += 1
            continue
        message_id = str(raw.get("id") or "").strip()
        decision = decisions.get(message_id) or {}
        if not include_removed and decision.get("status", "keep") == "remove":
            removed += 1
            continue

        content = _message_content(raw)
        if not message_id or not content:
            empty += 1
            continue

        author = raw.get("author") or {}
        author_name = str(author.get("name") or "unknown")
        canonical_name = canonical_names.get(author_name, author_name)
        role = "assistant" if author_name in bot_names else "user"
        if speaker_labels and not is_dm:
            content = f"[{canonical_name}] {content}"
        messages.append(
            DiscordMessage(
                message_id=message_id,
                role=role,
                content=content,
                author_name=canonical_name,
                timestamp=_parse_timestamp(raw.get("timestamp")),
            )
        )

    # DiscordChatExporter is normally chronological, but sorting makes imports
    # predictable when exports have been merged or manually edited.
    messages.sort(
        key=lambda message: (
            message.timestamp is None,
            message.timestamp or datetime.max.replace(tzinfo=UTC),
            message.message_id,
        )
    )
    return DiscordExport(
        path=path,
        channel_id=channel_id,
        channel_name=channel_name,
        guild_name=guild_name,
        is_dm=is_dm,
        total_messages=len(raw_messages),
        removed_messages=removed,
        empty_messages=empty,
        decisions_found=decisions_found,
        messages=tuple(messages),
    )


def split_into_sessions(
    messages: tuple[DiscordMessage, ...],
    *,
    gap_minutes: int = 30,
    max_messages: int = 100,
) -> list[list[DiscordMessage]]:
    """Split messages at long time gaps and a configurable hard limit."""
    if not messages:
        return []
    sessions: list[list[DiscordMessage]] = []
    current = [messages[0]]
    for message in messages[1:]:
        previous = current[-1]
        has_gap = False
        if previous.timestamp is not None and message.timestamp is not None:
            gap = (message.timestamp - previous.timestamp).total_seconds() / 60
            has_gap = gap > gap_minutes
        if has_gap or len(current) >= max_messages:
            sessions.append(current)
            current = []
        current.append(message)
    sessions.append(current)
    return sessions


def build_writes(
    export: DiscordExport,
    *,
    user_id: str,
    gap_minutes: int,
    max_session_messages: int,
) -> list[chat_store.ChatMessageWrite]:
    """Convert an export to deterministic transcript writes."""
    writes: list[chat_store.ChatMessageWrite] = []
    sessions = split_into_sessions(
        export.messages,
        gap_minutes=gap_minutes,
        max_messages=max_session_messages,
    )
    for session in sessions:
        conversation_id = f"discord:{export.channel_id}:{session[0].message_id}"
        writes.extend(
            chat_store.ChatMessageWrite(
                user_id=user_id,
                conversation_id=conversation_id,
                role=message.role,
                content=message.content,
                source="discord",
                external_id=message.message_id,
                created_at=message.timestamp,
            )
            for message in session
        )
    return writes


def _memory_fingerprint(
    export: DiscordExport, messages: list[DiscordMessage]
) -> str:
    payload = {
        "channel_id": export.channel_id,
        "messages": [
            [message.message_id, message.role, message.content] for message in messages
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def build_memory_jobs(
    export: DiscordExport,
    *,
    gap_minutes: int,
    max_session_messages: int,
    min_session_messages: int,
) -> list[MemoryJob]:
    """Build stable, provider-neutral memory inference jobs."""
    sessions = split_into_sessions(
        export.messages,
        gap_minutes=gap_minutes,
        max_messages=max_session_messages,
    )
    return [
        MemoryJob(
            fingerprint=_memory_fingerprint(export, session),
            channel_id=export.channel_id,
            channel_name=export.channel_name,
            guild_name=export.guild_name,
            messages=tuple(session),
        )
        for session in sessions
        if len(session) >= min_session_messages
    ]


def _job_metadata(job: MemoryJob) -> dict:
    first = job.messages[0].timestamp
    last = job.messages[-1].timestamp
    return {
        "backfill": True,
        "channel_id": job.channel_id,
        "channel_name": job.channel_name,
        "guild_name": job.guild_name,
        "session_fingerprint": job.fingerprint,
        "message_count": len(job.messages),
        "started_at": first.isoformat() if first else None,
        "ended_at": last.isoformat() if last else None,
    }


async def ingest_memory_job(
    job: MemoryJob, *, provider: str, user_id: str
) -> object:
    """Submit one session to the selected provider with inference enabled."""
    messages = [
        {"role": message.role, "content": message.content}
        for message in job.messages
    ]
    metadata = _job_metadata(job)
    if provider == "palace":
        return await palace._get_client().add(
            messages=messages,
            user_id=user_id,
            metadata=metadata,
            source="discord-backfill",
            infer=True,
        )
    if provider == "mem0":
        return await mem0.add(
            messages,
            user_id=user_id,
            source="discord-backfill",
            infer=True,
            metadata=metadata,
        )
    raise ValueError(f"Unsupported memory provider: {provider}")


def _memory_progress_namespace(provider: str, user_id: str) -> str:
    identity = hashlib.sha256(user_id.encode()).hexdigest()[:16]
    return f"discord-memory-backfill:v1:{provider}:{identity}"


async def ingest_memory_jobs(
    jobs: list[MemoryJob],
    *,
    provider: str,
    user_id: str,
    concurrency: int,
    reingest: bool,
) -> tuple[int, int, int]:
    """Ingest jobs and return ``(completed, skipped, errors)``."""
    namespace = _memory_progress_namespace(provider, user_id)
    try:
        progress = session_store.load_namespace(namespace)
    except Exception as exc:
        raise RuntimeError(f"Unable to load memory backfill progress: {exc}") from exc

    pending = [job for job in jobs if reingest or job.fingerprint not in progress]
    skipped = len(jobs) - len(pending)
    if not pending:
        return 0, skipped, 0

    semaphore = asyncio.Semaphore(concurrency)

    async def process(job: MemoryJob) -> tuple[bool, MemoryJob, str | None]:
        async with semaphore:
            try:
                await ingest_memory_job(job, provider=provider, user_id=user_id)
                await asyncio.to_thread(
                    session_store.save,
                    namespace,
                    job.fingerprint,
                    {
                        "provider": provider,
                        "user_id": user_id,
                        "channel_id": job.channel_id,
                        "message_count": len(job.messages),
                        "completed_at": datetime.now(UTC).isoformat(),
                    },
                )
            except Exception as exc:
                return False, job, str(exc)
            return True, job, None

    completed = 0
    errors = 0
    tasks = [asyncio.create_task(process(job)) for job in pending]
    for index, task in enumerate(asyncio.as_completed(tasks), start=1):
        success, job, error = await task
        if success:
            completed += 1
        else:
            errors += 1
            log.error(
                "Memory session %s in channel %s failed: %s",
                job.fingerprint[:12],
                job.channel_name,
                error,
            )
        if index % 10 == 0 or index == len(tasks):
            log.info(
                "  memory progress: %d/%d (%d failed)",
                index,
                len(tasks),
                errors,
            )
    return completed, skipped, errors


def _parse_name_map(values: list[str]) -> dict[str, str]:
    result = dict(DEFAULT_NAME_MAP)
    for value in values:
        source, separator, canonical = value.partition("=")
        if not separator or not source.strip() or not canonical.strip():
            raise ValueError(f"Invalid --name value {value!r}; expected DISCORD=NAME")
        result[source.strip()] = canonical.strip()
    return result


def _find_files(chats_dir: Path, files: list[Path]) -> list[Path]:
    candidates = files or sorted(chats_dir.glob("*.json"))
    return [
        path.resolve()
        for path in candidates
        if not path.stem.endswith("_decisions")
    ]


def _default_user_id() -> str:
    configured = os.environ.get("MEMORY_USER_ID") or os.environ.get("PALACE_USER_ID")
    if configured:
        return configured
    from backend import auth

    return auth.current_user_id() or "local-user"


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill DiscordChatExporter JSON into MyPalClara v2",
    )
    parser.add_argument(
        "--chats-dir",
        type=Path,
        default=PROJECT_ROOT / "chats",
        help="directory scanned when --file is omitted (default: ./chats)",
    )
    parser.add_argument(
        "--file",
        action="append",
        type=Path,
        default=[],
        help="specific export to import; may be repeated",
    )
    parser.add_argument(
        "--user-id",
        help="target MyPalClara/Clerk user (default: signed-in or configured user)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="preview without writing"
    )
    parser.add_argument(
        "--include-removed",
        action="store_true",
        help="ignore decisions files and include messages marked remove",
    )
    parser.add_argument(
        "--require-decisions",
        action="store_true",
        help="reject exports without a companion decisions file",
    )
    parser.add_argument(
        "--no-speaker-labels",
        action="store_true",
        help="do not prefix messages from group channels with the author name",
    )
    parser.add_argument(
        "--clara-bot",
        action="append",
        default=[],
        metavar="NAME",
        help="additional Discord bot author to import as assistant",
    )
    parser.add_argument(
        "--name",
        action="append",
        default=[],
        metavar="DISCORD=NAME",
        help="map a Discord author name to a canonical display name",
    )
    parser.add_argument(
        "--gap-minutes",
        type=int,
        default=30,
        help="start a new historical conversation after this gap (default: 30)",
    )
    parser.add_argument(
        "--max-session-messages",
        type=int,
        default=100,
        help="maximum messages per historical conversation (default: 100)",
    )
    parser.add_argument(
        "--ingest-memory",
        action="store_true",
        help="also infer memories with the active Palace or Mem0 provider",
    )
    parser.add_argument(
        "--memory-only",
        action="store_true",
        help="infer memories without writing the canonical transcript",
    )
    parser.add_argument(
        "--max-memory-session-messages",
        type=int,
        default=40,
        help="maximum messages per memory inference request (default: 40)",
    )
    parser.add_argument(
        "--min-memory-session-messages",
        type=int,
        default=2,
        help="minimum messages required for memory inference (default: 2)",
    )
    parser.add_argument(
        "--memory-concurrency",
        type=int,
        default=1,
        help="simultaneous memory inference requests (default: 1)",
    )
    parser.add_argument(
        "--reingest-memory",
        action="store_true",
        help="ignore completed memory checkpoints and submit every session again",
    )
    return parser


async def _run(args, *, files: list[Path], name_map: dict[str, str]) -> int:
    user_id = args.user_id or (
        "dry-run-user" if args.dry_run else _default_user_id()
    )
    bot_names = DEFAULT_CLARA_BOTS | set(args.clara_bot)
    total_candidates = 0
    total_inserted = 0
    total_sessions = 0
    all_memory_jobs: list[MemoryJob] = []
    imports: list[tuple[DiscordExport, list[chat_store.ChatMessageWrite]]] = []

    log.info("Target user: %s%s", user_id, " (dry run)" if args.dry_run else "")
    for path in files:
        try:
            export = load_export(
                path,
                include_removed=args.include_removed,
                require_decisions=args.require_decisions,
                clara_bots=bot_names,
                name_map=name_map,
                speaker_labels=not args.no_speaker_labels,
            )
        except ValueError as exc:
            log.error("%s", exc)
            return 1
        writes = build_writes(
            export,
            user_id=user_id,
            gap_minutes=args.gap_minutes,
            max_session_messages=args.max_session_messages,
        )
        sessions = {write.conversation_id for write in writes}
        total_candidates += len(writes)
        total_sessions += len(sessions)
        memory_jobs = build_memory_jobs(
            export,
            gap_minutes=args.gap_minutes,
            max_session_messages=args.max_memory_session_messages,
            min_session_messages=args.min_memory_session_messages,
        )
        all_memory_jobs.extend(memory_jobs)
        imports.append((export, writes))
        if args.include_removed and export.decisions_found:
            decision_status = "decisions ignored"
        elif export.decisions_found:
            decision_status = "decisions applied"
        else:
            decision_status = "no decisions"
        log.info(
            "%s: %d/%d messages, %d sessions, %d removed, %d empty (%s)",
            export.path.name,
            len(writes),
            export.total_messages,
            len(sessions),
            export.removed_messages,
            export.empty_messages,
            decision_status,
        )

    provider: str | None = None
    if args.ingest_memory and not args.dry_run:
        provider = await memory.provider_name(user_id)
        if provider is None:
            log.error(
                "No healthy memory provider is available; check MEMORY_PROVIDER "
                "and the Palace/Mem0 configuration"
            )
            return 1
        log.info(
            "Memory provider: %s (%d eligible sessions)",
            provider,
            len(all_memory_jobs),
        )

    if not args.dry_run and not args.memory_only:
        for export, writes in imports:
            try:
                inserted = chat_store.record_messages(writes)
            except Exception as exc:
                log.error("Database import failed for %s: %s", export.path.name, exc)
                return 1
            total_inserted += inserted
            log.info(
                "  inserted %d; skipped %d existing",
                inserted,
                len(writes) - inserted,
            )

    memory_completed = 0
    memory_skipped = 0
    memory_errors = 0
    if args.ingest_memory:
        if args.dry_run:
            log.info(
                "Memory dry run: %d sessions would be submitted to the active provider",
                len(all_memory_jobs),
            )
        else:
            try:
                memory_completed, memory_skipped, memory_errors = (
                    await ingest_memory_jobs(
                        all_memory_jobs,
                        provider=provider or "",
                        user_id=user_id,
                        concurrency=args.memory_concurrency,
                        reingest=args.reingest_memory,
                    )
                )
            except Exception as exc:
                log.error("Memory backfill failed: %s", exc)
                return 1
            log.info(
                "Memory backfill: %d completed, %d previously completed, %d failed",
                memory_completed,
                memory_skipped,
                memory_errors,
            )

    if args.dry_run:
        log.info(
            "Dry run complete: %d messages in %d sessions from %d file(s)",
            total_candidates,
            total_sessions,
            len(files),
        )
    elif not args.memory_only:
        log.info(
            "Backfill complete: inserted %d/%d messages in %d sessions from %d file(s)",
            total_inserted,
            total_candidates,
            total_sessions,
            len(files),
        )
    return 1 if memory_errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.memory_only:
        args.ingest_memory = True
    if args.gap_minutes < 0:
        parser.error("--gap-minutes must be zero or greater")
    if args.max_session_messages < 1:
        parser.error("--max-session-messages must be at least one")
    if args.max_memory_session_messages < 1:
        parser.error("--max-memory-session-messages must be at least one")
    if args.min_memory_session_messages < 1:
        parser.error("--min-memory-session-messages must be at least one")
    if args.memory_concurrency < 1:
        parser.error("--memory-concurrency must be at least one")
    try:
        name_map = _parse_name_map(args.name)
    except ValueError as exc:
        parser.error(str(exc))

    files = _find_files(args.chats_dir, args.file)
    if not files:
        parser.error(f"no Discord export JSON files found in {args.chats_dir}")
    missing = [path for path in files if not path.is_file()]
    if missing:
        parser.error(f"export file does not exist: {missing[0]}")

    return asyncio.run(_run(args, files=files, name_map=name_map))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
