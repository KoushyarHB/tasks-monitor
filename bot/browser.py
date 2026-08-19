"""Interactive card browser — pt: callback protocol, two-step flows, task lists."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from .config import Settings
from .messages import PRIORITY_DOT, build_keyboard, esc, nav_keyboard
from .plane_client import PlaneClient

logger = logging.getLogger(__name__)


# ── slug helpers ──────────────────────────────────────
def assignee_slug_of(label: str) -> str:
    return label.lower().replace(" ", "_")


def state_slug_of(name: str) -> str:
    return name.lower().replace(" ", "_")


def slug_to_label(slug: str, mapping: dict[str, str]) -> str:
    return mapping.get(slug, slug.replace("_", " ").title())


# ── callback parsing ──────────────────────────────────
@dataclass
class ParsedCallback:
    stage: str      # start | pick:assignee | pick:state | run | my | help
    payload: str    # "" | assignee|state | <slug> | "a:<a>:<s>" | "s:<s>:<a>"


def parse_callback(data: str) -> ParsedCallback | None:
    """Parse a pt: callback token (see SPEC §7.2).

    Returns None for ANY unknown/malformed token — unknown tokens must be
    rejected (spec §7.8), never silently accepted.
    """
    if not data.startswith("pt:"):
        return None
    rest = data[3:]
    head, _, tail = rest.partition(":")
    if head == "pick":
        # stage pick:<sub>:<slug> — sub must be assignee|state, slug required
        sub, _, pl = tail.partition(":")
        if sub not in ("assignee", "state") or not pl:
            return None
        return ParsedCallback(stage=f"pick:{sub}", payload=pl)
    if head == "run":
        # run:<order>:<a>:<s> — order must be a|s, and both values required
        order, _, rest_payload = tail.partition(":")
        if order not in ("a", "s") or not rest_payload:
            return None
        return ParsedCallback(stage="run", payload=tail)
    if head == "start":
        if tail not in ("assignee", "state"):
            return None
        return ParsedCallback(stage="start", payload=tail)
    if head in ("my", "help"):
        if tail:
            return None  # no payload allowed
        return ParsedCallback(stage=head, payload="")
    return None


def rows_from(items: list[tuple[str, str]], prefix: str) -> list[list[dict[str, str]]]:
    """Build keyboard rows (array-of-arrays) from (label, slug) items."""
    rows: list[list[dict[str, str]]] = []
    for label, slug in items:
        rows.append([{"text": label, "callback_data": f"{prefix}:{slug}"}])
    return rows


# ── browser ───────────────────────────────────────────
class Browser:
    """Stateless per-callback handler; refetches Plane data (60s cache)."""

    def __init__(self, client: PlaneClient, settings: Settings, send: Callable, answer: Callable):
        self.client = client
        self.settings = settings
        self.send = send          # send(text, keyboard) -> None
        self.answer = answer      # answer(query_id, toast) -> None
        self._cache: dict[str, Any] = {}

    # ── cached data ───────────────────────────────────
    def _data(self) -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]]]:
        import time
        now = time.time()
        if self._cache.get("ts", 0) < now - 60:
            states = self.client.get_states()
            members = self.client.get_members()
            issues = self.client.get_issues()
            self._cache = {"ts": now, "states": states, "members": members, "issues": issues}
        return self._cache["states"], self._cache["members"], self._cache["issues"]

    # ── dispatch ──────────────────────────────────────
    def handle(self, parsed: ParsedCallback, query_id: str = "") -> None:
        stage = parsed.stage
        if stage == "start":
            self._start(parsed.payload)
        elif stage == "pick:assignee":
            self._pick_assignee(parsed.payload)
        elif stage == "pick:state":
            self._pick_state(parsed.payload)
        elif stage == "run":
            self._run(parsed.payload)
        elif stage == "my":
            self._my()
        elif stage == "help":
            self._help()
        else:
            # unknown token — only a real callback can be answered
            if query_id:
                self.answer(query_id, "Unknown")
            return
        # Answer the Telegram callback ONLY when invoked from a button press
        # (slash commands / direct invocations pass no query_id — calling
        # answerCallbackQuery with an empty id is an API error).
        if query_id:
            self.answer(query_id, "OK")

    # ── stage 1: start ────────────────────────────────
    def _start(self, which: str) -> None:
        states, members, issues = self._data()
        if which == "assignee":
            items = self._assignee_items(states, members, issues)
            rows = rows_from(items, "pt:pick:assignee")
            rows.insert(0, [{"text": "🌐 All", "callback_data": "pt:pick:assignee:all"}])
            self.send("👤 <b>Pick an assignee</b> (or All):", rows)
        else:
            items = self._state_items(states, issues)
            rows = rows_from(items, "pt:pick:state")
            rows.insert(0, [{"text": "🌐 All", "callback_data": "pt:pick:state:all"}])
            self.send("🗂 <b>Pick a state</b> (or All):", rows)

    # ── stage 2a: assignee picked → states with counts ──
    def _pick_assignee(self, a_slug: str) -> None:
        states, members, issues = self._data()
        a_label = "All" if a_slug == "all" else slug_to_label(a_slug, {assignee_slug_of(l): l for l in members.values()})
        wanted = self._assignee_ids(a_slug)
        counts: dict[str, int] = {}
        for c in issues:
            as_ids = [str(a) for a in (c.get("assignee_ids") or [])]
            if a_slug == "all":
                ok = True
            elif a_slug == "unassigned":
                ok = not as_ids
            else:
                ok = any(i in (wanted or set()) for i in as_ids)
            if not ok:
                continue
            st = states.get(str(c.get("state_id", "")), c.get("state__group") or "?")
            counts[st] = counts.get(st, 0) + 1
        items = [(f"{name} ({counts.get(name, 0)})", state_slug_of(name))
                 for name in sorted(set(states.values()))
                 if counts.get(name, 0) > 0]
        if not items:
            items = [("(no tasks)", "all")]
        rows = rows_from(items, f"pt:run:a:{a_slug}")
        rows.insert(0, [{"text": "🌐 All", "callback_data": f"pt:run:a:{a_slug}:all"}])
        self.send(f"👤 {esc(a_label)} — now <b>pick a state</b> (or All):", rows)

    # ── stage 2b: state picked → assignees with counts ──
    def _pick_state(self, s_slug: str) -> None:
        states, members, issues = self._data()
        s_label = "All" if s_slug == "all" else slug_to_label(s_slug, {state_slug_of(n): n for n in set(states.values())})
        wanted_sids = None if s_slug == "all" else {sid for sid, name in states.items() if state_slug_of(name) == s_slug}
        counts: dict[str, int] = {}
        for c in issues:
            if wanted_sids is not None and str(c.get("state_id", "")) not in wanted_sids:
                continue
            for uid in [str(a) for a in (c.get("assignee_ids") or [])]:
                counts[uid] = counts.get(uid, 0) + 1
        items = []
        for label, slug in self._assignee_items(states, members, issues):
            if slug == "unassigned":
                n = sum(1 for c in issues
                        if not [str(a) for a in (c.get("assignee_ids") or [])]
                        and (wanted_sids is None or str(c.get("state_id", "")) in wanted_sids))
            else:
                uid_set = {uid for uid, lab in members.items() if assignee_slug_of(lab) == slug}
                n = sum(counts.get(u, 0) for u in uid_set)
            if n > 0:
                items.append((f"{label} ({n})", slug))
        if not items:
            items = [("(no tasks)", "all")]
        rows = rows_from(items, f"pt:run:s:{s_slug}")
        rows.insert(0, [{"text": "🌐 All", "callback_data": f"pt:run:s:{s_slug}:all"}])
        self.send(f"🗂 {esc(s_label)} — now <b>pick an assignee</b> (or All):", rows)

    # ── stage 3: run query ────────────────────────────
    def _run(self, payload: str) -> None:
        states, members, issues = self._data()
        order, _, rest = payload.partition(":")
        if order == "a":
            a_slug, s_slug = (rest.split(":", 1) + ["all"])[:2]
        else:
            s_slug, a_slug = (rest.split(":", 1) + ["all"])[:2]
        a_label = "All" if a_slug == "all" else slug_to_label(a_slug, {assignee_slug_of(l): l for l in members.values()})
        s_label = "All" if s_slug == "all" else slug_to_label(s_slug, {state_slug_of(n): n for n in set(states.values())})

        wanted = self._assignee_ids(a_slug)
        wanted_sids = None if s_slug == "all" else {sid for sid, name in states.items() if state_slug_of(name) == s_slug}

        matched = []
        for c in issues:
            as_ids = [str(a) for a in (c.get("assignee_ids") or [])]
            if a_slug == "all":
                ok_a = True
            elif a_slug == "unassigned":
                ok_a = not as_ids
            else:
                ok_a = any(i in wanted for i in as_ids)
            if wanted_sids is not None and str(c.get("state_id", "")) not in wanted_sids:
                ok_s = False
            else:
                ok_s = True
            if ok_a and ok_s:
                matched.append(c)

        matched.sort(key=lambda x: int(x.get("sequence_id") or 0))
        self._render_task_list(matched, f"Assignee: <b>{esc(a_label)}</b> · State: <b>{esc(s_label)}</b>")

    # ── my tasks ──────────────────────────────────────
    def _my(self) -> None:
        states, members, issues = self._data()
        me = self.settings.plane_user_id
        matched = [c for c in issues if me and me in [str(a) for a in (c.get("assignee_ids") or [])]]
        matched.sort(key=lambda x: int(x.get("sequence_id") or 0))
        self._render_task_list(matched, "🟦 <b>My Tasks</b> (assigned to you)", mine_only=True)

    # ── help ──────────────────────────────────────────
    def _help(self) -> None:
        text = (
            "🤖 <b>Plane Monitor — Commands</b>\n\n"
            "🔹 <b>/task_by_assignee</b>\n"
            "   Pick an assignee → pick a state → task list\n\n"
            "🔹 <b>/task_by_state</b>\n"
            "   Pick a state → pick an assignee → task list\n\n"
            "🔹 <b>/my_tasks</b>\n"
            "   All cards assigned to you\n\n"
            "🟦 marks your cards. Tap the buttons below to browse ⬇️"
        )
        kb = [
            [{"text": "👤 By Assignee", "callback_data": "pt:start:assignee"},
             {"text": "🗂 By State", "callback_data": "pt:start:state"}],
            [{"text": "🟦 My Tasks", "callback_data": "pt:my"}],
        ]
        self.send(text, kb)

    # ── rendering ─────────────────────────────────────
    def _render_task_list(self, cards: list[dict[str, Any]], header: str, mine_only: bool = False) -> None:
        states, members, _ = self._data()
        me = self.settings.plane_user_id
        lines = [f"🎯 <b>Tasks</b> — {header} ({len(cards)})", ""]
        buttons: list[list[dict[str, str]]] = []
        for c in cards[:15]:
            lines.extend(_card_block(c, states, members, me))
            lines.append("")
            url = (f"{self.settings.plane_base_url}/{self.settings.plane_workspace}"
                   f"/projects/{self.settings.plane_project_id}/issues/{c.get('id')}/")
            buttons.append([{"text": f"Card {c.get('sequence_id')}", "url": url}])
        if not cards:
            lines.append("  (no cards match)")
        lines.append("")
        lines.append("🔄 <i>Tap below to browse again</i>")
        text = "\n".join(lines)
        kb = buttons + nav_keyboard()
        self.send(text, kb)

    # ── helpers ───────────────────────────────────────
    def _assignee_items(self, states, members, issues) -> list[tuple[str, str]]:
        items = [(label, assignee_slug_of(label)) for label in members.values() if label]
        items.append(("Unassigned", "unassigned"))
        # dedup by slug
        seen = set()
        out = []
        for label, slug in items:
            if slug not in seen:
                seen.add(slug)
                out.append((label, slug))
        return sorted(out, key=lambda x: x[0].lower())

    def _state_items(self, states, issues) -> list[tuple[str, str]]:
        return [(name, state_slug_of(name)) for name in sorted(set(states.values()))]

    def _assignee_ids(self, a_slug: str) -> set[str] | None:
        """Resolve an assignee slug → set of user ids. None = all, empty = unassigned."""
        if a_slug == "all":
            return None
        if a_slug == "unassigned":
            return set()
        _, members, _ = self._data()
        return {uid for uid, label in members.items() if assignee_slug_of(label) == a_slug}


# ── card rendering (shared with the monitor's change report) ──
STATE_ICON = {
    "Backlog": "📋",
    "Todo": "📝",
    "In Progress": "🚧",
    "Done": "✅",
    "Cancelled": "🚫",
    "Reject": "⛔",
    "Test": "🧪",
    "Code Review": "👀",
}


def _card_block(c: dict[str, Any], states: dict[str, str], members: dict[str, str], me: str | None) -> list[str]:
    """Render one task card as a list of lines (title + meta), nice typography."""
    seq = c.get("sequence_id")
    name = c.get("name", "")
    st = states.get(str(c.get("state_id", "")), c.get("state__group") or "?")
    prio = c.get("priority") or "none"
    dot = PRIORITY_DOT.get(str(prio).lower(), "⚫")
    as_ids = [str(a) for a in (c.get("assignee_ids") or [])]
    a_names = ", ".join(members.get(i, i) for i in as_ids) or "Unassigned"
    mine = "🟦 " if me and me in as_ids else ""

    title = f"{mine}<b>[{seq}]</b> {esc(name)}"
    icon = STATE_ICON.get(st, "▪️")
    meta = f"      {icon} {esc(st)} · {dot} {esc(prio)} · 👤 {esc(a_names)}"
    return [title, meta]
