# Saba Tasks Monitor — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a standalone Telegram bot that watches a Plane project (watchdog + interactive card browser) per `SPEC.md`, as a new independent repo.

**Architecture:** Two concurrent loops in one Python process — (1) a poll loop that fetches Plane issues, diffs against a persisted JSON snapshot, and posts change reports to a Telegram channel (silent when clean); (2) a long-poll `getUpdates` loop that dispatches slash commands and inline-keyboard callbacks (`pt:` protocol) for the two-step browse flow. Plane auth via session cookies (API keys are rejected); Telegram delivery via Bot API with HTML + inline keyboards, chunked at 4096 chars.

**Tech Stack:** Python 3.10+, httpx, stdlib json/logging/datetime, pytest, plain JSON state file, systemd unit.

**Repo:** `~/tasks-monitor` (github.com/KoushyarHB/tasks-monitor) — currently empty, `SPEC.md` copied in.

---

## Phase 0 — Bootstrap (no code)

### Task 0: Initialize repo skeleton

**Objective:** Lay out the repository structure per SPEC §12 with a passing baseline.

**Files:**
- Create: `~/tasks-monitor/bot/__init__.py` (empty)
- Create: `~/tasks-monitor/requirements.txt`
- Create: `~/tasks-monitor/.env.example`
- Create: `~/tasks-monitor/.gitignore`
- Create: `~/tasks-monitor/README.md` (stub)
- Create: `~/tasks-monitor/tests/__init__.py` (empty)

**Step 1:** Create files:

`requirements.txt`:
```
httpx>=0.27
pytest>=8.0
```

`.gitignore`:
```
.env
*.log
__pycache__/
*.pyc
.venv/
state.json
bot.log
.pytest_cache/
```

`.env.example` (documented in SPEC §4):
```
PLANE_BASE_URL=https://plane.sabasystem.app
PLANE_WORKSPACE=tms
PLANE_PROJECT_ID=
PLANE_CSRF_TOKEN=
PLANE_SESSION_ID=
PLANE_USER_ID=
PLANE_FOCUS=mine
TG_BOT_TOKEN=
TG_CHAT_ID=-1004447454544
TG_PROXY=socks5h://192.168.1.2:1088
POLL_INTERVAL_SECONDS=300
STATE_FILE=./state.json
```

**Step 2:** Verify: `cd ~/tasks-monitor && python3 -c "import bot; print('ok')"` → prints `ok`. `pytest` → collects 0 tests, exit 0.

**Step 3:** Commit: `git add -A && git commit -m "chore: repo skeleton"`

---

## Phase 1 — Core building blocks (pure, testable)

### Task 1: config.py — env parsing

**Objective:** Parse and validate all env vars with defaults.

**Files:**
- Create: `bot/config.py`
- Test: `tests/test_config.py`

**Step 1: Write failing test** — asserts defaults for empty env, type coercion for `POLL_INTERVAL_SECONDS`, `PLANE_FOCUS` normalized to `mine|all`.

**Step 2:** Run `pytest tests/test_config.py -v` → FAIL.

**Step 3: Implement** — a `Settings` dataclass built from `os.environ`, with `.env`-file loading via a tiny parser (no python-dotenv dependency — or add `python-dotenv` if preferred; keep deps minimal).

**Step 4:** Run test → PASS.

**Step 5:** Commit: `feat: config parsing`

### Task 2: state.py — snapshot persistence

**Objective:** Atomic JSON snapshot load/save for the watchdog.

**Files:**
- Create: `bot/state.py`
- Test: `tests/test_state.py`

**Step 1: Failing test** — `save_state()` writes valid JSON; load returns identical dict; `load_state()` on missing file returns `None`; atomicity: write `.tmp` then `os.replace` (assert no `.tmp` left, file readable mid-write).

**Step 2:** Run → FAIL.

**Step 3: Implement** — `save_state(path, data)`, `load_state(path)` with atomic replace.

**Step 4:** Run → PASS.

**Step 5:** Commit: `feat: atomic snapshot state`

### Task 3: plane_client.py — auth headers + session expiry detection

**Objective:** Cookie-auth HTTP client for the Plane API with explicit 401/403 detection.

**Files:**
- Create: `bot/plane_client.py`
- Test: `tests/test_plane_client.py`

**Step 1: Failing test** — header construction includes `Cookie: csrftoken=...; sessionid=...` and `X-CSRFToken`; a `PlaneAuthError` is raised when response is 401/403; a `PlaneAuthError` carries the status code.

