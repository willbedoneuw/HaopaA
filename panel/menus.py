"""
panel/menus.py — text + inline button layouts for the control panel.
====================================================================

Pure presentation: builds the message text and Telethon inline keyboards for
each screen. No side effects, no network — the panel module wires these to
actions.
"""
from __future__ import annotations

from telethon import Button

import config
from core import db, matcher, throttle


# --------------------------------------------------------------------------- #
# Main menu.
# --------------------------------------------------------------------------- #
def main_menu():
    running = db.is_running()
    accounts = db.list_accounts()
    healthy = len([a for a in accounts if a.get("status") == "active"])
    quarantined = len([a for a in accounts if a.get("status") == "limited"])
    state = "🟢 در حالِ کار" if running else "⏸ متوقف"
    text = (
        "🎯 Target Scraper — پنلِ مدیریت\n\n"
        f"وضعیت: {state}\n"
        f"اکانت‌ها: {healthy} سالم / {quarantined} قرنطینه\n"
        f"گروه‌های فعال: {db.count_groups('active')}  |  "
        f"کارجوهای امروز: {db.count_candidates_today()}"
    )
    toggle = (Button.inline("⏸ توقفِ کار", b"engine_stop") if running
              else Button.inline("▶️ شروعِ کار", b"engine_start"))
    buttons = [
        [toggle],
        [Button.inline("🔑 کلیدواژه‌ها", b"kw"),
         Button.inline("📡 منابع و گروه‌ها", b"src")],
        [Button.inline("👤 اکانت‌ها", b"acc"),
         Button.inline("🧑‍💼 کارجوها", b"cand")],
        [Button.inline("⚙️ تنظیمات", b"settings"),
         Button.inline("🔧 تنظیماتِ فنی", b"tech")],
        [Button.inline("📊 وضعیتِ عملیاتی", b"status")],
    ]
    return text, buttons


# --------------------------------------------------------------------------- #
# Keywords.
# --------------------------------------------------------------------------- #
def keywords_menu():
    words = db.list_keywords()
    if words:
        lines = "\n".join(f"{i+1}. {w}" for i, w in enumerate(words))
    else:
        lines = "— هنوز کلیدواژه‌ای ثبت نشده —"
    text = (
        f"🔑 کلیدواژه‌های فعال ({len(words)})\n\n"
        f"{lines}\n\n"
        "ℹ️ کافیه فقط یکی از این‌ها تو متنِ پیام باشه."
    )
    buttons = [
        [Button.inline("➕ افزودن", b"kw_add"),
         Button.inline("🗑 حذفِ یکی", b"kw_dellist")],
        [Button.inline("🧹 پاک‌کردنِ همه", b"kw_clear")],
        [Button.inline("⬅️ بازگشت", b"main")],
    ]
    return text, buttons


def keywords_dellist():
    rows = db.list_keywords_rows()
    buttons = [[Button.inline(f"🗑 {r['word']}", f"kwdel:{r['id']}".encode())]
               for r in rows]
    buttons.append([Button.inline("⬅️ بازگشت", b"kw")])
    text = "کدوم کلیدواژه حذف بشه؟" if rows else "چیزی برای حذف نیست."
    return text, buttons


# --------------------------------------------------------------------------- #
# Sources & groups.
# --------------------------------------------------------------------------- #
def sources_menu():
    seeds = db.list_seeds()
    text = (
        "📡 منابع و گروه‌ها\n\n"
        f"🔹 کانال‌های منبع (Seed): {len(seeds)}\n"
        f"🔹 صفِ جوین: {db.count_pending_joins()} در انتظار\n"
        f"🔹 گروه‌های فعال: {db.count_groups('active')}\n"
        f"🔹 گروه‌های مرده: {db.count_groups('dead')}"
    )
    buttons = [
        [Button.inline("➕ افزودن کانالِ منبع", b"src_add"),
         Button.inline("📜 لیستِ منابع", b"src_list")],
        [Button.inline("📋 لیستِ گروه‌ها", b"grp_list"),
         Button.inline("⏳ صفِ جوین", b"jq_view")],
        [Button.inline("🔄 پیدا کردنِ لینکِ جدید (الان)", b"discover_now")],
        [Button.inline("⬅️ بازگشت", b"main")],
    ]
    return text, buttons


