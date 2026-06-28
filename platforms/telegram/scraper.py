"""
platforms/telegram/scraper.py — real-time keyword scraping ("the doorbell").
============================================================================

We do NOT poll and we do NOT re-implement getDifference by hand. Each account's
warm client gets ONE ``events.NewMessage`` handler with ``catch_up`` enabled, so
Telethon delivers new messages the instant they arrive and fills any gap that
opened while we were offline. New groups joined later by that account are
covered automatically (the handler is per-account, not per-group).

The hot path is intentionally cheap: 99% of messages just fail a fast keyword
check and are dropped. Only an actual *match* is handed to a tiny writer queue
that saves the candidate (atomic dedup) and posts the #Candidate card — so the
slow part (DB + log group) never blocks message intake.
"""
from __future__ import annotations

import asyncio

from telethon import events

import config
from core import db, logbus, matcher
from platforms.telegram import client as tg

# Small queue that holds only MATCHED candidates (few), not every message.
_cand_queue: asyncio.Queue | None = None
_handlers: dict = {}        # phone -> (client, handler_callback)
_writer_task: asyncio.Task | None = None


def _msg_link(event, msg_id) -> str:
    """Best-effort public link to the message (empty for private groups)."""
    try:
        chat = event.chat
        uname = getattr(chat, "username", None)
        if uname:
            return f"https://t.me/{uname}/{msg_id}"
        cid = getattr(chat, "id", None)
        if cid is not None:
            return f"https://t.me/c/{cid}/{msg_id}"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _make_handler(phone: str, account_id: int):
    async def handler(event):
        try:
            # groups only (and supergroups); ignore PMs / channels / our own msgs
            if not event.is_group:
                return
            text = event.raw_text or ""
            if not text:
                return
            hits = matcher.match(text)
            if not hits:
                return
            sender = await event.get_sender()
            if sender is None:
                return
            uid = getattr(sender, "id", None)
            if uid is None:
                return
            # skip bots
            if getattr(sender, "bot", False):
                return
            name_parts = [getattr(sender, "first_name", "") or "",
                          getattr(sender, "last_name", "") or ""]
            name = " ".join(p for p in name_parts if p).strip() or "—"
            username = getattr(sender, "username", "") or ""
            chat = await event.get_chat()
            group_title = getattr(chat, "title", "") or ""
            tg_id = getattr(chat, "id", None)
            grp = db.get_group_by_tgid(tg_id) if tg_id is not None else None
            group_db_id = grp["id"] if grp else None

            payload = {
                "user_key": f"tg:{uid}",
                "username": username,
                "name": name,
                "group_id": group_db_id,
                "group_title": group_title,
                "message": text[: config.MESSAGE_MAX_LEN],
                "matched_kw": "، ".join(hits),
                "account_id": account_id,
                "account_phone": phone,
                "msg_link": _msg_link(event, getattr(event, "id", 0)),
            }
            if _cand_queue is not None:
                await _cand_queue.put(payload)
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("اسکرپ", "پردازش پیام", e, account=phone)

    return handler


def _make_leave_handler(phone: str, account_id: int, my_uid):
    """Detect when THIS account is kicked/leaves a group and mark that group
    dead (group self-heal). Event-driven, no polling."""
    async def handler(event):
        try:
            if not (getattr(event, "user_left", False) or
                    getattr(event, "user_kicked", False)):
                return
            affected = set()
            if getattr(event, "user_id", None):
                affected.add(event.user_id)
            try:
                for u in (event.user_ids or []):
                    affected.add(u)
            except Exception:  # noqa: BLE001
                pass
            if my_uid and my_uid not in affected:
                return  # someone else left, not us
            chat = await event.get_chat()
            tg_id = getattr(chat, "id", None)
            grp = db.get_group_by_tgid(tg_id) if tg_id is not None else None
            if grp:
                db.set_group_status(grp["id"], "dead")
                db.recount_group_count(account_id)
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("اسکرپ", "کیک/خروج از گروه", e, account=phone)

    return handler


