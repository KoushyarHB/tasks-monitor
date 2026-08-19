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

    Extended protocol:
      pt:start:assignee|state
      pt:pick:assignee:<slug> | pt:pick:state:<slug>
      pt:run:a:<a>:<s> | pt:run:s:<s>:<a>
      pt:page:<a>:<s>:<n> | pt:page:s:<s>:<a>:<n> | pt:page:my:<n>
      pt:card:<issue_id>:<view>        view = list context to return to
      pt:back:<view>
      pt:my | pt:help
    """
    if not data.startswith("pt:"):
        return None
    rest = data[3:]
    head, _, tail = rest.partition(":")
    if head == "pick":
        sub, _, pl = tail.partition(":")
        if sub not in ("assignee", "state") or not pl:
            return None
        return ParsedCallback(stage=f"pick:{sub}", payload=pl)
    if head == "run":
        order, _, rest_payload = tail.partition(":")
        if order not in ("a", "s") or not rest_payload:
            return None
        return ParsedCallback(stage="run", payload=tail)
    if head == "page":
        # page:<order>:<x>:<y>:<n>  or  page:my:<n>
        parts = tail.split(":")
        if parts[0] == "my" and len(parts) >= 2:
            return ParsedCallback(stage="page:my", payload=parts[-1])
        if parts[0] in ("a", "s") and len(parts) >= 4:
            return ParsedCallback(stage=f"page:{parts[0]}", payload=":".join(parts[1:]))
        return None
    if head == "card":
        # card:<issue_id>:<view...>
        parts = tail.split(":", 1)
        if len(parts) == 2 and parts[0]:
            return ParsedCallback(stage="card", payload=tail)
        return None
    if head == "back":
        if not tail:
            return None
        return ParsedCallback(stage="back", payload=tail)
    if head == "clear":
        if tail:
            return None
        return ParsedCallback(stage="clear", payload="")
    if head == "noop":
        return ParsedCallback(stage="noop", payload="")
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

    def __init__(self, client: PlaneClient, settings: Settings, send: Callable, answer: Callable,
                 edit: Callable | None = None, clear_chat: Callable | None = None):
        self.client = client
        self.settings = settings
        self.send = send          # send(text, keyboard) -> None
        self.answer = answer      # answer(query_id, toast) -> None
        self.edit = edit          # edit(chat_id, message_id, text, keyboard) -> None
        self.clear_chat = clear_chat  # clear_chat(chat_id, answer) -> None
        self._cache: dict[str, Any] = {}

    # ── cached data ───────────────────────────────────
    def _data(self) -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]]]:
        import time
        now = time.time()
        if self._cache.get("ts", 0) < now - 60:
            states = self.client.get_states()
            members = self.client.get_members()
            labels = self.client.get_labels()
            issues = self.client.get_issues()
            self._cache = {"ts": now, "states": states, "members": members, "labels": labels, "issues": issues}
        return self._cache["states"], self._cache["members"], self._cache["issues"]

    def _labels(self) -> dict[str, str]:
        self._data()  # ensure cache warm
        return self._cache.get("labels", {})

    # ── dispatch ──────────────────────────────────────
    def handle(self, parsed: ParsedCallback, query_id: str = "", ctx: dict | None = None) -> None:
        """ctx carries chat_id / message_id for pop-up editing when present."""
        ctx = ctx or {}
        stage = parsed.stage
        if stage == "start":
            self._start(parsed.payload)
        elif stage == "pick:assignee":
            self._pick_assignee(parsed.payload)
        elif stage == "pick:state":
            self._pick_state(parsed.payload)
        elif stage == "run":
            self._run(parsed.payload, ctx=ctx)
        elif stage in ("page:a", "page:s", "page:my"):
            self._page(stage, parsed.payload, ctx=ctx)
        elif stage == "card":
            self._card(parsed.payload, ctx=ctx)
        elif stage == "back":
            self._back(parsed.payload, ctx=ctx)
        elif stage == "clear":
            self._clear(ctx=ctx)
        elif stage == "noop":
            pass  # disabled-button placeholder (answered with a toast)
        elif stage == "my":
            self._my(ctx=ctx)
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
    def _run(self, payload: str, ctx: dict | None = None) -> None:
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
        view = f"{order}:{a_slug}:{s_slug}"
        self._render_task_list(matched, f"Assignee: <b>{esc(a_label)}</b> · State: <b>{esc(s_label)}</b>",
                               view=view, ctx=ctx)

    # ── my tasks ──────────────────────────────────────
    def _my(self, ctx: dict | None = None) -> None:
        states, members, issues = self._data()
        me = self.settings.plane_user_id
        matched = [c for c in issues if me and me in [str(a) for a in (c.get("assignee_ids") or [])]]
        matched.sort(key=lambda x: int(x.get("sequence_id") or 0))
        self._render_task_list(matched, "🟦 <b>My Tasks</b> (assigned to you)",
                               view="my", ctx=ctx)

    # ── pagination ────────────────────────────────────
    def _page(self, stage: str, payload: str, ctx: dict | None = None) -> None:
        """pt:page:<...>:<n> — re-render a list view at page N."""
        states, members, issues = self._data()
        n = 1
        try:
            n = max(1, int(payload.split(":")[-1]))
        except (ValueError, IndexError):
            n = 1
        if stage == "page:my":
            me = self.settings.plane_user_id
            matched = [c for c in issues if me and me in [str(a) for a in (c.get("assignee_ids") or [])]]
            matched.sort(key=lambda x: int(x.get("sequence_id") or 0))
            self._render_task_list(matched, "🟦 <b>My Tasks</b> (assigned to you)", view="my", page=n, ctx=ctx)
            return
        # page:a:<a>:<s>:<n> or page:s:<s>:<a>:<n> — payload = "<a>:<s>:<n>"
        parts = payload.split(":")
        if len(parts) < 3:
            return
        order = stage[-1]  # 'a' or 's'
        a_slug, s_slug = (parts[0], parts[1]) if order == "a" else (parts[1], parts[0])
        a_label = "All" if a_slug == "all" else slug_to_label(a_slug, {assignee_slug_of(l): l for l in members.values()})
        s_label = "All" if s_slug == "all" else slug_to_label(s_slug, {state_slug_of(nn): nn for nn in set(states.values())})
        wanted = self._assignee_ids(a_slug)
        wanted_sids = None if s_slug == "all" else {sid for sid, name in states.items() if state_slug_of(name) == s_slug}
        matched = []
        for c in issues:
            as_ids = [str(a) for a in (c.get("assignee_ids") or [])]
            ok_a = True if a_slug == "all" else (not as_ids if a_slug == "unassigned" else any(i in (wanted or set()) for i in as_ids))
            ok_s = wanted_sids is None or str(c.get("state_id", "")) in wanted_sids
            if ok_a and ok_s:
                matched.append(c)
        matched.sort(key=lambda x: int(x.get("sequence_id") or 0))
        view = f"{order}:{a_slug}:{s_slug}"
        self._render_task_list(matched, f"Assignee: <b>{esc(a_label)}</b> · State: <b>{esc(s_label)}</b>",
                               view=view, page=n, ctx=ctx)

    # ── detail pop-up ─────────────────────────────────
    def _card(self, payload: str, ctx: dict | None = None) -> None:
        """pt:card:<seq>:<view> — show EVERYTHING about one task (edits msg).

        Uses sequence_id (not the UUID) in the callback data — full UUIDs
        push callback_data past Telegram's 64-byte cap and get silently dropped.
        """
        states, members, issues = self._data()
        labels = self._labels()
        seq_str, _, view = payload.partition(":")
        card = None
        for c in issues:
            if str(c.get("sequence_id", "")) == seq_str:
                card = c
                break
        if card is None:
            self.send("❌ Card not found", [[{"text": "⬅️ Back", "callback_data": f"pt:back:{view}"}]])
            return
        issue_id = card.get("id", "")
        me = self.settings.plane_user_id
        seq = card.get("sequence_id")
        name = card.get("name", "")
        st = states.get(str(card.get("state_id", "")), card.get("state__group") or "?")
        prio = card.get("priority") or "none"
        dot = PRIORITY_DOT.get(str(prio).lower(), "⚫")
        as_ids = [str(a) for a in (card.get("assignee_ids") or [])]
        a_names = ", ".join(members.get(i, i) for i in as_ids) or "Unassigned"
        mine = "🟦 " if me and me in as_ids else ""

        # fetch full detail incl. description_html
        desc = ""
        try:
            full = self.client._get(
                f"/api/workspaces/{self.settings.plane_workspace}"
                f"/projects/{self.settings.plane_project_id}/issues/{issue_id}/"
            )
            desc = (full.get("description_html") or full.get("description_stripped") or "").strip()
        except Exception:
            desc = ""

        lines = [
            f"{mine}<b>[{seq}]</b> {esc(name)}",
            "",
            f"      {STATE_ICON.get(st, '▪️')} <b>{esc(st)}</b> · {dot} {esc(prio)}",
            f"      👤 {esc(a_names)}",
        ]
        if labels and card.get("label_ids"):
            tag_names = [labels.get(str(lid), "") for lid in card.get("label_ids") or []]
            tag_names = [t for t in tag_names if t]
            if tag_names:
                lines.append("      🏷️ " + " ".join(f"#{t}" for t in tag_names))
        bits = []
        if card.get("sub_issues_count"):
            bits.append(f"🧩 {card['sub_issues_count']}")
        if card.get("attachment_count"):
            bits.append(f"📎 {card['attachment_count']}")
        if card.get("link_count"):
            bits.append(f"🔗 {card['link_count']}")
        if card.get("target_date"):
            bits.append(f"📅 {esc(str(card['target_date'])[:10])}")
        if card.get("start_date"):
            bits.append(f"🚩 {esc(str(card['start_date'])[:10])}")
        if card.get("estimate_point") is not None:
            bits.append(f"⏱ {card['estimate_point']}")
        if bits:
            lines.append("      " + " · ".join(bits))
        cb = card.get("created_by", "")
        if cb:
            lines.append(f"      ✍️ {esc(members.get(str(cb), str(cb)))}")
        if desc:
            # strip HTML tags for a readable pop-up
            import re
            plain = re.sub(r"<[^>]+>", " ", desc)
            plain = re.sub(r"\s+", " ", plain).strip()
            lines += ["", "<b>Description</b>", f"      {esc(plain[:900])}"]

        url = (f"{self.settings.plane_base_url}/{self.settings.plane_workspace}"
               f"/projects/{self.settings.plane_project_id}/issues/{issue_id}/")
        kb = [
            [{"text": "🔗 Open in Plane", "url": url}],
            [{"text": "⬅️ Back to list", "callback_data": f"pt:back:{view}"}],
        ]
        text = "\n".join(lines)
        if ctx and ctx.get("message_id") and self.edit:
            self.edit(ctx["chat_id"], ctx["message_id"], text, kb)
        else:
            self.send(text, kb)

    # ── back to list ──────────────────────────────────
    def _back(self, view: str, ctx: dict | None = None) -> None:
        """pt:back:<view> — return from detail pop-up to the list."""
        if view == "my":
            self._my(ctx=ctx)
        elif view.startswith(("a:", "s:")):
            order, _, rest = view.partition(":")
            a_slug, s_slug = (rest.split(":", 1) + ["all"])[:2]
            if order == "a":
                self._run(f"a:{a_slug}:{s_slug}", ctx=ctx)
            else:
                self._run(f"s:{s_slug}:{a_slug}", ctx=ctx)
        else:
            self._start("assignee")

    # ── clear chat ────────────────────────────────────
    def _clear(self, ctx: dict | None = None) -> None:
        """🧹 Delete every non-pinned message the bot has seen in the chat."""
        chat_id = (ctx or {}).get("chat_id") or self.settings.tg_chat_id
        if self.clear_chat:
            self.clear_chat(chat_id, self.answer)

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
            [{"text": "🧹 Clear chat (keep pinned)", "callback_data": "pt:clear"}],
        ]
        self.send(text, kb)

    # ── rendering ─────────────────────────────────────
    PAGE_SIZE = 15

    def _render_task_list(self, cards: list[dict[str, Any]], header: str,
                          view: str = "", page: int = 1, ctx: dict | None = None) -> None:
        states, members, _ = self._data()
        me = self.settings.plane_user_id
        labels = self._labels()
        total = len(cards)
        total_pages = max(1, -(-total // self.PAGE_SIZE))
        page = max(1, min(page, total_pages))
        start = (page - 1) * self.PAGE_SIZE
        shown = cards[start:start + self.PAGE_SIZE]

        lines = [f"🎯 <b>Tasks</b> — {header} ({total})"]
        if total_pages > 1:
            lines[0] += f" · 📄 {page}/{total_pages}"
        lines.append("")

        for c in shown:
            lines.extend(_card_block(c, states, members, me, labels))
            lines.append("")
        if not shown:
            lines.append("  (no cards match)")
        lines.append("")
        lines.append("🔄 <i>Tap 🔍 for full description</i>")

        buttons: list[list[dict[str, str]]] = []
        # 🔍 magnifier grid — 5 per row, opens the full-description pop-up
        # (uses sequence_id: full UUIDs would exceed Telegram's 64-byte cap)
        grid: list[dict[str, str]] = []
        for c in shown:
            seq = c.get("sequence_id", "")
            grid.append({"text": f"🔍[{seq}]", "callback_data": f"pt:card:{seq}:{view}"})
            if len(grid) == 5:
                buttons.append(grid)
                grid = []
        if grid:
            buttons.append(grid)
        # pagination row: ⏮️ ◀️ current ▶️ ⏭️ — disabled ends send noop
        if total_pages > 1:
            nav = []
            first_cb = "pt:noop" if page == 1 else f"pt:page:{view}:1"
            prev_cb = "pt:noop" if page == 1 else f"pt:page:{view}:{page - 1}"
            next_cb = "pt:noop" if page == total_pages else f"pt:page:{view}:{page + 1}"
            last_cb = "pt:noop" if page == total_pages else f"pt:page:{view}:{total_pages}"
            nav.append({"text": "⏮️", "callback_data": first_cb})
            nav.append({"text": "◀️", "callback_data": prev_cb})
            nav.append({"text": f"{page}/{total_pages}", "callback_data": "pt:noop"})
            nav.append({"text": "▶️", "callback_data": next_cb})
            nav.append({"text": "⏭️", "callback_data": last_cb})
            buttons.append(nav)
        # bottom nav — one slim row + big clear button
        buttons.append(nav_keyboard()[0])
        buttons.append([{"text": "🧹 Clear chat (keep pinned)", "callback_data": "pt:clear"}])

        text = "\n".join(lines)
        if ctx and ctx.get("message_id") and self.edit:
            self.edit(ctx["chat_id"], ctx["message_id"], text, buttons)
        else:
            self.send(text, buttons)

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


def _card_block(
    c: dict[str, Any],
    states: dict[str, str],
    members: dict[str, str],
    me: str | None,
    labels: dict[str, str] | None = None,
    detail_url: bool = False,
    plane_url: str = "",
) -> list[str]:
    """Render one task card as a list of lines (title + meta), nice typography."""
    seq = c.get("sequence_id")
    name = c.get("name", "")
    st = states.get(str(c.get("state_id", "")), c.get("state__group") or "?")
    prio = c.get("priority") or "none"
    dot = PRIORITY_DOT.get(str(prio).lower(), "⚫")
    as_ids = [str(a) for a in (c.get("assignee_ids") or [])]
    a_names = ", ".join(members.get(i, i) for i in as_ids) or "Unassigned"
    mine = "🟦 " if me and me in as_ids else ""
    draft = "📄 " if c.get("is_draft") else ""

    title = f"{mine}{draft}<b>[{seq}]</b> {esc(name)}"
    if detail_url and plane_url:
        title += f"  <a href=\"{plane_url}\">🔍</a>"
    icon = STATE_ICON.get(st, "▪️")
    meta = f"      {icon} {esc(st)} · {dot} {esc(prio)} · 👤 {esc(a_names)}"

    # extra meta bits — only shown when present (editorial restraint)
    bits = []
    if labels and c.get("label_ids"):
        tag_names = [labels.get(str(lid), "") for lid in c.get("label_ids") or []]
        tag_names = [t for t in tag_names if t]
        if tag_names:
            bits.append("🏷️ " + " ".join(f"#{t}" for t in tag_names))
    if c.get("sub_issues_count"):
        bits.append(f"🧩 {c['sub_issues_count']}")
    if c.get("attachment_count"):
        bits.append(f"📎 {c['attachment_count']}")
    if c.get("target_date"):
        bits.append(f"📅 {esc(str(c['target_date'])[:10])}")
    if c.get("start_date"):
        bits.append(f"🚩 {esc(str(c['start_date'])[:10])}")

    lines = [title, meta]
    if bits:
        lines.append("      " + " · ".join(bits))
    return lines
