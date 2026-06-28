"""
platforms/telegram/joiner.py — safe, throttled joining of queued links.
=======================================================================

A single loop consumes the join queue: it picks a healthy account that is
allowed to join (throttle/warmup/cap aware), waits the adaptive delay, then
joins. Public links use JoinChannel, private invites use ImportChatInvite.
Results are logged (#Join_Log) and failures are handled without ever crashing.

Only *groups* are kept (mega/basic groups). Broadcast-only channels are left
again so accounts don't fill up with the wrong thing.
"""
from __future__ import annotations

import asyncio
import re

from telethon import functions
from telethon.errors import (
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UserAlreadyParticipantError,
    ChannelsTooMuchError,
    PeerFloodError,
    ChannelPrivateError,
)

import config
from core import db, logbus, throttle
from platforms.telegram import client as tg

_INVITE_HASH_RE = re.compile(
    r"(?:t\.me|telegram\.me)/(?:joinchat/|\+)([\w\-]+)", re.IGNORECASE)
_PUBLIC_RE = re.compile(
    r"(?:t\.me|telegram\.me)/([A-Za-z][\w\d_]{3,})", re.IGNORECASE)


def _pick_account():
    """Pick the least-busy active account that may join right now. Discover
    accounts are used only if there is nothing else."""
    accounts = db.list_accounts_by_status("active")
    allowed = [a for a in accounts if throttle.can_join(a)]
    if not allowed:
        return None
    prefer = [a for a in allowed if not a.get("is_discover")] or allowed
    prefer.sort(key=lambda a: int(a.get("joins_today") or 0))
    return prefer[0]


def _chat_from_result(result):
    chats = getattr(result, "chats", None)
    if chats:
        return chats[0]
    return None


def _is_group(chat) -> bool:
    """True for mega/basic groups; False for broadcast-only channels."""
    if chat is None:
        return False
    if getattr(chat, "megagroup", False):
        return True
    if getattr(chat, "broadcast", False):
        return False
    # basic group (types.Chat) has neither flag
    return True


async def _join_link(client, link: str):
    """Join via public username or private invite. Returns the chat entity."""
    m = _INVITE_HASH_RE.search(link)
    if m:
        res = await client(functions.messages.ImportChatInviteRequest(m.group(1)))
        return _chat_from_result(res)
    m = _PUBLIC_RE.search(link)
    uname = m.group(1) if m else link.rsplit("/", 1)[-1]
    entity = await client.get_entity(uname)
    res = await client(functions.channels.JoinChannelRequest(entity))
    return _chat_from_result(res) or entity


async def _leave(client, chat) -> None:
    try:
        await client(functions.channels.LeaveChannelRequest(chat))
    except Exception:  # noqa: BLE001
        pass


async def _do_join(account: dict, item: dict) -> None:
    phone = account["phone"]
    link = item["link"]
    qid = item["id"]
    db.set_join_status(qid, "pending", account_id=account["id"])
    db.inc_join_tries(qid)
    try:
        async with tg.lock(phone):
            client = await tg.get_client(phone)
            chat = await asyncio.wait_for(_join_link(client, link), timeout=60)
    except FloodWaitError as e:
        secs = int(getattr(e, "seconds", 60))
        throttle.on_floodwait(account, secs)
        await logbus.card_account_limited(
            phone=phone, kind="FloodWait", duration=f"{secs}s",
            action="⏸ جوین متوقف شد | ✅ اسکرپ ادامه دارد")
        return
    except PeerFloodError as e:
        throttle.on_peerflood(account)
        moved = db.requeue_account_pending(account["id"])
        await logbus.card_account_limited(
            phone=phone, kind="PeerFlood", duration="چند ساعت",
            action=f"⏸ جوین متوقف | 🔁 {moved} جوین معطل به اکانت دیگر")
        await logbus.log_error("جوین", "ImportChatInvite/Join", e, account=phone)
        return
    except ChannelsTooMuchError as e:
        db.set_account_status(phone, "full")
        moved = db.requeue_account_pending(account["id"])
        await logbus.card_account_limited(
            phone=phone, kind="اکانت پُر", duration="—",
            action=f"📦 پُر شد | 🔁 {moved} جوین معطل به اکانت دیگر")
        await logbus.log_error("جوین", "Join (full)", e, account=phone)
        return
    except UserAlreadyParticipantError:
        chat = None  # already a member — try to record it below via best effort
    except (InviteHashExpiredError, InviteHashInvalidError, ChannelPrivateError) as e:
        db.set_join_status(qid, "failed")
        await logbus.card_join(account_phone=phone, group_title="", link=link,
                               ok=False, detail="لینک منقضی/نامعتبر/خصوصی")
        await logbus.log_error("جوین", "Join (bad link)", e, account=phone)
        return
    except RuntimeError as e:
        # account session problem — leave item pending for another account
        await logbus.log_error("جوین", "اتصال اکانت", e, account=phone)
        return
    except Exception as e:  # noqa: BLE001
        if int(item.get("tries") or 0) + 1 >= 3:
            db.set_join_status(qid, "failed")
        await logbus.card_join(account_phone=phone, group_title="", link=link,
                               ok=False, detail=type(e).__name__)
        await logbus.log_error("جوین", "Join", e, account=phone)
        return

    # --- success path -------------------------------------------------------
    if chat is None:
        # Already a participant (or chat object not returned): mark done. We
        # can't store tg_id/title, but the scraper handler covers it anyway.
        db.set_join_status(qid, "joined", account_id=account["id"])
        await logbus.card_join(account_phone=phone, group_title="(از قبل عضو)",
                               link=link, ok=True)
        return

    title = getattr(chat, "title", "") or ""
    tg_id = getattr(chat, "id", None)

    if not title or not _is_group(chat):
        # broadcast channel / user / not a group: leave it and skip.
        await _leave(client, chat)
        db.set_join_status(qid, "failed")
        await logbus.card_join(account_phone=phone, group_title=title, link=link,
                               ok=False, detail="گروه نبود (کانال/کاربر)")
        return

    db.set_join_status(qid, "joined", account_id=account["id"])
    db.add_group(link, tg_id, title, account["id"])
    db.recount_group_count(account["id"])
    throttle.on_success(account)
    await logbus.card_join(account_phone=phone, group_title=title, link=link, ok=True)


async def joiner_loop(should_run) -> None:
    """Main joiner loop. ``should_run`` is a zero-arg callable returning bool."""
    while should_run():
        try:
            if not db.is_running():
                await asyncio.sleep(config.JOINER_IDLE_SLEEP)
                continue
            item = db.next_pending_join()
            if not item:
                await asyncio.sleep(config.JOINER_IDLE_SLEEP)
                continue
            account = _pick_account()
            if not account:
                # everyone is capped / quarantined — wait and retry later.
                await asyncio.sleep(config.JOINER_IDLE_SLEEP)
                continue
            await _do_join(account, item)
            # space joins using *this* account's adaptive delay.
            fresh = db.get_account(account["phone"]) or account
            await asyncio.sleep(throttle.jittered_delay(fresh))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("جوین", "حلقه‌ی جوینر", e)
            await asyncio.sleep(config.JOINER_IDLE_SLEEP)
