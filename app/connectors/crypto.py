"""Encrypt connection secrets at rest with Fernet.

The key comes from SHIFTKEY_SECRET_KEY; when that is empty a key is generated once and kept
in <data_dir>/secret.key (mount that directory as a volume in Docker).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings

log = logging.getLogger("shiftkey.crypto")


def _derive(raw: str) -> bytes:
    """Accept either a real Fernet key or any passphrase (hashed to 32 bytes)."""
    try:
        Fernet(raw.encode())
        return raw.encode()
    except Exception:
        return base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    if settings.secret_key:
        return Fernet(_derive(settings.secret_key))
    os.makedirs(settings.data_dir, exist_ok=True)
    path = os.path.join(settings.data_dir, "secret.key")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return Fernet(fh.read().strip())
    key = Fernet.generate_key()
    with open(path, "wb") as fh:
        fh.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover
        pass
    log.warning("Generated a new secret key at %s – back it up or set SHIFTKEY_SECRET_KEY.", path)
    return Fernet(key)


def encrypt(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise ValueError("Stored secret cannot be decrypted – SHIFTKEY_SECRET_KEY changed? Re-enter the password.")
