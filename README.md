# Saba Tasks Monitor

A standalone Telegram bot that watches a **Plane** project and posts change reports to a Telegram channel — plus an interactive two-step card browser with inline keyboards.

- 🔄 **Watchdog** — polls Plane on an interval, diffs against a snapshot, posts change reports (silent when nothing changed)
- 🎛️ **Interactive browser** — `/task_by_assignee`, `/task_by_state`, `/my_tasks` + a pinned commands menu; pick assignee → state (or state → assignee) → filtered task list with per-card buttons
- 🔒 **Session-cookie auth** — works with Plane CE instances that reject API keys
- 🪶 **Zero LLM, zero database** — pure Python + httpx, one JSON state file

---

## Architecture

```
┌───────────────────┐        ┌─────────────────────────┐
│  Plane REST API   │        │  Telegram Bot API       │
└─────────┬─────────┘        └────────────┬────────────┘
          │ session-cookie auth           │ long-poll getUpdates
┌─────────▼───────────────────────────────▼─────────────┐
│  bot/                                                 │
│  ├─ main.py          entrypoint: both loops + dispatch│
│  ├─ config.py        env parsing (Settings)           │
│  ├─ plane_client.py  Plane API: issues/states/members │
│  ├─ monitor.py       watchdog diff engine + poll loop │
│  ├─ browser.py       pt: callback flows               │
│  ├─ messages.py      rendering, chunking, keyboards   │
│  ├─ telegram_client.py  Bot API delivery              │
│  ├─ state.py         atomic JSON snapshot             │
│  └─ models.py        Change dataclass                 │
└───────────────────────────────────────────────────────┘
```

Two independent loops run concurrently in one process:
1. **Poll loop** — every `POLL_INTERVAL_SECONDS`, fetch → diff → report → persist
2. **Update loop** — long-poll `getUpdates`, dispatch messages + callback queries

---

## Configuration

Copy `.env.example` → `.env` and fill in:

| Variable | Purpose |
|---|---|
| `PLANE_BASE_URL` | Plane instance origin (no trailing slash) |
| `PLANE_WORKSPACE` | workspace slug |
| `PLANE_PROJECT_ID` | project UUID |
| `PLANE_CSRF_TOKEN` | `csrftoken` cookie value (from logged-in browser) |
| `PLANE_SESSION_ID` | `sessionid` cookie value |
| `PLANE_USER_ID` | owner's Plane user UUID (for "my tasks") |
| `PLANE_FOCUS` | `mine` (only your cards trigger reports) or `all` |
| `TG_BOT_TOKEN` | bot token from @BotFather |
| `TG_CHAT_ID` | channel chat id (e.g. `-1004447454544`) |
| `TG_PROXY` | optional SOCKS5/HTTP proxy for Telegram (required in some networks) |
| `POLL_INTERVAL_SECONDS` | watchdog interval (default 300) |
| `STATE_FILE` | snapshot path (default `./state.json`) |

---

## Run (dev)

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in secrets
python -m bot.main
```

## Deploy (systemd)

```bash
sudo ./deploy/install.sh
journalctl -u saba-tasks-monitor -f
```

---

## Testing

```bash
pytest tests/ -v
```

89 tests cover: diff classification (incl. description changes + full new-card details), all three pagination styles (cursor / offset / next-URL) with dedup, keyboard shape validation (array-of-arrays + 64-byte callback cap), chunking at 4096, quote-safe HTML escaping, the `pt:run:a:`/`pt:run:s:` order disambiguation, strict pt: protocol rejection, poll-loop silent baseline, at-least-once delivery (state not advanced on send failure), 429 Retry-After + 5xx backoff, session-expiry propagation, slash-command routing (with no spurious callback answers), and dispatch routing with asserted sends.

---

## Interactive flows

**Flow A — by assignee** (`/task_by_assignee` or 👤 button):
1. Pick an assignee (buttons + 🌐 All)
2. Pick a state that has tasks for that assignee (counts shown, e.g. `Backlog (10)`)
3. Filtered task list with per-card buttons + nav

**Flow B — by state** (`/task_by_state` or 🗂 button):
1. Pick a state (buttons + 🌐 All)
2. Pick an assignee that has tasks in that state (counts shown)
3. Filtered task list

**`/my_tasks`** — all cards assigned to `PLANE_USER_ID`, marked 🟦.

**`❓ Help`** — the commands menu. The same menu is pinned to the channel at startup.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Plane auth rejected (401)` at startup | `PLANE_SESSION_ID`/`PLANE_CSRF_TOKEN` expired → re-login to Plane web UI, copy fresh cookies |
| `⚠️ Plane session expired` alert in channel | bot detected expiry mid-run; re-auth + restart |
| `Telegram API error 409` | another poller is using the same bot token (e.g. the old Hermes monitor) — stop it or use a fresh bot |
| Messages sent but no buttons render | never swallow `TelegramError` — the API error text (e.g. keyboard shape) tells you what's wrong |
| Nothing appears after a tap | check logs: the callback answer may have failed (`query is too old`) or the browser crashed — the code answers every callback and logs crashes |

---

## License

MIT
