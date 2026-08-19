"""Tests for bot.browser — callback parsing, two-step flows, rendering."""
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.browser import Browser, ParsedCallback, parse_callback
from bot.config import Settings
from bot.plane_client import PlaneClient

ME = "user-me"
BASE = "https://plane.test"

STATES = {"s-todo": "Todo", "s-done": "Done", "s-backlog": "Backlog"}
MEMBERS = {ME: "Koushyar Heidari", "u-fei": "feizyr"}


def issue(iid, seq, name, state="s-todo", prio="medium", assignees=None):
    return {
        "id": iid, "sequence_id": seq, "name": name, "state_id": state,
        "priority": prio, "assignee_ids": assignees or [],
    }


def make_browser(issues, me=ME):
    def router(request):
        url = str(request.url)
        if "/states/" in url:
            return httpx.Response(200, json={"results": [{"id": k, "name": v} for k, v in STATES.items()]})
        if "/members/" in url:
            return httpx.Response(200, json={"results": [{"member": {"id": k, "display_name": v}} for k, v in MEMBERS.items()]})
        if "/issues/" in url:
            return httpx.Response(200, json={"results": issues})
        return httpx.Response(404, json={})

    settings = Settings(
        plane_base_url=BASE, plane_workspace="tms", plane_project_id="proj",
        plane_csrf_token="c", plane_session_id="s", plane_user_id=me, plane_focus="mine",
    )
    client = PlaneClient(settings, transport=httpx.MockTransport(router))
    sent = []
    answered = []
    browser = Browser(client, settings,
                      send=lambda text, kb: sent.append((text, kb)),
                      answer=lambda qid, toast: answered.append((qid, toast)))
    return browser, sent, answered


# ── callback parsing ──────────────────────────────────
def test_parse_start():
    p = parse_callback("pt:start:assignee")
    assert p is not None and p.stage == "start" and p.payload == "assignee"


def test_parse_pick():
    p = parse_callback("pt:pick:assignee:feizyr")
    assert p is not None and p.stage == "pick:assignee" and p.payload == "feizyr"
    p = parse_callback("pt:pick:state:backlog")
    assert p.stage == "pick:state" and p.payload == "backlog"


def test_parse_run_order_markers():
    p = parse_callback("pt:run:a:koushyar_heidari:backlog")
    assert p.stage == "run" and p.payload == "a:koushyar_heidari:backlog"
    p = parse_callback("pt:run:s:backlog:feizyr")
    assert p.stage == "run" and p.payload == "s:backlog:feizyr"


def test_parse_my_help():
    assert parse_callback("pt:my").stage == "my"
    assert parse_callback("pt:help").stage == "help"


def test_parse_unknown_returns_none():
    assert parse_callback("xx:yy") is None
    assert parse_callback("pt:bogus") is None
    # strict protocol enforcement (spec §7.8 — unknown tokens rejected)
    assert parse_callback("pt:start:garbage") is None
    assert parse_callback("pt:pick:unknown:x") is None
    assert parse_callback("pt:pick:assignee:") is None        # empty slug
    assert parse_callback("pt:run:x:foo:bar") is None          # bad order marker
    assert parse_callback("pt:run:a:") is None                 # missing values
    assert parse_callback("pt:my:extra") is None               # payload not allowed
    assert parse_callback("pt:help:extra") is None


# ── stage 1 ───────────────────────────────────────────
def test_start_assignee_shows_buttons_plus_all():
    browser, sent, _ = make_browser([issue("i1", 1, "A")])
    browser.handle(parse_callback("pt:start:assignee"))
    text, kb = sent[-1]
    assert "Pick an assignee" in text
    assert any(b["callback_data"] == "pt:pick:assignee:all" for row in kb for b in row)
    labels = [b["text"] for row in kb for b in row]
    assert "Koushyar Heidari" in labels
    assert "Unassigned" in labels


def test_start_state_shows_states():
    browser, sent, _ = make_browser([issue("i1", 1, "A")])
    browser.handle(parse_callback("pt:start:state"))
    text, kb = sent[-1]
    assert "Pick a state" in text
    labels = [b["text"] for row in kb for b in row]
    assert "Backlog" in labels and "Todo" in labels and "Done" in labels


# ── stage 2: counts + dead-end prevention ─────────────
def test_pick_assignee_shows_only_states_with_tasks():
    issues = [
        issue("i1", 1, "A", state="s-backlog", assignees=[ME]),
        issue("i2", 2, "B", state="s-todo", assignees=[ME]),
        issue("i3", 3, "C", state="s-todo", assignees=["u-fei"]),
    ]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:pick:assignee:koushyar_heidari"))
    text, kb = sent[-1]
    labels = " ".join(b["text"] for row in kb for b in row)
    assert "Backlog (1)" in labels
    assert "Todo (1)" in labels
    assert "Done" not in labels  # no tasks in Done for this assignee


