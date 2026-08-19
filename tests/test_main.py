"""Tests for bot.main — update dispatch routing (asserts REAL sends)."""
import os
import sys
from urllib.parse import parse_qs

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.main import App
from bot.config import Settings

ME = "user-me"
BASE = "https://plane.test"
STATES = {"s-todo": "Todo", "s-backlog": "Backlog"}
MEMBERS = {ME: "Koushyar Heidari"}


def issue(iid, seq, name, state="s-todo", assignees=None):
    return {"id": iid, "sequence_id": seq, "name": name, "state_id": state,
            "priority": "high", "assignee_ids": assignees or []}


def make_app(issues):
    """Returns (app, sent_messages, answered_callbacks). sent_messages is a list
    of (method, form-dict) tuples captured from the mocked Telegram API."""
    sent: list[tuple[str, dict]] = []
    answered: list[tuple[str, str]] = []

    def router(request):
        url = str(request.url)
        if "/states/" in url:
            return httpx.Response(200, json={"results": [{"id": k, "name": v} for k, v in STATES.items()]})
        if "/members/" in url:
            return httpx.Response(200, json={"results": [{"member": {"id": k, "display_name": v}} for k, v in MEMBERS.items()]})
        if "/issues/" in url:
            return httpx.Response(200, json={"results": issues})
        method = url.rstrip("/").rsplit("/", 1)[-1]
        form = parse_qs(request.read().decode()) if request.content else {}
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "koush_yar_bot"}})
        if method == "setMyCommands":
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "sendMessage":
            sent.append((method, form))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})
        if method == "pinChatMessage":
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "answerCallbackQuery":
            answered.append((form.get("callback_query_id", [""])[0], form.get("text", [""])[0]))
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(404, json={})

    settings = Settings(
        plane_base_url=BASE, plane_workspace="tms", plane_project_id="proj",
        plane_csrf_token="c", plane_session_id="s", plane_user_id=ME, plane_focus="mine",
        tg_bot_token="t:tok", tg_chat_id="-100", tg_proxy="",
    )
    app = App(settings)
    app.plane._transport = httpx.MockTransport(router)
    app.tg._transport = httpx.MockTransport(router)
    return app, sent, answered


def test_slash_assignee_sends_picker():
    app, sent, answered = make_app([issue("i1", 1, "A")])
    app.dispatch({"message": {"text": "/task_by_assignee", "chat": {"id": -100}}})
    assert len(sent) == 1
    text = sent[0][1].get("text", [""])[0]
    assert "Pick an assignee" in text
    # keyboard has buttons + All
    import json as _json
    kb = _json.loads(sent[0][1].get("reply_markup", ["{}"])[0])
    assert any(b.get("callback_data") == "pt:pick:assignee:all"
               for row in kb["inline_keyboard"] for b in row)
    # slash command must NOT answer a callback (no query id)
    assert answered == []


def test_slash_my_tasks_sends_list():
    app, sent, answered = make_app([issue("i1", 1, "Mine", assignees=[ME])])
    app.dispatch({"message": {"text": "/my_tasks", "chat": {"id": -100}}})
    assert len(sent) == 1
    text = sent[0][1].get("text", [""])[0]
    assert "My Tasks" in text
    assert "Mine" in text


def test_callback_run_dispatches_and_answers():
    app, sent, answered = make_app([issue("i1", 1, "Mine", assignees=[ME])])
    app.dispatch({"callback_query": {"id": "q1", "data": "pt:run:a:koushyar_heidari:todo"}})
    assert len(sent) == 1
    text = sent[0][1].get("text", [""])[0]
    assert "Mine" in text
    # callback MUST be answered with the real query id
    assert any(qid == "q1" for qid, _ in answered)


def test_callback_unknown_answered_with_toast():
    app, sent, answered = make_app([])
    app.dispatch({"callback_query": {"id": "q9", "data": "pt:bogus"}})
    assert answered and answered[0][0] == "q9"
    assert "Unknown" in answered[0][1]


def test_self_check_passes_with_live_mocks():
    app, _, _ = make_app([issue("i1", 1, "A")])
    assert app.self_check() is True


def test_self_check_fails_on_plane_auth():
    def bad_router(request):
        url = str(request.url)
        if "/issues/" in url:
            return httpx.Response(401, json={})
        if url.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "bot"}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    settings = Settings(
        plane_base_url=BASE, plane_workspace="tms", plane_project_id="proj",
        plane_csrf_token="c", plane_session_id="s", plane_user_id=ME, plane_focus="mine",
        tg_bot_token="t:tok", tg_chat_id="-100", tg_proxy="",
    )
    app = App(settings)
    app.plane._transport = httpx.MockTransport(bad_router)
    app.tg._transport = httpx.MockTransport(bad_router)
    assert app.self_check() is False
