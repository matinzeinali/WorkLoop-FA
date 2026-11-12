"""File tools for the agents. Every path is relative to the project folder.

All tools go through resolve_safe_path(). Violations raise ToolError; the
message is shown to the agent so it can correct itself, and nothing crashes.

These functions are synchronous (plain filesystem work). The tool registry
runs them in a worker thread.
"""

from __future__ import annotations

import fnmatch
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

MAX_FILE_BYTES = 1024 * 1024  # 1 MB for read and write
MAX_LINES_PER_READ = 2000  # lines shown when no range is given
BINARY_SNIFF_BYTES = 8192

IGNORED_NAMES = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".idea", ".vscode"}
)
IGNORED_PATTERNS = ("*.pyc",)

# Windows device names that can't be used as file names (with or without extension).
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)

_CASE_INSENSITIVE_FS = sys.platform in ("win32", "darwin")


class ToolError(Exception):
    """A tool could not do what was asked. The message is meant for the agent."""


@dataclass
class ToolOutput:
    ok: bool
    content: str  # what the model sees
    summary: str  # short line for the tool_result event


# --- Path security --------------------------------------------------------------


def _path_key(path: Path | str) -> str:
    text = os.path.normpath(str(path))
    return text.casefold() if _CASE_INSENSITIVE_FS else text


def _is_inside(path: Path, root: Path) -> bool:
    p, r = _path_key(path), _path_key(root)
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)


def _is_git_part(part: str) -> bool:
    # Windows ignores trailing dots/spaces and case: ".GIT." is the same folder as ".git".
    return part.rstrip(". ").casefold() == ".git"


def _split_relative(rel_path: str) -> list[str]:
    if not isinstance(rel_path, str):
        raise ToolError("Path must be a string.")
    if "\x00" in rel_path:
        raise ToolError("Path contains a null byte.")
    text = rel_path.strip()
    if not text:
        raise ToolError("Path is empty. Use '.' for the project folder.")
    if text.startswith(("\\\\", "//")):
        raise ToolError(f"UNC/network paths are not allowed: {rel_path!r}. Use a path relative to the project.")
    win = PureWindowsPath(text)
    if win.drive or win.root or PurePosixPath(text).is_absolute():
        raise ToolError(f"Absolute paths are not allowed: {rel_path!r}. Use a path relative to the project.")
    if text.startswith("~"):
        raise ToolError(f"'~' paths are not allowed: {rel_path!r}. Use a path relative to the project.")
    if ":" in text:
        raise ToolError(f"':' is not allowed in paths (drive letters, alternate streams): {rel_path!r}.")

    parts = [p for p in text.replace("\\", "/").split("/") if p not in ("", ".")]
    for part in parts:
        if part == ".." or part.rstrip(". ") == "..":
            raise ToolError(f"'..' is not allowed in paths: {rel_path!r}.")
        if sys.platform == "win32":
            if part.split(".")[0].rstrip(" ").casefold() in _WINDOWS_RESERVED:
                raise ToolError(f"'{part}' is a reserved device name on Windows.")
            if any(ch in part for ch in '<>"|?*'):
                raise ToolError(f"Invalid characters in path part: {part!r}.")
    return parts


def _resolve(project_root: Path, rel_path: str, for_write: bool) -> tuple[Path, Path]:
    """Return (lexical path, fully resolved path); both are checked to be inside the root."""
    try:
        root = project_root.resolve(strict=True)
    except OSError as exc:
        raise ToolError(f"Project folder is not available: {project_root}") from exc
    parts = _split_relative(rel_path)
    lexical = root.joinpath(*parts)
    try:
        resolved = lexical.resolve(strict=False)  # follows symlinks and junctions
    except (OSError, RuntimeError) as exc:
        raise ToolError(f"Could not resolve path {rel_path!r}: {exc}") from exc

    if not _is_inside(resolved, root):
        raise ToolError(f"Path {rel_path!r} points outside the project folder (symlink or junction).")
    if for_write:
        rel_parts = Path(os.path.relpath(resolved, root)).parts if resolved != root else ()
        if any(_is_git_part(p) for p in (*parts, *rel_parts)):
            raise ToolError("Writing inside .git is not allowed.")
    return lexical, resolved


