"""
logbus.py — hashtag log cards posted to the Telegram log group.
===============================================================

One small module owns the panel bot reference and formats every card. All
sends are wrapped so a log-group hiccup can never crash the bot. This is a
personal tool, so error cards keep the *real* error text — just neatly
organised (section / account / operation / error / time) so you instantly see
what happened.

Card style uses the "--|" header/footer prefix and a dashed separator line.
"""
from __future__ import annotations

import asyncio
import time

import config

_bot = None  # Telethon bot client, set by main via init()

SEP = "-------------------------------"


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
        "--| 🔐 - #Account_Added\n"
        f"{SEP}\n"
        f"--| Phone - {phone}\n"
        f"• Name : {name or '—'}\n"
        "• State : ✅ Login OK\n"
        "• Session (move to another server):\n"
        f"{session_str}\n"
        f"{SEP}\n"
        "--| 🌍 - saved to database"
    )
    await _send(text)


async def card_candidate(*, name: str, username: str, group_title: str, message: str,
                         matched_kw: str, account_phone: str, count: int,
                         platform: str = "Telegram") -> None:
    uname = f"@{username}" if username else "—"
    text = (
        "--| 🔗 - #Candidate\n"
        f"--| 📡 #{platform}\n"
        f"{SEP}\n"
        f"--| Name - {name or '—'}\n"
        f"• id : {uname}\n"
        f"• Group: {group_title or '—'}\n"
        f"• Message: {_clip(message)}\n"
        f"• Matched keywords: {matched_kw}\n"
        f"{SEP}\n"
        f"--| 🌍 - {account_phone}\n"
        f"--| #tag_Candidate → {count}"
    )
    await _send(text)


async def card_join(*, account_phone: str, group_title: str, link: str,
                    ok: bool, detail: str = "") -> None:
    state = "✅ Joined" if ok else f"✗ Failed — {detail or 'error'}"
    text = (
        "--| 🚪 - #Join_Log\n"
        f"{SEP}\n"
        f"--| Account - {account_phone}\n"
        f"• Group : {group_title or '—'}\n"
        f"• Link : {link}\n"
        f"• State : {state}\n"
        f"{SEP}"
    )
    await _send(text)


async def card_account_limited(*, phone: str, kind: str, duration: str,
                               action: str) -> None:
    text = (
        "--| ⛔ - #Account_Limited\n"
        f"{SEP}\n"
        f"--| Account - {phone}\n"
        f"• Type : {kind}\n"
        f"• Duration : {duration}\n"
        f"• Action : {action}\n"
        f"{SEP}\n"
        f"--| 🌍 - {phone}"
    )
    await _send(text)


async def card_group_reassigned(*, group_title: str, from_phone: str,
                                reason: str) -> None:
    text = (
        "--| 🔁 - #Group_Reassigned\n"
        f"{SEP}\n"
        f"--| Group - {group_title or '—'}\n"
        f"• From account : {from_phone}\n"
        f"• Reason : {reason}\n"
        "• State : ↪️ back to join queue\n"
        f"{SEP}"
    )
    await _send(text)


async def card_status(*, running: bool, healthy: int, quarantined: int, dead: int,
                      active_groups: int, pending_joins: int, today: int,
                      join_delay: float) -> None:
    state = "🟢 Running" if running else "⏸ Stopped"
    text = (
        "--| 📈 - #Scrape_Status\n"
        f"{SEP}\n"
        f"--| State - {state}\n"
        f"• Accounts : {healthy} ok / {quarantined} quarantine / {dead} dead\n"
        f"• Active groups : {active_groups} | Join queue : {pending_joins}\n"
        f"• Candidates today : {today}\n"
        f"• Join delay : {int(join_delay)}s\n"
        f"{SEP}"
    )
    await _send(text)


async def card_error(*, section: str, account: str, operation: str,
                     error: str) -> None:
    text = (
        "--| ⚠️ - #Error\n"
        f"{SEP}\n"
        f"--| Section - {section}\n"
        f"• Account : {account or '—'}\n"
        f"• Operation : {operation}\n"
        f"• Error : {_clip(str(error), 400)}\n"
        f"• Time : {_hms()}\n"
        f"{SEP}"
    )
    await _send(text)


async def log_error(section: str, operation: str, exc, account: str = "") -> None:
    """Convenience wrapper used across the codebase inside except blocks."""
    await card_error(section=section, account=account, operation=operation,
                     error=f"{type(exc).__name__}: {exc}")
