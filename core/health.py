"""
core/health.py — self-heal + session checks.
=============================================

Periodically:
  * brings accounts out of quarantine once their wait is over,
  * re-activates a "full" account if it actually has room again,
  * detects a dead/revoked session and reassigns that account's groups so they
    keep being scraped by a healthy account.

We deliberately do NOT disconnect+reopen healthy clients (no reconnect churn).
We connect idempotently and ensure each non-dead account is attached to the
scraper, so scraping self-heals after a transient startup failure.
"""
from __future__ import annotations

import asyncio
import time

import config
from core import db, logbus
from platforms.telegram import client as tg


async def _heal_states() -> None:
    now = int(time.time())
    for acc in db.list_accounts():
        phone = acc["phone"]
        status = acc.get("status")
        q_until = int(acc.get("quarantine_until") or 0)
        # quarantine expired -> back to active
        if status == "limited" and q_until and q_until <= now:
            db.set_account_status(phone, "active")
        # "full" but actually has room now (groups died/reassigned)
        if status == "full":
            cnt = db.recount_group_count(acc["id"])
            if cnt < config.ACCOUNT_GROUP_LIMIT:
                db.set_account_status(phone, "active")


async def _probe_sessions() -> None:
    """For every non-dead account: make sure it's connected AND actually
    scraping. Detect dead/revoked sessions, mark them dead, detach their
    handler and reassign their groups so a healthy account re-joins them."""
    from platforms.telegram import scraper  # local import avoids any import cycle
    for acc in db.list_accounts():
        phone = acc["phone"]
        if acc.get("status") == "dead":
            continue
        try:
            async with tg.lock(phone):
                await asyncio.wait_for(tg.get_client(phone), timeout=40)
        except RuntimeError as e:
            if str(e) in ("unauthorized", "no_session"):
                db.set_account_status(phone, "dead")
                await scraper.detach_account(phone)
                moved = db.reassign_account_groups(acc["id"])
                db.recount_group_count(acc["id"])
                await logbus.card_account_limited(
                    phone=phone, kind="سشن خراب/باطل", duration="—",
                    action=f"🔴 اکانت خراب | 🔁 {moved} گروه برای واگذاری به صف رفت")
                if moved:
                    await logbus.card_group_reassigned(
                        group_title=f"{moved} گروه", from_phone=phone,
                        reason="سشن اکانت باطل شد")
            else:
                await logbus.log_error("سلامت", "بررسی سشن", e, account=phone)
            continue
        except Exception as e:  # noqa: BLE001
            # transient network error -> leave as-is, try again next cycle
            await logbus.log_error("سلامت", "بررسی سشن", e, account=phone)
            continue
        # Connected fine. If the engine is working, make sure this account is
        # actually attached to the scraper (self-heal a failed earlier attach).
        if db.is_running():
            try:
                await scraper.attach_account(phone)
            except Exception as e:  # noqa: BLE001
                await logbus.log_error("سلامت", "اتصال اسکرپ", e, account=phone)


async def run_once() -> None:
    await _heal_states()
    await _probe_sessions()


async def health_loop(should_run) -> None:
    while should_run():
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("سلامت", "حلقه‌ی سلامت", e)
        await asyncio.sleep(config.HEALTH_INTERVAL)
