"""
core/scheduler.py — periodic background loops.
==============================================

Three light loops:
  * discover_loop   — re-read seed channels every DISCOVER_INTERVAL so the join
                      queue never runs dry.
  * status_loop     — post a #Scrape_Status card every STATUS_CARD_INTERVAL.
  * maintenance_loop— once per local day: reset joins_today, advance the warmup
                      stage of young accounts, and run an extra discovery pass.

All loops are gated by ``should_run`` and are crash-proof.
"""
from __future__ import annotations

import asyncio
import datetime

import config
from core import db, logbus
from platforms.telegram import discover


async def discover_loop(should_run) -> None:
    # small initial delay so startup isn't hammered
    await asyncio.sleep(10)
    while should_run():
        try:
            if db.is_running():
                new = await discover.discover_once()
                if new:
                    await logbus.card_status(
                        running=True,
                        healthy=len(db.list_accounts_by_status("active")),
                        quarantined=len(db.list_accounts_by_status("limited")),
                        dead=len(db.list_accounts_by_status("dead")),
                        active_groups=db.count_groups("active"),
                        pending_joins=db.count_pending_joins(),
                        today=db.count_candidates_today(),
                        join_delay=config.JOIN_DELAY_START,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("کشف", "حلقه‌ی کشف دوره‌ای", e)
        await asyncio.sleep(config.DISCOVER_INTERVAL)


async def status_loop(should_run) -> None:
    while should_run():
        await asyncio.sleep(config.STATUS_CARD_INTERVAL)
        try:
            accounts = db.list_accounts()
            healthy = len([a for a in accounts if a.get("status") == "active"])
            quarantined = len([a for a in accounts if a.get("status") == "limited"])
            dead = len([a for a in accounts if a.get("status") == "dead"])
            # representative current join delay (smallest active delay)
            delays = [float(a.get("join_delay") or config.JOIN_DELAY_START)
                      for a in accounts if a.get("status") == "active"]
            jd = min(delays) if delays else config.JOIN_DELAY_START
            await logbus.card_status(
                running=db.is_running(), healthy=healthy, quarantined=quarantined,
                dead=dead, active_groups=db.count_groups("active"),
                pending_joins=db.count_pending_joins(),
                today=db.count_candidates_today(), join_delay=jd,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("وضعیت", "کارت دوره‌ای", e)


def _today_key() -> str:
    return datetime.date.today().isoformat()


async def maintenance_loop(should_run) -> None:
    """Detect a new local day and run the daily resets + warmup advance."""
    while should_run():
        try:
            last = db.get_setting("last_daily_run", "")
            today = _today_key()
            now_hour = datetime.datetime.now().hour
            try:
                refresh_hour = int(db.get_setting("cfg_refresh_hour",
                                                  config.SEED_REFRESH_HOUR))
            except Exception:  # noqa: BLE001
                refresh_hour = config.SEED_REFRESH_HOUR
            if last != today and now_hour >= refresh_hour:
                db.reset_joins_today_all()
                _advance_warmup()
                db.set_setting("last_daily_run", today)
                try:
                    if db.is_running():
                        await discover.discover_once()
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            await logbus.log_error("نگهداری", "حلقه‌ی روزانه", e)
        await asyncio.sleep(60)


def _advance_warmup() -> None:
    max_stage = len(config.WARMUP_STAGES) - 1
    for acc in db.list_accounts():
        stage = int(acc.get("warmup_stage") or 0)
        if stage < max_stage:
            db.update_throttle_state(acc["phone"], warmup_stage=stage + 1)
