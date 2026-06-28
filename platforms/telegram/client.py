"""
platforms/telegram/client.py — Telethon userbot layer.
======================================================

One warm client per account, opened lazily from the (encrypted) StringSession
stored in the DB, reused across rounds, and serialised by a per-account lock.
Sessions are stored as strings so accounts survive restarts and move between
servers with no re-login.

Only the panel ever drives the interactive login (phone -> code -> optional
2FA). Everything else just calls ``get_client(phone)``.
"""
from __future__ import annotations

import asyncio
import time

from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    FloodWaitError,
)

import config
from core import db, crypto_util

# Warm client registry.
_clients: dict = {}     # phone -> TelegramClient
_locks: dict = {}       # phone -> asyncio.Lock


def lock(phone: str) -> asyncio.Lock:
    lk = _locks.get(phone)
    if lk is None:
        lk = asyncio.Lock()
        _locks[phone] = lk
    return lk


def _new_client(session_str: str = "") -> TelegramClient:
    return TelegramClient(StringSession(session_str or None),
                          config.API_ID, config.API_HASH)


# --------------------------------------------------------------------------- #
# Interactive login (panel only).
# --------------------------------------------------------------------------- #
async def start_login(phone: str) -> dict:
    """Connect a fresh client and request the login code. Returns a ctx dict
    that is carried through the conversation. ``created_at`` lets the panel
    abandon stale half-finished logins."""
    client = _new_client("")
    await client.connect()
    sent = await client.send_code_request(phone)
    return {
        "client": client,
        "phone": phone,
        "phone_code_hash": getattr(sent, "phone_code_hash", None),
        "created_at": time.time(),
    }


async def finish_login(ctx: dict, code: str) -> bool:
    """Sign in with the SMS/app code. Returns True if done, False if a 2FA
    password is still required."""
    client = ctx["client"]
    try:
        await client.sign_in(ctx["phone"], code,
                             phone_code_hash=ctx.get("phone_code_hash"))
        return True
    except SessionPasswordNeededError:
        return False


async def finish_password(ctx: dict, password: str) -> None:
    client = ctx["client"]
    await client.sign_in(password=password)


async def commit_login(ctx: dict) -> dict:
    """After a successful sign-in: read account info, persist the (encrypted)
    session, register the warm client. Returns {phone, name, username, user_id,
    session_str}."""
    client = ctx["client"]
    phone = ctx["phone"]
    info = await account_info(client)
    session_str = client.session.save()
    db.upsert_account(phone, info.get("name", ""), info.get("username", ""),
                      info.get("user_id"), crypto_util.encrypt(session_str))
    _clients[phone] = client
    info["session_str"] = session_str
    return info


async def cancel_ctx(ctx: dict) -> None:
    """Disconnect a half-finished login (timeout / cancel)."""
    try:
        c = ctx.get("client")
        if c is not None:
            await c.disconnect()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Warm clients.
# --------------------------------------------------------------------------- #
async def get_client(phone: str) -> TelegramClient:
    """Return a connected+authorized warm client, opening it lazily from the
    stored session. Raises RuntimeError('no_session'/'unauthorized')."""
    c = _clients.get(phone)
    if c is not None and c.is_connected():
        return c
    acc = db.get_account(phone)
    if not acc or not acc.get("session"):
        raise RuntimeError("no_session")
    session_str = crypto_util.decrypt(acc["session"])
    if not session_str:
        raise RuntimeError("no_session")
    c = _new_client(session_str)
    await c.connect()
    if not await c.is_user_authorized():
        try:
            await c.disconnect()
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("unauthorized")
    _clients[phone] = c
    return c


async def drop_client(phone: str) -> None:
    c = _clients.pop(phone, None)
    if c is not None:
        try:
            await c.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def close_all() -> None:
    for phone in list(_clients.keys()):
        await drop_client(phone)


def is_warm(phone: str) -> bool:
    c = _clients.get(phone)
    return c is not None and c.is_connected()


# --------------------------------------------------------------------------- #
# Account info.
# --------------------------------------------------------------------------- #
def _full_name(me) -> str:
    parts = [getattr(me, "first_name", "") or "", getattr(me, "last_name", "") or ""]
    return " ".join(p for p in parts if p).strip() or "—"


async def account_info(client: TelegramClient) -> dict:
    me = await client.get_me()
    return {
        "user_id": getattr(me, "id", None),
        "name": _full_name(me),
        "username": getattr(me, "username", "") or "",
        "phone": getattr(me, "phone", "") or "",
    }


# --------------------------------------------------------------------------- #
# FloodWait-aware call wrapper.
# --------------------------------------------------------------------------- #
async def safe_call(coro_factory, *, retries: int = 1):
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except FloodWaitError as e:
            wait = min(int(getattr(e, "seconds", 5)) + 1, config.TG_FLOOD_MAX_WAIT)
            if attempt >= retries:
                raise
            attempt += 1
            await asyncio.sleep(wait)
