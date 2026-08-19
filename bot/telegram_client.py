"""Telegram Bot API client — sendMessage, callbacks, pinning, long-poll."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .messages import chunk_text

logger = logging.getLogger(__name__)

MAX_MSG = 4096


class TelegramError(Exception):
    """Raised on non-ok Telegram API responses (NEVER swallowed silently)."""


class TelegramClient:
    def __init__(self, token: str, proxy: str = "", chat_id: str = "", timeout: float = 40.0):
        self.token = token
        self.proxy = proxy
        self.chat_id = chat_id
        self.timeout = timeout
        self._transport = None  # injectable for tests

    # ── low-level ─────────────────────────────────────
    def _api(self, method: str, **params) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        client_kw: dict[str, Any] = {"timeout": self.timeout}
        if self.proxy:
            client_kw["proxy"] = self.proxy
        if self._transport:
            client_kw["transport"] = self._transport
        try:
            with httpx.Client(**client_kw) as client:
                r = client.post(url, data=params)
        except httpx.HTTPError as exc:
            raise TelegramError(f"Telegram transport error: {exc}") from exc
        try:
            body = r.json()
        except ValueError as exc:
            raise TelegramError(f"Telegram invalid JSON ({r.status_code})") from exc
        if not body.get("ok"):
            desc = body.get("description", "unknown error")
            code = body.get("error_code", r.status_code)
            raise TelegramError(f"Telegram API error {code}: {desc}")
        return body.get("result", {})

    # ── sends ─────────────────────────────────────────
    def send_message(
        self,
        text: str,
        chat_id: str | None = None,
        keyboard: list[list[dict[str, str]]] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        }
        if keyboard is not None:
            params["reply_markup"] = json.dumps({"inline_keyboard": keyboard}, ensure_ascii=False)
        return self._api("sendMessage", **params)

    def send_chunked(
        self,
        text: str,
        chat_id: str | None = None,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> list[dict[str, Any]]:
        """Send text in ≤4096-char chunks; keyboard only on the LAST chunk."""
        parts = chunk_text(text, MAX_MSG)
        sent = []
        for i, part in enumerate(parts):
            kb = keyboard if i == len(parts) - 1 else None
            sent.append(self.send_message(part, chat_id=chat_id, keyboard=kb))
        return sent

    def answer_callback(self, query_id: str, text: str = "") -> None:
        params = {"callback_query_id": query_id}
        if text:
            params["text"] = text
        self._api("answerCallbackQuery", **params)

    def pin_message(self, chat_id: str | None, message_id: int | str) -> None:
        self._api("pinChatMessage", chat_id=chat_id or self.chat_id, message_id=str(message_id))

    def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        self._api("setMyCommands", commands=json.dumps(commands, ensure_ascii=False))

    def get_me(self) -> dict[str, Any]:
        return self._api("getMe")

    # ── polling ───────────────────────────────────────
    def get_updates(self, offset: int | None = None, timeout: int = 50) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": str(timeout),
            "allowed_updates": json.dumps(["message", "callback_query", "channel_post"]),
        }
        if offset is not None:
            params["offset"] = str(offset)
        result = self._api("getUpdates", **params)
        return result if isinstance(result, list) else []
