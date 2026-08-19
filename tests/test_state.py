"""Tests for bot.state."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.state import load_state, save_state


def test_roundtrip(tmp_path):
    p = str(tmp_path / "state.json")
    data = {"issues": {"1": {"sequence_id": 5}}, "_fetched_at": "2026-08-18T09:10:00Z"}
    save_state(p, data)
    assert load_state(p) == data


def test_missing_file_returns_none(tmp_path):
    assert load_state(str(tmp_path / "nope.json")) is None


def test_corrupt_file_returns_none(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{ not json")
    assert load_state(str(p)) is None


def test_atomic_write_no_tmp_left(tmp_path):
    p = str(tmp_path / "state.json")
    save_state(p, {"a": 1})
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == []
    # file is readable mid-write guarantee: plain read works
    with open(p, encoding="utf-8") as f:
        assert json.load(f) == {"a": 1}


def test_creates_parent_dirs(tmp_path):
    p = str(tmp_path / "deep" / "nested" / "state.json")
    save_state(p, {"x": 1})
    assert load_state(p) == {"x": 1}
