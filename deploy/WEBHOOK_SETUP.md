# Plane Webhook — Setup Guide

The bot can receive **push events** from Plane so reports fire the moment an
issue changes, instead of waiting for the next `POLL_INTERVAL_SECONDS` poll.
The periodic poll stays enabled as a self-healing fallback.

The webhook UI in Plane is **owner-only**, so this is a two-person job:
**you** (admin) stand up the endpoint, then hand three values to the **owner**.

```
Plane (workspace owner creates webhook)
   │  POST /webhook/plane  (X-Plane-Signature = HMAC-SHA256 of raw body)
   ▼
public domain → reverse proxy (TLS) → bot on 127.0.0.1:8080
                                          │ wakes poll loop
                                          ▼
                                fetch → diff → Telegram report
```

---

## 1. What YOU (admin) must do

### 1.1 Install the new dependencies

```bash
pip install -r requirements.txt        # or: .venv/bin/pip install -r requirements.txt
```

### 1.2 Configure `.env`

Add these to `.env` (see `.env.example`):

```ini
# ── Plane webhook ──────────────────────────────────────────
# Secret you'll ALSO give the owner when they create the webhook.
# Use a long random string, e.g.  openssl rand -hex 32
PLANE_WEBHOOK_SECRET=<generate-a-random-secret>

# Where the receiver listens. 127.0.0.1 = only reachable via the
# reverse proxy on this machine (recommended).
WEBHOOK_HOST=127.0.0.1
WEBHOOK_PORT=8080
WEBHOOK_PATH=/webhook/plane
# Min gap between polls when webhook bursts arrive (debounce).
WEBHOOK_MIN_INTERVAL_SECONDS=10
```

> If `PLANE_WEBHOOK_SECRET` is empty, the endpoint fails closed (HTTP 500)
> — the bot will not accept unsigned webhooks.

### 1.3 Restart the bot

```bash
# root systemd service (deploy/install.sh):
sudo systemctl restart saba-tasks-monitor

# or user-level service (deploy/setup_standalone.sh):
systemctl --user restart saba-tasks-monitor
```

Confirm it's listening:

```bash
journalctl -u saba-tasks-monitor -f | grep webhook
# expect:  ✓ webhook server on 127.0.0.1:8080 (path /webhook/plane)
```

### 1.4 Expose it publicly over HTTPS

Plane posts to a URL it must be able to reach, so it needs a public HTTPS URL.
Bind is already `127.0.0.1`; put a TLS reverse proxy in front.

**Caddy (bundled config)** — set your real domain in `deploy/Caddyfile`:

```
plane-webhook.example.com {
	reverse_proxy 127.0.0.1:8080
}
```

```bash
caddy run --config deploy/Caddyfile
```

Caddy auto-provisions a Let's Encrypt certificate. **nginx** alternative:

```nginx
server {
    server_name plane-webhook.example.com;
    location /webhook/plane {
        proxy_pass http://127.0.0.1:8080;
    }
}
```

Your public endpoint is now:

```
https://plane-webhook.example.com/webhook/plane
```

---

## 2. What the OWNER must do

Send the owner these instructions plus the values:

- **Webhook URL:** `https://plane-webhook.example.com/webhook/plane` *(your real domain)*
- **Secret:** the exact `PLANE_WEBHOOK_SECRET` value from `.env`
- **Events:** enable the **Issue** events — created / updated / deleted

> To the owner:
>
> 1. In Plane go to **Workspace Settings → Webhooks → Add webhook**.
> 2. Paste the URL and the secret into the form.
> 3. Select the **Issue** events (created, updated, deleted) and save.
> 4. If the form offers a **Send test** button, press it — you should get a
>    green "successful delivery" from Plane.

---

## 3. Verify it end-to-end

1. **Check the logs** after the owner saves the webhook:
   ```bash
   journalctl -u saba-tasks-monitor -f
   # expect a line like:
   #   webhook: event=issue action=create
   ```
   (Plane sends nothing on save alone; events fire when issues actually change.)

2. **Change an issue** in Plane (rename it, move state, reassign, etc.) — the
   Telegram report should appear within seconds instead of up to 5 minutes.

3. **Sanity-check the endpoint yourself** (from a machine that can reach it):
   ```bash
   SECRET=<your PLANE_WEBHOOK_SECRET>
   BODY='{"event":"issue","action":"update","data":{"id":"t","project_id":"<PROJECT_ID>"}}'
   SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/.*= //')
   curl -i -X POST https://plane-webhook.example.com/webhook/plane \
        -H "Content-Type: application/json" \
        -H "X-Plane-Signature: $SIG" \
        -d "$BODY"
   # expect: HTTP/1.1 200 OK  {"ok":true}
   ```
   Without a valid signature you should get `403`.

---

## 4. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `✓ webhook server` line missing in logs | old deps not installed, or another service already bound to `WEBHOOK_PORT` |
| All requests return `500 {"error":"server not configured"}` | `PLANE_WEBHOOK_SECRET` is empty — set it and restart |
| `403 bad signature` in the logs | owner's secret doesn't match `PLANE_WEBHOOK_SECRET`, or a proxy re-encoded the body (don't let a proxy reformat JSON) |
| Plane shows "delivery failed" / webhook deactivated | endpoint unreachable — check the public URL from an external machine (TLS cert valid, port open, proxy up) |
| No report on a change | check `X-Plane-Event`/`event` in the logs; the bot only reacts to `issue` events and, when a project id is present, only for `PLANE_PROJECT_ID`. Other events are acked with `200` and ignored |
| Events arrive in a burst | expected — `WEBHOOK_MIN_INTERVAL_SECONDS` collapses them into one poll (default 10s) |

## 5. Fallback

If the webhook is ever down or misses an event, the normal
`POLL_INTERVAL_SECONDS` watchdog still catches every change — nothing is lost,
reports just wait for the next scheduled poll.