**Step 2:** Run → FAIL.

**Step 3: Implement** — `PlaneClient(base_url, workspace, csrf, session)` with `_headers()`, `_request(method, path)` raising `PlaneAuthError` on 401/403, `PlaneApiError` otherwise; httpx timeout 10/30.

**Step 4:** Run → PASS.

**Step 5:** Commit: `feat: plane auth client`

### Task 4: plane_client.py — issue/state/member fetching with dedup

**Objective:** Fetch issues (paginated, deduped), states, members; normalize to plain dicts.

**Files:**
- Modify: `bot/plane_client.py`
- Test: `tests/test_plane_client.py`

**Step 1: Failing test** — given a fake transport returning two pages with an overlapping issue, `get_issues()` returns the unique set (no duplicates, count correct); `get_states()` returns `{state_id: name}`; `get_members()` returns `{user_id: display_name}`; handles `{"results": [...]}` envelope AND flat-list response defensively.

**Step 2:** Run → FAIL.

**Step 3: Implement** — `get_issues()` loops pages with a seen-set on `issue["id"]`; `get_states()`; `get_members()` handling `member` nesting (`m["member"]["id"]`, `m["member"]["display_name"]`) and fallback fields.

**Step 4:** Run → PASS.

**Step 5:** Commit: `feat: plane issue fetch with pagination dedup`

---

## Phase 2 — Watchdog (monitor)

### Task 5: monitor.py — diff engine

**Objective:** Pure function: old snapshot + new issues + member/state maps → classified changes list.

**Files:**
- Create: `bot/monitor.py`
- Test: `tests/test_diff.py`

**Step 1: Failing test** — covers: new issue (no `old`), state changed (renders old→new names), priority changed, assignee added/removed (renders names with `+`/`-`), name changed, deleted (`🗑`), identical issue → no change, and issue with same fields → no entry.

**Step 2:** Run → FAIL.

**Step 3: Implement** — `diff_issues(old: dict|None, new_issues: list[dict], states: dict, members: dict, me: str|None) -> list[Change]` where `Change` is a dataclass with `issue_id, sequence_id, name, kind (new|state|priority|assignees|name|deleted), old, new, is_mine: bool`.

**Step 4:** Run → PASS.

**Step 5:** Commit: `feat: watchdog diff engine`

### Task 6: messages.py — report rendering + chunking + keyboards

**Objective:** Render the change report HTML exactly per SPEC §6.2; chunk at 4096; card buttons; silent when no changes.

**Files:**
- Create: `bot/messages.py`
- Test: `tests/test_chunking.py`, `tests/test_keyboards.py`

**Step 1: Failing test** —
- `build_report(changes, focus="mine")` → HTML contains `📋 <b>Plane Monitor</b>`, `🟦 MY ASSIGNED CARDS` only when a mine-change exists, `⬜ OTHER CARDS` section when focus=all or non-mine changes exist, html-escaped names, `🔗 Open project` button in keyboard.
- Empty changes → returns `None` (silent).
- `chunk_text(text, 4096)` splits on line boundaries, ≤4096 each, preserves all content.
- `build_keyboard(buttons)` → every top-level element is a list (array-of-arrays); each button ≤64-byte callback_data; card buttons each wrapped in own row.

**Step 2:** Run → FAIL.

**Step 3: Implement** — `html_escape`, `chunk_text`, `build_report`, `build_keyboard`, `card_button(seq, url)`.

**Step 4:** Run → PASS.

**Step 5:** Commit: `feat: report rendering + chunking`

### Task 7: telegram_client.py — Bot API client

**Objective:** sendMessage (HTML, keyboards), answerCallbackQuery, pinChatMessage, getUpdates long-poll, proxy support, 429 Retry-After handling.

**Files:**
- Create: `bot/telegram_client.py`
- Test: `tests/test_telegram_client.py`

**Step 1: Failing test** — with a fake httpx transport: `send_message` sends `parse_mode=HTML` + `reply_markup` JSON; `answer_callback` posts `callback_query_id`; `pin_message` posts `message_id`; proxy is included in transport when `TG_PROXY` set and omitted when empty; 429 raises retryable error carrying Retry-After.

**Step 2:** Run → FAIL.

