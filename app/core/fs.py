"""Small filesystem helpers that work on Python 3.11 as well as 3.12+."""

import os
import stat
import sys
from typing import Any


def is_junction(entry: Any) -> bool:
    """True for a Windows junction (directory reparse point).

    Accepts an os.DirEntry, a pathlib.Path or a path string. Python 3.12 added
    DirEntry.is_junction / Path.is_junction / os.path.isjunction; older versions
    fall back to checking the reparse-point attribute.
    """
    method = getattr(entry, "is_junction", None)
    if callable(method):
        return bool(method())
    path = entry.path if hasattr(entry, "path") and not isinstance(entry, (str, bytes)) else os.fspath(entry)
    if hasattr(os.path, "isjunction"):
        return os.path.isjunction(path)
    if sys.platform != "win32":
        return False
    try:
        attrs = os.lstat(path).st_file_attributes  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return False
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT) and not os.path.islink(path)
