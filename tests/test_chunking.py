"""Tests for bot.messages — chunking, keyboards, report rendering."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.messages import build_keyboard, build_report, chunk_text
from bot.models import Change

ME = "user-me"


def ch(kind="new", seq=5, name="Card", **kw):
    return Change(issue_id=f"i{seq}", sequence_id=seq, name=name, kind=kind, **kw)


# ── chunking ──────────────────────────────────────────
def test_chunk_small_text():
    assert chunk_text("hello") == ["hello"]


def test_chunk_splits_on_line_boundaries():
    text = "\n".join(f"line {i} " + "x" * 100 for i in range(100))
    chunks = chunk_text(text, 500)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 500
    assert "".join(chunks) == text  # content preserved


def test_chunk_single_long_line():
    text = "y" * 9000
    chunks = chunk_text(text, 4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == text


# ── keyboards ─────────────────────────────────────────
def test_keyboard_shape():
    kb = build_keyboard([[{"text": "a", "callback_data": "pt:start:assignee"}]])
    assert all(isinstance(row, list) for row in kb["inline_keyboard"])


def test_keyboard_rejects_flat_row():
    with pytest.raises(ValueError):
        build_keyboard([{"text": "a", "callback_data": "x"}])


def test_keyboard_rejects_long_callback():
    with pytest.raises(ValueError):
        build_keyboard([[{"text": "a", "callback_data": "p" * 65}]])


# ── report ────────────────────────────────────────────
def test_report_empty_changes_returns_none():
    assert build_report([], focus="mine") is None


def test_report_contains_header():
    c = ch()
    c.is_mine = True
    r = build_report([c], fetched_at="2026-08-18 09:10")
    assert r is not None
    text, _ = r
    assert "📋 <b>Plane Monitor</b> — 2026-08-18 09:10" in text


def test_report_new_card_shows_details():
    from bot.monitor import diff_issues
    # build a real "new" Change with full details via the diff engine
    new_issue = {"id": "i9", "sequence_id": 9, "name": "New card",
                 "state_id": "s-backlog", "priority": "high",
                 "assignee_ids": ["u-fei"], "created_by": "u-fei",
                 "created_at": "2026-08-18T10:13:00Z"}
    changes = diff_issues(None, [new_issue],
                          states={"s-backlog": "Backlog"},
                          members={"u-fei": "feizyr"}, me=ME)
    c = changes[0]
    c.is_mine = True
    r = build_report([c])
    text, _ = r
    assert "Status: Backlog" in text
    assert "Priority: high" in text
    assert "Assignees: feizyr" in text
    assert "Status: New" not in text  # real details, not the placeholder


def test_report_my_section_only_for_mine():
    mine = ch(kind="state", old="Todo", new="Done")
    mine.is_mine = True
    other = ch(seq=6, name="Other", kind="new")
    other.is_mine = False
    r = build_report([mine, other], focus="mine")
    text, _ = r
    assert "MY ASSIGNED CARDS" in text
    assert "OTHER CARDS" not in text  # dropped in mine-focus


def test_report_all_focus_includes_other_section():
    mine = ch(kind="new")
    mine.is_mine = True
    other = ch(seq=6, name="Other", kind="new")
    other.is_mine = False
    r = build_report([mine, other], focus="all")
    text, _ = r
    assert "MY ASSIGNED CARDS" in text
    assert "OTHER CARDS" in text


def test_report_escapes_html():
    c = ch(name='<script>"x"</script>')
    c.is_mine = True  # mine-focus default requires a mine card
    r = build_report([c])
    text, _ = r
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_report_keyboard_rows_all_lists():
    c = ch(seq=5, name="Card")
    c.issue_id = "abc123"
    c.is_mine = True
    r = build_report([c], base_url="https://p.test", workspace="tms", project_id="proj")
    _, rows = r
    assert all(isinstance(row, list) for row in rows)
    assert any(btn.get("text") == "Card 5" for row in rows for btn in row)
    assert any(btn.get("text") == "🔗 Open project" for row in rows for btn in row)


def test_report_card_buttons_capped_at_12():
    changes = [ch(seq=i, name=f"Card {i}") for i in range(20)]
    for c in changes:
        c.issue_id = f"id-{c.sequence_id}"
        c.is_mine = True
    r = build_report(changes, base_url="https://p.test", workspace="tms", project_id="p")
    _, rows = r
    card_btns = [b for row in rows for b in row if str(b.get("text", "")).startswith("Card ")]
    assert len(card_btns) <= 12