def sources_list():
    seeds = db.list_seeds()
    if seeds:
        lines = "\n".join(f"• {s['ref']}" for s in seeds)
        buttons = [[Button.inline(f"🗑 {s['ref']}", f"srcdel:{s['id']}".encode())]
                   for s in seeds]
    else:
        lines = "— هیچ کانالِ منبعی ثبت نشده —"
        buttons = []
    buttons.append([Button.inline("⬅️ بازگشت", b"src")])
    return f"📜 کانال‌های منبع:\n\n{lines}", buttons


def groups_list():
    groups = db.list_groups("active")
    shown = groups[:40]
    if shown:
        lines = "\n".join(f"🟢 {g.get('title') or g.get('link') or g['tg_id']}"
                          for g in shown)
        if len(groups) > len(shown):
            lines += f"\n… و {len(groups) - len(shown)} گروهِ دیگر"
    else:
        lines = "— هنوز عضوِ گروهی نشده —"
    return f"📋 گروه‌های فعال ({len(groups)}):\n\n{lines}", [
        [Button.inline("⬅️ بازگشت", b"src")]]


def join_queue_view():
    pending = db.count_pending_joins()
    return (
        f"⏳ صفِ جوین\n\n• در انتظار: {pending}\n\n"
        "این‌ها به‌ترتیب و با فاصله‌ی امن جوین می‌شن.",
        [[Button.inline("⬅️ بازگشت", b"src")]],
    )


# --------------------------------------------------------------------------- #
# Accounts.
# --------------------------------------------------------------------------- #
def _acc_line(a: dict) -> str:
    status = a.get("status")
    icon = {"active": "🟢", "limited": "🟡", "full": "📦", "dead": "🔴"}.get(status, "⚪")
    disc = "✅" if a.get("is_discover") else "—"
    cap = throttle.effective_daily_cap(a)
    return (f"{icon} {a['phone']} | گروه: {a.get('group_count') or 0} | "
            f"جوین امروز: {a.get('joins_today') or 0}/{cap} | کاوشگر: {disc}")


def accounts_menu():
    accounts = db.list_accounts()
    if accounts:
        lines = "\n".join(_acc_line(a) for a in accounts)
    else:
        lines = "— هیچ اکانتی اضافه نشده —"
    text = f"👤 اکانت‌ها ({len(accounts)})\n\n{lines}"
    buttons = [
        [Button.inline("➕ افزودن اکانت", b"acc_add")],
        [Button.inline("🧭 انتخابِ اکانت‌های کاوشگر", b"acc_disc")],
        [Button.inline("❤️ بررسیِ سلامتِ همه", b"acc_health"),
         Button.inline("🗑 حذفِ اکانت", b"acc_dellist")],
        [Button.inline("⬅️ بازگشت", b"main")],
    ]
    return text, buttons


def accounts_discover_toggle():
    accounts = db.list_accounts()
    buttons = []
    for a in accounts:
        mark = "✅" if a.get("is_discover") else "⬜"
        buttons.append([Button.inline(f"{mark} {a['phone']}",
                                      f"accdisc:{a['phone']}".encode())])
    buttons.append([Button.inline("⬅️ بازگشت", b"acc")])
    text = ("🧭 اکانت‌های کاوشگر (برای خواندنِ کانالِ منبع)\n\n"
            "این‌ها فقط کانال‌ها رو می‌خونن و لینکِ گروه پیدا می‌کنن.\n"
            "بقیه کارِ جوین و اسکرپ رو می‌کنن. (روی هرکدوم بزن تا روشن/خاموش شه)")
    return text, buttons


def accounts_dellist():
    accounts = db.list_accounts()
    buttons = [[Button.inline(f"🗑 {a['phone']}", f"accdel:{a['phone']}".encode())]
               for a in accounts]
    buttons.append([Button.inline("⬅️ بازگشت", b"acc")])
    text = "کدوم اکانت حذف بشه؟" if accounts else "اکانتی نیست."
    return text, buttons


