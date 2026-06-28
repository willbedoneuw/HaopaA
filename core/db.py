"""
db.py — the one and only place that talks to SQLite.
====================================================

Everything the bot needs to survive a restart lives here (accounts, groups,
join queue, keywords, candidates, seed channels, settings). All state is
persisted so a reboot resumes exactly where it left off.

Concurrency note (the "many people writing one notebook" problem):
  * journal_mode=WAL  -> readers and writers don't block each other as much.
  * busy_timeout      -> if the file is briefly locked, wait instead of erroring.
  * a single shared connection guarded by one lock for *writes*, so writes are
    serialised and never collide.

The functions here are synchronous (sqlite3 is sync) but tiny and fast, so
calling them from async code is fine.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import datetime

import config

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Connection + schema.
# --------------------------------------------------------------------------- #
def init_db() -> None:
    """Open the connection (creating the data dir + file) and build the schema.
    Safe to call multiple times."""
    global _conn
    os.makedirs(config.DATA_DIR, exist_ok=True)
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    try:
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.execute("PRAGMA busy_timeout=5000;")
        _conn.execute("PRAGMA foreign_keys=ON;")
    except Exception:  # noqa: BLE001
        pass
    _create_schema()


def _create_schema() -> None:
    cur = _conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            phone           TEXT UNIQUE,
            session         TEXT,                 -- encrypted StringSession
            status          TEXT DEFAULT 'active',-- active/limited/full/dead
            is_discover     INTEGER DEFAULT 0,    -- 1 = reads seed channels
            joins_today     INTEGER DEFAULT 0,
            join_delay      REAL,
            success_streak  INTEGER DEFAULT 0,
            last_floodwait  INTEGER DEFAULT 0,
            group_count     INTEGER DEFAULT 0,
            warmup_stage    INTEGER DEFAULT 0,
            quarantine_until INTEGER DEFAULT 0,
            name            TEXT DEFAULT '',
            username        TEXT DEFAULT '',
            user_id         INTEGER,
            added_at        INTEGER
        );

        CREATE TABLE IF NOT EXISTS groups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            link        TEXT,
            tg_id       INTEGER UNIQUE,
            title       TEXT DEFAULT '',
            account_id  INTEGER,
            status      TEXT DEFAULT 'active',     -- active/dead
            joined_at   INTEGER
        );

        CREATE TABLE IF NOT EXISTS join_queue (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            link             TEXT UNIQUE,
            status           TEXT DEFAULT 'pending', -- pending/joined/failed
            assigned_account INTEGER,
            tries            INTEGER DEFAULT 0,
            created_at       INTEGER
        );

        CREATE TABLE IF NOT EXISTS keywords (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            word  TEXT UNIQUE
        );

        CREATE TABLE IF NOT EXISTS candidates (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_key     TEXT UNIQUE,             -- platform:user_id (dedup)
            username     TEXT DEFAULT '',
            name         TEXT DEFAULT '',
            group_id     INTEGER,
            group_title  TEXT DEFAULT '',
            message      TEXT DEFAULT '',
            matched_kw   TEXT DEFAULT '',
            account_id   INTEGER,
            account_phone TEXT DEFAULT '',
            msg_link     TEXT DEFAULT '',
            created_at   INTEGER
        );

        CREATE TABLE IF NOT EXISTS seed_channels (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ref       TEXT UNIQUE,                -- @username or t.me link
            title     TEXT DEFAULT '',
            added_at  INTEGER
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_candidates_created ON candidates(created_at);
        CREATE INDEX IF NOT EXISTS idx_groups_account ON groups(account_id);
        CREATE INDEX IF NOT EXISTS idx_jq_status ON join_queue(status);
        """
    )
    _conn.commit()


# --------------------------------------------------------------------------- #
# Low-level helpers.
# --------------------------------------------------------------------------- #
def _write(sql: str, params: tuple = ()):  # returns lastrowid
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.lastrowid


def _query_all(sql: str, params: tuple = ()) -> list:
    cur = _conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _query_one(sql: str, params: tuple = ()):
    cur = _conn.execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row else None


def _now() -> int:
    return int(time.time())


