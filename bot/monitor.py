"""Watchdog diff engine — classify changes between snapshot and fresh issues."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .messages import build_report
from .plane_client import PlaneAuthError, PlaneApiError, PlaneClient
from .models import Change
from .state import load_state, save_state
from .telegram_client import TelegramClient, TelegramError

logger = logging.getLogger(__name__)


def _norm_assignees(issue: dict[str, Any] | None) -> list[str]:
    if not issue:
        return []
    return [str(a) for a in (issue.get("assignee_ids") or [])]


def _issue_description(issue: dict[str, Any] | None) -> str:
    """Normalize the description field — live payloads use description_html,
    older API shapes may use description_stripped or description."""
    if not issue:
        return ""
    for key in ("description_html", "description_stripped", "description"):
        val = issue.get(key)
        if val:
            return str(val)
    return ""


def _fmt_time(ts: Any) -> str:
    """Format a timestamp for display: '2026-08-19T11:27:02.945835Z' → '2026-08-19 11:27'."""
    if not ts:
        return ""
    s = str(ts)
    # ISO with T separator and optional fractional seconds / tz
    s = s.replace("T", " ").split(".")[0]
    if s.endswith("Z") or "+" in s:
        s = s.rstrip("Z").split("+")[0]
    # keep YYYY-MM-DD HH:MM (drop seconds for compactness)
    parts = s.split(" ")
    if len(parts) == 2 and len(parts[1]) >= 5:
        parts[1] = parts[1][:5]
    return " ".join(parts)


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
                # resolve the creator UUID to a display name (spec §6.2 shows
                # "Created by alighahremani", never a raw id)
                created_by=members.get(str((new or {}).get("created_by", "")), "")
                    or str((new or {}).get("created_by", "")),
                created_at=_fmt_time((new or {}).get("created_at", "")),
                # populate actual card details so reports show them (spec §6.2)
                new=st_name(str((new or {}).get("state_id", ""))),
                old=(new or {}).get("priority", "") or "none",
                extra={"assignees": assignee_names(_norm_assignees(new))},
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
        o_desc = _issue_description(old)
        n_desc = _issue_description(new)

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
        if o_desc != n_desc and o_desc and n_desc:
            changes.append(Change(
                issue_id=iid, sequence_id=old_seq, name=name, kind="description",
                old="", new="", is_mine=is_mine,
            ))

    return changes


def _build_snapshot(issues: list[dict[str, Any]], ts: str) -> dict[str, Any]:
    """Build the persisted snapshot dict from fresh issues."""
    return {
        "issues": {
            str(i["id"]): {
                "sequence_id": i.get("sequence_id"),
                "name": i.get("name"),
                "state_id": i.get("state_id"),
                "priority": i.get("priority"),
                "assignee_ids": i.get("assignee_ids") or [],
                "created_by": i.get("created_by"),
                "created_at": i.get("created_at"),
                "updated_at": i.get("updated_at"),
                "description_html": i.get("description_html")
                    or i.get("description_stripped")
                    or i.get("description")
                    or "",
            }
            for i in issues
        },
        "_fetched_at": ts,
    }


def run_poll_once(
    client: PlaneClient,
    tg: TelegramClient,
    settings: Settings,
    state_path: str,
) -> str | None:
    """One watchdog cycle: fetch → diff → report → persist.

    Returns the report text (or None when silent). Raises PlaneAuthError
    when the session is rejected so the caller can alert.
    """
    states = client.get_states()
    members = client.get_members()
    issues = client.get_issues()

    old = load_state(state_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if old is None:
        # FIRST RUN — baseline silently, never post "all cards are new"
        new_snapshot = _build_snapshot(issues, now)
        save_state(state_path, new_snapshot)
        logger.info("baselined %d issues (first run, silent)", len(issues))
        return None
    changes = diff_issues(
        old, issues,
        states=states, members=members, me=settings.plane_user_id,
    )

    new_snapshot = _build_snapshot(issues, now)
    report = build_report(
        changes,
        focus=settings.plane_focus,
        me=settings.plane_user_id,
        base_url=settings.plane_base_url,
        workspace=settings.plane_workspace,
        project_id=settings.plane_project_id,
        fetched_at=now,
    )
    if report is None:
        # nothing changed — persist snapshot, stay silent
        save_state(state_path, new_snapshot)
        return None
    text, rows = report
    # deliver FIRST, then persist: if delivery fails the snapshot is NOT
    # advanced, so the next poll re-detects the change and retries
    # (at-least-once semantics — a lost notification is worse than a duplicate).
    tg.send_chunked(text, keyboard=rows)
    save_state(state_path, new_snapshot)
    logger.info("posted %d-change report (%d chars)", len(changes), len(text))
    return text


class PollLoop:
    """Runs run_poll_once on an interval, woken early by webhook kicks (debounced).

    The loop thread is the ONLY thread that fetches/diffs/delivers, so a webhook
    kick never races with a scheduled poll over state.json or Telegram delivery.
    """

    def __init__(self, client: PlaneClient, tg: TelegramClient, settings: Settings):
        self.client = client
        self.tg = tg
        self.settings = settings
        self.stop = False
        self._wake = threading.Event()
        self._last_run = 0.0

    def kick(self) -> None:
        """Wake the loop early — called from the webhook server thread."""
        self._wake.set()

    def stop(self) -> None:
        """Signal shutdown; unblocks an in-progress wait immediately."""
        self.stop = True
        self._wake.set()

    def _run_cycle(self) -> None:
        try:
            run_poll_once(
                self.client, self.tg, self.settings, self.settings.state_file,
            )
        except PlaneAuthError as exc:
            logger.error("Plane session expired: %s", exc)
            try:
                self.tg.send_message(
                    "⚠️ <b>Plane session expired</b> — please re-authenticate "
                    "(update PLANE_SESSION_ID / PLANE_CSRF_TOKEN)."
                )
            except TelegramError as te:
                logger.error("failed to alert about session expiry: %s", te)
        except PlaneApiError as exc:
            logger.warning("transient Plane API error (skipping cycle): %s", exc)
        except TelegramError as exc:
            logger.error("Telegram delivery error: %s", exc)
        except Exception as exc:
            logger.exception("poll cycle crashed: %s", exc)

    def run(self) -> None:
        """Loop body: run a cycle, then wait for a webhook kick or the interval.

        Kicks collapse via the debounce window — a burst of webhook events
        triggers at most one poll per WEBHOOK_MIN_INTERVAL_SECONDS.
        """
        interval = self.settings.poll_interval_seconds
        debounce = self.settings.webhook_min_interval_seconds
        while not self.stop:
            if time.monotonic() - self._last_run >= debounce:
                self._run_cycle()
                self._last_run = time.monotonic()
            self._wake.wait(timeout=interval)
            self._wake.clear()