**Step 3: Implement** — `TelegramClient(token, proxy, chat_id)` with `send_message`, `send_chunked(text, buttons)` (chunks then posts), `answer_callback`, `pin_message`, `get_updates`, `set_my_commands`; non-ok responses raise `TelegramError` with API description (NEVER swallow — the "nothing appears" bug).

**Step 4:** Run → PASS.

**Step 5:** Commit: `feat: telegram delivery client`

### Task 8: monitor.py — poll loop orchestration

**Objective:** Wire fetch → diff → report → state-save; baseline silently on first run; session-expiry alert; keep-alive on transient errors.

**Files:**
- Modify: `bot/monitor.py`
- Test: `tests/test_diff.py` (integration-ish)

**Step 1: Failing test** — with fake client+state: first run saves snapshot, posts nothing; second run with a changed issue posts exactly one report with that change; `PLANE_FOCUS=mine` omits other-card details; `PlaneAuthError` triggers an alert message and no crash.

**Step 2:** Run → FAIL.

**Step 3: Implement** — `run_poll_once(client, tg, settings, state_path) -> str|None` returns the report (or None) and persists state; `PollLoop` wrapper with try/except for `PlaneAuthError` (alert + skip), `PlaneApiError` (log + skip).

**Step 4:** Run → PASS.

**Step 5:** Commit: `feat: poll loop with silent baseline`

---

## Phase 3 — Interactive browser

### Task 9: browser.py — callback protocol parsing

**Objective:** Parse `pt:` callback data per SPEC §7.2 with the `a:`/`s:` order marker; reject unknown tokens.

**Files:**
- Create: `bot/browser.py`
- Test: `tests/test_flows.py`

**Step 1: Failing test** — parses `pt:start:assignee` → stage `start`, payload `assignee`; `pt:pick:assignee:feizyr` → `pick:assignee`, `feizyr`; `pt:run:a:koushyar_heidari:backlog` → assignee=`koushyar_heidari`, state=`backlog`; `pt:run:s:backlog:feizyr` → state=`backlog`, assignee=`feizyr`; `pt:my`, `pt:help`; unknown `pt:x` → `None`. Also: slugify functions (`assignee_slug_of`, `state_slug_of`), `unassigned` sentinel.

**Step 2:** Run → FAIL.

**Step 3: Implement** — `parse_callback(data) -> ParsedCallback|None`, `slugify`, `rows_from(items, prefix)` building array-of-arrays.

**Step 4:** Run → PASS.

**Step 5:** Commit: `feat: callback protocol parser`

### Task 10: browser.py — stage handlers (start/pick with counts)

**Objective:** Implement `start` and `pick` stages with counts and dead-end prevention (only show options with ≥1 task).

**Files:**
- Modify: `bot/browser.py`
- Test: `tests/test_flows.py`

**Step 1: Failing test** — fake data with 2 assignees, 3 states, only 2 states have tasks for assignee X: `handle_start("assignee")` builds assignee buttons; `handle_pick("assignee", X)` builds state buttons containing ONLY the 2 non-empty states, labels `Name (count)`, plus `🌐 All` at top; empty → single `(no tasks)` button; counts correct.

**Step 2:** Run → FAIL.

**Step 3: Implement** — `handle_start(chat, kind)`, `handle_pick(chat, kind, slug)` using `get_issues`/`get_states`/`get_members` with 60s cache.

**Step 4:** Run → PASS.

**Step 5:** Commit: `feat: two-step pick with counts`

### Task 11: browser.py — run/my/help handlers + result list

**Objective:** Implement final task-list rendering (SPEC §7.4), `pt:my`, `pt:help`, nav keyboards on every result.

**Files:**
- Modify: `bot/browser.py`
- Test: `tests/test_flows.py`, `tests/test_chunking.py`

**Step 1: Failing test** — run with assignee+state filters returns only matching cards, sorted by sequence_id, header shows `(N)` total, `🟦` marker for mine, card buttons each in own row + nav rows; `handle_my` filters to `PLANE_USER_ID`; `handle_help` returns the exact help text; chunked variant keeps buttons only on last chunk.

**Step 2:** Run → FAIL.

**Step 3: Implement** — `handle_run(chat, order, a_slug, s_slug)`, `handle_my(chat)`, `handle_help(chat)`; refactor shared `render_task_list(cards, ...)`.

**Step 4:** Run → PASS.

**Step 5:** Commit: `feat: task list + my + help`

### Task 12: update loop — getUpdates dispatch + slash commands + pinned menu

