"""
config.py — central configuration for Target Scraper.
======================================================

Loads values from a local ``.env`` file (if present) and the process
environment. Nothing here connects to the network; this module only reads
settings and exposes safe defaults so the rest of the code can stay clean.
"""
from __future__ import annotations

import os

# --------------------------------------------------------------------------- #
# Load .env (best effort — never crash if the lib or file is missing).
# --------------------------------------------------------------------------- #
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:  # noqa: BLE001
    # Minimal fallback parser so the bot still boots without python-dotenv.
    try:
        _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(_env_path):
            with open(_env_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except Exception:  # noqa: BLE001
        pass


def _int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except Exception:  # noqa: BLE001
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, default)).strip())
    except Exception:  # noqa: BLE001
        return default


# --------------------------------------------------------------------------- #
# Credentials / identity.
# --------------------------------------------------------------------------- #
API_ID = _int("API_ID", 0)
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID = _int("OWNER_ID", 0)
LOG_GROUP_ID = _int("LOG_GROUP_ID", 0)
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "").strip()

# --------------------------------------------------------------------------- #
# Paths.
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "data.db")
PANEL_SESSION = os.path.join(DATA_DIR, "panel_bot")  # Telethon bot session file

# --------------------------------------------------------------------------- #
# Join throttle (AIMD). Safe-by-default; some are editable from the panel and
# then persisted in the settings table (DB overrides these defaults at runtime).
# --------------------------------------------------------------------------- #
JOIN_DELAY_START = _float("JOIN_DELAY_START", 90.0)   # seconds between joins
JOIN_DELAY_FLOOR = _float("JOIN_DELAY_FLOOR", 60.0)   # never go faster than this
JOIN_DELAY_CEIL = _float("JOIN_DELAY_CEIL", 600.0)    # never go slower than this
JOIN_DAILY_CAP = _int("JOIN_DAILY_CAP", 30)           # max joins per account/day
WARMUP_STAGES = [5, 10, 20, 30]                       # daily cap ramp for new acc

SUCCESS_STREAK_THRESHOLD = 5     # successful joins in a row before speeding up
DELAY_DECREASE_FACTOR = 0.8      # delay *= this on a good streak
FLOODWAIT_DELAY_FACTOR = 1.5     # delay grows by this after a FloodWait
FLOOD_BUFFER = 5                 # extra seconds added on top of a FloodWait
JOIN_JITTER_MIN = 0.8            # randomise each delay between
JOIN_JITTER_MAX = 1.2            #   these multipliers (look human)
PEERFLOOD_QUARANTINE = 3 * 3600  # hard-limit quarantine (seconds)

ACCOUNT_GROUP_LIMIT = 490        # treat account as "full" near Telegram's ~500

# --------------------------------------------------------------------------- #
# Timers / intervals (seconds).
# --------------------------------------------------------------------------- #
ONBOARD_TIMEOUT = 300            # abandon a half-finished add-account after 5 min
STATUS_CARD_INTERVAL = 1800      # how often #Scrape_Status is posted
HEALTH_INTERVAL = 300            # self-heal / session check cadence
DISCOVER_INTERVAL = 3600         # how often seed channels are re-read
SEED_REFRESH_HOUR = 3            # local hour for the daily heavy refresh + resets
JOINER_IDLE_SLEEP = 15           # sleep when the join queue is empty

# Telegram flood handling for the userbots (reading/joining).
TG_FLOOD_MAX_WAIT = 3600         # cap how long we ever auto-wait on FloodWait

# Candidate message text is truncated in cards/DB to keep things sane.
MESSAGE_MAX_LEN = 600


def missing() -> list:
    """Return a list of required settings that are not configured yet."""
    out = []
    if not API_ID:
        out.append("API_ID")
    if not API_HASH:
        out.append("API_HASH")
    if not BOT_TOKEN:
        out.append("BOT_TOKEN")
    if not OWNER_ID:
        out.append("OWNER_ID")
    if not LOG_GROUP_ID:
        out.append("LOG_GROUP_ID")
    return out
