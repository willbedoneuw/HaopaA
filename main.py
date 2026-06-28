"""
main.py — Target Scraper entry point.
=====================================

Boots the panel bot, wires everything together, and (restart-safe) resumes the
engine if it was running before the last shutdown.

Run:  python main.py
"""
from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient

import config
from core import db, logbus, matcher, engine
from panel import panel


async def _amain() -> None:
    missing = config.missing()
    if missing:
        print("❌ تنظیماتِ لازم ناقصه (در .env پر کن): " + ", ".join(missing))
        print("   نمونه را در .env.example ببین.")
        return

    # DB first (also creates the data/ dir the bot session file needs).
    db.init_db()
    matcher.rebuild()

    bot = TelegramClient(config.PANEL_SESSION, config.API_ID, config.API_HASH)
    await bot.start(bot_token=config.BOT_TOKEN)

    logbus.init(bot)
    panel.init(bot, engine)

    # Tell the log group we're up (best effort).
    try:
        await bot.send_message(
            config.LOG_GROUP_ID,
            "🤖 Target Scraper بالا آمد.\nبرای پنل به ربات /start بده.",
            link_preview=False,
        )
    except Exception:  # noqa: BLE001
        pass

    # Restart-safe: if we were working before, start again.
    try:
        await engine.resume_if_running()
    except Exception as e:  # noqa: BLE001
        await logbus.log_error("بوت", "resume_if_running", e)

    print("✅ Target Scraper آماده است. (Ctrl+C برای خروج)")
    await bot.run_until_disconnected()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\n👋 خاموش شد.")
    except Exception as e:  # noqa: BLE001
        print(f"خطای کشنده: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