def test_pick_assignee_all_counts():
    issues = [
        issue("i1", 1, "A", state="s-backlog", assignees=[ME]),
        issue("i2", 2, "B", state="s-todo", assignees=["u-fei"]),
    ]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:pick:assignee:all"))
    labels = " ".join(b["text"] for row in sent[-1][1] for b in row)
    assert "Backlog (1)" in labels and "Todo (1)" in labels


def test_pick_assignee_no_tasks_shows_deadend_notice():
    browser, sent, _ = make_browser([issue("i1", 1, "A", state="s-backlog", assignees=["u-fei"])])
    browser.handle(parse_callback("pt:pick:assignee:koushyar_heidari"))
    labels = " ".join(b["text"] for row in sent[-1][1] for b in row)
    assert "(no tasks)" in labels


def test_pick_state_shows_only_assignees_with_tasks():
    issues = [
        issue("i1", 1, "A", state="s-todo", assignees=[ME]),
        issue("i2", 2, "B", state="s-todo", assignees=["u-fei"]),
        issue("i3", 3, "C", state="s-backlog", assignees=["u-fei"]),
    ]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:pick:state:todo"))
    labels = " ".join(b["text"] for row in sent[-1][1] for b in row)
    assert "Koushyar Heidari (1)" in labels
    assert "feizyr (1)" in labels


# ── stage 3: run ──────────────────────────────────────
def test_run_assignee_first_filters_correctly():
    issues = [
        issue("i1", 1, "Mine backlog", state="s-backlog", assignees=[ME]),
        issue("i2", 2, "Mine todo", state="s-todo", assignees=[ME]),
        issue("i3", 3, "Fei backlog", state="s-backlog", assignees=["u-fei"]),
    ]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:run:a:koushyar_heidari:backlog"))
    text, kb = sent[-1]
    assert "Mine backlog" in text
    assert "Mine todo" not in text
    assert "Fei backlog" not in text
    assert "(1)" in text  # header count
    # mine marker
    assert "🟦" in text


def test_run_state_first_filters_correctly():
    issues = [
        issue("i1", 1, "Mine backlog", state="s-backlog", assignees=[ME]),
        issue("i2", 2, "Fei backlog", state="s-backlog", assignees=["u-fei"]),
        issue("i3", 3, "Fei todo", state="s-todo", assignees=["u-fei"]),
    ]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:run:s:backlog:feizyr"))
    text, _ = sent[-1]
    assert "Fei backlog" in text
    assert "Mine backlog" not in text
    assert "Fei todo" not in text


def test_run_all_all_returns_everything():
    issues = [issue("i1", 1, "A"), issue("i2", 2, "B", assignees=["u-fei"])]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:run:a:all:all"))
    text, _ = sent[-1]
    assert "A" in text and "B" in text
    assert "(2)" in text


# ── my / help ─────────────────────────────────────────
def test_my_only_own_cards():
    issues = [
        issue("i1", 1, "Mine", assignees=[ME]),
        issue("i2", 2, "Not mine", assignees=["u-fei"]),
    ]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:my"))
    text, kb = sent[-1]
    assert "Mine" in text
    assert "Not mine" not in text
    # nav present
    cb = [b["callback_data"] for row in kb for b in row if "callback_data" in b]
    assert "pt:start:assignee" in cb


def test_help_text():
    browser, sent, _ = make_browser([])
    browser.handle(parse_callback("pt:help"))
    text, kb = sent[-1]
    assert "Plane Monitor — Commands" in text
    assert "/task_by_assignee" in text


def test_all_keyboards_valid_shape():
    browser, sent, _ = make_browser([issue("i1", 1, "A", assignees=[ME])])
    for cb in ["pt:start:assignee", "pt:start:state", "pt:my", "pt:help"]:
        browser.handle(parse_callback(cb))
        text, kb = sent[-1]
        assert all(isinstance(row, list) for row in kb), f"bad keyboard for {cb}"
        for row in kb:
            for b in row:
                cd = b.get("callback_data")
                if cd:
                    assert len(cd) <= 64


def test_card_block_shows_labels_and_meta():
    from bot.browser import _card_block
    from bot.messages import esc
    c = {
        "id": "i1", "sequence_id": 42, "name": "Big card", "state_id": "s-todo",
        "priority": "high", "assignee_ids": [ME], "is_draft": False,
        "label_ids": ["l-backend", "l-bug"],
        "sub_issues_count": 3, "attachment_count": 2,
        "target_date": "2026-09-01T00:00:00Z",
    }
    labels = {"l-backend": "backend", "l-bug": "bug"}
    lines = _card_block(c, STATES, MEMBERS, ME, labels)
    text = "\n".join(lines)
    assert "#backend" in text and "#bug" in text
    assert "🧩 3" in text
    assert "📎 2" in text
    assert "📅 2026-09-01" in text
    assert "🟦" in text  # mine marker