async def _writer_loop():
    """Persist matched candidates one at a time (atomic dedup) and post cards,
    with a small gap so the log group isn't flooded."""
    assert _cand_queue is not None
    while True:
        payload = await _cand_queue.get()
        try:
            rowid = db.add_candidate(
                payload["user_key"], payload["username"], payload["name"],
                payload["group_id"], payload["group_title"], payload["message"],
                payload["matched_kw"], payload["account_id"],
                payload["account_phone"], payload["msg_link"],
            )
            if rowid:  # newly inserted (not a duplicate person)
                await logbus.card_candidate(
                    name=payload["name"], username=payload["username"],
                    group_title=payload["group_title"], message=payload["message"],
                    matched_kw=payload["matched_kw"],
                    account_phone=payload["account_phone"],
                    count=db.count_candidates_total(),
                )
                await asyncio.sleep(1.0)  # be gentle with the log group
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("اسکرپ", "ثبت کارجو", e,
                                   account=payload.get("account_phone", ""))
        finally:
            _cand_queue.task_done()


async def _sync_dialogs(client, account_id: int) -> None:
    """Register every group the account is CURRENTLY a member of, so the panel
    reflects reality (including groups it already belonged to before being
    added to the bot). Read-only — no joining."""
    try:
        async for d in client.iter_dialogs():
            try:
                if getattr(d, "is_group", False):
                    ent = d.entity
                    uname = getattr(ent, "username", None)
                    link = f"https://t.me/{uname}" if uname else ""
                    db.add_group(link, getattr(ent, "id", None),
                                 getattr(ent, "title", "") or "", account_id)
            except Exception:  # noqa: BLE001
                continue
        db.recount_group_count(account_id)
    except Exception as e:  # noqa: BLE001
        await logbus.log_error("اسکرپ", "ثبت گروه‌های فعلی اکانت", e)


async def attach_account(phone: str) -> bool:
    """Warm an account's client (with catch_up) and attach its scrape handler.
    Returns True on success."""
    if phone in _handlers:
        return True
    account = db.get_account(phone)
    if not account:
        return False
    try:
        client = await tg.get_client(phone)
        try:
            await client.catch_up()  # fill anything missed while offline
        except Exception:  # noqa: BLE001
            pass
        await _sync_dialogs(client, account["id"])  # record current memberships
        cb = _make_handler(phone, account["id"])
        client.add_event_handler(cb, events.NewMessage(incoming=True))
        cb_leave = _make_leave_handler(phone, account["id"], account.get("user_id"))
        client.add_event_handler(cb_leave, events.ChatAction())
        _handlers[phone] = (client, [cb, cb_leave])
        return True
    except RuntimeError as e:
        await logbus.log_error("اسکرپ", "اتصال اکانت", e, account=phone)
        return False
    except Exception as e:  # noqa: BLE001
        await logbus.log_error("اسکرپ", "اتصال اکانت", e, account=phone)
        return False


async def detach_account(phone: str) -> None:
    pair = _handlers.pop(phone, None)
    if pair is not None:
        client, callbacks = pair
        for cb in callbacks:
            try:
                client.remove_event_handler(cb)
            except Exception:  # noqa: BLE001
                pass


async def start() -> int:
    """Start the writer loop and attach handlers for all active accounts.
    Returns the number of accounts attached."""
    global _cand_queue, _writer_task
    if _cand_queue is None:
        _cand_queue = asyncio.Queue(maxsize=5000)
    if _writer_task is None or _writer_task.done():
        _writer_task = asyncio.create_task(_writer_loop())
    attached = 0
    for acc in db.list_accounts():
        # Everything except a dead session should keep scraping — a join-limited
        # or "full" account still reads the groups it already belongs to.
        if acc.get("status") == "dead":
            continue
        if await attach_account(acc["phone"]):
            attached += 1
    return attached


async def stop() -> None:
    """Detach all handlers and stop the writer loop (clients stay connected)."""
    global _writer_task
    for phone in list(_handlers.keys()):
        await detach_account(phone)
    if _writer_task is not None:
        _writer_task.cancel()
        _writer_task = None
