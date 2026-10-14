"""Local file storage tools — local-storage half of the v1 files tool module.

Save, list, read, and delete files that persist across sessions.
Files are stored per user under a personal subdirectory of the storage
root, resolved from the signed-in user (see backend.conversation).

Config comes from .env: CLARA_FILES_DIR (default ./clara_files),
CLARA_MAX_FILE_SIZE (default 50 MB).
"""

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.tools import tool

from backend import conversation

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

LOCAL_FILES_DIR = Path(os.environ.get("CLARA_FILES_DIR", "./clara_files"))
MAX_FILE_SIZE = int(os.environ.get("CLARA_MAX_FILE_SIZE", str(50 * 1024 * 1024)))


@dataclass
class FileInfo:
    """Information about a stored file."""

    name: str
    path: Path
    size: int
    created_at: datetime
    user_id: str


@dataclass
class FileResult:
    """Result of a file operation."""

    success: bool
    message: str
    file_info: FileInfo | None = None


class LocalFileManager:
    """Manages local file storage for users."""

    def __init__(self, base_dir: Path = LOCAL_FILES_DIR):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _sanitize_id(self, id_str: str) -> str:
        """Sanitize an ID for filesystem use."""
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in id_str)

    def _storage_dir(self, user_id: str) -> Path:
        """Get or create the storage directory for a user."""
        storage_dir = self.base_dir / self._sanitize_id(user_id)
        if storage_dir.is_symlink():
            raise ValueError("User storage directory cannot be a symbolic link")
        storage_dir.mkdir(parents=True, exist_ok=True)
        if storage_dir.resolve().parent != self.base_dir.resolve():
            raise ValueError("User storage directory escapes the configured root")
        return storage_dir

    def _sanitize_filename(self, filename: str) -> str | None:
        """Return a safe basename, rejecting paths and traversal attempts."""
        if not filename or Path(filename).name != filename or "\\" in filename:
            return None
        safe = "".join(c if c.isalnum() or c in ".-_" else "_" for c in filename)
        safe = safe.lstrip(".")
        return safe or None

    def save_file(self, user_id: str, filename: str, content: str) -> FileResult:
        """Save text content to a local file."""
        try:
            safe_name = self._sanitize_filename(filename)
            if safe_name is None:
                return FileResult(success=False, message="Invalid filename")
            file_path = self._storage_dir(user_id) / safe_name

            if file_path.is_symlink():
                return FileResult(success=False, message="Refusing to overwrite a symbolic link")

            content_bytes = content.encode("utf-8")
            if len(content_bytes) > MAX_FILE_SIZE:
                return FileResult(
                    success=False,
                    message=f"File too large ({len(content_bytes)} bytes, max {MAX_FILE_SIZE})",
                )

            file_path.write_text(content, encoding="utf-8")
            return FileResult(
                success=True,
                message=f"Saved to local storage: {safe_name}",
                file_info=FileInfo(
                    name=safe_name,
                    path=file_path,
                    size=len(content_bytes),
                    created_at=datetime.now(UTC),
                    user_id=user_id,
                ),
            )
        except Exception as e:
            return FileResult(success=False, message=f"Error saving file: {e}")

    def list_files(self, user_id: str) -> list[FileInfo]:
        """List all files for a user, newest first."""
        storage_dir = self._storage_dir(user_id)
        files = []
        for file_path in storage_dir.iterdir():
            if file_path.is_file():
                stat = file_path.stat()
                files.append(
                    FileInfo(
                        name=file_path.name,
                        path=file_path,
                        size=stat.st_size,
                        created_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                        user_id=user_id,
                    )
                )
        files.sort(key=lambda f: f.created_at, reverse=True)
        return files

    def read_file(self, user_id: str, filename: str) -> FileResult:
        """Read a file's content."""
        try:
            safe_name = self._sanitize_filename(filename)
            if safe_name is None:
                return FileResult(success=False, message="Invalid filename")
            file_path = self._storage_dir(user_id) / safe_name

            if not file_path.exists():
                return FileResult(success=False, message=f"File not found: {safe_name}")
            if file_path.is_symlink():
                return FileResult(success=False, message="Refusing to read a symbolic link")
            if file_path.stat().st_size > MAX_FILE_SIZE:
                return FileResult(
                    success=False,
                    message=f"File too large (max {MAX_FILE_SIZE} bytes)",
                )

            try:
                content = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = f"[Binary file: {file_path.stat().st_size} bytes]"

            stat = file_path.stat()
            return FileResult(
                success=True,
                message=content,
                file_info=FileInfo(
                    name=safe_name,
                    path=file_path,
                    size=stat.st_size,
                    created_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                    user_id=user_id,
                ),
            )
        except Exception as e:
            return FileResult(success=False, message=f"Error reading file: {e}")

    def delete_file(self, user_id: str, filename: str) -> FileResult:
        """Delete a file."""
        try:
            safe_name = self._sanitize_filename(filename)
            if safe_name is None:
                return FileResult(success=False, message="Invalid filename")
            file_path = self._storage_dir(user_id) / safe_name

            if not file_path.exists():
                return FileResult(success=False, message=f"File not found: {safe_name}")
            if file_path.is_symlink():
                return FileResult(success=False, message="Refusing to delete a symbolic link")

            file_path.unlink()
            return FileResult(success=True, message=f"Deleted: {safe_name}")
        except Exception as e:
            return FileResult(success=False, message=f"Error deleting file: {e}")


