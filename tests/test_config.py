"""Tests for bot.config."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import Settings, _load_dotenv


def test_defaults_with_empty_env():
    s = Settings.from_env({})
    assert s.plane_base_url == ""
    assert s.poll_interval_seconds == 300
    assert s.plane_focus == "mine"
    assert s.state_file == "./state.json"


def test_type_coercion_poll_interval():
    s = Settings.from_env({"POLL_INTERVAL_SECONDS": "60"})
    assert s.poll_interval_seconds == 60
    s = Settings.from_env({"POLL_INTERVAL_SECONDS": "not-a-number"})
    assert s.poll_interval_seconds == 300


def test_focus_normalized():
    assert Settings.from_env({"PLANE_FOCUS": "ALL"}).plane_focus == "all"
    assert Settings.from_env({"PLANE_FOCUS": "bogus"}).plane_focus == "mine"


def test_plane_headers():
    s = Settings.from_env({
        "PLANE_CSRF_TOKEN": "csrf123",
        "PLANE_SESSION_ID": "sess456",
        "PLANE_BASE_URL": "https://plane.test",
        "PLANE_WORKSPACE": "tms",
    })
    h = s.plane_headers
    assert h["X-CSRFToken"] == "csrf123"
    assert "csrftoken=csrf123" in h["Cookie"]
    # Plane CE v1.x cookie name is session-id (dash), verified against live instance
    assert "session-id=sess456" in h["Cookie"]
    assert "sessionid=sess456" not in h["Cookie"]
    assert h["Referer"] == "https://plane.test/tms/"


def test_dotenv_loader(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# comment\nPLANE_WORKSPACE=tms\nTEST_DOTENV_UNIQUE_VAR=xyz123\n")
    _load_dotenv(f)
    assert os.environ.get("PLANE_WORKSPACE") == "tms"
    assert os.environ.get("TEST_DOTENV_UNIQUE_VAR") == "xyz123"


def test_base_url_trailing_slash_stripped():
    s = Settings.from_env({"PLANE_BASE_URL": "https://plane.sabasystem.app/"})
    assert s.plane_base_url == "https://plane.sabasystem.app"
