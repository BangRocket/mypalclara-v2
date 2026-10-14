"""Workspace file tools — host-only port of the v1 workspace tool module.

Gives the agent read/write access to markdown files in the shared
workspace (``backend/workspace/``): SOUL.md, IDENTITY.md, USER.md, etc.
SOUL.md is read-only (owner-controlled); any other ``.md`` file can be
read, updated, or created.
"""

from pathlib import Path

from langchain_core.tools import tool

WORKSPACE_DIR = Path(__file__).resolve().parent.parent / "workspace"
READONLY_FILES = frozenset({"SOUL.md"})


def _sanitize_filename(filename: str) -> str | None:
    """Sanitize a filename, returning None if invalid.

    Prevents path traversal and ensures the file stays in the workspace dir.
    """
    name = Path(filename).name
    if not name or name != filename:
        return None
    if ".." in name or "/" in name or "\\" in name:
        return None
    return name


@tool
async def workspace_list() -> str:
    """List all workspace files that shape your behavior and memory."""
    if not WORKSPACE_DIR.is_dir():
        return "Workspace directory not found."

    files = sorted(WORKSPACE_DIR.glob("*.md"))
    if not files:
        return "No workspace files found."

    lines = []
    for f in files:
        size = f.stat().st_size
        readonly = " (read-only)" if f.name in READONLY_FILES else ""
        lines.append(f"- **{f.name}** ({size:,} bytes){readonly}")

    return "**Workspace files:**\n" + "\n".join(lines)


@tool
async def workspace_read(filename: str) -> str:
    """Read a workspace file by name. Use workspace_list first to see available files.

    Args:
        filename: Name of the file to read (e.g., 'MEMORY.md')
    """
    safe_name = _sanitize_filename(filename)
    if not safe_name:
        return f"Error: Invalid filename '{filename}'."

    filepath = WORKSPACE_DIR / safe_name
    if not filepath.exists():
        return f"Error: '{safe_name}' not found. Use workspace_list to see available files."

    content = filepath.read_text(encoding="utf-8")
    return f"**{safe_name}:**\n\n{content}"


@tool
async def workspace_write(filename: str, content: str, mode: str = "overwrite") -> str:
    """Update a workspace file. SOUL.md is read-only.
    Use mode 'append' to add to the end, or 'overwrite' to replace entirely.

    Args:
        filename: Name of the file to write (e.g., 'MEMORY.md')
        content: Content to write
        mode: Write mode: 'overwrite' replaces the file, 'append' adds to the end
    """
    safe_name = _sanitize_filename(filename)
    if not safe_name:
        return f"Error: Invalid filename '{filename}'."

    if safe_name in READONLY_FILES:
        return f"Error: '{safe_name}' is read-only (owner-controlled). You cannot edit this file."

    if mode not in {"append", "overwrite"}:
        return "Error: mode must be 'append' or 'overwrite'."

    filepath = WORKSPACE_DIR / safe_name
    if not filepath.exists():
        return f"Error: '{safe_name}' not found. Use workspace_create to make a new file."

    if mode == "append":
        existing = filepath.read_text(encoding="utf-8")
        filepath.write_text(existing + "\n" + content, encoding="utf-8")
        new_size = filepath.stat().st_size
        return f"Appended to '{safe_name}' ({new_size:,} bytes)."
    else:
        filepath.write_text(content, encoding="utf-8")
        return f"Updated '{safe_name}' ({len(content):,} bytes)."


@tool
async def workspace_create(filename: str, content: str = "") -> str:
    """Create a new .md file in the workspace. Cannot create files that already
    exist or use the reserved name SOUL.md.

    Args:
        filename: Name for the new file (must end in .md, e.g., 'PROJECTS.md')
        content: Initial content for the file (optional, defaults to empty)
    """
    safe_name = _sanitize_filename(filename)
    if not safe_name:
        return f"Error: Invalid filename '{filename}'."

    if not safe_name.endswith(".md"):
        return f"Error: Workspace files must be markdown (.md). Got '{safe_name}'."

    if safe_name in READONLY_FILES:
        return f"Error: '{safe_name}' is a reserved name."

    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    filepath = WORKSPACE_DIR / safe_name
    if filepath.exists():
        return f"Error: '{safe_name}' already exists. Use workspace_write to update it."

    filepath.write_text(content, encoding="utf-8")
    return f"Created '{safe_name}' ({len(content):,} bytes)."


TOOLS = [workspace_list, workspace_read, workspace_write, workspace_create]
