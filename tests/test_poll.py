"""Integration-style tests for the poll loop (fake clients, real state file)."""
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import Settings
from bot.monitor import PlaneAuthError, run_poll_once
from bot.plane_client import PlaneClient
from bot.telegram_client import TelegramClient

ME = "user-me"
BASE = "https://plane.test"


def issue(iid, seq, name, state="s-todo", prio="medium", assignees=None):
    return {
        "id": iid, "sequence_id": seq, "name": name, "state_id": state,
        "priority": prio, "assignee_ids": assignees or [],
    }


def build_clients(issue_handler, sent_messages):
    """issue_handler receives the issue-batch list to return; states/members are static."""
    states = {"s-todo": "Todo", "s-done": "Done", "s-backlog": "Backlog"}
    members = {ME: "Koushyar Heidari", "u-fei": "feizyr"}

    def router(request):
        url = str(request.url)
        if "/states/" in url:
            return httpx.Response(200, json={"results": [
                {"id": k, "name": v} for k, v in states.items()]})
        if "/members/" in url:
            return httpx.Response(200, json={"results": [
                {"member": {"id": k, "display_name": v}} for k, v in members.items()]})
        if "/issues/" in url:
            return httpx.Response(200, json=issue_handler())
        return httpx.Response(404, json={})

    settings = Settings(
        plane_base_url=BASE, plane_workspace="tms", plane_project_id="proj",
        plane_csrf_token="c", plane_session_id="s", plane_user_id=ME,
        plane_focus="mine", state_file="./test_state.json",
    )
    plane = PlaneClient(settings, transport=httpx.MockTransport(router))

    def tg_handler(request):
        from urllib.parse import parse_qs
        form = parse_qs(request.read().decode())
        sent_messages.append(form.get("text", [""])[0])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent_messages)}})

    tg = TelegramClient("t:tok", proxy="", chat_id="-100")
    tg._transport = httpx.MockTransport(tg_handler)
    return settings, plane, tg


def test_first_run_baselines_silently(tmp_path):
    sent = []
    state_path = str(tmp_path / "state.json")
    settings, plane, tg = build_clients(
        lambda: [issue("i1", 5, "Card", assignees=[ME])], sent,
    )
    result = run_poll_once(plane, tg, settings, state_path)
    assert result is None  # silent baseline
    assert sent == []  # nothing posted
    assert os.path.exists(state_path)  # snapshot saved


def test_second_run_reports_only_changes(tmp_path):
    sent = []
    state_path = str(tmp_path / "state.json")
    batches = iter([
        [issue("i1", 5, "Card", state="s-todo", assignees=[ME])],
        [issue("i1", 5, "Card", state="s-done", assignees=[ME])],
    ])
    settings, plane, tg = build_clients(lambda: next(batches), sent)
    run_poll_once(plane, tg, settings, state_path)  # baseline
    result = run_poll_once(plane, tg, settings, state_path)
    assert result is not None
    assert len(sent) == 1
    assert "Todo → Done" in sent[0]


def test_no_change_second_run_is_silent(tmp_path):
    sent = []
    state_path = str(tmp_path / "state.json")
    settings, plane, tg = build_clients(
        lambda: [issue("i1", 5, "Card", assignees=[ME])], sent,
    )
    run_poll_once(plane, tg, settings, state_path)
    run_poll_once(plane, tg, settings, state_path)
    assert sent == []


def test_auth_error_propagates(tmp_path):
    sent = []
    state_path = str(tmp_path / "state.json")

    def bad_router(request):
        return httpx.Response(401, json={"detail": "no"})

    settings = Settings(
        plane_base_url=BASE, plane_workspace="tms", plane_project_id="proj",
        plane_csrf_token="c", plane_session_id="s", plane_user_id=ME,
        plane_focus="mine", state_file="./test_state.json",
    )
    plane = PlaneClient(settings, transport=httpx.MockTransport(bad_router))
    tg = TelegramClient("t:tok", proxy="", chat_id="-100")
    try:
        run_poll_once(plane, tg, settings, str(tmp_path / "state.json"))
        assert False, "should raise PlaneAuthError"
    except PlaneAuthError:
        pass


