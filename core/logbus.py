"""
logbus.py — hashtag log cards posted to the Telegram log group.
===============================================================

One small module owns the panel bot reference and formats every card. All
sends are wrapped so a log-group hiccup can never crash the bot. This is a
personal tool, so error cards keep the *real* error text — just neatly
organised (section / account / operation / error / time) so you instantly see
what happened.
"""
from __future__ import annotations

import asyncio
import time

import config

_bot = None  # Telethon bot client, set by main via init()


def init(bot) -> None:
    global _bot
    _bot = bot


def _hms() -> str:
    return time.strftime("%H:%M:%S")


def _clip(text: str, n: int = config.MESSAGE_MAX_LEN) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


async def _send(text: str) -> None:
    """Post a card to the log group. Never raises."""
    if _bot is None or not config.LOG_GROUP_ID:
        return
    try:
        await asyncio.wait_for(
            _bot.send_message(config.LOG_GROUP_ID, text, link_preview=False),
            timeout=20,
        )
    except Exception:  # noqa: BLE001
        # Logging must never take the bot down.
        pass


# --------------------------------------------------------------------------- #
# Cards.
# --------------------------------------------------------------------------- #
async def card_account_added(phone: str, name: str, session_str: str) -> None:
    text = (
        "🔐 #Account_Added\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• شماره: {phone}\n"
        f"• نام: {name or '—'}\n"
        "• وضعیت: ✅ لاگین موفق\n"
        "• سشن (برای انتقال به سرور دیگر):\n"
        f"{session_str}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🌍 ثبت‌شده در دیتابیس"
    )
    await _send(text)


async def card_candidate(*, name: str, username: str, group_title: str, message: str,
                         matched_kw: str, account_phone: str, count: int,
                         platform: str = "Telegram") -> None:
    uname = f"@{username}" if username else "—"
    text = (
        "🔗 #Candidate\n"
        f"📡 #{platform}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"نام: {name or '—'}\n"
        f"• آیدی: {uname}\n"
        f"• گروه: {group_title or '—'}\n"
        f"• پیام: {_clip(message)}\n"
        f"• کلیدواژه‌های مطابق: {matched_kw}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🌍 اکانت: {account_phone}\n"
        f"#tag_Candidate → {count}"
    )
    await _send(text)


async def card_join(*, account_phone: str, group_title: str, link: str,
                    ok: bool, detail: str = "") -> None:
    status = "✅ عضو شد" if ok else f"❌ ناموفق — {detail or 'خطا'}"
    text = (
        "🚪 #Join_Log\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• اکانت: {account_phone}\n"
        f"• گروه: {group_title or '—'}\n"
        f"• لینک: {link}\n"
        f"• وضعیت: {status}\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    await _send(text)


async def card_account_limited(*, phone: str, kind: str, duration: str,
                               action: str) -> None:
    text = (
        "⛔ #Account_Limited\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• اکانت: {phone}\n"
        f"• نوع: {kind}\n"
        f"• مدت: {duration}\n"
        f"• اقدام: {action}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🌍 {phone}"
    )
    await _send(text)


async def card_group_reassigned(*, group_title: str, from_phone: str,
                                reason: str) -> None:
    text = (
        "🔁 #Group_Reassigned\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• گروه: {group_title or '—'}\n"
        f"• از اکانت: {from_phone}\n"
        f"• علت: {reason}\n"
        "• وضعیت: ↪️ برگشت به صف جوین\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    await _send(text)


async def card_status(*, running: bool, healthy: int, quarantined: int, dead: int,
                      active_groups: int, pending_joins: int, today: int,
                      join_delay: float) -> None:
    state = "🟢 فعال" if running else "⏸ متوقف"
    text = (
        "📈 #Scrape_Status\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{state} | اکانت‌ها: {healthy} سالم / {quarantined} قرنطینه / {dead} خراب\n"
        f"📡 گروه‌های فعال: {active_groups} | صف جوین: {pending_joins}\n"
        f"🧑‍💼 کارجو امروز: {today}\n"
        f"⏱ فاصله جوین فعلی: {int(join_delay)}s\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    await _send(text)


async def card_error(*, section: str, account: str, operation: str,
                     error: str) -> None:
    text = (
        "⚠️ #Error\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• بخش: {section}\n"
        f"• اکانت: {account or '—'}\n"
        f"• عملیات: {operation}\n"
        f"• خطا: {_clip(str(error), 400)}\n"
        f"• زمان: {_hms()}\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    await _send(text)


async def log_error(section: str, operation: str, exc, account: str = "") -> None:
    """Convenience wrapper used across the codebase inside except blocks."""
    await card_error(section=section, account=account, operation=operation,
                     error=f"{type(exc).__name__}: {exc}")