def resolve_safe_path(project_root: Path, rel_path: str, *, for_write: bool = False) -> Path:
    """Absolute path for rel_path inside project_root, or raise ToolError."""
    return _resolve(project_root, rel_path, for_write)[1]


def _display(root: Path, path: Path) -> str:
    rel = os.path.relpath(path, root.resolve())
    return "." if rel == "." else Path(rel).as_posix()


# --- list_files -----------------------------------------------------------------


def is_ignored(name: str) -> bool:
    """True for folders and files that tools and snapshots skip (.git, node_modules, *.pyc, ...)."""
    return name in IGNORED_NAMES or any(fnmatch.fnmatch(name, pat) for pat in IGNORED_PATTERNS)



def list_files(project_root: Path, path: str = ".", recursive: bool = False, max_entries: int = 500) -> ToolOutput:
    max_entries = max(1, min(int(max_entries), 5000))
    target = resolve_safe_path(project_root, path)
    root = project_root.resolve()
    if not target.exists():
        raise ToolError(f"Folder not found: {path}")
    if not target.is_dir():
        raise ToolError(f"Not a folder: {path}. Use read_file for files.")

    entries: list[tuple[str, str, int | None]] = []
    truncated = False

    def visit(folder: Path) -> bool:
        """Add entries of folder; return False once max_entries is reached."""
        nonlocal truncated
        try:
            items = sorted(os.scandir(folder), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except OSError:
            return True
        for item in items:
            if is_ignored(item.name):
                continue
            item_path = Path(item.path)
            # Junctions (Windows) are links too, but DirEntry.is_symlink() doesn't report them.
            is_link = item.is_symlink() or item.is_junction()
            if is_link:
                try:
                    if not _is_inside(item_path.resolve(), root):
                        continue  # never reveal what a link outside the project points to
                except OSError:
                    continue
            is_dir = item.is_dir()
            if len(entries) >= max_entries:
                truncated = True
                return False
            size = None
            if not is_dir:
                try:
                    size = item.stat().st_size
                except OSError:
                    size = None
            entries.append((_display(root, item_path), "dir" if is_dir else "file", size))
            if recursive and is_dir and not is_link and not visit(item_path):
                return False
        return True

    visit(target)

    shown = _display(root, target)
    lines = [f"{'dir ' if kind == 'dir' else 'file'}  {p}{'/' if kind == 'dir' else ''}"
             + (f"  ({size} bytes)" if size is not None else "")
             for p, kind, size in entries]
    if not lines:
        lines = ["(empty)"]
    header = f"Contents of {shown}{' (recursive)' if recursive else ''}: {len(entries)} entries"
    if truncated:
        lines.append(f"[Truncated at {max_entries} entries. List a subfolder or raise max_entries.]")
    summary = f"Listed {len(entries)} entries in {shown}" + (" (truncated)" if truncated else "")
    return ToolOutput(True, header + "\n" + "\n".join(lines), summary)


# --- read helpers ---------------------------------------------------------------


def _looks_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True  # UTF-16; treat as binary for line tools
    control = sum(1 for b in sample if b < 32 and b not in (9, 10, 12, 13, 27))
    return control / len(sample) > 0.30


def _decode(data: bytes) -> tuple[str, bool, bool]:
    """Return (text, had_bom, used_replacement)."""
    bom = data.startswith(b"\xef\xbb\xbf")
    if bom:
        data = data[3:]
    try:
        return data.decode("utf-8"), bom, False
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), bom, True


def _read_text_file(path: Path, rel: str) -> tuple[bytes, str, bool, bool]:
    if not path.exists():
        raise ToolError(f"File not found: {rel}")
    if not path.is_file():
        raise ToolError(f"Not a file: {rel}")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ToolError(f"File is too large ({size} bytes, limit {MAX_FILE_BYTES}).")
    data = path.read_bytes()
    if _looks_binary(data[:BINARY_SNIFF_BYTES]):
        raise ToolError(f"{rel} looks like a binary file; it can't be read as text.")
    text, bom, replaced = _decode(data)
    return data, text, bom, replaced


# --- read_file ------------------------------------------------------------------


