"""
panel/panel.py — the Bot-API control panel (Telethon bot).
==========================================================

Owner-only. Wires the inline menus (panel/menus.py) to real actions: managing
keywords, seed channels and accounts, the two-step start/stop of the engine,
and the interactive add-account login (phone -> code -> optional 2FA) which
also posts the session string to the log group for easy migration.

Everything is guarded so a bad button or message can never crash the bot.
"""
from __future__ import annotations

import asyncio
import time

from telethon import events, Button
from telethon.errors import MessageNotModifiedError

import config
from core import db, logbus, matcher, health
from platforms.telegram import client as tg, discover, scraper
from panel import menus

_bot = None
_engine = None

# owner_id -> {"mode": str, "login": ctx, "ts": float}
_pending: dict = {}


def init(bot, engine) -> None:
    global _bot, _engine
    _bot = bot
    _engine = engine
    bot.add_event_handler(_on_start, events.NewMessage(pattern=r"^/start$"))
    bot.add_event_handler(_on_callback, events.CallbackQuery())
    bot.add_event_handler(_on_text, events.NewMessage(incoming=True))
    asyncio.create_task(_sweep_pending())


def _is_owner(event) -> bool:
    try:
        return int(event.sender_id) == int(config.OWNER_ID)
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #
async def _edit(event, builder, *args) -> None:
    text, buttons = builder(*args)
    try:
        await event.edit(text, buttons=buttons, link_preview=False)
    except MessageNotModifiedError:
        pass
    except Exception:  # noqa: BLE001
        try:
            await event.respond(text, buttons=buttons, link_preview=False)
        except Exception:  # noqa: BLE001
            pass


async def _respond(event, builder, *args) -> None:
    text, buttons = builder(*args)
    try:
        await event.respond(text, buttons=buttons, link_preview=False)
    except Exception:  # noqa: BLE001
        pass


async def _prompt(event, text: str, back: bytes = b"main") -> None:
    try:
        await event.edit(text, buttons=[[Button.inline("⬅️ انصراف", back)]],
                         link_preview=False)
    except MessageNotModifiedError:
        pass
    except Exception:  # noqa: BLE001
        await event.respond(text)


def _set_pending(owner: int, mode: str, **extra) -> None:
    data = {"mode": mode, "ts": time.time()}
    data.update(extra)
    _pending[owner] = data


def _clear_pending(owner: int) -> None:
    _pending.pop(owner, None)


async def _cancel_pending(owner: int) -> None:
    """Drop any pending text-input step; if it was a half-finished login,
    disconnect its client too. Called whenever the owner taps a button."""
    pend = _pending.pop(owner, None)
    if pend and pend.get("login"):
        await tg.cancel_ctx(pend["login"])


# --------------------------------------------------------------------------- #
# /start.
# --------------------------------------------------------------------------- #
async def _on_start(event) -> None:
    if not _is_owner(event):
        return
    _clear_pending(event.sender_id)
    await _respond(event, menus.main_menu)


# --------------------------------------------------------------------------- #
# Callback router.
# --------------------------------------------------------------------------- #
async def _on_callback(event) -> None:
    if not _is_owner(event):
        try:
            await event.answer("⛔", alert=False)
        except Exception:  # noqa: BLE001
            pass
        return
    data = (event.data or b"").decode("utf-8", "ignore")
    owner = event.sender_id
    # Any button press cancels a half-typed input step (avoids misrouting the
    # next message the owner types). Prompt actions re-arm it right after.
    await _cancel_pending(owner)
    try:
        await _dispatch(event, owner, data)
    except Exception as e:  # noqa: BLE001
        await logbus.log_error("پنل", f"callback {data}", e)
    try:
        await event.answer()
    except Exception:  # noqa: BLE001
        pass


