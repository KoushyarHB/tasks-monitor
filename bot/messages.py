"""Message rendering, HTML escaping, chunking, and keyboard builders."""
from __future__ import annotations

import html as _html
from typing import Any

from .models import Change

MAX_MSG = 4096
PROJECT_URL_PLACEHOLDER = "{project_url}"


def esc(text: Any) -> str:
    """HTML-escape &, <, > AND quotes — spec requires all four characters."""
    return _html.escape(str(text if text is not None else ""), quote=True)


def chunk_text(text: str, limit: int = MAX_MSG) -> list[str]:
    """Split on line boundaries, each chunk ≤ limit. Preserves all content.

    Chunks are joined back with '' to reproduce the exact input.
    """
    if len(text) <= limit:
        return [text]
    lines = text.split("\n")
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0

    def flush() -> None:
        nonlocal cur, cur_len
        if cur:
            chunks.append("\n".join(cur) + "\n")  # trailing \n = separator to next chunk
        cur, cur_len = [], 0

    for line in lines:
        if len(line) > limit:
            flush()
            while len(line) > limit:
                chunks.append(line[:limit])  # hard-split pieces (no trailing \n)
                line = line[limit:]
            cur = [line]
            cur_len = len(line)
            continue
        add = len(line) + (1 if cur else 0)  # +1 for the "\n" separator
        if cur and cur_len + add > limit:
            flush()
            add = len(line)
        cur.append(line)
        cur_len += add

    if cur:
        chunks.append("\n".join(cur))
    return chunks


def build_keyboard(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    """Validate + wrap rows into a Telegram inline_keyboard payload.

    Every top-level element MUST be a list (array-of-arrays). Raises ValueError
    if a row is not a list or a button's callback_data exceeds 64 bytes.
    """
    for row in rows:
        if not isinstance(row, list):
            raise ValueError("keyboard rows must be lists (array-of-arrays)")
        for btn in row:
            cd = btn.get("callback_data")
            if cd is not None and len(cd) > 64:
                raise ValueError(f"callback_data too long ({len(cd)} > 64): {cd}")
    return {"inline_keyboard": rows}


def card_button(seq: int | None, issue_url: str) -> dict[str, str]:
    return {"text": f"Card {seq}", "url": issue_url}


def nav_keyboard() -> list[list[dict[str, str]]]:
    return [
        [{"text": "👤 By Assignee", "callback_data": "pt:start:assignee"},
         {"text": "🗂 By State", "callback_data": "pt:start:state"}],
        [{"text": "🟦 My Tasks", "callback_data": "pt:my"},
         {"text": "❓ Commands", "callback_data": "pt:help"}],
    ]


def project_url_button(base_url: str, workspace: str, project_id: str) -> dict[str, str]:
    return {"text": "🔗 Open project", "url": f"{base_url}/{workspace}/projects/{project_id}/"}


def build_report(
    changes: list[Change],
    *,
    focus: str = "mine",
    me: str | None = None,
    base_url: str = "",
    workspace: str = "",
    project_id: str = "",
    fetched_at: str | None = None,
) -> tuple[str, list[list[dict[str, str]]]] | None:
    """Build the change-report message + keyboard.

    Returns (text, keyboard_rows) or None when there is nothing to report
    (silent-when-clean contract).
    """
    if not changes:
        return None

    mine = [c for c in changes if c.is_mine]
    others = [c for c in changes if not c.is_mine]
    # In "mine" focus, only my cards are reportable; anything else is silent
    if focus == "mine":
        if not mine:
            return None
        others = []

    lines: list[str] = []
    ts = fetched_at or ""
    lines.append(f"📋 <b>Plane Monitor</b> — {esc(ts)}")
    lines.append("")

    if mine:
        lines.append("🟦 <b>MY ASSIGNED CARDS</b>")
        lines.append("")
        for c in mine:
            lines.extend(_change_lines(c))
            lines.append("")
        if focus == "mine" and not others:
            pass  # no others section in mine-focus unless desired
        else:
            lines.append("")

    if others:
        lines.append("⬜ <b>OTHER CARDS</b>")
        lines.append("")
        for c in others:
            lines.extend(_change_lines(c, concise=True))
            lines.append("")
        lines.append("")

    text = "\n".join(lines).rstrip()
    if not text:
        return None

    # keyboard: card buttons (one per change, capped 12) + project button
    rows: list[list[dict[str, str]]] = []
    # find issue ids for the sequence numbers shown
    seq_to_url: dict[int, str] = {}
    for c in changes:
        if c.sequence_id is not None and c.sequence_id not in seq_to_url:
            seq_to_url[c.sequence_id] = (
                f"{base_url}/{workspace}/projects/{project_id}/issues/{c.issue_id}/"
            )
    for seq in list(seq_to_url)[:12]:
        rows.append([card_button(seq, seq_to_url[seq])])
    rows.append([project_url_button(base_url, workspace, project_id)])
    return text, rows


def _change_lines(c: Change, concise: bool = False) -> list[str]:
    seq = c.sequence_id
    tag = "🆕" if c.kind == "new" else ("🗑" if c.kind == "deleted" else "🔄")
    if concise:
        head = f"   {tag} [{seq}] <code>{esc(c.name)}</code>"
        detail = _change_detail(c, short=True)
        return [head + (f" — {detail}" if detail else "")]
    head = f"   {tag} <b>[{seq}]</b> <code>{esc(c.name)}</code>"
    lines = [head]
    detail = _change_detail(c)
    if detail:
        lines.append(f"       {detail}")
    if c.kind == "new" and c.created_by:
        lines.append(f"       Created by {esc(c.created_by)} at {esc(c.created_at)}")
    return lines


def _change_detail(c: Change, short: bool = False) -> str:
    if c.kind == "new":
        # actual card details (populated by the diff engine)
        parts = []
        st = c.new or ""
        prio = c.old or ""
        if st and st != "?":
            parts.append(f"Status: {esc(st)}")
        if prio and prio != "none":
            parts.append(f"Priority: {esc(prio)}")
        assignees = c.extra.get("assignees") or ""
        if isinstance(assignees, list):
            assignees = ", ".join(assignees)
        if assignees:
            parts.append("Assignees: " + esc(assignees))
        return " · ".join(parts)
    if c.kind == "deleted":
        return "removed"
    if c.kind == "state":
        return f"State: {esc(c.old)} → {esc(c.new)}"
    if c.kind == "priority":
        return f"Priority: {esc(c.old)} → {esc(c.new)}"
    if c.kind == "assignees":
        parts = c.extra.get("parts") or []
        if parts:
            return "Assignees: " + ", ".join(esc(p) for p in parts)
        return f"Assignees: {esc(c.old)} → {esc(c.new)}"
    if c.kind == "name":
        return f"Title: {esc(c.old)} → {esc(c.new)}"
    if c.kind == "description":
        return "Description updated"
    return ""
