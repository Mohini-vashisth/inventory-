// WhatsApp relay — a small always-on front door for the inventory app's WhatsApp webhook.
//
// Why it exists: the app runs on one PC. While that PC is off, Meta can't deliver customers'
// WhatsApp messages and nothing can answer them. This Cloudflare Worker is what Meta talks to
// instead (it is always up):
//
//   * it checks Meta's signature, then passes the message on to the app UNCHANGED (same body,
//     same signature, so the app's own check still passes);
//   * if the app doesn't answer (PC off, restarting), it keeps the message in a queue, tells the
//     customer "we've received your message and will reply shortly" (once every few hours), and
//     returns 200 to Meta so Meta stops retrying;
//   * every minute (cron) and on every new message it replays the queue to the app, oldest first.
//     The app skips messages it has already seen and orders them by their sent time, so a replay
//     is always safe.
//
// Secrets (set with `wrangler secret put`, see README.md): APP_SECRET, VERIFY_TOKEN, ACCESS_TOKEN.
// Plain variables (wrangler.toml): PHONE_NUMBER_ID, PC_WEBHOOK_URL, and optionally ACK_TEXT,
// ACK_COOLDOWN_SECONDS, FORWARD_TIMEOUT_MS, MAX_QUEUE_AGE_SECONDS, GRAPH_VERSION.
// Storage: a D1 database bound as DB (schema.sql).

const DEFAULTS = {
  ACK_TEXT:
    "Thanks for your message. We've received it and will reply shortly.",
  ACK_COOLDOWN_SECONDS: 6 * 3600,
  FORWARD_TIMEOUT_MS: 5000,
  MAX_QUEUE_AGE_SECONDS: 7 * 24 * 3600,
  GRAPH_VERSION: "v21.0",
  REPLAY_BATCH: 50,
};

const setting = (env, name) => (env[name] !== undefined && env[name] !== "" ? env[name] : DEFAULTS[name]);

// ── Security ────────────────────────────────────────────────────────────────

function toHex(buffer) {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function constantTimeEqual(a, b) {
  if (a.length !== b.length) return false;
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return difference === 0;
}

/** Meta signs the raw body: X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(app secret, body). */
export async function validSignature(rawBody, header, secret) {
  if (!secret || !header || !header.startsWith("sha256=")) return false; // an unset secret never validates
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const expected = toHex(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(rawBody)));
  return constantTimeEqual(header.slice("sha256=".length), expected);
}

// ── Reading a webhook payload ───────────────────────────────────────────────

function allMessages(payload) {
  const messages = [];
  for (const entry of (payload && payload.entry) || []) {
    for (const change of entry.changes || []) {
      for (const message of (change.value && change.value.messages) || []) messages.push(message);
    }
  }
  return messages;
}

/** True when the payload carries customer messages (not just delivery receipts). */
export function hasMessages(payload) {
  return allMessages(payload).length > 0;
}

/** The distinct customer numbers that wrote in this payload. */
export function customersIn(payload) {
  return [...new Set(allMessages(payload).map((m) => m.from).filter(Boolean))];
}

// ── Passing a message on to the app ─────────────────────────────────────────

