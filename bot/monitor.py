"""Watchdog diff engine — classify changes between snapshot and fresh issues."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Change:
    issue_id: str
    sequence_id: int | None
    name: str
    kind: str  # new | state | priority | assignees | name | deleted
    old: str = ""
    new: str = ""
    is_mine: bool = False
    created_by: str = ""
    created_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _norm_assignees(issue: dict[str, Any] | None) -> list[str]:
    if not issue:
        return []
    return [str(a) for a in (issue.get("assignee_ids") or [])]


def diff_issues(
    old_snapshot: dict[str, Any] | None,
    new_issues: list[dict[str, Any]],
    states: dict[str, str] | None = None,
    members: dict[str, str] | None = None,
    me: str | None = None,
) -> list[Change]:
    """Compare a snapshot's issue map against fresh issues.

    old_snapshot: {"issues": {id: {...}}} or None (first run → baseline).
    Returns classified changes; empty list when nothing changed.
    """
    states = states or {}
    members = members or {}
    old_map = (old_snapshot or {}).get("issues", {}) if old_snapshot else {}
    new_map: dict[str, dict[str, Any]] = {}
    for iss in new_issues:
        iid = str(iss.get("id", ""))
        if iid:
            new_map[iid] = iss

    def st_name(sid: str) -> str:
        return states.get(sid, sid)

    def assignee_names(ids: list[str]) -> str:
        return ", ".join(members.get(i, i) for i in ids) or "Unassigned"

    changes: list[Change] = []
    all_ids = set(old_map) | set(new_map)

    for iid in sorted(all_ids):
        old = old_map.get(iid)
        new = new_map.get(iid)
        old_seq = int(old.get("sequence_id") or 0) if old else int(new.get("sequence_id") or 0)
        name = (new or old or {}).get("name", "")
        is_mine = bool(me) and (me in _norm_assignees(new) or (old is not None and me in _norm_assignees(old)))

        if old is None:
            changes.append(Change(
                issue_id=iid, sequence_id=old_seq, name=name, kind="new",
                is_mine=is_mine,
                created_by=(new or {}).get("created_by", ""),
                created_at=(new or {}).get("created_at", ""),
            ))
            continue

        if new is None:
            changes.append(Change(
                issue_id=iid, sequence_id=old_seq, name=name, kind="deleted",
                is_mine=is_mine,
            ))
            continue

        o_state, n_state = str(old.get("state_id", "")), str(new.get("state_id", ""))
        o_prio, n_prio = str(old.get("priority", "")), str(new.get("priority", ""))
        o_as, n_as = _norm_assignees(old), _norm_assignees(new)
        o_name = str(old.get("name", ""))
        n_name = str(new.get("name", ""))

        if o_state != n_state:
            changes.append(Change(
                issue_id=iid, sequence_id=old_seq, name=name, kind="state",
                old=st_name(o_state), new=st_name(n_state), is_mine=is_mine,
            ))
        if o_prio != n_prio:
            changes.append(Change(
                issue_id=iid, sequence_id=old_seq, name=name, kind="priority",
                old=o_prio or "none", new=n_prio or "none", is_mine=is_mine,
            ))
        if set(o_as) != set(n_as):
            added = [assignee_names([x]) for x in n_as if x not in o_as]
            removed = [assignee_names([x]) for x in o_as if x not in n_as]
            parts = [f"+{a}" for a in added] + [f"-{r}" for r in removed]
            changes.append(Change(
                issue_id=iid, sequence_id=old_seq, name=name, kind="assignees",
                old=assignee_names(o_as), new=assignee_names(n_as),
                is_mine=is_mine, extra={"parts": parts},
            ))
        if o_name != n_name and o_name and n_name:
            changes.append(Change(
                issue_id=iid, sequence_id=old_seq, name=name, kind="name",
                old=o_name, new=n_name, is_mine=is_mine,
            ))

    return changes
