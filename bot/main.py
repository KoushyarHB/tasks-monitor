"""Entrypoint — startup self-check, both loops (poll + update), graceful shutdown."""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time

from .browser import Browser, parse_callback
from .config import Settings
from .monitor import PollLoop
from .plane_client import PlaneAuthError, PlaneClient
from .telegram_client import TelegramClient, TelegramError

logger = logging.getLogger(__name__)

SLASH_COMMANDS = [
    {"command": "task_by_assignee", "description": "Tasks by assignee (pick assignee → state)"},
    {"command": "task_by_state", "description": "Tasks by state (pick state → assignee)"},
    {"command": "my_tasks", "description": "My assigned tasks"},
]


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.plane = PlaneClient(settings)
        self.tg = TelegramClient(settings.tg_bot_token, proxy=settings.tg_proxy, chat_id=settings.tg_chat_id)
        self.poll_loop = PollLoop(self.plane, self.tg, settings)
        self.stop = False
        self._browser: Browser | None = None

    @property
    def browser(self) -> Browser:
        if self._browser is None:
            self._browser = Browser(
                self.plane, self.settings,
                send=lambda text, kb: self._send_recorded(text, kb),
                answer=lambda qid, toast: self._safe_answer(qid, toast),
                edit=lambda chat_id, mid, text, kb: self.tg.edit_message(text, chat_id, mid, keyboard=kb),
                clear_chat=self._clear_chat,
            )
        return self._browser

    # ── message tracking (for 🧹 clear chat) ──────────
    def _send_recorded(self, text: str, kb) -> None:
        # send_chunked → send_message → record_message: ids tracked internally
        self.tg.send_chunked(text, keyboard=kb)

    def _clear_chat(self, chat_id, answer) -> None:
        """Delete EVERY non-pinned message the bot has seen in the chat.

        Ids are recorded on every send path (send_message/send_chunked) plus
        channel posts observed in getUpdates — so this clears watchdog
        reports, browser lists, and user messages alike. Only the pinned
        message (commands menu) survives; its id stays tracked so future
        clears still skip it.
        """
        pinned = self._load_menu_id() or self.tg.get_pinned_message_id(chat_id)
        ids = self.tg.tracked_ids()
        deleted = 0
        failures = 0
        for mid in ids:
            if mid == pinned:
                continue
            try:
                self.tg.delete_message(chat_id, mid)
                deleted += 1
            except TelegramError as exc:
                failures += 1
                logger.debug("clear: could not delete %s: %s", mid, exc)
        # keep only the menu id (or nothing) so old ids don't accumulate
        self.tg.prune_ids([pinned] if pinned else [])
        logger.info("clear chat: deleted %d, failed %d", deleted, failures)
        # ALWAYS re-ensure the pinned menu exists — if the previous pin was
        # itself deleted (e.g. manual history clear), a fresh menu is posted
        # and pinned so the channel is never left menu-less.
        menu_id = self.ensure_pinned_menu()
        toast = f"🧹 Cleared {deleted} messages (pinned kept)"
        if failures:
            toast += f" · {failures} skipped (too old / system)"
        if menu_id is not None:
            toast += f" · 📌 menu #{menu_id}"
        try:
            answer("", toast)
        except Exception:
            pass

    def _safe_answer(self, qid: str, toast: str) -> None:
        try:
            self.tg.answer_callback(qid, toast)
        except TelegramError as exc:
            logger.warning("callback answer failed (may be stale): %s", exc)

    # ── startup ───────────────────────────────────────
    def self_check(self) -> bool:
        """Verify Plane + Telegram connectivity. Returns False on fatal failure."""
        try:
            ok = self.plane.check_auth()
            if not ok:
                logger.error("Plane auth failed at startup (session rejected)")
                return False
            logger.info("✓ plane connected")
        except PlaneAuthError as exc:
            logger.error("Plane auth failed at startup: %s", exc)
            return False
        try:
            me = self.tg.get_me()
            logger.info("✓ telegram connected: @%s", me.get("username", "?"))
        except TelegramError as exc:
            logger.error("Telegram unreachable at startup: %s", exc)
            return False
        return True

    def setup_telegram(self) -> None:
        """setMyCommands + ensure the commands menu is pinned."""
        try:
            self.tg.set_my_commands(SLASH_COMMANDS)
            logger.info("✓ slash commands registered")
        except TelegramError as exc:
            logger.warning("setMyCommands failed: %s", exc)
        self.ensure_pinned_menu()

    MENU_ID_PATH = os.path.expanduser("~/.hermes/state/standalone_menu_id.json")

    def _load_menu_id(self) -> int | None:
        try:
            with open(self.MENU_ID_PATH) as f:
                return int(json.load(f))
        except Exception:
            return None

    def _save_menu_id(self, mid: int) -> None:
        try:
            os.makedirs(os.path.dirname(self.MENU_ID_PATH), exist_ok=True)
            with open(self.MENU_ID_PATH, "w") as f:
                json.dump(mid, f)
        except Exception:
            pass

    def ensure_pinned_menu(self) -> int | None:
        """Guarantee the commands menu is pinned — WITHOUT churning the pin.

        Priority:
          1. Persisted menu id still exists → re-pin THE SAME message.
          2. getChat reports a pinned message → adopt it.
          3. Neither → post a fresh menu, pin it, persist its id.

        The persisted id is the source of truth: even if getChat transiently
        returns no pin (network hiccup), we never delete or replace the menu.
        """
        menu_id = self._load_menu_id()
        if menu_id is not None:
            try:
                self.tg.pin_message(None, menu_id)  # idempotent re-pin
                logger.info("✓ commands menu pinned (id %s)", menu_id)
                return menu_id
            except TelegramError:
                logger.info("menu %s gone — will recreate", menu_id)
                menu_id = None
        try:
            existing = self.tg.get_pinned_message_id(None)
            if existing is not None:
                self._save_menu_id(existing)
                logger.info("✓ commands menu pinned (id %s)", existing)
                return existing
        except TelegramError:
            pass
        try:
            text = (
                "🤖 <b>Plane Monitor — Commands</b>\n\n"
                "🔹 /task_by_assignee — by assignee, then state\n"
                "🔹 /task_by_state — by state, then assignee\n"
                "🔹 /my_tasks — your cards\n\n"
                "Tap the buttons below to browse ⬇️"
            )
            kb = [
                [{"text": "👤 By Assignee", "callback_data": "pt:start:assignee"},
                 {"text": "🗂 By State", "callback_data": "pt:start:state"}],
                [{"text": "🟦 My Tasks", "callback_data": "pt:my"},
                 {"text": "❓ Help", "callback_data": "pt:help"}],
                [{"text": "🧹 Clear chat (keep pinned)", "callback_data": "pt:clear"}],
            ]
            res = self.tg.send_message(text, keyboard=kb)
            mid = int(res["message_id"])
            try:
                self.tg.pin_message(None, mid)
                self._save_menu_id(mid)
                logger.info("✓ commands menu pinned (id %s)", mid)
            except TelegramError as exc:
                logger.info("pin skipped (may already be pinned): %s", exc)
            return mid
        except TelegramError as exc:
            logger.warning("pinned menu failed: %s", exc)
            return None

    # ── update loop ───────────────────────────────────
    def dispatch(self, update: dict) -> None:
        """Route one Telegram update to the right handler."""
        if "callback_query" in update:
            cb = update["callback_query"]
            data = cb.get("data", "")
            qid = cb.get("id", "")
            parsed = parse_callback(data)
            if parsed is None:
                self._safe_answer(qid, "Unknown action")
                return
            # context for pop-up editing (editMessageText needs chat + message id)
            msg = cb.get("message") or {}
            ctx = {
                "chat_id": msg.get("chat", {}).get("id"),
                "message_id": msg.get("message_id"),
            } if msg.get("message_id") else {}
            # record the tapped message's id so 🧹 can delete old lists too
            if msg.get("message_id") and msg.get("chat", {}).get("id") == self.settings.tg_chat_id:
                self.tg.record_message(int(msg["message_id"]))
            try:
                self.browser.handle(parsed, qid, ctx=ctx)
            except TelegramError as exc:
                logger.error("browser handler delivery error: %s", exc)
            except Exception as exc:
                logger.exception("browser handler crashed: %s", exc)
                self._safe_answer(qid, "❌ error")
            return

        # messages / channel posts
        msg = update.get("message") or update.get("channel_post") or {}
        if msg.get("message_id") and msg.get("chat", {}).get("id") == self.settings.tg_chat_id:
            self.tg.record_message(int(msg["message_id"]))
        text = msg.get("text", "")
        if text.startswith("/"):
            cmd = text.split()[0].lstrip("/").replace("-", "_")
            self._slash(cmd, update)

    def _slash(self, cmd: str, update: dict) -> None:
        if cmd in ("task_by_assignee", "task_by_assignee@koush_yar_bot"):
            self.browser.handle(parse_callback("pt:start:assignee"))
        elif cmd in ("task_by_state", "task_by_state@koush_yar_bot"):
            self.browser.handle(parse_callback("pt:start:state"))
        elif cmd in ("my_tasks", "my_tasks@koush_yar_bot"):
            self.browser.handle(parse_callback("pt:my"))
        elif cmd in ("help", "commands"):
            self.browser.handle(parse_callback("pt:help"))
        else:
            logger.info("unknown command: %s", cmd)

    def run_update_loop(self) -> None:
        offset = 0
        while not self.stop:
            try:
                updates = self.tg.get_updates(offset=offset)
            except TelegramError as exc:
                logger.warning("getUpdates error (retrying): %s", exc)
                time.sleep(3)
                continue
            for upd in updates:
                u_id = upd.get("update_id")
                if u_id is not None and u_id >= offset:
                    offset = u_id + 1
                try:
                    self.dispatch(upd)
                except Exception as exc:
                    logger.exception("dispatch crashed: %s", exc)

    # ── poll loop ─────────────────────────────────────
    def run_poll_loop(self) -> None:
        interval = self.settings.poll_interval_seconds
        while not self.stop:
            self.poll_loop.tick()
            # sleep in small slices so stop() is responsive
            for _ in range(int(interval)):
                if self.stop:
                    return
                time.sleep(1)

    # ── lifecycle ─────────────────────────────────────
    def start(self) -> None:
        if not self.self_check():
            raise SystemExit(1)
        self.setup_telegram()
        # first poll immediately (baselines silently), then the loops
        t_poll = threading.Thread(target=self.run_poll_loop, daemon=True, name="poll")
        t_upd = threading.Thread(target=self.run_update_loop, daemon=True, name="updates")
        t_poll.start()
        t_upd.start()
        logger.info("both loops running (poll=%ss)", self.settings.poll_interval_seconds)

        def _sig(signum, frame):
            logger.info("signal %s received — shutting down", signum)
            self.stop = True

        signal.signal(signal.SIGTERM, _sig)
        signal.signal(signal.SIGINT, _sig)
        while not self.stop:
            time.sleep(1)
        logger.info("shutdown complete")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    App(settings).start()


if __name__ == "__main__":
    main()
