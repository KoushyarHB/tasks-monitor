"""Tests for bot.webhook_server — signature verification, routing, filtering."""
import hashlib
import hmac
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import Settings
from bot.webhook_server import MAX_BODY_BYTES, create_app, verify_signature

SECRET = "sekret"
PROJECT = "proj1"


def make_app(secret=SECRET, project_id=PROJECT):
    settings = Settings(
        plane_base_url="https://plane.test",
        plane_workspace="tms",
        plane_project_id=project_id,
        plane_csrf_token="c",
        plane_session_id="s",
        plane_user_id="u",
        webhook_path="/webhook/plane",
        plane_webhook_secret=secret,
    )
    kicks: list[str] = []
    app = create_app(settings, lambda: kicks.append(1))
    return app, kicks


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(app, payload, secret=SECRET, sig=None, path="/webhook/plane", method="post"):
    body = json.dumps(payload)
    headers = {"X-Plane-Signature": sig if sig is not None else sign(body.encode(), secret)}
    return getattr(app.test_client(), method)(path, data=body, content_type="application/json", headers=headers)


def issue_payload(project_id=PROJECT, action="update", event="issue", data=None):
    return {
        "event": event,
        "action": action,
        "webhook_id": "wh1",
        "workspace_id": "ws1",
        "workspace_slug": "tms",
        "data": data if data is not None else {"id": "iss1", "project_id": project_id},
        "activity": None,
    }


def test_verify_signature():
    body = b'{"hello":"world"}'
    good = sign(body)
    assert verify_signature(SECRET, body, good) is True
    assert verify_signature(SECRET, body, "bogus") is False
    assert verify_signature(SECRET, body, None) is False
    assert verify_signature("", body, good) is False


def test_valid_webhook_kicks():
    app, kicks = make_app()
    r = post(app, issue_payload())
    assert r.status_code == 200
    assert kicks == [1]


def test_deleted_issue_forwarded_without_project_id():
    app, kicks = make_app()
    r = post(app, issue_payload(data={"id": "iss9"}))
    assert r.status_code == 200
    assert kicks == [1]


def test_wrong_signature_rejected():
    app, kicks = make_app()
    r = post(app, issue_payload(), sig="deadbeef")
    assert r.status_code == 403
    assert kicks == []


def test_missing_signature_rejected():
    app, kicks = make_app()
    body = json.dumps(issue_payload())
    r = app.test_client().post("/webhook/plane", data=body, content_type="application/json")
    assert r.status_code == 403
    assert kicks == []


def test_no_secret_configured_fails_closed():
    app, kicks = make_app(secret="")
    r = post(app, issue_payload())
    assert r.status_code == 500
    assert kicks == []


def test_non_issue_event_ignored():
    app, kicks = make_app()
    r = post(app, issue_payload(event="cycle"))
    assert r.status_code == 200
    assert kicks == []


def test_other_project_issue_ignored():
    app, kicks = make_app(project_id=PROJECT)
    r = post(app, issue_payload(project_id="other-proj"))
    assert r.status_code == 200
    assert kicks == []


def test_unknown_path_404():
    app, _ = make_app()
    r = post(app, issue_payload(), path="/nope")
    assert r.status_code == 404


def test_wrong_method_405():
    app, kicks = make_app()
    r = post(app, issue_payload(), method="get")
    assert r.status_code == 405
    assert kicks == []


def test_oversized_body_413():
    app, kicks = make_app()
    big = json.dumps({"event": "issue", "data": {"x": "y" * (MAX_BODY_BYTES + 100)}})
    sig = sign(big.encode())
    r = app.test_client().post(
        "/webhook/plane", data=big, content_type="application/json",
        headers={"X-Plane-Signature": sig},
    )
    assert r.status_code == 413
    assert kicks == []
