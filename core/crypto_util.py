"""
crypto_util.py — encrypt/decrypt the stored Telegram sessions.
==============================================================

Sessions are the *full key* to an account, so they are never stored in plain
text. We derive a stable Fernet key from ENCRYPTION_KEY (or, as a fallback,
from BOT_TOKEN) and use it symmetrically. Same key on every server => sessions
move between servers transparently.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet

import config


def _fernet() -> Fernet:
    raw = (config.ENCRYPTION_KEY or config.BOT_TOKEN or "target-scraper-default").encode("utf-8")
    # SHA-256 -> 32 bytes -> urlsafe base64 == a valid Fernet key.
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(key)


_F = _fernet()


def encrypt(plain: str) -> str:
    """Encrypt a string; returns text safe to store in the DB."""
    if plain is None:
        plain = ""
    return _F.encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    """Decrypt a string produced by :func:`encrypt`. Returns "" on failure
    (e.g. corrupted value or key change) instead of raising."""
    if not token:
        return ""
    try:
        return _F.decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""
