import test from "node:test";
import assert from "node:assert/strict";
import {
  acknowledgeCustomers, customersIn, forwardToApp, handleRequest, hasMessages, replayQueue, validSignature,
} from "./worker.js";

const SECRET = "app-secret";
const ENV = {
  APP_SECRET: SECRET, VERIFY_TOKEN: "verify-me", ACCESS_TOKEN: "token", PHONE_NUMBER_ID: "123",
  PC_WEBHOOK_URL: "https://pc.example/webhooks/whatsapp/",
};

async function sign(body, secret = SECRET) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
  return "sha256=" + [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function payload(...phones) {
  return JSON.stringify({ entry: [{ changes: [{ value: { messages: phones.map((from, i) => ({ from, id: `wamid.${from}.${i}`, type: "text", text: { body: "hi" } })) } }] }] });
}
const STATUS_ONLY = JSON.stringify({ entry: [{ changes: [{ value: { statuses: [{ id: "x", status: "delivered" }] } }] }] });

/** An in-memory stand-in for the D1 store. */
function memoryStore() {
  const items = [];
  const acks = new Map();
  let nextId = 1;
  return {
    items, acks,
    async size() { return items.length; },
    async enqueue(body, signature, receivedAt) { items.push({ id: nextId++, received_at: receivedAt, signature, body, attempts: 0 }); },
    async peek(limit) { return items.slice(0, limit); },
    async remove(id) { items.splice(items.findIndex((i) => i.id === id), 1); },
    async bump(id) { items.find((i) => i.id === id).attempts++; },
    async ackedSince(phone, since) { return acks.has(phone) && acks.get(phone) > since; },
    async markAcked(phone, now) { acks.set(phone, now); },
    async unmarkAcked(phone) { acks.delete(phone); },
  };
}

/** A fetch that records calls: the PC answers `pcOk`, the Graph API answers `graphOk`. */
function fakeFetch({ pcOk = true, graphOk = true } = {}) {
  const calls = { pc: [], graph: [] };
  const fn = async (url, options) => {
    if (url.startsWith("https://graph.facebook.com")) {
      calls.graph.push({ url, body: JSON.parse(options.body), headers: options.headers });
      return { ok: graphOk, status: graphOk ? 200 : 500 };
    }
    calls.pc.push({ url, body: options.body, headers: options.headers });
    if (pcOk === "throw") throw new Error("connection refused");
    return { ok: pcOk, status: pcOk ? 200 : 502 };
  };
  fn.calls = calls;
  return fn;
}

async function post(body, { env = ENV, store = memoryStore(), fetchFn = fakeFetch(), signature, now = () => 1_000_000 } = {}) {
  const request = new Request("https://relay.example/", {
    method: "POST", body, headers: { "x-hub-signature-256": signature ?? (await sign(body)) },
  });
  const response = await handleRequest(request, env, { store, fetch: fetchFn, now });
  return { response, store, fetchFn };
}

// ── signature and handshake ──

test("signature: right secret passes, wrong body/secret/format fails, unset secret never validates", async () => {
  const body = payload("1");
  assert.equal(await validSignature(body, await sign(body), SECRET), true);
  assert.equal(await validSignature(body + " ", await sign(body), SECRET), false);
  assert.equal(await validSignature(body, await sign(body, "other"), SECRET), false);
  assert.equal(await validSignature(body, "nope", SECRET), false);
  assert.equal(await validSignature(body, "sha256=" + "0".repeat(64), ""), false); // an empty secret validates nothing
  assert.equal(await validSignature(body, "", SECRET), false);
});

test("handshake: echoes the challenge only for the right token; an unset token verifies nothing", async () => {
  const ask = (token, env = ENV) => handleRequest(
    new Request(`https://relay.example/?hub.mode=subscribe&hub.verify_token=${token}&hub.challenge=abc123`), env);
  const ok = await ask("verify-me");
  assert.equal(ok.status, 200);
  assert.equal(await ok.text(), "abc123");
  assert.equal((await ask("wrong")).status, 403);
  assert.equal((await ask("", { ...ENV, VERIFY_TOKEN: "" })).status, 403);
});

test("a message with a bad signature is refused and nothing is passed on or kept", async () => {
  const fetchFn = fakeFetch();
  const { response, store } = await post(payload("1"), { signature: "sha256=00", fetchFn });
  assert.equal(response.status, 403);
  assert.equal(fetchFn.calls.pc.length, 0);
  assert.equal(store.items.length, 0);
});

// ── the app is up ──

test("when the app is up the message is passed on unchanged and nothing else happens", async () => {
  const body = payload("919800000001");
  const signature = await sign(body);
  const fetchFn = fakeFetch();
  const { response, store } = await post(body, { fetchFn, signature });
  assert.equal(response.status, 200);
  assert.equal(fetchFn.calls.pc.length, 1);
  assert.equal(fetchFn.calls.pc[0].url, ENV.PC_WEBHOOK_URL);
  assert.equal(fetchFn.calls.pc[0].body, body); // byte for byte, so the app's own signature check still passes
  assert.equal(fetchFn.calls.pc[0].headers["x-hub-signature-256"], signature);
  assert.equal(store.items.length, 0);
  assert.equal(fetchFn.calls.graph.length, 0); // no "we'll reply shortly" when we can reply
});

// ── the app is down ──

for (const mode of [false, "throw"]) {
  test(`when the app is down (${mode === false ? "error response" : "unreachable"}) the message is kept and the customer told`, async () => {
    const body = payload("919800000001");
    const fetchFn = fakeFetch({ pcOk: mode });
    const { response, store } = await post(body, { fetchFn });
    assert.equal(response.status, 200); // so Meta stops retrying: the relay now owns it
    assert.equal(store.items.length, 1);
    assert.equal(store.items[0].body, body);
    assert.equal(fetchFn.calls.graph.length, 1);
    assert.equal(fetchFn.calls.graph[0].body.to, "919800000001");
    assert.equal(fetchFn.calls.graph[0].body.type, "text");
    assert.match(fetchFn.calls.graph[0].body.text.body, /reply shortly/);
    assert.equal(fetchFn.calls.graph[0].headers.authorization, "Bearer token");
    assert.match(fetchFn.calls.graph[0].url, /\/123\/messages$/);
  });
}

test("a customer is told once per cooldown, not for every message", async () => {
  const store = memoryStore();
  const fetchFn = fakeFetch({ pcOk: false });
  await post(payload("919800000001"), { store, fetchFn, now: () => 1_000_000 });
  await post(payload("919800000001"), { store, fetchFn, now: () => 1_000_000 + 60_000 });
  assert.equal(fetchFn.calls.graph.length, 1);
  assert.equal(store.items.length, 2); // both are kept
  await post(payload("919800000001"), { store, fetchFn, now: () => 1_000_000 + 7 * 3600 * 1000 });
  assert.equal(fetchFn.calls.graph.length, 2); // a new day's outage gets a new message
});

test("each customer in one delivery is told, once", async () => {
  const fetchFn = fakeFetch({ pcOk: false });
  await post(payload("111", "222", "111"), { fetchFn });
  assert.deepEqual(fetchFn.calls.graph.map((c) => c.body.to).sort(), ["111", "222"]);
});

test("delivery receipts alone are neither kept nor answered", async () => {
  const fetchFn = fakeFetch({ pcOk: false });
  const { response, store } = await post(STATUS_ONLY, { fetchFn });
  assert.equal(response.status, 200);
  assert.equal(store.items.length, 0);
  assert.equal(fetchFn.calls.graph.length, 0);
});

test("if the acknowledgement fails the customer can be told on their next message", async () => {
  const store = memoryStore();
  await post(payload("919800000001"), { store, fetchFn: fakeFetch({ pcOk: false, graphOk: false }) });
  assert.equal(store.acks.size, 0);
  assert.equal(store.items.length, 1); // the message itself is still safe
  const fetchFn = fakeFetch({ pcOk: false });
  await post(payload("919800000001"), { store, fetchFn });
  assert.equal(fetchFn.calls.graph.length, 1);
});

test("an authenticated body that isn't JSON is acknowledged and dropped", async () => {
  const { response, store } = await post("not json");
  assert.equal(response.status, 200);
  assert.equal(store.items.length, 0);
});

// ── replay ──

test("the queue is replayed oldest first and emptied", async () => {
  const store = memoryStore();
  await store.enqueue("first", "sig1", 1_000);
  await store.enqueue("second", "sig2", 2_000);
  const order = [];
  const delivered = await replayQueue(ENV, { store, now: () => 3_000, forward: async (body, sig) => { order.push([body, sig]); return true; } });
  assert.equal(delivered, 2);
  assert.deepEqual(order, [["first", "sig1"], ["second", "sig2"]]);
  assert.equal(store.items.length, 0);
});

test("replay stops at the first failure so order is kept, and counts the attempt", async () => {
  const store = memoryStore();
  for (const body of ["a", "b", "c"]) await store.enqueue(body, "s", 1_000);
  const seen = [];
  const delivered = await replayQueue(ENV, { store, now: () => 2_000, forward: async (body) => { seen.push(body); return body === "a"; } });
  assert.equal(delivered, 1);
  assert.deepEqual(seen, ["a", "b"]); // c is not tried: it must not overtake b
  assert.deepEqual(store.items.map((i) => i.body), ["b", "c"]);
  assert.equal(store.items[0].attempts, 1);
});

test("messages older than the maximum age are given up on", async () => {
  const store = memoryStore();
  await store.enqueue("ancient", "s", 0);
  await store.enqueue("recent", "s", 8 * 24 * 3600 * 1000);
  const seen = [];
  await replayQueue(ENV, { store, now: () => 9 * 24 * 3600 * 1000, forward: async (body) => { seen.push(body); return true; } });
  assert.deepEqual(seen, ["recent"]);
  assert.equal(store.items.length, 0);
});

test("a new message waits behind the queue instead of overtaking it", async () => {
  const store = memoryStore();
  await store.enqueue("old", "sig-old", 1_000);
  const fetchFn = fakeFetch();
  const body = payload("919800000001");
  const { response } = await post(body, { store, fetchFn, now: () => 5_000 });
  assert.equal(response.status, 200);
  assert.deepEqual(fetchFn.calls.pc.map((c) => c.body), ["old", body]); // old first, then the new one
  assert.equal(store.items.length, 0);
});

test("if the app is still down the new message joins the queue behind the old ones", async () => {
  const store = memoryStore();
  await store.enqueue("old", "sig-old", 1_000);
  const body = payload("919800000001");
  await post(body, { store, fetchFn: fakeFetch({ pcOk: false }), now: () => 5_000 });
  assert.deepEqual(store.items.map((i) => i.body), ["old", body]);
});

// ── small helpers ──

test("payload helpers", () => {
  assert.equal(hasMessages(JSON.parse(payload("1"))), true);
  assert.equal(hasMessages(JSON.parse(STATUS_ONLY)), false);
  assert.equal(hasMessages(null), false);
  assert.deepEqual(customersIn(JSON.parse(payload("1", "2", "1"))), ["1", "2"]);
});

test("forwardToApp reports a refused connection as down, not as an error", async () => {
  assert.equal(await forwardToApp("b", "s", ENV, async () => { throw new Error("refused"); }), false);
  assert.equal(await forwardToApp("b", "s", ENV, async () => ({ ok: true })), true);
});

test("acknowledgeCustomers can be given a custom message and cooldown", async () => {
  const fetchFn = fakeFetch();
  await acknowledgeCustomers(JSON.parse(payload("1")), { ...ENV, ACK_TEXT: "Back soon." }, { store: memoryStore(), fetch: fetchFn, now: () => 1 });
  assert.equal(fetchFn.calls.graph[0].body.text.body, "Back soon.");
});
