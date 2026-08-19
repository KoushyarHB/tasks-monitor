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
