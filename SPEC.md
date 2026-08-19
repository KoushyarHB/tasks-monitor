# Build a Standalone "Plane Tasks Monitor" Telegram Bot

You are an expert backend engineer. Build a **self-contained, standalone Telegram bot** that watches a Plane (Plane CE) project and reports changes to a Telegram channel, plus an interactive card browser with inline keyboards. The bot must run as an independent service (own process, own codebase) — **not** inside any agent framework. No LLM, no AI runtime: pure Python + HTTP.

This spec is a complete replication brief. Follow it exactly. Where behavior is described, reproduce it faithfully; where an exact string/format is shown, use it verbatim.

---

## 1. Product Summary

Two capabilities in one bot:

1. **Watchdog** — polls the Plane REST API on an interval, diffs the latest state against a persisted snapshot, and posts **change reports** to a Telegram channel (silent when nothing changed).
2. **Interactive browser** — lets users browse tasks via a two-step inline-keyboard flow (assignee → state → filtered list, or state → assignee → filtered list), plus slash commands and a pinned commands menu in the channel.

The bot talks to **two external systems**:
- **Plane REST API** (project management) — read-only usage
- **Telegram Bot API** (messaging)

---

## 2. Tech Stack (choose or justify deviations)

- **Python 3.10+** (reference implementation is Python; you may use any language if you replicate ALL behavior, but Python is strongly preferred)
- **HTTP**: `httpx` or `requests` (synchronous is fine; asyncio optional)
- **No web framework required** — long-polling via `getUpdates` is the delivery mechanism
- **Dependencies kept minimal**: one HTTP client, stdlib `json`/`datetime`/`logging`
- **Persistence**: plain JSON files on disk (no database)
- **Deployment**: a `systemd` unit file (or equivalent) so the bot survives reboots; optional Dockerfile

---

## 3. System Architecture

```
┌───────────────────────┐        ┌────────────────────────────┐
│  Plane REST API       │        │  Telegram Bot API          │
│  (Plane CE instance)  │◄──────►│  (long-poll getUpdates)    │
└───────────┬───────────┘        └─────────────┬──────────────┘
            │ session-cookie auth              │ inline keyboards
┌───────────▼───────────┐        ┌─────────────▼──────────────┐
│  bot/                 │        │  bot/                      │
│  ├─ main.py           │        │  ├─ telegram_client.py     │
│  ├─ plane_client.py   │        │  ├─ callbacks.py           │
│  ├─ monitor.py        │        │  └─ messages.py            │
│  ├─ browser.py        │        │                           │
│  ├─ state.py          │        │                           │
│  └─ config.py         │        │                           │
└───────────────────────┘        └────────────────────────────┘
```

Two independent loops in one process:
1. **Poll loop** (watchdog): every N seconds, fetch issues → diff vs snapshot → post report if changed
2. **Update loop** (interactive): long-poll `getUpdates`, dispatch `message` and `callback_query` events

Run both as concurrent threads/tasks (or a single loop with both timers and the poll interleaved — your choice, but both MUST be live at the same time).

---

## 4. Configuration (all via environment variables)

Create a `.env.example` documenting every variable:

| Variable | Purpose |
|---|---|
| `PLANE_BASE_URL` | e.g. `https://plane.sabasystem.app` — the Plane instance origin (no trailing slash) |
| `PLANE_WORKSPACE` | workspace slug, e.g. `tms` |
| `PLANE_PROJECT_ID` | project UUID |
| `PLANE_CSRF_TOKEN` | login cookie value (see §5.1) |
| `PLANE_SESSION_ID` | login session cookie value (see §5.1) |
| `PLANE_USER_ID` | the bot owner's Plane user UUID (used for "my tasks" filtering) |
| `PLANE_FOCUS` | `mine` (default) or `all` — see §6.3 |
| `TG_BOT_TOKEN` | Telegram bot token from @BotFather |
| `TG_CHAT_ID` | channel/chat ID where reports + browsing happen, e.g. `-1004447454544` |
| `TG_PROXY` | optional SOCKS5/HTTP proxy for Telegram API, e.g. `socks5h://192.168.1.2:1088` (required in some networks) |
| `POLL_INTERVAL_SECONDS` | default `300` (5 min) |
| `STATE_FILE` | path to snapshot JSON, default `./state.json` |