async def _dispatch(event, owner: int, data: str) -> None:
    # --- navigation ---
    if data == "main":
        _clear_pending(owner)
        return await _edit(event, menus.main_menu)
    if data == "kw":
        return await _edit(event, menus.keywords_menu)
    if data == "src":
        return await _edit(event, menus.sources_menu)
    if data == "acc":
        return await _edit(event, menus.accounts_menu)
    if data == "cand":
        return await _edit(event, menus.candidates_menu)
    if data == "settings":
        return await _edit(event, menus.settings_menu)
    if data == "tech":
        return await _edit(event, menus.tech_menu)
    if data == "status":
        return await _edit(event, menus.status_menu)

    # --- engine on/off ---
    if data == "engine_start":
        if not db.list_accounts():
            return await _prompt(event, "اول حداقل یک اکانت اضافه کن.", b"acc")
        await _engine.start()
        return await _edit(event, menus.main_menu)
    if data == "engine_stop":
        await _engine.stop()
        return await _edit(event, menus.main_menu)

    # --- keywords ---
    if data == "kw_add":
        _set_pending(owner, "kw_add")
        return await _prompt(event, "🔑 کلیدواژه‌ی جدید رو بفرست:", b"kw")
    if data == "kw_dellist":
        return await _edit(event, menus.keywords_dellist)
    if data.startswith("kwdel:"):
        try:
            db.remove_keyword_by_id(int(data.split(":", 1)[1]))
            matcher.rebuild()
        except Exception:  # noqa: BLE001
            pass
        return await _edit(event, menus.keywords_dellist)
    if data == "kw_clear":
        db.clear_keywords()
        matcher.rebuild()
        return await _edit(event, menus.keywords_menu)

    # --- sources ---
    if data == "src_add":
        _set_pending(owner, "src_add")
        return await _prompt(event, "📡 آیدی کانال (@username) یا لینکش رو بفرست:", b"src")
    if data == "src_list":
        return await _edit(event, menus.sources_list)
    if data.startswith("srcdel:"):
        try:
            db.remove_seed_by_id(int(data.split(":", 1)[1]))
        except Exception:  # noqa: BLE001
            pass
        return await _edit(event, menus.sources_list)
    if data == "grp_list":
        return await _edit(event, menus.groups_list)
    if data == "jq_view":
        return await _edit(event, menus.join_queue_view)
    if data == "discover_now":
        await event.answer("در حال جستجو…")
        try:
            new = await discover.discover_once()
            await _prompt(event, f"🔄 {new} لینکِ جدید به صفِ جوین اضافه شد.", b"src")
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("کشف", "کشف دستی", e)
            await _prompt(event, "خطا در جستجو (در گروهِ لاگ ثبت شد).", b"src")
        return

    # --- accounts ---
    if data == "acc_add":
        _set_pending(owner, "onb_phone")
        return await _prompt(event, "📱 شماره‌ی اکانت رو با کدِ کشور بفرست:\n"
                                    "مثال: +98912xxxxxxx", b"acc")
    if data == "acc_disc":
        return await _edit(event, menus.accounts_discover_toggle)
    if data.startswith("accdisc:"):
        phone = data.split(":", 1)[1]
        acc = db.get_account(phone)
        if acc:
            new_flag = not acc.get("is_discover")
            db.set_discover(phone, new_flag)
            try:
                if new_flag:
                    # became read-only: stop scraping + hand off its groups
                    await scraper.detach_account(phone)
                    db.reassign_account_groups(acc["id"])
                    db.recount_group_count(acc["id"])
                elif db.is_running():
                    # became a scraper account: start scraping
                    await scraper.attach_account(phone)
            except Exception as e:  # noqa: BLE001
                await logbus.log_error("اکانت", "تغییر نقش کاوشگر", e, account=phone)
        return await _edit(event, menus.accounts_discover_toggle)
    if data == "acc_health":
        await event.answer("در حال بررسی…")
        try:
            await health.run_once()
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("سلامت", "بررسی دستی", e)
        return await _edit(event, menus.accounts_menu)
    if data == "acc_dellist":
        return await _edit(event, menus.accounts_dellist)
    if data.startswith("accdel:"):
        phone = data.split(":", 1)[1]
        try:
            await scraper.detach_account(phone)
            await tg.drop_client(phone)
            db.delete_account(phone)
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("اکانت", "حذف اکانت", e, account=phone)
        return await _edit(event, menus.accounts_dellist)

    # --- settings / tech edits ---
    if data == "set_refresh":
        _set_pending(owner, "set_refresh")
        return await _prompt(event, "🔁 ساعتِ رفرشِ روزانه (۰ تا ۲۳) رو بفرست:", b"settings")
    if data == "set_cap":
        _set_pending(owner, "set_cap")
        return await _prompt(event, "✏️ سقفِ جوینِ روزانه‌ی هر اکانت رو بفرست:", b"tech")
    if data == "set_floor":
        _set_pending(owner, "set_floor")
        return await _prompt(event, "✏️ کفِ فاصله‌ی جوین (ثانیه) رو بفرست:", b"tech")
    if data == "set_ceil":
        _set_pending(owner, "set_ceil")
        return await _prompt(event, "✏️ سقفِ فاصله‌ی جوین (ثانیه) رو بفرست:", b"tech")
    if data == "set_warmup":
        _set_pending(owner, "set_warmup")
        return await _prompt(
            event,
            "✏️ مراحلِ گرم‌کردن رو بفرست (مثلاً: 5,10,20,30)\n"
            "برای خاموش‌کردنِ گرم‌کردن بنویس: off", b"tech")