/** True when the app accepted the delivery (a 2xx). Any error, timeout or non-2xx counts as "down". */
export async function forwardToApp(rawBody, signature, env, fetchFn = fetch) {
  try {
    const response = await fetchFn(env.PC_WEBHOOK_URL, {
      method: "POST",
      headers: { "content-type": "application/json", "x-hub-signature-256": signature },
      body: rawBody,
      signal: AbortSignal.timeout(Number(setting(env, "FORWARD_TIMEOUT_MS"))),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/** Replay the queue to the app, oldest first, stopping at the first failure so order is kept. */
export async function replayQueue(env, deps = {}) {
  const store = deps.store || d1Store(env.DB);
  const forward = deps.forward || ((body, signature) => forwardToApp(body, signature, env, deps.fetch));
  const now = deps.now ? deps.now() : Date.now();
  const maxAgeMs = Number(setting(env, "MAX_QUEUE_AGE_SECONDS")) * 1000;
  let delivered = 0;
  for (const item of await store.peek(Number(setting(env, "REPLAY_BATCH")))) {
    if (now - item.received_at > maxAgeMs) { // too old to be useful: give up rather than retry forever
      await store.remove(item.id);
      continue;
    }
    if (!(await forward(item.body, item.signature))) {
      await store.bump(item.id);
      break;
    }
    await store.remove(item.id);
    delivered++;
  }
  return delivered;
}

// ── Telling the customer ────────────────────────────────────────────────────

/** Send the "we'll reply shortly" message to each customer in the payload, at most once per cooldown. */
export async function acknowledgeCustomers(payload, env, deps = {}) {
  const store = deps.store || d1Store(env.DB);
  const fetchFn = deps.fetch || fetch;
  const now = deps.now ? deps.now() : Date.now();
  const cooldownMs = Number(setting(env, "ACK_COOLDOWN_SECONDS")) * 1000;
  const sent = [];
  for (const phone of customersIn(payload)) {
    if (await store.ackedSince(phone, now - cooldownMs)) continue;
    await store.markAcked(phone, now); // before sending, so two deliveries at once can't both send
    try {
      const response = await fetchFn(
        `https://graph.facebook.com/${setting(env, "GRAPH_VERSION")}/${env.PHONE_NUMBER_ID}/messages`,
        {
          method: "POST",
          headers: { authorization: `Bearer ${env.ACCESS_TOKEN}`, "content-type": "application/json" },
          body: JSON.stringify({
            messaging_product: "whatsapp", to: phone, type: "text",
            text: { body: setting(env, "ACK_TEXT") },
          }),
        },
      );
      if (!response.ok) throw new Error(`Graph API ${response.status}`);
      sent.push(phone);
    } catch (error) {
      await store.unmarkAcked(phone); // it didn't go: allow the next message to try again
      console.log(`acknowledgement to ${phone} failed: ${error.message}`);
    }
  }
  return sent;
}

// ── The webhook itself ──────────────────────────────────────────────────────

function handshake(url, env) {
  const mode = url.searchParams.get("hub.mode");
  const token = url.searchParams.get("hub.verify_token") || "";
  const challenge = url.searchParams.get("hub.challenge") || "";
  // An unset VERIFY_TOKEN must never verify anything ('' === '' would pass a request with no token).
  if (env.VERIFY_TOKEN && mode === "subscribe" && constantTimeEqual(token, env.VERIFY_TOKEN)) {
    return new Response(challenge, { status: 200, headers: { "content-type": "text/plain" } });
  }
  return new Response("Verification failed", { status: 403 });
}

export async function handleRequest(request, env, deps = {}) {
  const url = new URL(request.url);
  if (request.method === "GET") return handshake(url, env);
  if (request.method !== "POST") return new Response("Method not allowed", { status: 405 });

  const rawBody = await request.text();
  const signature = request.headers.get("x-hub-signature-256") || "";
  if (!(await validSignature(rawBody, signature, env.APP_SECRET))) {
    return new Response("Invalid signature", { status: 403 });
  }

  let payload;
  try {
    payload = JSON.parse(rawBody);
  } catch {
    return new Response("ok", { status: 200 }); // authenticated but unusable: acknowledge, nothing to keep
  }

  const store = deps.store || d1Store(env.DB);
  const forward = deps.forward || ((body, sig) => forwardToApp(body, sig, env, deps.fetch));

  // Messages already waiting must go first, or this newer one would overtake them.
  if (await store.size()) {
    if (hasMessages(payload)) await store.enqueue(rawBody, signature, deps.now ? deps.now() : Date.now());
    await replayQueue(env, { ...deps, store, forward });
    // Still not caught up: the app is still down, so this customer is waiting too.
    if (hasMessages(payload) && (await store.size())) await acknowledgeCustomers(payload, env, { ...deps, store });
    return new Response("ok", { status: 200 });
  }

  if (await forward(rawBody, signature)) return new Response("ok", { status: 200 });

  // The app is not answering.
  if (hasMessages(payload)) {
    await store.enqueue(rawBody, signature, deps.now ? deps.now() : Date.now());
    await acknowledgeCustomers(payload, env, { ...deps, store });
  }
  return new Response("ok", { status: 200 }); // we hold it now; Meta needn't retry
}

// ── Storage (Cloudflare D1) ─────────────────────────────────────────────────

export function d1Store(db) {
  return {
    async size() {
      const row = await db.prepare("SELECT COUNT(*) AS n FROM queue").first();
      return row ? row.n : 0;
    },
    async enqueue(body, signature, receivedAt) {
      await db.prepare("INSERT INTO queue (received_at, signature, body, attempts) VALUES (?, ?, ?, 0)")
        .bind(receivedAt, signature, body).run();
    },
    async peek(limit) {
      const { results } = await db.prepare("SELECT id, received_at, signature, body FROM queue ORDER BY id ASC LIMIT ?")
        .bind(limit).all();
      return results;
    },
    async remove(id) {
      await db.prepare("DELETE FROM queue WHERE id = ?").bind(id).run();
    },
    async bump(id) {
      await db.prepare("UPDATE queue SET attempts = attempts + 1 WHERE id = ?").bind(id).run();
    },
    async ackedSince(phone, sinceMs) {
      const row = await db.prepare("SELECT sent_at FROM acks WHERE phone = ?").bind(phone).first();
      return Boolean(row && row.sent_at > sinceMs);
    },
    async markAcked(phone, now) {
      await db.prepare("INSERT INTO acks (phone, sent_at) VALUES (?, ?) ON CONFLICT(phone) DO UPDATE SET sent_at = excluded.sent_at")
        .bind(phone, now).run();
    },
    async unmarkAcked(phone) {
      await db.prepare("DELETE FROM acks WHERE phone = ?").bind(phone).run();
    },
  };
}

export default {
  fetch: (request, env) => handleRequest(request, env),
  // Cron trigger (every minute): bring the app up to date once it is reachable again.
  async scheduled(_event, env) {
    await replayQueue(env);
  },
};