# --------------------------------------------------------------------------- #
# settings (key/value) — also used for the engine on/off flag.
# --------------------------------------------------------------------------- #
def get_setting(key: str, default=None):
    row = _query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value) -> None:
    _write(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def is_running() -> bool:
    return get_setting("engine_running", "0") == "1"


def set_running(flag: bool) -> None:
    set_setting("engine_running", "1" if flag else "0")


# --------------------------------------------------------------------------- #
# accounts.
# --------------------------------------------------------------------------- #
def upsert_account(phone: str, name: str, username: str, user_id, session_enc: str) -> None:
    """Insert a fresh account or update an existing one after (re)login."""
    existing = get_account(phone)
    if existing:
        _write(
            "UPDATE accounts SET name=?, username=?, user_id=?, session=?, "
            "status='active' WHERE phone=?",
            (name, username, user_id, session_enc, phone),
        )
    else:
        _write(
            "INSERT INTO accounts(phone, session, status, join_delay, warmup_stage, "
            "name, username, user_id, added_at) "
            "VALUES(?, ?, 'active', ?, 0, ?, ?, ?, ?)",
            (phone, session_enc, config.JOIN_DELAY_START, name, username, user_id, _now()),
        )


def set_account_session(phone: str, session_enc: str) -> None:
    _write("UPDATE accounts SET session=? WHERE phone=?", (session_enc, phone))


def get_account(phone: str):
    return _query_one("SELECT * FROM accounts WHERE phone=?", (phone,))


def get_account_by_id(aid: int):
    return _query_one("SELECT * FROM accounts WHERE id=?", (aid,))


def list_accounts() -> list:
    return _query_all("SELECT * FROM accounts ORDER BY id")


def list_accounts_by_status(status: str) -> list:
    return _query_all("SELECT * FROM accounts WHERE status=? ORDER BY id", (status,))


def list_discover_accounts() -> list:
    return _query_all(
        "SELECT * FROM accounts WHERE is_discover=1 AND status='active' ORDER BY id"
    )


def set_account_status(phone: str, status: str) -> None:
    _write("UPDATE accounts SET status=? WHERE phone=?", (status, phone))


def set_discover(phone: str, flag: bool) -> None:
    _write("UPDATE accounts SET is_discover=? WHERE phone=?", (1 if flag else 0, phone))


def delete_account(phone: str) -> None:
    _write("DELETE FROM accounts WHERE phone=?", (phone,))


def update_throttle_state(phone: str, *, join_delay=None, joins_today=None,
                          success_streak=None, last_floodwait=None,
                          warmup_stage=None, quarantine_until=None) -> None:
    """Patch only the fields that are provided (None = leave unchanged)."""
    sets, params = [], []
    for col, val in (
        ("join_delay", join_delay),
        ("joins_today", joins_today),
        ("success_streak", success_streak),
        ("last_floodwait", last_floodwait),
        ("warmup_stage", warmup_stage),
        ("quarantine_until", quarantine_until),
    ):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    if not sets:
        return
    params.append(phone)
    _write(f"UPDATE accounts SET {', '.join(sets)} WHERE phone=?", tuple(params))


def inc_joins_today(phone: str) -> None:
    _write("UPDATE accounts SET joins_today = joins_today + 1 WHERE phone=?", (phone,))


def reset_joins_today_all() -> None:
    _write("UPDATE accounts SET joins_today=0", ())


def recount_group_count(account_id: int) -> int:
    row = _query_one(
        "SELECT COUNT(*) AS c FROM groups WHERE account_id=? AND status='active'",
        (account_id,),
    )
    c = row["c"] if row else 0
    _write("UPDATE accounts SET group_count=? WHERE id=?", (c, account_id))
    return c


# --------------------------------------------------------------------------- #
# seed channels.
# --------------------------------------------------------------------------- #
def add_seed(ref: str, title: str = "") -> None:
    _write(
        "INSERT OR IGNORE INTO seed_channels(ref, title, added_at) VALUES(?, ?, ?)",
        (ref, title, _now()),
    )


def list_seeds() -> list:
    return _query_all("SELECT * FROM seed_channels ORDER BY id")


def remove_seed(ref: str) -> None:
    _write("DELETE FROM seed_channels WHERE ref=?", (ref,))


def remove_seed_by_id(sid: int) -> None:
    _write("DELETE FROM seed_channels WHERE id=?", (sid,))


# --------------------------------------------------------------------------- #
# join queue.
# --------------------------------------------------------------------------- #
def enqueue_link(link: str) -> bool:
    """Add a discovered link if it's not already queued or joined. Returns True
    if a NEW row was created."""
    if not link:
        return False
    # Already a known group? skip.
    if _query_one("SELECT 1 FROM groups WHERE link=?", (link,)):
        return False
    before = _query_one("SELECT 1 FROM join_queue WHERE link=?", (link,))
    if before:
        return False
    _write(
        "INSERT OR IGNORE INTO join_queue(link, status, created_at) VALUES(?, 'pending', ?)",
        (link, _now()),
    )
    return True


def next_pending_join():
    return _query_one(
        "SELECT * FROM join_queue WHERE status='pending' ORDER BY id LIMIT 1"
    )


def set_join_status(qid: int, status: str, account_id=None) -> None:
    if account_id is not None:
        _write(
            "UPDATE join_queue SET status=?, assigned_account=? WHERE id=?",
            (status, account_id, qid),
        )
    else:
        _write("UPDATE join_queue SET status=? WHERE id=?", (status, qid))


def inc_join_tries(qid: int) -> None:
    _write("UPDATE join_queue SET tries = tries + 1 WHERE id=?", (qid,))


def count_pending_joins() -> int:
    row = _query_one("SELECT COUNT(*) AS c FROM join_queue WHERE status='pending'")
    return row["c"] if row else 0


def clear_join_queue() -> int:
    """Mark all not-yet-joined items (pending + failed) as 'skipped'. They
    leave the active queue AND are never re-added by discovery again (the
    enqueue check blocks any link already in the table). This is what the user
    wants: clearing that does NOT cause the same old links to be re-requested.
    Returns how many were affected."""
    row = _query_one(
        "SELECT COUNT(*) AS c FROM join_queue WHERE status IN ('pending','failed')")
    n = row["c"] if row else 0
    _write("UPDATE join_queue SET status='skipped' "
           "WHERE status IN ('pending','failed')", ())
    return n


def requeue_account_pending(account_id: int) -> int:
    """Send an account's *pending* (not-yet-joined) queue items back to the
    unassigned pool so another account can take them. Returns how many."""
    rows = _query_all(
        "SELECT id FROM join_queue WHERE assigned_account=? AND status='pending'",
        (account_id,),
    )
    _write(
        "UPDATE join_queue SET assigned_account=NULL WHERE assigned_account=? AND status='pending'",
        (account_id,),
    )
    return len(rows)


# --------------------------------------------------------------------------- #
# groups.
# --------------------------------------------------------------------------- #
def add_group(link: str, tg_id, title: str, account_id: int) -> None:
    _write(
        "INSERT INTO groups(link, tg_id, title, account_id, status, joined_at) "
        "VALUES(?, ?, ?, ?, 'active', ?) "
        "ON CONFLICT(tg_id) DO UPDATE SET account_id=excluded.account_id, "
        "status='active', title=excluded.title",
        (link, tg_id, title, account_id, _now()),
    )


def get_group_by_tgid(tg_id):
    return _query_one("SELECT * FROM groups WHERE tg_id=?", (tg_id,))


def list_groups(status: str | None = None) -> list:
    if status:
        return _query_all("SELECT * FROM groups WHERE status=? ORDER BY id", (status,))
    return _query_all("SELECT * FROM groups ORDER BY id")


def set_group_status(gid: int, status: str) -> None:
    _write("UPDATE groups SET status=? WHERE id=?", (status, gid))


def count_groups(status: str | None = None) -> int:
    if status:
        row = _query_one("SELECT COUNT(*) AS c FROM groups WHERE status=?", (status,))
    else:
        row = _query_one("SELECT COUNT(*) AS c FROM groups")
    return row["c"] if row else 0


def reassign_account_groups(account_id: int) -> int:
    """A dead/full account loses its groups: mark them pending re-join by
    putting their links back in the queue and detaching them. Returns count."""
    rows = _query_all(
        "SELECT * FROM groups WHERE account_id=? AND status='active'", (account_id,)
    )
    for g in rows:
        if g.get("link"):
            _write(
                "INSERT OR IGNORE INTO join_queue(link, status, created_at) "
                "VALUES(?, 'pending', ?)",
                (g["link"], _now()),
            )
        _write("UPDATE groups SET status='dead' WHERE id=?", (g["id"],))
    return len(rows)


# --------------------------------------------------------------------------- #
# keywords.
# --------------------------------------------------------------------------- #
def add_keyword(word: str) -> None:
    word = (word or "").strip()
    if word:
        _write("INSERT OR IGNORE INTO keywords(word) VALUES(?)", (word,))


def remove_keyword(word: str) -> None:
    _write("DELETE FROM keywords WHERE word=?", (word,))


def clear_keywords() -> None:
    _write("DELETE FROM keywords", ())


def list_keywords() -> list:
    return [r["word"] for r in _query_all("SELECT word FROM keywords ORDER BY id")]


def list_keywords_rows() -> list:
    return _query_all("SELECT id, word FROM keywords ORDER BY id")


def remove_keyword_by_id(kid: int) -> None:
    _write("DELETE FROM keywords WHERE id=?", (kid,))


# --------------------------------------------------------------------------- #
# candidates.
# --------------------------------------------------------------------------- #
def add_candidate(user_key: str, username: str, name: str, group_id, group_title: str,
                  message: str, matched_kw: str, account_id, account_phone: str,
                  msg_link: str) -> int | None:
    """Atomically insert a candidate. Returns the new row id, or None if this
    user_key already existed (deduped => no duplicate log)."""
    with _lock:
        cur = _conn.execute("SELECT 1 FROM candidates WHERE user_key=?", (user_key,))
        if cur.fetchone():
            return None
        cur = _conn.execute(
            "INSERT OR IGNORE INTO candidates(user_key, username, name, group_id, "
            "group_title, message, matched_kw, account_id, account_phone, msg_link, "
            "created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_key, username, name, group_id, group_title, message, matched_kw,
             account_id, account_phone, msg_link, _now()),
        )
        _conn.commit()
        return cur.lastrowid if cur.rowcount else None


def _start_of_today() -> int:
    now = datetime.datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def _start_of_week() -> int:
    now = datetime.datetime.now()
    start = (now - datetime.timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def count_candidates_total() -> int:
    row = _query_one("SELECT COUNT(*) AS c FROM candidates")
    return row["c"] if row else 0


def count_candidates_today() -> int:
    row = _query_one("SELECT COUNT(*) AS c FROM candidates WHERE created_at>=?",
                     (_start_of_today(),))
    return row["c"] if row else 0


def count_candidates_week() -> int:
    row = _query_one("SELECT COUNT(*) AS c FROM candidates WHERE created_at>=?",
                     (_start_of_week(),))
    return row["c"] if row else 0