def test_card_block_hides_empty_meta():
    from bot.browser import _card_block
    c = {"id": "i1", "sequence_id": 42, "name": "Plain", "state_id": "s-todo",
         "priority": "none", "assignee_ids": [ME]}
    lines = _card_block(c, STATES, MEMBERS, ME, {})
    assert len(lines) == 2  # no extra meta line when nothing present


# ── pagination ────────────────────────────────────────
def test_list_paginated_with_20_cards():
    issues = [issue(f"i{n}", n, f"Card {n}", assignees=[ME]) for n in range(1, 21)]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:my"))
    text, kb = sent[-1]
    assert "📄 1/2" in text          # pagination indicator
    assert "Card 15" in text         # page 1 shows first 15
    assert "Card 16" not in text     # page 2 content not on page 1
    # next-page button present
    cb = [b["callback_data"] for row in kb for b in row if "callback_data" in b]
    assert any(c == "pt:page:my:2" for c in cb)
    # prev absent on page 1
    assert not any(c == "pt:page:my:0" for c in cb)


def test_page_navigation_renders_page_2():
    issues = [issue(f"i{n}", n, f"Card {n}", assignees=[ME]) for n in range(1, 21)]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:page:my:2"))
    text, kb = sent[-1]
    assert "📄 2/2" in text
    assert "Card 16" in text
    assert "Card 1\n" not in text and "Card 1 " not in text  # page-1 cards absent
    cb = [b["callback_data"] for row in kb for b in row if "callback_data" in b]
    assert any(c == "pt:page:my:1" for c in cb)  # prev button on page 2


def test_pagination_assignee_view():
    issues = [issue(f"i{n}", n, f"Card {n}", state="s-backlog", assignees=[ME]) for n in range(1, 20)]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:run:a:koushyar_heidari:backlog"))
    text, kb = sent[-1]
    cb = [b["callback_data"] for row in kb for b in row if "callback_data" in b]
    assert any(c == "pt:page:a:koushyar_heidari:backlog:2" for c in cb)


# ── detail pop-up ─────────────────────────────────────
def test_card_detail_shows_everything():
    issues = [{"id": "i1", "sequence_id": 42, "name": "Big card", "state_id": "s-todo",
               "priority": "high", "assignee_ids": [ME], "label_ids": ["l1"],
               "sub_issues_count": 3, "target_date": "2026-09-01T00:00:00Z",
               "created_by": ME}]
    # detail endpoint returns description
    def router(request):
        url = str(request.url)
        if "/states/" in url:
            return httpx.Response(200, json={"results": [{"id": "s-todo", "name": "Todo"}]})
        if "/members/" in url:
            return httpx.Response(200, json={"results": [{"member": {"id": ME, "display_name": "Koushyar Heidari"}}]})
        if "/issues/i1/" in url and not url.endswith("/issues/"):
            return httpx.Response(200, json={
                "id": "i1", "name": "Big card", "description_html": "<p>Do the thing</p>",
                "state_id": "s-todo", "priority": "high", "assignee_ids": [ME],
            })
        return httpx.Response(200, json={"results": issues})

    settings = Settings(
        plane_base_url=BASE, plane_workspace="tms", plane_project_id="proj",
        plane_csrf_token="c", plane_session_id="s", plane_user_id=ME, plane_focus="mine",
    )
    client = PlaneClient(settings, transport=httpx.MockTransport(router))
    sent = []
    browser = Browser(client, settings,
                      send=lambda text, kb: sent.append((text, kb)),
                      answer=lambda qid, toast: None,
                      edit=lambda chat, mid, text, kb: sent.append((text, kb, "EDIT")))
    browser.handle(parse_callback("pt:card:42:my"))
    text = sent[-1][0]
    assert "Big card" in text
    assert "Description" in text
    assert "Do the thing" in text
    assert "Todo" in text
    assert "high" in text


def test_card_detail_edits_message_when_ctx_present():
    issues = [issue("i1", 42, "Card", assignees=[ME])]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:card:42:my"), query_id="q1",
                   ctx={"chat_id": -100, "message_id": 55})
    # falls back to send when edit is None (test browser has no edit)
    assert sent and "Card" in sent[-1][0]


def test_back_returns_to_list():
    issues = [issue("i1", 1, "A", assignees=[ME])]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:back:my"))
    text = sent[-1][0]
    assert "My Tasks" in text


def test_back_to_assignee_view():
    issues = [issue("i1", 1, "A", state="s-todo", assignees=[ME])]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:back:a:koushyar_heidari:todo"))
    text = sent[-1][0]
    assert "Assignee: <b>Koushyar Heidari</b> · State: <b>Todo</b>" in text


