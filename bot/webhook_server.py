"""Plane webhook receiver — verify signature, then wake the poll loop.

Plane (self-hosted) POSTs workspace events to a configured webhook URL with:
  - X-Plane-Signature: HMAC-SHA256 hexdigest of the raw JSON body, keyed by
    the webhook secret (only set when a secret is configured)
  - body: {"event", "action", "webhook_id", "workspace_id", "workspace_slug",
           "data", "activity"}
We verify the signature, ignore events that don't concern our project, reply
200 immediately, and only set an event that wakes the poll loop (all the
fetch/diff/deliver work stays in the poll thread).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Callable

from flask import Flask, jsonify, request

from .config import Settings

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1024 * 1024  # 1 MB cap — Plane issue payloads are far smaller


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """True when `signature` equals the HMAC-SHA256 hex of `body` under `secret`."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def create_app(settings: Settings, kick: Callable[[], None]) -> Flask:
    """Build the webhook Flask app. `kick` wakes the poll loop (debounced there)."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES

    @app.route(settings.webhook_path, methods=["POST"])
    def plane_webhook():
        if not settings.plane_webhook_secret:
            logger.error("webhook secret not configured — rejecting request")
            return jsonify({"error": "server not configured"}), 500

        # get_data(cache=True) keeps the raw bytes so get_json() can still parse
        # the same body; the signature is over those exact bytes.
        body = request.get_data(cache=True)

        sig = request.headers.get("X-Plane-Signature")
        if not verify_signature(settings.plane_webhook_secret, body, sig):
            logger.warning("webhook signature mismatch — rejected")
            return jsonify({"error": "bad signature"}), 403

        payload = request.get_json(silent=True)
        payload = payload if isinstance(payload, dict) else {}

        if payload.get("event") != "issue":
            return jsonify({"ok": True}), 200

        # Deleted issues carry data={"id": ...} (no project_id) — forward.
        data = payload.get("data")
        if (
            isinstance(data, dict)
            and data.get("project_id")
            and str(data["project_id"]) != settings.plane_project_id
        ):
            return jsonify({"ok": True}), 200

        logger.info("webhook: event=%s action=%s", payload.get("event"), payload.get("action"))
        kick()
        return jsonify({"ok": True}), 200

    @app.errorhandler(404)
    def _not_found(_e: Any):
        return jsonify({"error": "not found"}), 404

    @app.errorhandler(405)
    def _method_not_allowed(_e: Any):
        return jsonify({"error": "method not allowed"}), 405

    @app.errorhandler(413)
    def _payload_too_large(_e: Any):
        return jsonify({"error": "payload too large"}), 413

    return app