**Objective:** Long-poll loop; route messages (slash commands, channel posts) and callback queries to browser handlers; `setMyCommands` + pinned menu at startup; answer every callback promptly.

**Files:**
- Create: `bot/main.py`
- Test: `tests/test_flows.py` (dispatch routing with fake updates)

**Step 1: Failing test** — dispatch: a `message` with text `/task_by_assignee` → browser start-assignee flow posts buttons; a `callback_query` with `pt:run:a:...` → run flow posts list AND `answerCallbackQuery` called; unknown callback → answered with toast, no crash; `channel_post` type handled as message.

**Step 2:** Run → FAIL.

**Step 3: Implement** — `dispatch_update(update, ctx)` in main; `run_update_loop()`; startup sequence: `getMe` → `setMyCommands` (3 commands per SPEC §7.1) → `pinChatMessage` (tolerate 409 already-pinned) → start both loops.

**Step 4:** Run → PASS.

**Step 5:** Commit: `feat: update loop + slash commands + pinned menu`

---

## Phase 4 — Ops, docs, verification

### Task 13: main.py — entrypoint, both loops, graceful shutdown

**Objective:** `python -m bot.main` runs both loops concurrently (threads or asyncio), SIGTERM/SIGINT clean shutdown, startup self-check (Plane reachable + Telegram reachable, nonzero exit with clear message if fatal).

**Files:**
- Modify: `bot/main.py`
- Test: manual smoke (SPEC §11 checklist items 1–2, 11)

**Step 1:** Implement `main()`; self-check: GET one issue (if `PlaneAuthError` → log + exit 1); `getMe` (fail → exit 1); then start poll loop + update loop.

**Step 2:** Verify: `cd ~/tasks-monitor && python -m bot.main` with a bogus `.env` → exits nonzero with clear auth error. With a fake transport in tests, both loops tick.

**Step 3:** Commit: `feat: entrypoint + graceful shutdown`

### Task 14: Deployment artifacts

**Objective:** systemd unit + README + final polish per SPEC §10, §12.

**Files:**
- Create: `deploy/saba-tasks-monitor.service`
- Create: `deploy/install.sh` (venv create, pip install, systemctl enable)
- Modify: `README.md` (full: config table, deploy steps, troubleshooting: session expiry / proxy / 409)

**Step 1:** Write files per SPEC §10 template.

**Step 2:** Verify README commands are copy-pasteable from a clean shell (venv + install + enable).

**Step 3:** Commit: `docs: deployment + README`

### Task 15: Full verification against live systems

**Objective:** Run SPEC §11 checklist 1–12 with real credentials provided by the user (Plane cookies + bot token + channel ID).

**Files:** none (verification only)

**Step 1:** Configure real `.env` (user-provided secrets, never committed).

**Step 2:** Execute checklist:
1. Startup logs ✓ connected
2. First run baselines silently (check channel + log)
3. Manual Plane change → report within interval
4. No-change second run → silence
5–9. All five browser flows via channel buttons
10. No API errors in log
11. `kill` → systemd restart → no duplicate flood (state survives)
12. Bogus session → `⚠️` alert + retry, no crash

**Step 3:** Fix any failures found; re-run.

**Step 4:** Commit any fixes: `fix: verification findings`

### Task 16: Final commit + push

**Objective:** Ship.

**Step 1:** `git add -A && git commit -m "feat: saba tasks monitor v1.0"` and `git push origin main`.

**Step 2:** Verify: `git ls-remote origin` shows the commit; repo has all files from SPEC §12 tree.

---

## Risks / Open Questions

1. **Plane API shapes vary by version** — the client is defensive (both `results` envelope and flat list, member nesting variants). Real-device verification (Task 15) resolves the actual shapes.
2. **Session refresh automation** (SPEC §5.1 optional) — deferred: first version detects + alerts + manual refresh; automate only if the SSO flow proves tractable against the live instance.
3. **Channel message routing** — whether slash commands arrive as `message` or `channel_post` varies; dispatch handles both (Task 12 test).
4. **`TG_CHAT_ID`** is a channel; inline keyboards in channels require the bot to be channel admin — verify in Task 15.
5. **No database** — single JSON state file; two concurrent writes impossible by design (single poll loop), atomic replace prevents corruption.

## Test Command Reference

```bash
cd ~/tasks-monitor
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest tests/ -v            # all unit tests
pytest tests/test_diff.py -v
pytest tests/test_flows.py -v
python -m bot.main          # smoke (with .env)
```
