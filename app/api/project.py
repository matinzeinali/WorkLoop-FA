"""Project folder endpoints: the folder the agents read, write and run commands in."""

import os
import sys
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, StringConstraints
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.core.errors import ApiError, ErrorCode
from app.core.queue import ensure_no_active_run
from app.db import repository as repo
from app.db.database import get_db

router = APIRouter(tags=["project"])

DbSession = Annotated[Session, Depends(get_db)]

# Folders skipped when counting project files.
IGNORED_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"})


class ProjectOut(BaseModel):
    path: str | None
    exists: bool
    name: str | None
    file_count: int | None


class ProjectUpdate(BaseModel):
    path: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


def _is_junction(path: str) -> bool:
    """os.path.isjunction exists only on Python 3.12+; fall back to the reparse-point check."""
    if hasattr(os.path, "isjunction"):
        return os.path.isjunction(path)
    if sys.platform != "win32":
        return False
    try:
        import stat

        attrs = os.lstat(path).st_file_attributes  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return False
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def count_files(root: Path) -> int:
    """Count files under root, skipping IGNORED_DIRS and unreadable folders.

    Symlinked folders and Windows junctions are not followed (os.walk follows junctions).
    """
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in IGNORED_DIRS
            and not os.path.islink(p := os.path.join(dirpath, d))
            and not _is_junction(p)
        ]
        total += len(filenames)
    return total


def project_info(path: str | None) -> dict[str, Any]:
    if not path:
        return {"path": None, "exists": False, "name": None, "file_count": None}
    folder = Path(path)
    exists = folder.is_dir()
    return {
        "path": str(folder),
        "exists": exists,
        "name": folder.name or str(folder),  # a drive root like "E:\\" has no name
        "file_count": count_files(folder) if exists else None,
    }


# --- Path safety ----------------------------------------------------------------


def _system_dirs() -> list[Path]:
    if sys.platform == "win32":
        names = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
        return [Path(v) for n in names if (v := os.environ.get(n))]
    return [Path(p) for p in ("/bin", "/boot", "/dev", "/etc", "/lib", "/proc", "/sbin", "/sys", "/usr",
                              "/var", "/System", "/Library")]


def _key(path: Path) -> str:
    return os.path.normcase(str(path))


def _is_within(path: Path, parent: Path) -> bool:
    p, q = _key(path), _key(parent)
    return p == q or p.startswith(q.rstrip("\\/") + os.sep)


def normalize_project_path(raw: str) -> Path:
    """Absolute, resolved path, or raise a VALIDATION_ERROR explaining why it can't be used."""
    try:
        path = Path(os.path.expandvars(raw)).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ApiError.validation("path", f"Invalid path: {exc}") from exc

    if not path.exists():
        raise ApiError.validation("path", f"Folder does not exist: {path}")
    if not path.is_dir():
        raise ApiError.validation("path", f"Not a folder: {path}")
    if not os.access(path, os.R_OK | os.W_OK):
        raise ApiError.validation("path", f"Folder is not readable and writable: {path}")

    if path == Path(path.anchor):
        raise ApiError.validation("path", "A drive or filesystem root cannot be the project folder.")
    if _key(path) == _key(Path.home()):
        raise ApiError.validation("path", "Your home folder cannot be the project folder. Pick a subfolder.")
    for system_dir in _system_dirs():
        if _is_within(path, system_dir):
            raise ApiError.validation("path", f"System folders cannot be the project folder: {system_dir}")
    # Agents must never edit WorkLoop itself.
    if _is_within(path, BASE_DIR) or _is_within(BASE_DIR, path):
        raise ApiError.validation(
            "path", "The project folder cannot contain, or be inside, the WorkLoop folder."
        )
    return path


# --- Routes ---------------------------------------------------------------------


@router.get("/project", response_model=ProjectOut)
def get_project(db: DbSession) -> dict[str, Any]:
    return project_info(repo.get_project(db))


class BrowseOut(BaseModel):
    path: str | None  # null when the user closed the dialog


def _ask_directory() -> str | None:
    """Open the OS folder picker on this machine (the backend runs locally)."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)  # show above the browser
    try:
        chosen = filedialog.askdirectory(parent=root, mustexist=True, title="WorkLoop")
    finally:
        root.destroy()
    return str(Path(chosen)) if chosen else None


@router.post("/project/browse", response_model=BrowseOut)
async def browse_folder() -> dict[str, Any]:
    """Let the user pick a folder with the native dialog. Does not change the project."""
    import asyncio

    try:
        path = await asyncio.to_thread(_ask_directory)
    except Exception as exc:  # noqa: BLE001 - e.g. no display / Tk not installed
        raise ApiError(ErrorCode.BAD_REQUEST, f"Folder dialog is not available: {exc}", 400) from exc
    return {"path": path}


@router.put("/project", response_model=ProjectOut, dependencies=[Depends(ensure_no_active_run)])
def set_project(body: ProjectUpdate, db: DbSession) -> dict[str, Any]:
    path = normalize_project_path(body.path)
    repo.set_project(db, str(path))
    return project_info(str(path))
