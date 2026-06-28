"""
core/engine.py — the start/stop controller.
============================================

Owns the background tasks (joiner, scraper writer, discovery, status,
maintenance, health) and the persisted on/off flag. The panel calls
``start()`` / ``stop()``; ``main`` calls ``resume_if_running()`` on boot so the
bot remembers whether it should be working (restart-safe).
"""
from __future__ import annotations

import asyncio

import config
from core import db, logbus, matcher, scheduler, health
from platforms.telegram import joiner, scraper

_tasks: list = []


def _should_run() -> bool:
    return db.is_running()


def _spawn(coro) -> None:
    _tasks.append(asyncio.create_task(coro))


async def start() -> dict:
    """Turn the engine on: persist the flag, attach scrapers, spawn loops.
    Returns a small summary dict for the panel."""
    db.set_running(True)
    matcher.rebuild()
    attached = await scraper.start()

    # clear any finished task refs, then spawn fresh loops
    _clear_finished()
    if not _loops_alive():
        _spawn(joiner.joiner_loop(_should_run))
        _spawn(scheduler.discover_loop(_should_run))
        _spawn(scheduler.status_loop(_should_run))
        _spawn(scheduler.maintenance_loop(_should_run))
        _spawn(health.health_loop(_should_run))

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
    return {"attached": attached, "groups": db.count_groups("active")}


async def stop() -> None:
    """Turn the engine off: persist the flag, cancel loops, detach scrapers."""
    db.set_running(False)
    for t in _tasks:
        t.cancel()
    # let cancellations settle
    for t in _tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _tasks.clear()
    try:
        await scraper.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        await logbus.card_status(
            running=False,
            healthy=len(db.list_accounts_by_status("active")),
            quarantined=len(db.list_accounts_by_status("limited")),
            dead=len(db.list_accounts_by_status("dead")),
            active_groups=db.count_groups("active"),
            pending_joins=db.count_pending_joins(),
            today=db.count_candidates_today(),
            join_delay=config.JOIN_DELAY_START,
        )
    except Exception:  # noqa: BLE001
        pass


async def resume_if_running() -> None:
    """On boot, if the persisted flag says we were working, start again."""
    if db.is_running():
        await start()


def _clear_finished() -> None:
    global _tasks
    _tasks = [t for t in _tasks if not t.done()]


def _loops_alive() -> bool:
    return any(not t.done() for t in _tasks)
