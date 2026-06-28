"""
throttle.py — adaptive join rate control (AIMD), per account.
=============================================================

Goal: join groups as fast as is *safe*, and back off hard the moment Telegram
pushes back — so accounts survive.

Rules (all per-account, all persisted in the DB so a restart keeps the state):
  * Start delay 90s, floor 60s, ceiling 600s.
  * After N successful joins in a row -> delay *= 0.8 (down to the floor).
  * On FloodWait(W) -> sleep W (+buffer), delay = min(ceil, max(delay, W)*1.5),
    reset streak, quarantine the account until the wait is over.
  * On a heavy limit (PeerFlood) -> multi-hour quarantine + mark limited.
  * Jitter every delay 0.8x–1.2x so it never looks robotic.
  * Daily cap per account (warmup-aware: a brand-new account ramps 5→10→20→30).

Important: throttling only affects JOINING. An account that is rate-limited for
joins keeps scraping the groups it already belongs to — that is handled in the
scraper, not here.
"""
from __future__ import annotations

import random
import time

import config
from core import db


def _ov_int(name: str, default: int) -> int:
    v = db.get_setting(f"cfg_{name}")
    try:
        return int(v) if v is not None else default
    except Exception:  # noqa: BLE001
        return default


def _ov_float(name: str, default: float) -> float:
    v = db.get_setting(f"cfg_{name}")
    try:
        return float(v) if v is not None else default
    except Exception:  # noqa: BLE001
        return default


def cfg_daily_cap() -> int:
    return _ov_int("daily_cap", config.JOIN_DAILY_CAP)


def cfg_floor() -> float:
    return _ov_float("delay_floor", config.JOIN_DELAY_FLOOR)


def cfg_ceil() -> float:
    return _ov_float("delay_ceil", config.JOIN_DELAY_CEIL)


def cfg_warmup_stages() -> list:
    """Warmup ramp, editable from the panel (cfg_warmup). Examples:
    "5,10,20,30" -> ramp; "off"/"0"/empty -> warmup disabled (use daily cap)."""
    raw = db.get_setting("cfg_warmup")
    if raw is None:
        return list(config.WARMUP_STAGES)
    raw = str(raw).strip().lower()
    if raw in ("off", "0", "none", ""):
        return []
    try:
        stages = [int(x) for x in raw.replace(" ", "").split(",") if x]
        return stages or []
    except Exception:  # noqa: BLE001
        return list(config.WARMUP_STAGES)


def effective_daily_cap(account: dict) -> int:
    """Daily join cap. With warmup enabled, a young account ramps up; with
    warmup off, it's simply the daily cap."""
    cap = cfg_daily_cap()
    stages = cfg_warmup_stages()
    if not stages:
        return cap
    stage = int(account.get("warmup_stage") or 0)
    stage = max(0, min(stage, len(stages) - 1))
    return min(cap, stages[stage])


def can_join(account: dict) -> bool:
    """Is this account allowed to attempt a join right now?"""
    if account.get("status") != "active":
        return False
    if int(account.get("quarantine_until") or 0) > int(time.time()):
        return False
    if int(account.get("joins_today") or 0) >= effective_daily_cap(account):
        return False
    if int(account.get("group_count") or 0) >= config.ACCOUNT_GROUP_LIMIT:
        return False
    return True


def reason_blocked(account: dict) -> str:
    """Human-readable reason an account can't join (for status/debug)."""
    if account.get("status") != "active":
        return f"status={account.get('status')}"
    if int(account.get("quarantine_until") or 0) > int(time.time()):
        return "quarantine"
    if int(account.get("joins_today") or 0) >= effective_daily_cap(account):
        return "daily_cap"
    if int(account.get("group_count") or 0) >= config.ACCOUNT_GROUP_LIMIT:
        return "full"
    return "ok"


def jittered_delay(account: dict) -> float:
    base = float(account.get("join_delay") or config.JOIN_DELAY_START)
    base = max(cfg_floor(), min(cfg_ceil(), base))
    return base * random.uniform(config.JOIN_JITTER_MIN, config.JOIN_JITTER_MAX)


def on_success(account: dict) -> None:
    """Register a successful join: bump streak, maybe speed up, count the join."""
    phone = account["phone"]
    streak = int(account.get("success_streak") or 0) + 1
    delay = float(account.get("join_delay") or config.JOIN_DELAY_START)
    if streak >= config.SUCCESS_STREAK_THRESHOLD:
        delay = max(cfg_floor(), delay * config.DELAY_DECREASE_FACTOR)
        streak = 0
    db.update_throttle_state(phone, join_delay=delay, success_streak=streak)


def on_floodwait(account: dict, seconds: int) -> int:
    """Register a FloodWait. Returns how long the joiner should sleep."""
    phone = account["phone"]
    delay = float(account.get("join_delay") or config.JOIN_DELAY_START)
    delay = min(cfg_ceil(), max(delay, seconds) * config.FLOODWAIT_DELAY_FACTOR)
    until = int(time.time()) + seconds + config.FLOOD_BUFFER
    db.update_throttle_state(
        phone, join_delay=delay, success_streak=0,
        last_floodwait=int(time.time()), quarantine_until=until,
    )
    return seconds + config.FLOOD_BUFFER


def on_peerflood(account: dict) -> None:
    """Heavy limit: quarantine the account for a few hours and mark it limited."""
    phone = account["phone"]
    until = int(time.time()) + config.PEERFLOOD_QUARANTINE
    db.update_throttle_state(phone, success_streak=0, quarantine_until=until)
    db.set_account_status(phone, "limited")
