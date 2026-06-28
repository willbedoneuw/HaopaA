"""
platforms/telegram/discover.py — find group links from seed channels.
=====================================================================

Reads recent messages of the channels you mark as "seed" (using the accounts
you designate as *discover* accounts) and extracts Telegram group/invite links
from the message text + link entities. New links are pushed into the join queue
(deduped). It never reads bios and never uses global search — only message
content, exactly as designed.
"""
from __future__ import annotations

import asyncio
import re

import config
from core import db, logbus
from platforms.telegram import client as tg

# t.me / telegram.me links: private invites (+hash / joinchat/hash) and public.
_INVITE_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/|\+)[\w\-]+", re.IGNORECASE)
_PUBLIC_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][\w\d_]{3,})", re.IGNORECASE)

# usernames that are never groups — skip them.
_SKIP_USERNAMES = {"joinchat", "share", "addstickers", "addemoji", "proxy",
                   "socks", "iv", "bg", "setlanguage", "c"}

READ_LIMIT = 200  # how many recent messages per seed channel to scan


def _normalize(link: str) -> str:
    link = link.strip().rstrip("/")
    if not link.lower().startswith("http"):
        link = "https://" + link
    return link


def extract_links(text: str) -> set:
    """Extract candidate group/invite links from a piece of text."""
    if not text:
        return set()
    out = set()
    for m in _INVITE_RE.findall(text):
        out.add(_normalize(m))
    for uname in _PUBLIC_RE.findall(text):
        if uname.lower() in _SKIP_USERNAMES:
            continue
        out.add(_normalize(f"https://t.me/{uname}"))
    return out


def _links_from_message(msg) -> set:
    """Pull links from a message's text and its url entities."""
    links = set()
    text = getattr(msg, "message", None) or getattr(msg, "raw_text", None) or ""
    links |= extract_links(text)
    # Hidden hyperlinks (MessageEntityTextUrl) carry the real URL separately.
    try:
        for ent, _txt in (msg.get_entities_text() or []):
            url = getattr(ent, "url", None)
            if url:
                links |= extract_links(url)
    except Exception:  # noqa: BLE001
        pass
    return links


async def _scan_seed_with(client, ref: str) -> int:
    """Scan one seed channel with one client. Returns new links enqueued."""
    new = 0
    entity = await asyncio.wait_for(client.get_entity(ref), timeout=30)
    async for msg in client.iter_messages(entity, limit=READ_LIMIT):
        for link in _links_from_message(msg):
            if db.enqueue_link(link):
                new += 1
    return new


async def discover_once() -> int:
    """One discovery pass over every seed channel. Returns total new links.
    Distributes seeds across the designated discover accounts (round-robin)."""
    seeds = db.list_seeds()
    accounts = db.list_discover_accounts()
    if not seeds or not accounts:
        return 0

    total_new = 0
    idx = 0
    for seed in seeds:
        # round-robin a healthy discover account for each seed
        acc = accounts[idx % len(accounts)]
        idx += 1
        phone = acc["phone"]
        ref = seed["ref"]
        try:
            async with tg.lock(phone):
                client = await tg.get_client(phone)
                total_new += await _scan_seed_with(client, ref)
        except RuntimeError as e:
            # no_session / unauthorized -> let health handle the account
            await logbus.log_error("کشف", f"خواندن منبع {ref}", e, account=phone)
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("کشف", f"خواندن منبع {ref}", e, account=phone)
    return total_new
