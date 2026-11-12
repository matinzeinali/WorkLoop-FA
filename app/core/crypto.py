"""Encrypt and decrypt API keys with Fernet.

The Fernet key lives in a local file under data/ and is created on first use.
"""

import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class CryptoError(Exception):
    """Raised when an API key cannot be decrypted."""


def _load_or_create_key(path: Path) -> bytes:
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # Exclusive create so two processes starting together cannot overwrite each other.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes().strip()  # another process created it first
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


@lru_cache
def _fernet() -> Fernet:
    return Fernet(_load_or_create_key(get_settings().key_file_path))


def ensure_key() -> None:
    """Create the key file if missing. Called at startup."""
    _fernet()


def encrypt_api_key(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_api_key(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CryptoError("Stored API key could not be decrypted.") from exc


def make_key_preview(plain: str) -> str:
    """Return a safe preview such as 'sk-a...9xQ2'. Never returns the full key."""
    key = plain.strip()
    if len(key) < 12:
        return "****"
    return f"{key[:4]}...{key[-4:]}"
