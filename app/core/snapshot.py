"""Project snapshots: which files existed, with size, mtime and sha256 (no content).

A snapshot is taken when a prompt fails and the run pauses in needs_user. After the
user fixes things by hand, diff_snapshot() tells what they added, removed or modified.

Walking and hashing run in a worker thread so the server keeps responding.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select

from app.agents.schemas import FileChanges
from app.db.database import SessionLocal
from app.db.models import FileSnapshot, Snapshot
from app.tools.file_tools import is_ignored

logger = logging.getLogger(__name__)

MAX_FILES = 20_000
HASH_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class _FileInfo:
    rel_path: str  # POSIX style
    size: int
    mtime_ns: int


def _walk(root: Path, limit: int | None = None) -> tuple[list[_FileInfo], bool]:
    """Files under root in a stable (sorted) order, skipping ignored names and every link.

    Returns (files, truncated). Symlinks and Windows junctions are never followed or listed.
    limit defaults to MAX_FILES (read at call time).
    """
    limit = MAX_FILES if limit is None else limit
    files: list[_FileInfo] = []
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        subfolders = []
        for entry in entries:
            if is_ignored(entry.name):
                continue
            try:
                if entry.is_symlink() or entry.is_junction():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    subfolders.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if len(files) >= limit:
                return files, True
            rel = Path(entry.path).relative_to(root).as_posix()
            files.append(_FileInfo(rel, st.st_size, st.st_mtime_ns))
        stack.extend(reversed(subfolders))  # visit subfolders in name order
    return files, False


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(HASH_CHUNK):
                digest.update(chunk)
    except OSError:
        return None  # vanished or unreadable while walking
    return digest.hexdigest()


def _hashes(root: Path, files: list[_FileInfo], known: dict[str, tuple[int, int, str]]) -> dict[str, str]:
    """sha256 per file; reuses a known hash when size and mtime are unchanged."""
    out: dict[str, str] = {}
    for info in files:
        cached = known.get(info.rel_path)
        if cached is not None and cached[0] == info.size and cached[1] == info.mtime_ns:
            out[info.rel_path] = cached[2]
            continue
        digest = _sha256(root / info.rel_path)
        if digest is not None:
            out[info.rel_path] = digest
    return out


def _known_hashes_for_run(run_id: int) -> dict[str, tuple[int, int, str]]:
    """Latest saved (size, mtime_ns, sha256) per path from earlier snapshots of this run."""
    with SessionLocal() as db:
        rows = db.execute(
            select(FileSnapshot.rel_path, FileSnapshot.size, FileSnapshot.mtime_ns, FileSnapshot.sha256)
            .where(FileSnapshot.run_id == run_id)
            .order_by(FileSnapshot.id)
        ).all()
    return {r.rel_path: (r.size, r.mtime_ns, r.sha256) for r in rows}


def _take_snapshot_sync(project_root: Path, run_id: int, prompt_id: int | None) -> int:
    root = project_root.resolve()
    files, truncated = _walk(root)
    hashes = _hashes(root, files, _known_hashes_for_run(run_id))
    with SessionLocal() as db:
        snap = Snapshot(run_id=run_id, prompt_id=prompt_id, project_path=str(root), truncated=truncated)
        db.add(snap)
        db.flush()
        rows = [
            FileSnapshot(
                snapshot_id=snap.id, run_id=run_id, prompt_id=prompt_id, rel_path=f.rel_path,
                size=f.size, mtime_ns=f.mtime_ns, sha256=hashes[f.rel_path],
            )
            for f in files
            if f.rel_path in hashes
        ]
        db.add_all(rows)
        snap.file_count = len(rows)
        db.commit()
        logger.info(
            "Snapshot %s: %d files%s", snap.id, len(rows), " (truncated)" if truncated else ""
        )
        return snap.id


async def take_snapshot(project_root: Path, run_id: int, prompt_id: int | None) -> int:
    """Save the current state of the project. Returns the snapshot id."""
    return await asyncio.to_thread(_take_snapshot_sync, Path(project_root), run_id, prompt_id)


def _diff_sync(snapshot_id: int, project_root: Path) -> FileChanges:
    with SessionLocal() as db:
        snap = db.get(Snapshot, snapshot_id)
        if snap is None:
            raise LookupError(f"Snapshot {snapshot_id} not found")
        saved = {
            r.rel_path: (r.size, r.mtime_ns, r.sha256)
            for r in db.scalars(select(FileSnapshot).where(FileSnapshot.snapshot_id == snapshot_id)).all()
        }
        truncated = snap.truncated
    root = project_root.resolve()
    # Removed / modified: check every saved path directly, so the file limit can't distort them.
    removed: list[str] = []
    modified: list[str] = []
    for rel, (size, mtime_ns, sha) in saved.items():
        path = root / rel
        try:
            st = path.stat()
        except OSError:
            removed.append(rel)
            continue
        if not path.is_file():
            removed.append(rel)
        elif (st.st_size, st.st_mtime_ns) != (size, mtime_ns) and _sha256(path) != sha:
            modified.append(rel)
    # Added: walk the project. With a truncated snapshot, files past the limit were never
    # recorded, so new files can't be told apart from them; report none in that case.
    added: list[str] = []
    if truncated:
        logger.warning("Snapshot %s was truncated; added files can't be detected", snapshot_id)
    else:
        files, _ = _walk(root, limit=MAX_FILES * 5)
        added = [f.rel_path for f in files if f.rel_path not in saved]
    return FileChanges(added=sorted(added), removed=sorted(removed), modified=sorted(modified))


async def diff_snapshot(snapshot_id: int, project_root: Path) -> FileChanges:
    """Files added, removed and modified since the snapshot (compared by sha256), sorted."""
    return await asyncio.to_thread(_diff_sync, snapshot_id, Path(project_root))


def _delete_sync(snapshot_id: int) -> None:
    with SessionLocal() as db:
        db.execute(delete(FileSnapshot).where(FileSnapshot.snapshot_id == snapshot_id))
        db.execute(delete(Snapshot).where(Snapshot.id == snapshot_id))
        db.commit()


async def discard_snapshot(snapshot_id: int) -> None:
    await asyncio.to_thread(_delete_sync, snapshot_id)
