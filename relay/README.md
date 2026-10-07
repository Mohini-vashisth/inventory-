# WhatsApp relay (Cloudflare Worker)

The inventory app runs on one PC. While it is off, Meta can't deliver customers' WhatsApp messages and
nothing answers them. This small Worker is what Meta talks to instead — it is always up. It:

1. checks Meta's signature, then passes each message to the app unchanged (same body, same signature,
   so the app's own check still passes);
2. if the app doesn't answer, **keeps the message**, tells the customer *"Thanks for your message. We've
   received it and will reply shortly."* (once every 6 hours per customer) and returns 200 to Meta;
3. replays everything it kept, oldest first, every minute and whenever a new message arrives — the app
   skips messages it has already seen and orders them by sent time, so replays are safe.

Free tier is plenty (Workers + D1). Secrets live in Cloudflare, not in this repo.

## One-time setup (needs your Cloudflare login — about 10 minutes)

```bash
cd relay
npx wrangler login                                  # opens a browser

npx wrangler d1 create whatsapp-relay               # prints a database_id
#   -> paste it into wrangler.toml (database_id)
npx wrangler d1 execute whatsapp-relay --remote --file=schema.sql

#   -> in wrangler.toml set PHONE_NUMBER_ID (App Dashboard -> WhatsApp -> API Setup; same value as the
#      app's WHATSAPP_PHONE_NUMBER_ID). PC_WEBHOOK_URL is already the Funnel address.

npx wrangler secret put APP_SECRET      # Meta App Secret (same as the app's WHATSAPP_APP_SECRET)
npx wrangler secret put VERIFY_TOKEN    # same as the app's WHATSAPP_VERIFY_TOKEN
npx wrangler secret put ACCESS_TOKEN    # same as the app's WHATSAPP_ACCESS_TOKEN

npx wrangler deploy                     # prints https://matta-whatsapp-relay.<you>.workers.dev
```

Then in Meta (App Dashboard -> WhatsApp -> Configuration -> Webhook) set the **Callback URL** to the
Worker's address and enter the same verify token; keep the `messages` field subscribed. Nothing changes
in the app: its `/webhooks/whatsapp/` keeps its own signature check, and keeps the Funnel path.

## Check it works

1. Stop the app: `sc stop InventoryApp` on the office PC.
2. Message the WhatsApp number from a phone — you should get the "we'll reply shortly" message.
3. `sc start InventoryApp` — within a minute the bot answers and the query appears in the app.

## Roll back

Point Meta's Callback URL back at `https://mdw.tail2734e7.ts.net/webhooks/whatsapp/`. Anything still in
the Worker's queue can be left; delete the Worker when you no longer need it.

## Tests

```bash
cd relay && npm test        # node's built-in runner, no packages needed
```
