"""Entrypoint — startup self-check, both loops (poll + update), graceful shutdown."""
from __future__ import annotations

import logging
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
                send=lambda text, kb: self.tg.send_chunked(text, keyboard=kb),
                answer=lambda qid, toast: self._safe_answer(qid, toast),
            )
        return self._browser

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
        """setMyCommands + pin the commands menu (idempotent)."""
        try:
            self.tg.set_my_commands(SLASH_COMMANDS)
            logger.info("✓ slash commands registered")
        except TelegramError as exc:
            logger.warning("setMyCommands failed: %s", exc)
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
            ]
            res = self.tg.send_message(text, keyboard=kb)
            try:
                self.tg.pin_message(None, res["message_id"])
                logger.info("✓ commands menu pinned")
            except TelegramError as exc:
                logger.info("pin skipped (may already be pinned): %s", exc)
        except TelegramError as exc:
            logger.warning("pinned menu failed: %s", exc)

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
            try:
                self.browser.handle(parsed, qid)
            except TelegramError as exc:
                logger.error("browser handler delivery error: %s", exc)
            except Exception as exc:
                logger.exception("browser handler crashed: %s", exc)
                self._safe_answer(qid, "❌ error")
            return

        # messages / channel posts
        msg = update.get("message") or update.get("channel_post") or {}
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
