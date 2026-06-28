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
    InviteRequestSentError,
)

import config
from core import db, logbus, throttle
from platforms.telegram import client as tg

_INVITE_HASH_RE = re.compile(
    r"(?:t\.me|telegram\.me)/(?:joinchat/|\+)([\w\-]+)", re.IGNORECASE)
_PUBLIC_RE = re.compile(
    r"(?:t\.me|telegram\.me)/([A-Za-z][\w\d_]{3,})", re.IGNORECASE)


def _pick_account():
    """Pick the least-busy active account allowed to join right now.

    Discover accounts are READ-ONLY: if you have at least one dedicated
    (non-discover) account, ONLY those ever join. Discover accounts join only
    when there is NO non-discover account at all (so the bot still works if
    every account happens to be a discover account)."""
    active = db.list_accounts_by_status("active")
    has_joiner = any(not a.get("is_discover") for a in db.list_accounts())
    pool = [a for a in active if not a.get("is_discover")] if has_joiner else active
    allowed = [a for a in pool if throttle.can_join(a)]
    if not allowed:
        return None
    allowed.sort(key=lambda a: int(a.get("joins_today") or 0))
    return allowed[0]


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


async def _record_group(client, account: dict, qid: int, link: str, chat) -> None:
    """Record a newly-joined group (or skip a non-group)."""
    phone = account["phone"]
    if chat is None:
        db.set_join_status(qid, "joined", account_id=account["id"])
        await logbus.card_join(account_phone=phone, group_title="(عضو)",
                               link=link, ok=True)
        return
    title = getattr(chat, "title", "") or ""
    if not title or not _is_group(chat):
        await _leave(client, chat)
        db.set_join_status(qid, "failed")
        await logbus.card_join(account_phone=phone, group_title=title, link=link,
                               ok=False, detail="گروه نبود (کانال/کاربر)")
        return
    db.set_join_status(qid, "joined", account_id=account["id"])
    db.add_group(link, getattr(chat, "id", None), title, account["id"])
    db.recount_group_count(account["id"])
    throttle.on_success(account)   # streak / speed-up only (cap is counted elsewhere)
    await logbus.card_join(account_phone=phone, group_title=title, link=link, ok=True)


async def _do_join(account: dict, item: dict) -> bool:
    """Attempt one join. The daily cap counts every join REQUEST actually sent
    to Telegram (success, already-member, bad link, flood) — Telegram limits
    *attempts*, not just successes. Returns True if a request was sent (so the
    loop paces it with the full safe delay)."""
    phone = account["phone"]
    link = item["link"]
    qid = item["id"]
    db.set_join_status(qid, "pending", account_id=account["id"])
    db.inc_join_tries(qid)
    attempted = False
    try:
        try:
            async with tg.lock(phone):
                client = await tg.get_client(phone)
                attempted = True   # about to send a real join request
                chat = await asyncio.wait_for(_join_link(client, link), timeout=60)
        except FloodWaitError as e:
            secs = int(getattr(e, "seconds", 60))
            throttle.on_floodwait(account, secs)
            await logbus.card_account_limited(
                phone=phone, kind="FloodWait", duration=f"{secs}s",
                action="⏸ جوین متوقف شد | ✅ اسکرپ ادامه دارد")
            return True
        except PeerFloodError as e:
            throttle.on_peerflood(account)
            moved = db.requeue_account_pending(account["id"])
            await logbus.card_account_limited(
                phone=phone, kind="PeerFlood", duration="چند ساعت",
                action=f"⏸ جوین متوقف | 🔁 {moved} جوین معطل به اکانت دیگر")
            await logbus.log_error("جوین", "Join", e, account=phone)
            return True
        except ChannelsTooMuchError as e:
            db.set_account_status(phone, "full")
            moved = db.requeue_account_pending(account["id"])
            await logbus.card_account_limited(
                phone=phone, kind="اکانت پُر", duration="—",
                action=f"📦 پُر شد | 🔁 {moved} جوین معطل به اکانت دیگر")
            await logbus.log_error("جوین", "Join (full)", e, account=phone)
            return True
        except UserAlreadyParticipantError:
            # request was sent; the group is already recorded (a prior join or
            # _sync_dialogs at startup), so just close this duplicate link.
            db.set_join_status(qid, "joined", account_id=account["id"])
            return True
        except InviteRequestSentError:
            # group needs ADMIN APPROVAL — the join request was sent and is
            # pending. Not an error and must NOT be retried; close it cleanly.
            db.set_join_status(qid, "failed")
            await logbus.card_join(
                account_phone=phone, group_title="", link=link, ok=False,
                detail="⏳ درخواست عضویت فرستاده شد (نیاز به تأیید ادمین)")
            return True
        except (InviteHashExpiredError, InviteHashInvalidError, ChannelPrivateError) as e:
            db.set_join_status(qid, "failed")
            await logbus.card_join(account_phone=phone, group_title="", link=link,
                                   ok=False, detail="لینک منقضی/نامعتبر/خصوصی")
            await logbus.log_error("جوین", "Join (bad link)", e, account=phone)
            return True
        except RuntimeError as e:
            # session problem — no request sent; leave item pending
            await logbus.log_error("جوین", "اتصال اکانت", e, account=phone)
            return False
        except Exception as e:  # noqa: BLE001
            if int(item.get("tries") or 0) + 1 >= 3:
                db.set_join_status(qid, "failed")
            await logbus.card_join(account_phone=phone, group_title="", link=link,
                                   ok=False, detail=type(e).__name__)
            await logbus.log_error("جوین", "Join", e, account=phone)
            return True
        await _record_group(client, account, qid, link, chat)
        return True
    finally:
        # Count EVERY join request that actually went to Telegram against the
        # daily cap — this is what really protects the account from flood/bans.
        if attempted:
            db.inc_joins_today(phone)


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
            attempted = await _do_join(account, item)
            if attempted:
                # a join request was sent -> pace it with the full safe delay.
                fresh = db.get_account(account["phone"]) or account
                await asyncio.sleep(throttle.jittered_delay(fresh))
            else:
                # no request sent (session issue) -> move on quickly.
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("جوین", "حلقه‌ی جوینر", e)
            await asyncio.sleep(config.JOINER_IDLE_SLEEP)