def read_file(
    project_root: Path, path: str, start_line: int | None = None, end_line: int | None = None
) -> ToolOutput:
    target = resolve_safe_path(project_root, path)
    rel = _display(project_root, target)
    if not target.exists():
        raise ToolError(f"File not found: {path}")
    if not target.is_file():
        raise ToolError(f"Not a file: {path}. Use list_files for folders.")
    if start_line is not None and start_line < 1:
        raise ToolError("start_line must be 1 or more.")
    if end_line is not None and end_line < 1:
        raise ToolError("end_line must be 1 or more.")
    if start_line is not None and end_line is not None and end_line < start_line:
        raise ToolError("end_line must be greater than or equal to start_line.")

    size = target.stat().st_size
    with target.open("rb") as fh:
        sample = fh.read(BINARY_SNIFF_BYTES)
    if _looks_binary(sample):
        raise ToolError(f"{rel} looks like a binary file ({size} bytes); it can't be read as text.")

    has_range = start_line is not None or end_line is not None
    if size > MAX_FILE_BYTES and not has_range:
        raise ToolError(
            f"{rel} is {size} bytes (limit {MAX_FILE_BYTES} without a range). "
            "Read part of it with start_line and end_line."
        )

    first = start_line or 1
    replaced = False
    capped = False  # range output hit the 1 MB limit
    selected: list[str] = []
    total = 0
    selected_bytes = 0
    with target.open("rb") as fh:
        for raw in fh:
            total += 1
            if total == 1 and raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            if total < first or (end_line is not None and total > end_line) or capped:
                continue
            if not has_range and len(selected) >= MAX_LINES_PER_READ:
                continue
            if selected_bytes + len(raw) > MAX_FILE_BYTES:
                capped = True
                continue
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                line = raw.decode("utf-8", errors="replace")
                replaced = True
            selected.append(line.rstrip("\r\n"))
            selected_bytes += len(raw)

    if total == 0:
        return ToolOutput(True, f"{rel} (0 lines)\n(empty file)", f"Read {rel} (empty)")
    if first > total:
        raise ToolError(f"start_line {first} is past the end of {rel} ({total} lines).")

    last_shown = first + len(selected) - 1
    width = len(str(last_shown))
    body = "\n".join(f"{n:>{width}} | {line}" for n, line in zip(range(first, last_shown + 1), selected, strict=True))
    header = f"{rel} ({total} lines, showing {first}-{last_shown})"
    notes = []
    if last_shown < total and not has_range:
        notes.append(f"[Showing the first {len(selected)} of {total} lines. Use start_line/end_line for more.]")
    elif capped:
        notes.append("[Output capped at 1 MB. Ask for a smaller range.]")
    if replaced:
        notes.append("[File is not valid UTF-8; invalid bytes were replaced with U+FFFD.]")
    content = header + "\n" + body + ("\n" + "\n".join(notes) if notes else "")
    return ToolOutput(True, content, f"Read {rel} lines {first}-{last_shown} of {total}")


# --- write helpers --------------------------------------------------------------