All values may be empty strings; code must handle empty `TG_PROXY` gracefully (no proxy).

---

## 5. Plane API Integration

### 5.1 Authentication — CRITICAL

**Plane CE rejects API keys.** The instance under integration returns `401` for `Authorization: Bearer <key>`. The only working auth is **session-cookie auth**:

1. A human logs in to the Plane web UI (the instance uses Gitea SSO).
2. The browser ends up with two cookies: `csrftoken` and `sessionid` (exact names may vary — verify).
3. Those cookie values are provided via `PLANE_CSRF_TOKEN` / `PLANE_SESSION_ID`.
4. The bot sends them on every request as a `Cookie` header:
   ```
   Cookie: csrftoken=<PLANE_CSRF_TOKEN>; sessionid=<PLANE_SESSION_ID>
   ```
   Plus (required by Plane's CSRF middleware): `X-CSRFToken: <PLANE_CSRF_TOKEN>` header.

**Session expiry handling is MANDATORY:** if any API call returns `401`/`403`, the bot must:
- Log a clear error naming the expiry
- Optionally attempt a programmatic re-login (see below)
- **Post an alert message to the channel**: `⚠️ Plane session expired — please re-authenticate` (so humans know)
- Keep retrying on subsequent poll cycles (don't crash)

**Optional but recommended:** implement a `login()` that POSTs to the SSO flow so the session can be refreshed. If the instance's SSO is Gitea-based, the flow is: GET the Plane login page → follow redirect to Gitea → POST credentials → capture cookies → write them back to the env/config file. If this proves too brittle for the target instance, a documented manual-refresh procedure (update `.env`, restart) is acceptable — but the code MUST at least detect and loudly report expiry.

### 5.2 Core endpoints

All paths under `{PLANE_BASE_URL}/api/workspaces/{PLANE_WORKSPACE}`. The bot needs:

- **List issues**: `GET /projects/{PLANE_PROJECT_ID}/issues/`
  - Paginated: `?per_page=50&cursor=<page>` (or offset-based — inspect actual API; handle BOTH)
  - **KNOWN BUG TO HANDLE: the paginated API returns duplicate issues across pages.** You MUST deduplicate by issue `id` with a seen-set while paging.
  - Response payload: `{"results": [...issues...]}` (Plane CE v1.x). Handle `results` vs `results`+`next_cursor` vs flat list — be defensive.
- **List states**: `GET /projects/{PLANE_PROJECT_ID}/states/` → `{"results": [{id, name, group, ...}]}`
- **List members**: `GET /workspace-members/` or `GET /projects/{PLANE_PROJECT_ID}/members/` → `{"results": [{member: {id, display_name, first_name, ...}}]}` (shape varies by version — normalize into `{user_id: display_name}`)

### 5.3 Issue field quirks (learned the hard way — do not "fix" these)

- **State is `state_id`** (a UUID), NOT `state` / `state__group`. To render state names, build `{state_id: state_name}` from the states endpoint.
- **Assignees are `assignee_ids`** (array of user UUIDs), NOT `assignees` (an array of objects with `.id`). Normalize to strings before comparing.
- Useful per-issue fields: `id`, `sequence_id` (the short number shown as `[N]`), `name`, `state_id`, `assignee_ids`, `priority`, `created_by`, `created_at`, `updated_at`, `description_html`, `labels`, `target_date`.
- `priority` is a string like `urgent|high|medium|low|none`.
- `sequence_id` is what card buttons show (`Card 5`).

### 5.4 Rate limiting & failures

- Respect `Retry-After` on 429. Back off on 5xx (exponential, capped).
- Timeout every request (10s connect, 30s total).
- Never crash the poll loop on a transient failure — log, skip this cycle.

---

## 6. Watchdog Behavior (the "monitor")

### 6.1 Snapshot & diff

- Every `POLL_INTERVAL_SECONDS`, fetch ALL issues (deduped).
- Persist a snapshot JSON:
  ```json
  {
    "issues": { "<issue_id>": {"sequence_id": 5, "name": "...", "state_id": "...", "priority": "high", "assignee_ids": ["..."], "created_by": "...", "created_at": "...", "updated_at": "..."} },
    "_fetched_at": "2026-08-18T09:10:00Z"
  }
  ```
- On the **first run** (no snapshot file): baseline silently — DO NOT post "all cards are new". Store snapshot, no message. (Optional: log "baselined N issues" to stdout only.)
- On subsequent runs: compute per-issue changes between old and new snapshot. Classify each change:
  - **new** — issue id not in old snapshot
  - **state changed** — `state_id` differs (render old→new state names)
  - **priority changed**
  - **assignees changed** (list diff; render added/removed names)
  - **name/description changed**
  - **deleted** — id in old snapshot, missing in new (report `🗑 removed`)
- After a successful diff, write the new snapshot (atomic: write temp file then `os.replace`).

### 6.2 Report format (HTML, sent with `parse_mode=HTML`)

```
📋 <b>Plane Monitor</b> — 2026-08-18 09:10 UTC

🟦 <b>MY ASSIGNED CARDS</b>

🆕 <b>[132]</b> <code>Implement Road Loading backend</code>
   Status: Backlog · Priority: medium
   Assignees: arianmiramini1381
   Created by alighahremani at 2026-08-18 10:13

🔄 <b>[70]</b> <code>Show job number in file details</code>
   State: Todo → Done
   Priority: high → urgent
   Assignees: +feizyr, -arianmiramini1381
   Changed by <whoever> at <when>

⬜ <b>OTHER CARDS</b>
   [112] <code>Design Road Loading form</code> — Backlog (new)
   [97] <code>Add invoice creation</code> — Todo → Done

🔗 Open project
```

Rules:
- **`PLANE_FOCUS=mine`**: section 🟦 contains ONLY cards where the owner's `PLANE_USER_ID` is in `assignee_ids`; every OTHER-card change is condensed to a single concise line (or the ⬜ section is omitted entirely). The owner's cards always get full detail.
- **`PLANE_FOCUS=all`**: everything gets full detail.
- **HTML-escape all card names and member names** (`&`, `<`, `>`, `"`). Use `html.escape`.
- Timestamps in UTC, format `YYYY-MM-DD HH:MM` (or ISO — be consistent).
- If there are NO changes: print nothing, send nothing. **Silence is the correct output.** (Verifiable: run twice, second run must produce zero outbound messages.)
- **Chunking:** Telegram hard-limits messages at 4096 chars. If the report exceeds it, split into multiple sequential `sendMessage` calls on clean line boundaries. Only the LAST chunk carries the inline keyboard.
- Include one inline button per changed card: `[Button: Card <sequence_id>]` → URL to the issue: `{PLANE_BASE_URL}/{PLANE_WORKSPACE}/projects/{PLANE_PROJECT_ID}/issues/{issue_id}/`. Cap at 12 buttons per message row-group (Telegram allows max 100 buttons but UX degrades; if more changes than 12, show the first 12).
- End with a `🔗 Open project` button → project URL.

### 6.3 "My tasks" focus

`PLANE_USER_ID` must be matched **as a string** against `assignee_ids` (normalize both sides with `str()`). The user id in the real deployment is a UUID; never compare case-sensitively without normalizing to lowercase, since UUIDs may differ in case.

---

## 7. Interactive Browser (the "two-step flow")

### 7.1 Slash commands (register via `setMyCommands`)

```
task_by_assignee — Tasks by assignee (pick assignee → state)
task_by_state     — Tasks by state (pick state → assignee)
my_tasks          — My assigned tasks
```

Call `setMyCommands` at startup with exactly these three commands (+ descriptions). Telegram shows them when the user types `/` in the channel.

### 7.2 Inline-keyboard protocol (`callback_data` schema)

All bot-driven messages carry an inline keyboard whose `callback_data` follows this exact scheme (prefix `pt:` = "plane tasks"):

| Callback data | Meaning |
|---|---|
| `pt:start:assignee` | Show assignee buttons (first step of assignee-flow) |
| `pt:start:state` | Show state buttons (first step of state-flow) |
| `pt:pick:assignee:<slug>` | User picked assignee `<slug>` → show state buttons with counts |
| `pt:pick:state:<slug>` | User picked state `<slug>` → show assignee buttons with counts |
| `pt:run:a:<assignee_slug>:<state_slug>` | RUN query: assignee-first order |
| `pt:run:s:<state_slug>:<assignee_slug>` | RUN query: state-first order |
| `pt:my` | Show owner's cards |
| `pt:help` | Show commands help |

**IMPORTANT — do not "simplify" this:** the `a:`/`s:` marker inside `pt:run:` is REQUIRED because `run:<x>:<y>` is ambiguous (assignee-first vs state-first flows produce the same token shape with swapped semantics). Keep the marker.

Slugs: lowercase, spaces → `_` (e.g. assignee `Koushyar Heidari` → `koushyar_heidari`; state `Code Review` → `code_review`). The special slug `all` = "All". The special slug `unassigned` for assignees = no assignee.

### 7.3 Flows (two-step, exactly as specified)

**Flow A — `/task_by_assignee` or "👤 By Assignee" button:**
1. Bot posts: `👤 <b>Pick an assignee</b> (or All):` with one button per assignee + a `🌐 All` button at top
2. User taps an assignee → bot posts: `👤 <label> — now <b>pick a state</b> (or All):` with one button per state **that has at least one task for that assignee**, each labeled `State Name (count)` (e.g. `Backlog (10)`), + `🌐 All` at top
3. User taps a state → bot posts the task list (see §7.4)

**Flow B — `/task_by_state` or "🗂 By State" button:**
1. Bot posts: `🗂 <b>Pick a state</b> (or All):` with one button per state (with counts for ALL tasks in that state) + `🌐 All`
2. User taps a state → bot posts: `🗂 <label> — now <b>pick an assignee</b> (or All):` with one button per assignee **that has at least one task in that state**, labeled `Name (count)`, + `🌐 All`
3. User taps an assignee → bot posts the task list

**Mandatory UX rules:**
- **Counts on second-stage buttons** — only show options that have ≥1 matching task. Never present a dead-end (like "Code Review (0)"). If zero matches exist for a filter combination, show a single `(no tasks)` button (or a plain text notice) — never an empty keyboard.
- **`🌐 All` must be present at BOTH stages** of both flows.
- Every result message ends with a nav keyboard:
  ```
  [👤 By Assignee] [🗂 By State]
  [🟦 My Tasks] [❓ Commands]
  ```
- Every task row in results is preceded by `🟦 ` if the owner (`PLANE_USER_ID`) is an assignee of that card.

### 7.4 Result list format

```
🎯 <b>Tasks</b> — Assignee: <b>Koushyar Heidari</b> · State: <b>Backlog</b> (10)

  🟦<b>[45]</b> <code>Fix login redirect</code>
       Backlog · high · Koushyar Heidari, feizyr

  <b>[112]</b> <code>Design Road Loading form</code>
       Backlog · medium · feizyr

🔄 <i>Tap below to browse again</i>
[buttons: one per card → issue URL, labeled `Card <sequence_id>`]
[nav keyboard]
```

- Sorted by `sequence_id` ascending.
- `(N)` in the header = total match count (not just the displayed slice).
- Chunk at 4096 chars on line boundaries; buttons only on the last chunk.
- **Keyboard format pitfall (learned the hard way):** Telegram's `inline_keyboard` must be an **array of arrays** (rows of buttons). Card buttons must each be wrapped in their own row: `[[card1], [card2], ...] + nav_rows`. A flat list of buttons mixed with row-arrays produces API error `Bad Request: expected an Array of InlineKeyboardButton` — and if your code swallows the error silently, the user sees NOTHING. Log send failures loudly.

### 7.5 `pt:my` — My Tasks

Post `🟦 <b>My Tasks</b> (assigned to you)` + all cards where `PLANE_USER_ID ∈ assignee_ids`, sorted by `sequence_id`, same per-card format + buttons + nav. If none: `(no tasks assigned to you)`.

### 7.6 `pt:help` — commands menu

```
🤖 <b>Plane Monitor — Commands</b>

🔹 /task_by_assignee
   Pick an assignee → pick a state → task list

🔹 /task_by_state
   Pick a state → pick an assignee → task list

🔹 /my_tasks
   All cards assigned to you

🟦 marks your cards. Tap the buttons below to browse ⬇️
```
Plus nav keyboard `[👤 By Assignee] [🗂 By State]` / `[🟦 My Tasks]`.

### 7.7 Pinned commands menu (channel onboarding)

At startup (and idempotently on demand), post AND pin a menu message in the channel:

```
🤖 <b>Plane Monitor — Commands</b>

🔹 /task_by_assignee — by assignee, then state
🔹 /task_by_state — by state, then assignee
🔹 /my_tasks — your cards

Tap the buttons below to browse ⬇️
```
Keyboard:
```
[👤 By Assignee] [🗂 By State]
[🟦 My Tasks] [❓ Help]
```
Pinning: call `pinChatMessage` with the returned `message_id`. If already pinned (409/400), ignore the error gracefully.

### 7.8 Callback handling requirements

- Every `callback_query` MUST be answered with `answerCallbackQuery` (with a short toast like `✓ 10 tasks` or `OK`), even when the action succeeded. Unanswered callbacks leave a spinner on the user's device and, after ~60s, Telegram rejects the answer (`Bad Request: query is too old...`).
- Answer promptly (within ~1–2s of processing); if the handler does slow work (network fetch), answer first with `OK`/progress, then post the result.
- Callback data is capped at **64 bytes** by Telegram. Keep slugs short enough to fit (`pt:pick:assignee:arianmiramini1381` = 33 bytes — fine). Validate at build time: truncate or error if >64.
- Ignore callback queries whose data doesn't start with `pt:` (or unknown `pt:` tokens) — but still answer with a polite toast.

---

## 8. Telegram Delivery Client

- `sendMessage(chat_id, text, parse_mode="HTML", reply_markup=<json>, disable_web_page_preview=True)`
- `editMessageText` optional (nice-to-have: update the "pick" message in place instead of spamming new ones — but NEW messages are acceptable and simpler; choose one and be consistent)
- All requests through `TG_PROXY` if set (curl-style `--proxy`; in Python use `httpx.Proxy` / `requests` `proxies=`)
- Long-poll: `getUpdates?timeout=50&allowed_updates=["message","callback_query"]` (drop `channel_post` if you don't need it; but if the bot must receive slash commands typed in a channel, those arrive as `channel_post` updates with `chat.type=="channel"` — TEST which update type your channel delivers and handle it)
- Handle `409 Conflict` (another poller on same token — fatal, log clearly)
- Handle Telegram 400 errors on send (e.g. message too long) by chunking; 429 by `Retry-After`

---

## 9. State & Persistence

- One JSON file (`STATE_FILE`) for the watchdog snapshot.
- Atomic writes: write `state.json.tmp` → `os.replace()` to `state.json`.
- The interactive browser does NOT need persistence (stateless per callback; it refetches data on each tap).
- Optional: cache Plane data (issues/states/members) for 60s to avoid hammering the API when a user taps rapidly.

---

## 10. Reliability & Ops

- **Logging**: `logging` module, INFO level, to stdout (systemd/journald picks it up) AND a rotating file (`bot.log`, 5MB × 3).
- **Startup self-check**: verify Plane reachability (GET one issue, exit nonzero with clear error if auth fails — but don't crash the whole service; enter "retry" mode), verify Telegram `getMe`, `setMyCommands`, `pinChatMessage`.
- **Graceful shutdown**: SIGTERM/SIGINT → stop polling, flush state, exit 0.
- **systemd unit** (provide `deploy/saba-tasks-monitor.service`):
  ```ini
  [Unit]
  Description=Saba Tasks Monitor
  After=network-online.target

  [Service]
  WorkingDirectory=/opt/saba-tasks-monitor
  EnvironmentFile=/opt/saba-tasks-monitor/.env
  ExecStart=/opt/saba-tasks-monitor/.venv/bin/python -m bot.main
  Restart=always
  RestartSec=5

  [Install]
  WantedBy=multi-user.target
  ```
- **README.md** with: prerequisites, `cp .env.example .env` + fill-in table, `pip install -r requirements.txt`, `systemctl --user enable --now saba-tasks-monitor` (or root variant), verification checklist (below), troubleshooting (session expiry, proxy, 409).

---

## 11. Testing & Verification Checklist (MUST all pass before "done")

1. `python -m bot.main` starts, connects to Telegram, logs `✓ telegram connected`, `✓ plane connected (N issues)`.
2. First run baselines WITHOUT posting anything to the channel (verify via bot log / no new message in channel).
3. Manually change a card's state in Plane (or insert a test issue) → within one poll interval a report appears in the channel with the correct old→new transition and a `Card N` button linking to the issue.
4. Second run with no further changes → NO message (silence verified).
5. `/task_by_assignee` → assignee buttons appear; tapping one shows only states with counts; tapping a state shows the filtered list with card buttons.
6. `/task_by_state` → state-first flow works with counts and assignee filtering.
7. `🌐 All` works at both stages of both flows.
8. `/my_tasks` shows only cards assigned to `PLANE_USER_ID`, marked `🟦`.
9. `❓ Help` (pinned menu button) shows the commands help.
10. No API errors logged on a full happy-path pass; no unhandled exceptions.
11. `kill` the process → systemd restarts it; state survives (no duplicate "new card" flood on restart).
12. Session expiry simulation: set a bogus `PLANE_SESSION_ID` → bot logs auth failure and posts `⚠️` alert to channel, keeps retrying, doesn't crash.

---

## 12. Deliverables

```
saba-tasks-monitor/
├── bot/
│   ├── __init__.py
│   ├── main.py            # entrypoint: config, startup self-check, run both loops
│   ├── config.py          # env parsing, defaults
│   ├── plane_client.py    # auth headers, issues/states/members, dedup pagination, re-login hook
│   ├── monitor.py         # fetch→diff→report (watchdog)
│   ├── browser.py         # callback dispatch: start/pick/run/my/help + two-step flows
│   ├── messages.py        # report/list rendering, HTML escaping, chunking, keyboards
│   ├── telegram_client.py # sendMessage/answerCallbackQuery/pin/getUpdates + proxy
│   └── state.py           # snapshot load/save (atomic)
├── deploy/
│   └── saba-tasks-monitor.service
├── tests/
│   ├── test_diff.py       # unit tests for change classification (new/state/priority/assignees)
│   ├── test_dedup.py      # duplicate issues across pages collapse to unique set
│   ├── test_keyboards.py  # every keyboard is a valid array-of-arrays; callback_data ≤64 bytes
│   ├── test_chunking.py   # >4096-char report splits on line boundaries, buttons only last chunk
│   └── test_flows.py      # state-machine tests for pt:pick/pt:run token parsing (a:/s: order!)
├── .env.example
├── requirements.txt
├── README.md
└── LICENSE
```

Unit tests MUST cover at minimum: diff classification, pagination dedup, keyboard shape validation, chunking, and the `pt:run:a:`/`pt:run:s:` order disambiguation.

---

## 13. Pitfalls Checklist (encode these as tests/comments — they are real bugs that bit the original)

- [ ] `inline_keyboard` must be array-of-arrays; flat list + rows mixed = `expected an Array of InlineKeyboardButton`, and if errors are swallowed the user sees nothing
- [ ] Plane pagination returns duplicate issues — dedup by `id`
- [ ] Plane uses `state_id` / `assignee_ids` (not `state` / `assignees`)
- [ ] First run = baseline, never a flood of "new card" posts
- [ ] Callback data ≤ 64 bytes
- [ ] Always `answerCallbackQuery` promptly; stale query IDs get rejected
- [ ] HTML-escape all user-derived strings
- [ ] Proxy optional but must work when set (some networks require it for Telegram)
- [ ] Silent when clean (empty diff → zero messages) is a feature, not a bug
- [ ] Session cookie expiry must be detected, alerted, and retried — never silent-death

---

## 14. Final Instructions

- Write clean, well-commented, typed Python. Docstrings on every module and public function.
- Do NOT stub anything — every function must be implemented and exercised by the test suite.
- Run the tests; they must pass. Then run the self-check; it must connect to both APIs with the provided credentials.
- When finished, report: file tree, how to configure, how to deploy, verification results (paste the checklist results), and any deviations from this spec (with justification).
