# Tool Configuration Notes

Clara has internal tools available in every conversation:

- **terminal**: run shell commands, manage a working directory, read/write
  files on the host (`run_command`, `read_file`, `write_file`, ...).
  Dangerous commands are blocked by a pattern list.
- **process**: start and monitor named background processes
  (`process_start`, `process_output`, `process_stop`, ...).
- **workspace**: read and update the markdown files in this workspace.
  `SOUL.md` is read-only; new files must be `.md`.
- **chat_history**: search and page through the stored cross-interface
  transcript (console + Discord).
- **files**: save/list/read/delete files under `CLARA_FILES_DIR`
  (default `./clara_files`), stored per user.
- **memory**: search, list, add, update, and delete long-term memories in
  the active MyPalace or Mem0 provider. Unavailable when no configured
  provider is reachable.
- **browser**: headless Chromium automation (navigate, screenshot, extract,
  click, type, evaluate JS). Only available when Playwright is installed.
- **obsidian**: list, read, search, create, append, trash, and open vault
  notes through the official Obsidian CLI. Daily-note and tag tools are also
  available when `OBSIDIAN_CLI` is installed. Configure a target with
  `OBSIDIAN_VAULT` (name or ID) or `OBSIDIAN_VAULT_PATH` (local directory).

Tool-specific notes:

- Prefer `workspace_write` append mode for journal-style files.
- `save_to_local` is for durable user-facing artifacts; use the terminal
  tools for scratch work.
