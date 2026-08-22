/**
 * Plane webhook receiver — Vercel serverless function.
 *
 * Plane (owner-created webhook) POSTs events here. We:
 *   1. Verify X-Plane-Signature (HMAC-SHA256 of the raw body).
 *   2. On success, wake the home bot by posting "⚡wake:plane" to the
 *      Telegram channel via a SEPARATE wake bot token.
 *   3. The main bot (admin in the channel, long-polling getUpdates 24/7)
 *      sees that channel post and kicks its poll loop → instant report.
 *
 * No domain needed: Vercel gives https://<project>.vercel.app/api/webhook
 * Serverless-safe: we keep zero state — Telegram IS the wake channel.
 *
 * Env vars (set in Vercel dashboard):
 *   PLANE_WEBHOOK_SECRET  — the HMAC secret (match your .env value)
 *   WAKE_BOT_TOKEN        — token of a small bot that posts ⚡wake to the channel
 *   TG_CHAT_ID            — the channel id (e.g. -100...)
 */
import crypto from 'crypto';

function timingSafeEqualHex(a, b) {
  try {
    const ba = Buffer.from(a, 'hex');
    const bb = Buffer.from(b, 'hex');
    return ba.length === bb.length && crypto.timingSafeEqual(ba, bb);
  } catch {
    return false;
  }
}

async function readRawBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return Buffer.concat(chunks);
}

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'method not allowed' });
  }

  const secret = process.env.PLANE_WEBHOOK_SECRET;
  if (!secret) {
    return res.status(500).json({ error: 'server not configured' });
  }

  const raw = await readRawBody(req);
  const sig = req.headers['x-plane-signature'];
  const expected = crypto.createHmac('sha256', secret).update(raw).digest('hex');

  if (!sig || !timingSafeEqualHex(expected, sig)) {
    return res.status(403).json({ error: 'bad signature' });
  }

  // Valid event → wake the home bot via Telegram (the always-on channel).
  const wakeToken = process.env.WAKE_BOT_TOKEN;
  const chatId = process.env.TG_CHAT_ID;
  if (wakeToken && chatId) {
    try {
      await fetch(`https://api.telegram.org/bot${wakeToken}/sendMessage`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chat_id: chatId,
          text: '⚡wake:plane',
          disable_notification: true,
        }),
      });
    } catch (err) {
      console.error('wake delivery failed:', err);
      return res.status(502).json({ error: 'wake failed' });
    }
  }

  return res.status(200).json({ ok: true });
}
