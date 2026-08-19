"""Tests for bot.monitor.diff_issues — change classification."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.models import Change
from bot.monitor import diff_issues

ME = "user-me"

STATES = {"s-todo": "Todo", "s-done": "Done", "s-backlog": "Backlog"}
MEMBERS = {"u-me": "Koushyar Heidari", "u-fei": "feizyr", "u-ari": "arianmiramini1381"}


def issue(iid, seq, name, state="s-todo", prio="medium", assignees=None, **kw):
    d = {
        "id": iid, "sequence_id": seq, "name": name, "state_id": state,
        "priority": prio, "assignee_ids": assignees or [],
    }
    d.update(kw)
    return d


def snapshot(issues):
    return {"issues": {str(i["id"]): i for i in issues}}


def test_new_issue():
    changes = diff_issues(None, [issue("i1", 5, "New card", state="s-backlog")], STATES, MEMBERS, ME)
    assert len(changes) == 1
    c = changes[0]
    assert c.kind == "new"
    assert c.sequence_id == 5
    assert c.name == "New card"
    assert c.is_mine is False


def test_new_issue_mine():
    changes = diff_issues(None, [issue("i1", 5, "New card", assignees=[ME])], STATES, MEMBERS, ME)
    assert changes[0].is_mine is True


def test_state_changed_renders_names():
    old = snapshot([issue("i1", 5, "Card", state="s-todo")])
    new = [issue("i1", 5, "Card", state="s-done")]
    changes = diff_issues(old, new, STATES, MEMBERS, ME)
    kinds = [c.kind for c in changes]
    assert "state" in kinds
    c = next(c for c in changes if c.kind == "state")
    assert c.old == "Todo"
    assert c.new == "Done"


def test_priority_changed():
    old = snapshot([issue("i1", 5, "Card", prio="high")])
    new = [issue("i1", 5, "Card", prio="urgent")]
    changes = diff_issues(old, new, STATES, MEMBERS, ME)
    c = next(c for c in changes if c.kind == "priority")
    assert c.old == "high"
    assert c.new == "urgent"


def test_assignee_changed():
    old = snapshot([issue("i1", 5, "Card", assignees=["u-fei"])])
    new = [issue("i1", 5, "Card", assignees=["u-fei", "u-ari"])]
    changes = diff_issues(old, new, STATES, MEMBERS, ME)
    c = next(c for c in changes if c.kind == "assignees")
    assert c.extra["parts"] == ["+arianmiramini1381"]


def test_name_changed():
    old = snapshot([issue("i1", 5, "Old name")])
    new = [issue("i1", 5, "New name")]
    changes = diff_issues(old, new, STATES, MEMBERS, ME)
    assert any(c.kind == "name" for c in changes)


def test_deleted():
    old = snapshot([issue("i1", 5, "Card"), issue("i2", 6, "Other")])
    new = [issue("i2", 6, "Other")]
    changes = diff_issues(old, new, STATES, MEMBERS, ME)
    c = next(c for c in changes if c.kind == "deleted")
    assert c.issue_id == "i1"
    assert c.sequence_id == 5


def test_unchanged_issue_no_change():
    old = snapshot([issue("i1", 5, "Card", state="s-todo", prio="medium", assignees=["u-fei"])])
    new = [issue("i1", 5, "Card", state="s-todo", prio="medium", assignees=["u-fei"])]
    assert diff_issues(old, new, STATES, MEMBERS, ME) == []


def test_assignee_mine_transition():
    old = snapshot([issue("i1", 5, "Card", assignees=["u-fei"])])
    new = [issue("i1", 5, "Card", assignees=["u-fei", ME])]
    changes = diff_issues(old, new, STATES, MEMBERS, ME)
    assert any(c.is_mine for c in changes)


def test_description_change_detected():
    old = snapshot([issue("i1", 5, "Card", description_html="<p>old</p>")])
    new = [issue("i1", 5, "Card", description_html="<p>new</p>")]
    changes = diff_issues(old, new, STATES, MEMBERS, ME)
    assert any(c.kind == "description" for c in changes)


def test_description_unchanged_no_change():
    old = snapshot([issue("i1", 5, "Card", description_html="<p>same</p>")])
    new = [issue("i1", 5, "Card", description_html="<p>same</p>")]
    assert diff_issues(old, new, STATES, MEMBERS, ME) == []


def test_new_card_carries_full_details():
    new_issue = issue("i1", 5, "New card", state="s-backlog", prio="high",
                      assignees=["u-fei"], created_by="u-fei",
                      created_at="2026-08-18T10:13:00Z")
    changes = diff_issues(None, [new_issue], STATES, MEMBERS, ME)
    c = changes[0]
    assert c.kind == "new"
    assert c.new == "Backlog"          # state name
    assert c.old == "high"             # priority
    assert c.extra["assignees"] == "feizyr"  # names resolved via members
