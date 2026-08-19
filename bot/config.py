"""Environment configuration for the Saba Tasks Monitor bot."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: str | os.PathLike | None = None) -> None:
    """Minimal .env loader (no external dependency)."""
    if path is None:
        path = Path(os.path.dirname(os.path.abspath(__file__))).parent / ".env"
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # strip optional quotes
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


@dataclass(frozen=True)
class Settings:
    plane_base_url: str = ""
    plane_workspace: str = ""
    plane_project_id: str = ""
    plane_csrf_token: str = ""
    plane_session_id: str = ""
    plane_user_id: str = ""
    plane_focus: str = "mine"
    tg_bot_token: str = ""
    tg_chat_id: str = ""
    tg_proxy: str = ""
    poll_interval_seconds: int = 300
    state_file: str = "./state.json"

    @classmethod
    def from_env(cls, env: dict | None = None, dotenv_path: str | None = None) -> "Settings":
        """Build Settings from env (optionally loading a .env file first)."""
        if dotenv_path is not None:
            _load_dotenv(dotenv_path)
        e = os.environ if env is None else env

        def s(key: str, default: str = "") -> str:
            return (e.get(key) or default).strip()

        try:
            poll = int(s("POLL_INTERVAL_SECONDS", "300"))
        except ValueError:
            poll = 300

        focus = s("PLANE_FOCUS", "mine").lower()
        if focus not in ("mine", "all"):
            focus = "mine"

        return cls(
            plane_base_url=s("PLANE_BASE_URL").rstrip("/"),
            plane_workspace=s("PLANE_WORKSPACE"),
            plane_project_id=s("PLANE_PROJECT_ID"),
            plane_csrf_token=s("PLANE_CSRF_TOKEN"),
            plane_session_id=s("PLANE_SESSION_ID"),
            plane_user_id=s("PLANE_USER_ID"),
            plane_focus=focus,
            tg_bot_token=s("TG_BOT_TOKEN"),
            tg_chat_id=s("TG_CHAT_ID"),
            tg_proxy=s("TG_PROXY"),
            poll_interval_seconds=max(30, poll),
            state_file=s("STATE_FILE", "./state.json"),
        )

    @property
    def plane_headers(self) -> dict[str, str]:
        """Cookie + CSRF headers required by Plane CE."""
        headers = {"X-CSRFToken": self.plane_csrf_token}
        cookie_parts = []
        if self.plane_csrf_token:
            cookie_parts.append(f"csrftoken={self.plane_csrf_token}")
        if self.plane_session_id:
            cookie_parts.append(f"sessionid={self.plane_session_id}")
        if cookie_parts:
            headers["Cookie"] = "; ".join(cookie_parts)
        return headers