# ── strict protocol for new stages ────────────────────
def test_parse_new_stages():
    p = parse_callback("pt:page:my:2")
    assert p is not None and p.stage == "page:my" and p.payload == "2"
    p = parse_callback("pt:page:a:koushyar_heidari:backlog:3")
    assert p is not None and p.stage == "page:a"
    p = parse_callback("pt:card:42:my")
    assert p is not None and p.stage == "card" and p.payload == "42:my"
    p = parse_callback("pt:back:my")
    assert p is not None and p.stage == "back" and p.payload == "my"
    # malformed
    assert parse_callback("pt:page:x:1") is None
    assert parse_callback("pt:card:") is None
    assert parse_callback("pt:back:") is None


def test_all_list_callback_data_within_64_bytes():
    """Regression: Telegram silently DROPS callback_data >64 bytes."""
    issues = [issue(f"i{n}", n, f"Card {n}", state="s-backlog", assignees=[ME]) for n in range(1, 20)]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:run:a:koushyar_heidari:backlog"))
    text, kb = sent[-1]
    for row in kb:
        for b in row:
            cd = b.get("callback_data")
            if cd:
                assert len(cd) <= 64, f"callback_data too long ({len(cd)}): {cd}"
    # 🔍 grid buttons present, compact (5 per row), using sequence_id
    card_cbs = [b["callback_data"] for row in kb for b in row
                if b.get("callback_data", "").startswith("pt:card:")]
    assert len(card_cbs) == 15  # one per visible card
    for cb in card_cbs:
        assert ":i" not in cb  # no UUID-shaped segment
    # grid rows never exceed 5 buttons
    grid_rows = [row for row in kb
                 if any(b.get("callback_data", "").startswith("pt:card:") for b in row)]
    for row in grid_rows:
        assert len(row) <= 5


# ── 5-button pagination ───────────────────────────────
def test_pagination_5_buttons_first_page():
    issues = [issue(f"i{n}", n, f"Card {n}", assignees=[ME]) for n in range(1, 40)]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:my"))
    _, kb = sent[-1]
    # find the pagination row (contains the page indicator)
    pager = None
    for row in kb:
        labels = [b.get("text", "") for b in row]
        if any("/3" in l for l in labels):
            pager = row
    assert pager is not None, "pager row missing"
    labels = [b.get("text", "") for b in pager]
    assert labels == ["⏮️", "◀️", "1/3", "▶️", "⏭️"]
    cbs = [b.get("callback_data", "") for b in pager]
    # first/prev disabled on page 1 (noop)
    assert cbs[0] == "pt:noop" and cbs[1] == "pt:noop"
    # next/last active
    assert cbs[3] == "pt:page:my:2" and cbs[4] == "pt:page:my:3"


def test_pagination_5_buttons_middle_page():
    issues = [issue(f"i{n}", n, f"Card {n}", assignees=[ME]) for n in range(1, 40)]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:page:my:2"))
    _, kb = sent[-1]
    pager = next(row for row in kb
                 if any(b.get("text", "") == "2/3" for b in row))
    labels = [b.get("text", "") for b in pager]
    assert labels == ["⏮️", "◀️", "2/3", "▶️", "⏭️"]
    cbs = [b.get("callback_data", "") for b in pager]
    assert cbs[0] == "pt:page:my:1"   # first active
    assert cbs[1] == "pt:page:my:1"   # prev active
    assert cbs[3] == "pt:page:my:3"   # next active
    assert cbs[4] == "pt:page:my:3"   # last active


def test_pagination_5_buttons_last_page():
    issues = [issue(f"i{n}", n, f"Card {n}", assignees=[ME]) for n in range(1, 40)]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:page:my:3"))
    _, kb = sent[-1]
    pager = next(row for row in kb
                 if any(b.get("text", "") == "3/3" for b in row))
    cbs = [b.get("callback_data", "") for b in pager]
    assert cbs[3] == "pt:noop" and cbs[4] == "pt:noop"  # next/last disabled


# ── clear chat ────────────────────────────────────────
def test_clear_chat_button_present():
    issues = [issue("i1", 1, "A", assignees=[ME])]
    browser, sent, _ = make_browser(issues)
    browser.handle(parse_callback("pt:my"))
    _, kb = sent[-1]
    assert any(b.get("text", "").startswith("🧹") for row in kb for b in row)


def test_clear_chat_invokes_callback():
    cleared = []
    issues = [issue("i1", 1, "A", assignees=[ME])]
    browser, _, _ = make_browser(issues)
    browser.clear_chat = lambda chat_id, answer: cleared.append((chat_id, answer))
    browser.handle(parse_callback("pt:clear"), query_id="q1",
                   ctx={"chat_id": -100, "message_id": 1})
    assert cleared and cleared[0][0] == -100
