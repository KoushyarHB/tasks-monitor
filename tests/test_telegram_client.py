"""Tests for bot.telegram_client."""
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.telegram_client import TelegramClient, TelegramError


def make_client(handler):
    c = TelegramClient(token="test:token", proxy="", chat_id="-100")
    c._transport = httpx.MockTransport(handler)
    return c


def test_send_message_posts_html_and_markup():
    captured = {}

    def handler(request):
        captured["body"] = dict(request.read().decode().split("&")[0:0]) if False else None
        # parse urlencoded body
        from urllib.parse import parse_qs
        captured["form"] = parse_qs(request.read().decode())
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    c = make_client(handler)
    c.send_message("Hello <b>x</b>", keyboard=[[{"text": "A", "callback_data": "pt:my"}]])
    assert captured["form"]["parse_mode"][0] == "HTML"
    kb = json.loads(captured["form"]["reply_markup"][0])
    assert kb["inline_keyboard"][0][0]["callback_data"] == "pt:my"
    assert captured["form"]["chat_id"][0] == "-100"


def test_error_not_swallowed():
    def handler(request):
        return httpx.Response(400, json={"ok": False, "error_code": 400, "description": "Bad Request: expected an Array of InlineKeyboardButton"})

    c = make_client(handler)
    with pytest.raises(TelegramError) as ei:
        c.send_message("x")
    assert "InlineKeyboardButton" in str(ei.value)


def test_answer_callback():
    captured = {}

    def handler(request):
        from urllib.parse import parse_qs
        captured["form"] = parse_qs(request.read().decode())
        return httpx.Response(200, json={"ok": True, "result": True})

    c = make_client(handler)
    c.answer_callback("qid123", "✓ done")
    assert captured["form"]["callback_query_id"][0] == "qid123"
    assert captured["form"]["text"][0] == "✓ done"


def test_pin_message():
    captured = {}

    def handler(request):
        from urllib.parse import parse_qs
        captured["form"] = parse_qs(request.read().decode())
        return httpx.Response(200, json={"ok": True, "result": True})

    c = make_client(handler)
    c.pin_message("-100", 42)
    assert captured["form"]["message_id"][0] == "42"


def test_send_chunked_buttons_only_last():
    sent = []

    def handler(request):
        from urllib.parse import parse_qs
        form = parse_qs(request.read().decode())
        sent.append(form)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    c = make_client(handler)
    big = "\n".join(f"line {i} " + "x" * 200 for i in range(30))
    c.send_chunked(big, keyboard=[[{"text": "Nav", "callback_data": "pt:help"}]])
    assert len(sent) > 1
    for i, form in enumerate(sent):
        if i < len(sent) - 1:
            assert "reply_markup" not in form
        else:
            assert "reply_markup" in form


def test_get_updates_allowed_types():
    captured = {}

    def handler(request):
        from urllib.parse import parse_qs
        captured["form"] = parse_qs(request.read().decode())
        return httpx.Response(200, json={"ok": True, "result": []})

    c = make_client(handler)
    c.get_updates(offset=5)
    assert captured["form"]["offset"][0] == "5"
    allowed = json.loads(captured["form"]["allowed_updates"][0])
    assert "callback_query" in allowed
