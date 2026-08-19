"""Telegram Bot API client — sendMessage, callbacks, pinning, long-poll."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

from .messages import chunk_text

logger = logging.getLogger(__name__)

MAX_MSG = 4096


class TelegramError(Exception):
    """Raised on non-ok Telegram API responses (NEVER swallowed silently)."""

    def __init__(self, message: str, status_code: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class TelegramRateLimitError(TelegramError):
    """Raised on HTTP 429 — carries Retry-After seconds for backoff."""


class TelegramClient:
    MSG_IDS_PATH = os.path.expanduser("~/.hermes/state/standalone_msg_ids.json")

    def __init__(self, token: str, proxy: str = "", chat_id: str = "", timeout: float = 40.0):
        self.token = token
        self.proxy = proxy
        self.chat_id = chat_id
        self.timeout = timeout
        self._transport = None  # injectable for tests

    # ── message-id tracking (for 🧹 clear chat) ───────
    def record_message(self, message_id: int) -> None:
        """Persist a message id so 🧹 clear can delete it later.

        Every send path calls this — nothing the bot posts is ever lost.
        """
        try:
            os.makedirs(os.path.dirname(self.MSG_IDS_PATH), exist_ok=True)
            ids: list[int] = []
            if os.path.exists(self.MSG_IDS_PATH):
                try:
                    with open(self.MSG_IDS_PATH) as f:
                        ids = json.load(f)
                except Exception:
                    ids = []
            if message_id not in ids:
                ids.append(message_id)
            with open(self.MSG_IDS_PATH, "w") as f:
                json.dump(ids[-2000:], f)  # keep last 2000
        except Exception:
            pass

    def tracked_ids(self) -> list[int]:
        try:
            with open(self.MSG_IDS_PATH) as f:
                return json.load(f)
        except Exception:
            return []

    def prune_ids(self, keep: list[int]) -> None:
        """Rewrite the file keeping only the given ids (e.g. pinned)."""
        try:
            with open(self.MSG_IDS_PATH, "w") as f:
                json.dump(keep[-2000:], f)
        except Exception:
            pass

    # ── low-level ─────────────────────────────────────
    def _api(self, method: str, max_retries: int = 3, **params) -> dict[str, Any]:
        """Call the Bot API with 429 Retry-After + transient 5xx backoff."""
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        client_kw: dict[str, Any] = {"timeout": self.timeout}
        if self.proxy:
            client_kw["proxy"] = self.proxy
        if self._transport:
            client_kw["transport"] = self._transport

        attempt = 0
        while True:
            attempt += 1
            try:
                with httpx.Client(**client_kw) as client:
                    r = client.post(url, data=params)
            except httpx.HTTPError as exc:
                raise TelegramError(f"Telegram transport error: {exc}") from exc
            if r.status_code == 429:
                retry_after = 0
                try:
                    retry_after = int(r.headers.get("Retry-After", "0"))
                except ValueError:
                    retry_after = 0
                if attempt < max_retries:
                    time.sleep(min(retry_after, 30) if retry_after else 2 * attempt)
                    continue
                raise TelegramRateLimitError(
                    f"Telegram rate limited (429) after {attempt} attempts",
                    status_code=429, retry_after=retry_after,
                )
            if r.status_code >= 500 and attempt < max_retries:
                time.sleep(2 * attempt)
                continue
            try:
                body = r.json()
            except ValueError as exc:
                raise TelegramError(f"Telegram invalid JSON ({r.status_code})") from exc
            if not body.get("ok"):
                desc = body.get("description", "unknown error")
                code = body.get("error_code", r.status_code)
                raise TelegramError(f"Telegram API error {code}: {desc}", status_code=code)
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
        result = self._api("sendMessage", **params)
        mid = result.get("message_id")
        if mid:
            self.record_message(int(mid))
        return result

    def edit_message(
        self,
        text: str,
        chat_id: str,
        message_id: int | str,
        keyboard: list[list[dict[str, str]]] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        """Replace an existing message (used for the task-detail pop-up)."""
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": str(message_id),
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        }
        if keyboard is not None:
            params["reply_markup"] = json.dumps({"inline_keyboard": keyboard}, ensure_ascii=False)
        return self._api("editMessageText", **params)

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

    def delete_message(self, chat_id: str, message_id: int | str) -> None:
        """Delete a message (bot needs delete permission in the chat)."""
        self._api("deleteMessage", chat_id=chat_id, message_id=str(message_id))

    def get_pinned_message_id(self, chat_id: str | None = None) -> int | None:
        """Return the pinned message id of the chat, or None."""
        try:
            chat = self._api("getChat", chat_id=chat_id or self.chat_id)
            pm = chat.get("pinned_message") or {}
            return pm.get("message_id")
        except TelegramError:
            return None

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