def test_focus_mine_omits_other_cards(tmp_path):
    sent = []
    state_path = str(tmp_path / "state.json")
    batches = iter([
        [issue("i1", 1, "Mine", assignees=[ME]), issue("i2", 2, "Other")],
        [issue("i1", 1, "Mine", state="s-done", assignees=[ME]), issue("i2", 2, "Other")],
    ])
    settings, plane, tg = build_clients(lambda: next(batches), sent)
    run_poll_once(plane, tg, settings, state_path)
    run_poll_once(plane, tg, settings, state_path)
    text = "".join(sent)
    assert "Mine" in text
    assert "Other" not in text  # other-card change hidden in mine-focus


def test_state_not_advanced_when_delivery_fails(tmp_path):
    """At-least-once: if send_chunked raises, the snapshot must NOT advance,
    so the next poll re-detects the change and retries."""
    import json as _json
    from bot.telegram_client import TelegramError

    state_path = str(tmp_path / "state.json")
    batches = iter([
        [issue("i1", 5, "Card", state="s-todo", assignees=[ME])],
        [issue("i1", 5, "Card", state="s-done", assignees=[ME])],
        [issue("i1", 5, "Card", state="s-done", assignees=[ME])],
    ])
    sent = []
    settings, plane, tg = build_clients(lambda: next(batches), sent)

    # baseline (delivery not involved)
    run_poll_once(plane, tg, settings, state_path)
    before = _json.load(open(state_path))
    assert before["issues"]["i1"]["state_id"] == "s-todo"

    # inject a delivery failure on the next send
    orig_send = tg.send_chunked

    def failing_send(*a, **kw):
        raise TelegramError("delivery down")

    tg.send_chunked = failing_send
    try:
        run_poll_once(plane, tg, settings, state_path)
    except TelegramError:
        pass
    finally:
        tg.send_chunked = orig_send

    # state must still be the OLD snapshot → change not yet acknowledged
    after = _json.load(open(state_path))
    assert after["issues"]["i1"]["state_id"] == "s-todo"

    # next poll (delivery healthy) re-detects the change and posts
    result = run_poll_once(plane, tg, settings, state_path)
    assert result is not None
    assert "Todo → Done" in result


# ── wake never lost (regression: debounce window swallowing kicks) ──
def test_wake_during_debounce_never_lost():
    """A kick arriving inside the debounce window must trigger a run as soon
    as the cooldown expires — never be swallowed by a full-interval wait."""
    import time as _time
    import threading
    from bot.monitor import PollLoop

    class FakeClient:
        def get_states(self): return {}
        def get_members(self): return {}
        def get_issues(self): return []

    class FakeTG:
        def send_message(self, *a, **k): pass
        def send_chunked(self, *a, **k): return []

    settings = Settings(
        plane_base_url="https://x", plane_workspace="w", plane_project_id="p",
        plane_csrf_token="c", plane_session_id="s", plane_user_id="u",
        poll_interval_seconds=300, webhook_min_interval_seconds=0.2,
    )
    loop = PollLoop(FakeClient(), FakeTG(), settings)
    cycles = []
    loop._run_cycle = lambda: cycles.append(_time.monotonic())

    t = threading.Thread(target=loop.run, daemon=True)
    t.start()
    _time.sleep(0.3)          # first scheduled cycle runs
    _time.sleep(0.3)
    n0 = len(cycles)
    loop.kick()               # wake during... cooldown or wait
    _time.sleep(0.5)          # debounce is 0.2s → kick fires quickly
    n1 = len(cycles)
    loop.stop()
    t.join(timeout=2)
    # the kick must have produced a cycle quickly (not swallowed for 300s)
    assert n1 >= n0 + 1, f"kick swallowed: cycles {n0} → {n1}"