def _atomic_write(target: Path, data: bytes) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # e.g. path too long on Windows, or a file where a folder should be
        raise ToolError(f"Could not create the folder for {target.name}: {exc.strerror or exc}") from exc
    original_mode = None
    if target.exists():
        try:
            original_mode = stat.S_IMODE(target.stat().st_mode)
        except OSError:
            original_mode = None
    fd, tmp_name = tempfile.mkstemp(prefix=".wl-tmp-", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if original_mode is not None:
            os.chmod(tmp, original_mode)
        os.replace(tmp, target)
    except PermissionError as exc:
        tmp.unlink(missing_ok=True)
        raise ToolError(f"Permission denied writing {target.name} (is it open in another program?).") from exc
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise ToolError(f"Could not write {target.name}: {exc}") from exc


def _apply_line_endings(text: str, crlf: bool) -> str:
    text = text.replace("\r\n", "\n")
    return text.replace("\n", "\r\n") if crlf else text


# --- write_file -----------------------------------------------------------------


def write_file(project_root: Path, path: str, content: str) -> ToolOutput:
    target = resolve_safe_path(project_root, path, for_write=True)
    rel = _display(project_root, target)
    if target.exists() and not target.is_file():
        raise ToolError(f"{rel} exists and is not a file.")
    if target == project_root.resolve():
        raise ToolError("Give a file path, not the project folder.")

    existed = target.exists()
    crlf = bom = False
    if existed:
        try:
            old = target.read_bytes()
        except OSError as exc:
            raise ToolError(f"Could not read existing {rel}: {exc}") from exc
        crlf = b"\r\n" in old
        bom = old.startswith(b"\xef\xbb\xbf")

    text = _apply_line_endings(content, crlf) if existed else content
    data = (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ToolError(f"Content is too large ({len(data)} bytes, limit {MAX_FILE_BYTES}).")

    _atomic_write(target, data)
    action = "updated" if existed else "created"
    label = f"{action.capitalize()} {rel}"
    return ToolOutput(True, f"{label} ({len(data)} bytes written).", f"{label} ({len(data)} bytes)")


# --- edit_file ------------------------------------------------------------------


def edit_file(project_root: Path, path: str, old_text: str, new_text: str) -> ToolOutput:
    if not old_text:
        raise ToolError("old_text must not be empty.")
    target = resolve_safe_path(project_root, path, for_write=True)
    rel = _display(project_root, target)
    _raw, text, bom, replaced = _read_text_file(target, rel)
    if replaced:
        raise ToolError(f"{rel} is not valid UTF-8; editing it could corrupt it. Use write_file instead.")

    # Match the model's text (usually LF) against the file in its own line-ending style and replace
    # only that span, so the rest of the file keeps its bytes (mixed CRLF/LF files stay as they are).
    old_lf = old_text.replace("\r\n", "\n")
    new_lf = new_text.replace("\r\n", "\n")
    has_crlf = "\r\n" in text
    has_lf = "\n" in text.replace("\r\n", "")
    styles = ["\r\n"] if has_crlf and not has_lf else ["\n"] if not has_crlf else ["\n", "\r\n"]
    first, old, new = -1, old_lf, new_lf
    for eol in styles:
        candidate = old_lf.replace("\n", eol)
        first = text.find(candidate)
        if first != -1:
            old, new = candidate, new_lf.replace("\n", eol)
            break
    if first == -1:
        raise ToolError(
            f"old_text not found in {rel}. It must match the file exactly, including spaces and "
            "indentation. Read the file again and copy the text."
        )
    if text.find(old, first + 1) != -1:
        count = text.count(old)
        raise ToolError(
            f"old_text is ambiguous: it appears {count if count > 1 else 'more than one'} times in {rel}. "
            "Add more surrounding lines so it matches exactly once."
        )

    updated = text[:first] + new + text[first + len(old):]
    data = (b"\xef\xbb\xbf" if bom else b"") + updated.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ToolError(f"Result would be too large ({len(data)} bytes, limit {MAX_FILE_BYTES}).")
    _atomic_write(target, data)

    start = text.count("\n", 0, first) + 1
    end = start + max(new_lf.count("\n") - (1 if new_lf.endswith("\n") else 0), 0)
    lines = f"line {start}" if start == end else f"lines {start}-{end}"
    if not new:
        lines = f"removed text at line {start}"
    return ToolOutput(True, f"Edited {rel}: {lines}.", f"Edited {rel} ({lines})")


# --- delete_file ----------------------------------------------------------------


def delete_file(project_root: Path, path: str) -> ToolOutput:
    lexical, resolved = _resolve(project_root, path, for_write=True)
    rel = _display(project_root, lexical if lexical.is_symlink() else resolved)
    if lexical.is_symlink():
        lexical.unlink()  # remove the link itself, never its target
        return ToolOutput(True, f"Deleted symlink {rel}.", f"Deleted {rel}")
    if not resolved.exists():
        raise ToolError(f"File not found: {path}")
    if resolved.is_dir():
        raise ToolError(f"{rel} is a folder. Only files can be deleted.")
    try:
        resolved.unlink()
    except OSError as exc:
        raise ToolError(f"Could not delete {rel}: {exc}") from exc
    return ToolOutput(True, f"Deleted {rel}.", f"Deleted {rel}")
