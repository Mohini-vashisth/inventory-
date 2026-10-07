-- Run once:  npx wrangler d1 execute whatsapp-relay --remote --file=schema.sql
CREATE TABLE IF NOT EXISTS queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at INTEGER NOT NULL,   -- when the relay got it (ms)
  signature TEXT NOT NULL,        -- Meta's X-Hub-Signature-256, passed on unchanged
  body TEXT NOT NULL,             -- the raw webhook body, passed on unchanged
  attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS acks (
  phone TEXT PRIMARY KEY,         -- customer number that was told "we'll reply shortly"
  sent_at INTEGER NOT NULL
);