# --------------------------------------------------------------------------- #
# Candidates (counts only).
# --------------------------------------------------------------------------- #
def candidates_menu():
    text = (
        "🧑‍💼 کارجوهای پیداشده\n\n"
        f"📊 امروز: {db.count_candidates_today()}\n"
        f"📊 این هفته: {db.count_candidates_week()}\n"
        f"📊 کلِ کل: {db.count_candidates_total()}\n\n"
        "دیتای کاملِ هر کارجو در گروهِ لاگ (کارت #Candidate) می‌آید."
    )
    buttons = [
        [Button.inline("🔄 به‌روزرسانی", b"cand")],
        [Button.inline("⬅️ بازگشت", b"main")],
    ]
    return text, buttons


# --------------------------------------------------------------------------- #
# Settings.
# --------------------------------------------------------------------------- #
def settings_menu():
    refresh = db.get_setting("cfg_refresh_hour", str(config.SEED_REFRESH_HOUR))
    log_ok = "متصل ✅" if config.LOG_GROUP_ID else "تنظیم نشده ❌"
    text = (
        "⚙️ تنظیمات\n\n"
        f"• گروهِ لاگ: {log_ok}\n"
        f"• ساعتِ رفرشِ روزانه‌ی منابع: {refresh}:۰۰\n"
    )
    buttons = [
        [Button.inline("🔁 تغییرِ ساعتِ رفرش", b"set_refresh")],
        [Button.inline("⬅️ بازگشت", b"main")],
    ]
    return text, buttons


# --------------------------------------------------------------------------- #
# Technical / engine settings.
# --------------------------------------------------------------------------- #
def tech_menu():
    text = (
        "🔧 تنظیماتِ فنی\n\n"
        "📥 روشِ دریافت: زنگِ درِ تلگرام (بلادرنگ) ✅\n"
        "🔎 تطبیقِ کلیدواژه: فوری، منطقِ «یکی کافیه»\n"
        f"🔑 کلیدواژه‌های فعال: {matcher.count()}\n\n"
        "⏱ کنترلِ جوین (برای امنیتِ اکانت‌ها):\n"
        f"   • فاصله‌ی شروع: {int(config.JOIN_DELAY_START)}s\n"
        f"   • کفِ فاصله: {int(throttle.cfg_floor())}s | "
        f"سقف: {int(throttle.cfg_ceil())}s\n"
        f"   • سقفِ جوینِ روزانه‌ی هر اکانت: {throttle.cfg_daily_cap()}\n"
        f"   • گرم‌کردنِ اکانتِ نو: {' → '.join(str(x) for x in config.WARMUP_STAGES)}"
    )
    buttons = [
        [Button.inline("✏️ سقفِ روزانه", b"set_cap")],
        [Button.inline("✏️ کفِ فاصله", b"set_floor"),
         Button.inline("✏️ سقفِ فاصله", b"set_ceil")],
        [Button.inline("⬅️ بازگشت", b"main")],
    ]
    return text, buttons


# --------------------------------------------------------------------------- #
# Operational status.
# --------------------------------------------------------------------------- #
def status_menu():
    accounts = db.list_accounts()
    healthy = len([a for a in accounts if a.get("status") == "active"])
    quarantined = len([a for a in accounts if a.get("status") == "limited"])
    full = len([a for a in accounts if a.get("status") == "full"])
    dead = len([a for a in accounts if a.get("status") == "dead"])
    running = "🟢 فعال" if db.is_running() else "⏸ متوقف"
    text = (
        "📊 وضعیتِ عملیاتی\n\n"
        f"ربات: {running}\n"
        f"👤 اکانت‌ها: {healthy} سالم، {quarantined} قرنطینه، {full} پُر، {dead} خراب\n"
        f"📡 گروه‌های فعال: {db.count_groups('active')}\n"
        f"⏳ صفِ جوین: {db.count_pending_joins()}\n"
        f"🧑‍💼 کارجو امروز: {db.count_candidates_today()} | "
        f"این هفته: {db.count_candidates_week()}"
    )
    buttons = [
        [Button.inline("🔄 به‌روزرسانی", b"status")],
        [Button.inline("⬅️ بازگشت", b"main")],
    ]
    return text, buttons
