# Vercel Webhook Receiver (Option A — 24/7, free, no domain)

Deploy this folder to Vercel. The function verifies Plane's HMAC signature and
wakes your home bot via Telegram — so Plane events fire reports **instantly**
without any tunnel, domain, or VPS.

## Architecture

```
Plane (owner-created webhook)
   │ POST /api/webhook  (X-Plane-Signature = HMAC-SHA256)
   ▼
Vercel function (this folder)  ← 24/7, free, https://<project>.vercel.app/api/webhook
   │ sendMessage "⚡wake:plane" via WAKE_BOT_TOKEN
   ▼
Your channel (Saba Tasks Monitor (Standalone))
   │ main bot (admin, long-polling getUpdates 24/7) sees it
   ▼
poll loop kicks → fetch → diff → Telegram report (seconds, not minutes)
```

## Prerequisites (5 minutes)

1. **A wake bot** — create a tiny bot via @BotFather (e.g. `@saba_wake_bot`),
   copy its token. It ONLY posts the ⚡wake marker.
2. **Add the wake bot to the channel as admin** (post permission is enough).
3. **Your main bot must already be admin in the channel** (it is — it pins the
   menu there).

## Deploy

1. Push this repo to GitHub (or use `vercel` CLI from this folder).
2. In Vercel: **New Project → import the repo → Root Directory = `vercel`**.
3. Set env vars (Project → Settings → Environment Variables):

   | Name | Value |
   |------|-------|
   | `PLANE_WEBHOOK_SECRET` | the same secret as your bot's `.env` |
   | `WAKE_BOT_TOKEN` | the wake bot's token (`123456:ABC...`) |
   | `TG_CHAT_ID` | your channel id (e.g. `-100...`) |

4. Deploy. Your public endpoint:

   ```
   https://<your-project>.vercel.app/api/webhook
   ```

## Hand to the owner

| Value | Content |
|-------|---------|
| **Webhook URL** | `https://<your-project>.vercel.app/api/webhook` |
| **Secret** | the `PLANE_WEBHOOK_SECRET` value |
| **Events** | Issue: created / updated / deleted |

## Verify

```bash
SECRET=<your PLANE_WEBHOOK_SECRET>
BODY='{"event":"issue","action":"update","data":{"id":"t","project_id":"<PROJECT_ID>"}}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/.*= //')
curl -X POST https://<your-project>.vercel.app/api/webhook \
     -H "Content-Type: application/json" \
     -H "X-Plane-Signature: $SIG" \
     -d "$BODY"
# expect: {"ok":true}   (and a ⚡wake:plane post in the channel that
#  the main bot consumes + deletes within seconds)
```

Without a valid signature → `403 {"error":"bad signature"}`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `500 server not configured` | `PLANE_WEBHOOK_SECRET` missing in Vercel env |
| `403 bad signature` | secret mismatch, or body re-encoded by a proxy |
| `502 wake failed` | `WAKE_BOT_TOKEN`/`TG_CHAT_ID` wrong, or wake bot not in channel |
| ⚡wake appears but no report | check `journalctl -u saba-tasks-monitor` for "webhook wake received"; Plane may be filtering events |
| ⚡wake stays visible in channel | main bot lacks delete permission, or wake text differs (`⚡wake:plane`) |

## Fallback

If the webhook ever misses an event, the normal `POLL_INTERVAL_SECONDS`
watchdog still catches everything — nothing is lost, reports just wait.
