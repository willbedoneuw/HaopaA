# Target Scraper

A Telegram-only **userbot scraper** that collects *raw candidate data*. It joins
groups discovered from your seed channels and, in real time, logs every message
whose author matches one of your keywords — so you can do your own analysis
later. It does **not** send messages, DM anyone, or do any scoring/AI.

> Clean, multi-file layout so a Rubika adapter can be added later without
> touching the core.

## What it does

- **Accounts** — add a Telegram account from the panel (phone → code → optional
  2FA). The session is stored encrypted in the DB and also posted to the log
  group (`#Account_Added`) so you can move the account to another server with no
  re-login.
- **Discovery** — accounts you mark as *discover* read your seed channels and
  extract group/invite links from message content (never bios).
- **Safe joining** — a single throttled loop joins queued links with an adaptive
  (AIMD) delay, per-account daily cap and warm-up ramp, so accounts survive.
- **Real-time scraping** — each account gets one `NewMessage` handler with
  `catch_up`, so new messages arrive instantly and nothing is missed after a
  restart. A fast keyword check (OR logic, one keyword is enough) flags matches.
- **Dedup** — each person is logged once (`user_key = tg:<user_id>`, atomic).
- **Resilience** — restart-safe (all state in SQLite), self-healing accounts,
  group reassignment on dead sessions, and a clean `#Error` card for every
  failure. The bot never crashes.

A join-rate-limited account **keeps scraping** the groups it is already in; only
joining pauses.

## Log cards (to the log group)

`#Account_Added` · `#Candidate` · `#Join_Log` · `#Account_Limited` ·
`#Group_Reassigned` · `#Scrape_Status` · `#Error`

## Project layout

```
main.py            entry point (restart-safe boot)
config.py          reads .env
core/              db, crypto, logbus, matcher, throttle, health, scheduler, engine
platforms/telegram/ client (login/session), discover, joiner, scraper
panel/             Bot-API panel + menus
data/              data.db + bot session  (gitignored)
```

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill it in
python main.py            # open the bot and send /start
```

Required `.env` values: `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`,
`LOG_GROUP_ID` (set `ENCRYPTION_KEY` too for production).

## Notes

- Sending the collected data to your central log group is intentional.
- Telethon specifics (login, joining, real-time updates) must be exercised on a
  real device/server; the dev sandbox has no network to Telegram.