_manager: LocalFileManager | None = None


def _get_manager() -> LocalFileManager:
    global _manager
    if _manager is None:
        _manager = LocalFileManager()
    return _manager


def _format_size(size: int) -> str:
    """Format file size for display."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.1f} MB"


def _format_age(dt: datetime) -> str:
    """Format file age for display."""
    delta = datetime.now(UTC) - dt
    if delta.days > 0:
        return f"{delta.days}d ago"
    elif delta.seconds > 3600:
        return f"{delta.seconds // 3600}h ago"
    elif delta.seconds > 60:
        return f"{delta.seconds // 60}m ago"
    else:
        return "just now"


def _format_file_list(files: list[FileInfo]) -> str:
    """Format a list of files for display."""
    if not files:
        return "No files saved."
    lines = ["**Saved Files:**"]
    for f in files:
        lines.append(f"- `{f.name}` ({_format_size(f.size)}, {_format_age(f.created_at)})")
    return "\n".join(lines)


@tool
async def save_to_local(filename: str, content: str) -> str:
    """Save content to persistent file storage that survives across sessions.
    Use this to save important results, generated content, or data that
    should be available later. Files are stored per-user.

    Args:
        filename: Name for the file (e.g., 'results.csv', 'notes.md').
            Will be saved in the user's personal storage.
        content: The content to save to the file.
    """
    try:
        result = _get_manager().save_file(conversation.current_user_id(), filename, content)
        if result.success and result.file_info is not None:
            return f"Saved: `{result.file_info.name}` ({_format_size(result.file_info.size)})"
        return f"Error: {result.message}"
    except Exception as e:
        return f"Error saving file: {e}"


@tool
async def list_local_files() -> str:
    """List files saved in local storage. Shows files you've saved or that were
    uploaded by the user."""
    try:
        files = _get_manager().list_files(conversation.current_user_id())
        return _format_file_list(files)
    except Exception as e:
        return f"Error listing files: {e}"


@tool
async def read_local_file(filename: str) -> str:
    """Read content from a locally saved file. Use this to retrieve previously
    saved data or uploaded files.

    Args:
        filename: Name of the file to read.
    """
    try:
        result = _get_manager().read_file(conversation.current_user_id(), filename)
        if result.success:
            content = result.message
            if len(content) > 50000:
                content = content[:50000] + "\n\n... (truncated, file too large)"
            return content
        return f"Error: {result.message}"
    except Exception as e:
        return f"Error reading file: {e}"


@tool
async def delete_local_file(filename: str) -> str:
    """Delete a file from local storage.

    Args:
        filename: Name of the file to delete.
    """
    try:
        result = _get_manager().delete_file(conversation.current_user_id(), filename)
        if result.success:
            return result.message
        return f"Error: {result.message}"
    except Exception as e:
        return f"Error deleting file: {e}"


TOOLS = [save_to_local, list_local_files, read_local_file, delete_local_file]