# --------------------------------------------------------------------------- #
# Text input router (only acts when there is a pending step).
# --------------------------------------------------------------------------- #
async def _on_text(event) -> None:
    if not _is_owner(event):
        return
    if not event.is_private:
        return
    text = (event.raw_text or "").strip()
    if text.startswith("/"):
        return
    owner = event.sender_id
    pend = _pending.get(owner)
    if not pend:
        return
    mode = pend.get("mode")
    try:
        if mode == "kw_add":
            db.add_keyword(text)
            matcher.rebuild()
            _clear_pending(owner)
            await _respond(event, menus.keywords_menu)
        elif mode == "src_add":
            db.add_seed(text.lstrip("@") if not text.startswith("http") else text)
            _clear_pending(owner)
            await _respond(event, menus.sources_menu)
        elif mode == "set_refresh":
            db.set_setting("cfg_refresh_hour", max(0, min(23, int(text))))
            _clear_pending(owner)
            await _respond(event, menus.settings_menu)
        elif mode == "set_cap":
            db.set_setting("cfg_daily_cap", max(1, int(text)))
            _clear_pending(owner)
            await _respond(event, menus.tech_menu)
        elif mode == "set_floor":
            db.set_setting("cfg_delay_floor", max(1, int(text)))
            _clear_pending(owner)
            await _respond(event, menus.tech_menu)
        elif mode == "set_ceil":
            db.set_setting("cfg_delay_ceil", max(1, int(text)))
            _clear_pending(owner)
            await _respond(event, menus.tech_menu)
        elif mode == "set_warmup":
            val = text.strip().lower()
            if val in ("off", "0", "none", "خاموش"):
                db.set_setting("cfg_warmup", "off")
            else:
                # validate it's a comma list of numbers
                stages = [int(x) for x in val.replace(" ", "").split(",") if x]
                db.set_setting("cfg_warmup", ",".join(str(s) for s in stages))
            _clear_pending(owner)
            await _respond(event, menus.tech_menu)
        elif mode in ("onb_phone", "onb_code", "onb_pass"):
            await _handle_onboarding(event, owner, pend, mode, text)
    except ValueError:
        await event.respond("عدد معتبر بفرست.")
    except Exception as e:  # noqa: BLE001
        _clear_pending(owner)
        await logbus.log_error("پنل", f"ورودی {mode}", e)
        await event.respond("خطا رخ داد (در گروهِ لاگ ثبت شد).")


async def _handle_onboarding(event, owner: int, pend: dict, mode: str, text: str) -> None:
    if mode == "onb_phone":
        phone = text.replace(" ", "")
        try:
            ctx = await tg.start_login(phone)
        except Exception as e:  # noqa: BLE001
            _clear_pending(owner)
            await logbus.log_error("اکانت", "ارسال کد", e, account=phone)
            await event.respond(f"نشد کد فرستاده شه: {type(e).__name__}")
            return
        _set_pending(owner, "onb_code", login=ctx)
        await event.respond("📩 کدی که تلگرام فرستاد رو بفرست (می‌تونی با فاصله هم بنویسی):")
        return

    if mode == "onb_code":
        ctx = pend.get("login")
        code = text.replace(" ", "").replace("-", "")
        try:
            done = await tg.finish_login(ctx, code)
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("اکانت", "ورود با کد", e,
                                   account=ctx.get("phone", ""))
            await event.respond(f"کد درست نبود ({type(e).__name__}). دوباره بفرست:")
            return
        if not done:
            _set_pending(owner, "onb_pass", login=ctx)
            await event.respond("🔒 رمزِ دومرحله‌ای رو بفرست:")
            return
        await _finalize_login(event, owner, ctx)
        return

    if mode == "onb_pass":
        ctx = pend.get("login")
        try:
            await tg.finish_password(ctx, text)
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("اکانت", "رمز دومرحله‌ای", e,
                                   account=ctx.get("phone", ""))
            await event.respond(f"رمز درست نبود ({type(e).__name__}). دوباره بفرست:")
            return
        await _finalize_login(event, owner, ctx)
        return


async def _finalize_login(event, owner: int, ctx: dict) -> None:
    info = await tg.commit_login(ctx)
    _clear_pending(owner)
    phone = info.get("phone") or ctx.get("phone", "")
    await event.respond(
        f"✅ اکانت {phone} اضافه شد!\n(رشته‌ی سشن تو گروهِ لاگ ذخیره شد)")
    await logbus.card_account_added(phone, info.get("name", ""),
                                    info.get("session_str", ""))
    # if the engine is already working, start scraping this account right away.
    if db.is_running():
        try:
            # detach first so a re-login re-attaches the handler to the NEW client
            await scraper.detach_account(phone)
            await scraper.attach_account(phone)
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("اسکرپ", "اتصال اکانت جدید", e, account=phone)
    await _respond(event, menus.accounts_menu)


# --------------------------------------------------------------------------- #
# Stale onboarding sweeper.
# --------------------------------------------------------------------------- #
async def _sweep_pending() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for owner in list(_pending.keys()):
            pend = _pending.get(owner) or {}
            if now - float(pend.get("ts", now)) > config.ONBOARD_TIMEOUT:
                ctx = pend.get("login")
                if ctx:
                    await tg.cancel_ctx(ctx)
                _clear_pending(owner)
