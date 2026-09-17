# Inventory & Manufacturing System — CLAUDE.md

## Project overview

Django 4.2 app for a steel coil manufacturing business. Covers the full workflow:
raw material (coil) intake → order management → part cutting → step-by-step production tracking → dispatch.

Single Django app: `materials` inside the `inventory` project directory.

## Running the dev server

```bash
cd inventory
python3 manage.py runserver
```

Migrations:
```bash
python3 manage.py makemigrations
python3 manage.py migrate
```

## Key dependencies

- Django 4.2, django-jazzmin (admin theme), qrcode + Pillow (coil QR tags), python-dotenv, djangorestframework, whitenoise (static files in production), gunicorn (production WSGI server)

## Environment variables (`.env` in `inventory/`, template at `inventory/.env.example`)

| Variable | Purpose |
|---|---|
| `DJANGO_DEBUG` | `True`/`False`. Defaults to `True` (local dev only) — **must be `False`** on any real deployment |
| `DJANGO_SECRET_KEY` | Django secret key. Falls back to an insecure dev-only value if unset, but **raises `ImproperlyConfigured` at startup if `DJANGO_DEBUG=False` and this isn't set** — a misconfigured prod deploy can't silently boot on the known dev key |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hostnames/IPs Django will answer to. Required once `DJANGO_DEBUG=False` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Comma-separated, full origin with scheme (e.g. `https://quote.mattadrawing.com`). Only needed for a hostname reached through a reverse proxy rather than directly — see the Cloudflare Tunnel section below |
| `PUBLIC_QUOTE_BASE_URL` | Full origin, no trailing slash (e.g. `https://quote.mattadrawing.com`). The link a quote-request email points to. Admins only ever reach this app over Tailscale, so leaving this unset would put that private address in an email sent to an external customer — see the Cloudflare Tunnel section below |
| `EMPLOYEE_PIN` | Shared PIN for employee portal (default: `1234`) |
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_USE_TLS` | SMTP config |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | SMTP credentials — Gmail needs an App Password, not the account password |
| `DEFAULT_FROM_EMAIL` | Sender address for quote emails |

## Deploying to a real machine (not local dev)

Local dev (`DEBUG=True`, `manage.py runserver`) skips several things a real deployment needs:

1. Set `.env` from `inventory/.env.example` — at minimum `DJANGO_DEBUG=False`, a real `DJANGO_SECRET_KEY`, and `DJANGO_ALLOWED_HOSTS` set to the machine's hostname/IP.
2. `python3 manage.py collectstatic --noinput` — with `DEBUG=False`, `runserver` no longer serves CSS/JS itself. WhiteNoise (already in `MIDDLEWARE`) serves whatever `collectstatic` gathers into `staticfiles/`. Re-run this after any static-asset change.
3. Run under a real WSGI server, not `runserver` — the dev server isn't hardened for unattended use. `requirements.txt` installs the right one per OS automatically (environment markers): `gunicorn` on Mac/Linux, `waitress` on Windows — **gunicorn does not run on Windows at all**, it depends on Unix-only OS features.
   ```bash
   # Mac/Linux
   gunicorn inventory.wsgi:application --bind 0.0.0.0:8000

   # Windows
   waitress-serve --host=0.0.0.0 --port=8000 inventory.wsgi:application
   ```
4. Schedule `python3 manage.py backup_db` (cron/Task Scheduler, e.g. nightly) — copies `db.sqlite3` to `db_backups/` with a timestamp and prunes anything older than `--keep-days` (default 30).

### Hosting decision: on-site at the plant, not cloud (revisit if this changes)

Decided 2026-09-07. The whole app — code and `db.sqlite3` together — runs on one machine at the plant, not split across cloud + on-site or hosted purely in the cloud. Reasoning:

- The database needs to physically stay on-site.
- Splitting app (cloud) from database (on-site) was considered and rejected — every request would round-trip to the plant over the internet, the plant's connection becomes a single point of failure for *everyone* including remote users, and it costs more than either pure option while getting the reliability of neither.
- Pure cloud hosting was considered and rejected for now — it would mean plant employees lose access entirely if the plant's internet drops, even though they're standing next to the machines the app tracks.

**For the owner's remote access**: install [Tailscale](https://tailscale.com) on the plant machine and on the owner's devices (free tier: up to 6 users, unlimited devices). This puts the owner's phone/laptop virtually on the plant's LAN — they reach the exact same app and database as someone on-site, from anywhere, without exposing anything to the open internet. Once set up, add the plant machine's Tailscale hostname (looks like `<machine-name>.<tailnet-name>.ts.net`) to `DJANGO_ALLOWED_HOSTS` in `.env` alongside its LAN IP.

If this ever moves to the cloud instead, the deployment steps above (WhiteNoise, gunicorn, `.env`) carry over unchanged — the only new work would be picking a host and, if the platform doesn't offer real persistent disk, migrating off SQLite to Postgres.

### Public quote form: Cloudflare Tunnel, scoped to `/quote/*` only

Decided 2026-09-14. The customer-facing quote form (`/quote/<token>/`) needs to be reachable from outside — by people who aren't on Tailscale — without exposing the rest of the app (admin, employee portal, API) to the public internet.

Rejected approaches:
- **Exposing the whole app publicly** — the employee portal (PIN-only) and admin login would then be open to credential-stuffing/brute-force from anywhere, not just the plant network.
- **Migrating `mattadrawing.com`'s DNS to Cloudflare** — the domain's DNS is managed by the hosting provider behind GoDaddy (nameservers `ns1/ns2.md-19.webhostbox.net`, a cPanel host), and that same account also serves the company's live website and email. Moving the whole zone risked breaking both over one mistyped record. Cloudflare's dashboard also no longer allows adding a bare subdomain as its own zone (root-domain-only now), which would have needed this anyway.

What's actually in place: a **Cloudflare Tunnel** (`cloudflared`, tunnel name `matta-quote-tunnel`) runs as a Windows service on the plant PC (installed via the token command from the Cloudflare Zero Trust dashboard → Networks → Tunnels). It has one published application route: hostname `quote.mattadrawing.com`, **path `^/quote`**, forwarding to `http://localhost:8000` — anything on that hostname outside `/quote/*` hits Cloudflare's automatic catch-all and 404s before ever reaching Django. This means only the quote form is reachable from that subdomain; the rest of the app's URLs simply aren't routed.

DNS: a single CNAME record was added in cPanel — `quote` → `<tunnel-id>.cfargotunnel.com` — **without** moving `mattadrawing.com`'s nameservers to Cloudflare at all. `cfargotunnel.com` is Cloudflare's own domain, so this CNAME works from any DNS provider; the tunnel and its routing are entirely independent of who's authoritative for the rest of the zone. The main website and email records were never touched.

Three settings this depends on (`inventory/settings.py`): `DJANGO_CSRF_TRUSTED_ORIGINS` must include `https://quote.mattadrawing.com` (Cloudflare terminates HTTPS at its edge; without this, the quote form's POST fails CSRF checks), `SECURE_PROXY_SSL_HEADER` is set to trust Cloudflare's `X-Forwarded-Proto` header so Django knows the original request was HTTPS (additive, doesn't affect direct Tailscale access, which never sends that header), and `PUBLIC_QUOTE_BASE_URL` fixes the link in quote-request emails (`materials/views.py::_dispatch_quote_email`) to always point at `https://quote.mattadrawing.com` regardless of which host the admin happened to be browsing on — without it, `request.build_absolute_uri()` would build the link from the admin's own (Tailscale-only) request host, producing an email link no external customer could ever open. Live-verified 2026-09-15: `quote.mattadrawing.com` reachable end to end (DNS → tunnel → app) from a real external device.

### Why `db.sqlite3` isn't tracked in git

It was committed and untracked twice before (`git log` shows both flips, each reverted within days) — untracking it broke a workflow where the database was being passed between machines via `git pull`/`push` in lieu of a real deployment. Now that the app runs as one persistent instance rather than being re-cloned onto different machines, that workflow no longer applies: `git pull` only touches code, and `db.sqlite3` sits on the deployed machine untouched by git, backed up separately via `backup_db`. **If you ever go back to syncing data between machines via git, this file needs to be tracked again** — the two reverts weren't accidents.

## Importing from the client's spreadsheet (`materials/management/commands/import_excel.py`)

`import_excel` fully replaces every `Material` row on each run (delete-all + reimport in one transaction) — it's a one-time/occasional full reimport tool, not routine syncing, since a coil's `coil_no` may already be printed on a physical QR tag by the time you run it again. `--reset-sequence` additionally rewinds `coil_no` to start counting from 1 — deleting rows alone doesn't rewind SQLite's autoincrement counter, so without this flag newly imported coils keep counting up from wherever the old ones left off (this is why coil numbering on a machine that's had earlier test data can start well above 1). **Only use `--reset-sequence` if nothing in the current data has a printed QR tag yet** — every coil gets renumbered.

The real client sheet has extra columns beyond `COLUMN_MAP` — `ISSUED QTY 1`, `ISSUED QTY 2`, `ISSUED QTY 3` — recording how much of a coil was already issued/used before this app existed. These are summed into `Material.legacy_used_weight` on import (0 if the columns are missing or blank), so a coil's used/unused status correctly reflects real-world history from day one instead of every freshly imported coil looking "Unused" regardless of prior usage.

## Architecture

### Models (`materials/models.py`)

- **GateEntry** — one truck's delivery, logged from its invoice (vendor, vehicle no., invoice no., total weight) before any of its coils are individually registered. `vendor` is the raw-material *supplier* who delivered the truck — one per delivery. A truck can carry a mixed load of different coil *brands* though, so `company` (the brand/mill of the coil — e.g. "Tata Steel") lives on `GateEntryLot` instead, along with grade/size, since those can also vary lot to lot within one delivery. `total_weight` is the figure for the *entire* delivery. `no_of_coils()` sums every lot's coil count; `weight_per_coil()` is a rough average (`total_weight ÷ total coils across all lots`) — a paper reference, not a measurement, and less precise the more a delivery's lots vary in size. `coils_registered()`/`coils_remaining()`/`is_complete()` roll up the same stats from every lot
- **GateEntryLot** — one company(brand)/grade/size batch within a gate entry (e.g. "3 coils of Tata Steel EN8D 1.2mm"), all delivered by the same vendor (`GateEntryLot.gate_entry.vendor`). Its own `coils_registered()`/`coils_remaining()`/`is_complete()` cap how many coils can be registered against *that lot specifically* — New Coil Entry always targets a lot, never a gate entry directly, and always requires an open (incomplete) one — see Employee workflow below
- **Material** — raw coil inventory (grade, size mm, vendor, quantity kg, heat no). `weight_used()`/`weight_remaining()`/`is_used_up()` are the single source of truth for how much of a coil has been allocated to orders — admin, the REST API, and the coil-picking flow all call these rather than each computing their own aggregate. `weight_used()` = weight allocated via `OrderCoilPick`s **plus** `legacy_used_weight` (usage that happened before this coil was tracked in the app, imported from the spreadsheet's `ISSUED QTY 1/2/3` columns — see the import section below). `archived_at` marks a mistaken entry as archived (hidden from normal admin/employee views, excluded from coil picking) without ever deleting it or renumbering `coil_no` — that number may already be on a physical QR tag, so it's never reused or reassigned. `lot` links a coil to the gate entry lot (vendor/grade/size batch within a truck delivery) it physically came from (null for historical/imported coils, which predate gate entries); `invoice_weight` is that lot's parent gate entry's rough average per-coil figure, copied onto the coil at registration time — `quantity` remains the actual measured weight and is what every usage calculation (`weight_used()` etc.) is based on, never `invoice_weight`
- **OrderCoilPick** — one coil scanned/picked against an order's raw-material requirement (`materials/views.py::pick_coil_for_order`). `weight_allocated` need not be the coil's full remaining weight — a coil can be split across multiple orders, the same partial-use philosophy `weight_used()` already applies. `output_equivalent()` converts `weight_allocated` into finished-product terms via the `AllowedCoilSpec.raw_material_ratio` matching this coil's grade/size under the order's product type (falls back to 1:1 if none matches)
- **GradeOption** / **SizeOption** — admin-managed lists of valid grades/sizes shown as tap-to-pick options on the Gate Entry form's lot rows (`/gate-entry/`); keeps the picker in sync without a code change. `manage.py backfill_options` (one-off, re-runnable) seeds these from every distinct grade/size already in `Material`, as-is — it doesn't try to merge differently-spelled duplicates (e.g. `EN8D` vs `EN-8D`), that's left for an admin to clean up afterward via the option admin pages
- **ProductType** — final product definition with preset grade + size; has ordered ProcessSteps and AllowedCoilSpecs. `item_code` is the product's identifying code (was called `name` until it was renamed to match how the business actually references products). Grade + size is the identity of a product type — `unique_together` enforces one ProductType per grade/size combo
- **AllowedCoilSpec** — admin-configured coil grade/size that can be used as raw material for a ProductType. `raw_material_ratio` (default `1.000`) is "kg of this raw material needed to produce 1 kg of finished product" (e.g. `1.100` = 10% wastage) — since yield varies by which raw material spec is used, the ratio lives here rather than on ProductType itself, and it's what converts an order's `quantity` (an output weight) into how much raw material actually needs to be picked
- **ProcessStep** — one manufacturing step belonging to a ProductType (ordered)
- **ProductionJob** — links a picked coil (`OrderCoilPick`) to a ProductType + Order — one job per picked coil. `status` (`pending`/`in_progress`/`on_hold`/`completed`) is a rollup of its steps' latest StepLog, computed by `recalculate_status()` — the single source of truth, called from the employee step-update view and from `StepLogAdmin` (add/change/delete) so it stays correct no matter where a StepLog came from. A step logged `failed` (only possible via the admin — the employee portal only ever logs `in_progress`/`completed`) always puts the job on `on_hold`, shown to employees as a red banner on the job detail page, so a failed step can't silently look pending/in-progress forever
- **StepLog** — append-only log of step status changes for a job. `status` includes `failed`, settable only through the admin (`/admin/materials/steplog/`) — there's no "mark failed" action in the employee portal
- **Customer** — company name, email, phone, UUID quote token (regenerated after each form submission)
- **Order** — customer requirement: product_type FK, grade, size, quantity, delivery date, status. `order_no` (displayed as `ORD-####`) is a separate field from the primary key, assigned sequentially on creation and **kept gap-free**: deleting an order renumbers every order after it down by one, via a `post_delete` signal receiver (`_close_order_number_gap` in `materials/models.py`) that fires regardless of how the delete happens (admin single/bulk delete, `.delete()` on a queryset). This is the opposite of `coil_no`/`job_no`, which are deliberately *never* reused because they may already be on a printed tag — an order number is just an internal reference nobody prints ahead of time, so closing the gap was requested instead. One consequence: an order's displayed number **can change** if an earlier order is deleted, so don't treat `ORD-####` as a permanent identifier in code — use the primary key for that (`Order.pk`, used throughout `materials/urls.py`/views for routing)

### Order statuses
`pending` → `confirmed` → `in_production` → `completed` (or `cancelled`)

- **pending**: submitted via customer quote form, awaiting admin review
- **confirmed**: admin approved, ready for employees to pick coils against it
- **in_production**: first coil picked against this order
- **completed**: admin dispatched

### Raw-material stock check on confirm

`Order.available_raw_material_output()` (`materials/models.py`) sums how much finished-product output the currently in-stock raw material could cover — across every `AllowedCoilSpec` on the order's product type (grade/size + ratio), or any non-archived coil with remaining weight if none are configured (same wildcard fallback the picking flow uses). `has_sufficient_raw_material()` compares that against `Order.quantity`; both return `None` if no product type is set yet (nothing to check against).

Checked once, at `order_confirm` (`materials/views.py`) — not at quote submission, since the exact grade/size requirement is only locked in once a product type is assigned, which confirming already requires. If stock looks short, the admin gets an immediate `messages.warning()` on confirming, **and** a persistent "⚠️ Low stock" badge stays on that order's row in `/orders/` for as long as it's still true — scoped to `confirmed`/`in_production` orders only (not pending/completed/cancelled, where it isn't actionable). On-screen only, deliberately no email — the owner sees it browsing the dashboard, not in an inbox.

### Two user roles

**Admin/staff** (`is_staff=True` Django user):
- Access via `/admin-login/` → Django admin (`/admin/`) or `/orders/` dashboard
- Can confirm/reject/dispatch orders, manage product types, view all data
- Materials list shows a Used/Unused status badge and filter (computed from allocated weight, not stored). A mistaken coil entry should be **archived** (bulk action in the admin), not deleted — archiving hides it from the materials list and from employee coil-selection/picking, but keeps its `coil_no` intact. Archived coils are hidden by default; `?archived=yes` or `?archived=all` in the admin URL shows them

**Employee** (PIN-based session):
- Access via `/employee/` — requires PIN (`EMPLOYEE_PIN` in `.env`)
- Can register coils, pick coils against orders, update production step progress
- "Log out" button on the portal (`/employee-logout/`, POST-only) clears the session — the shared PIN stays valid, only that browser's login is ended

## Employee workflow

1. New Coil Entry — always starts with a gate entry, never a bare coil form:
   - Gate Entry → `/gate-entry/` — one page, one submission, for the whole truck. Top: date/vehicle no./**vendor** (the supplier)/invoice no./total weight. Vehicle no. is normalized to uppercase, letters/digits only, as you type (no fixed slot layout — real plates vary too much in segment length, e.g. RTO code can be 1-4 digits, series 1-4 letters, to force one); `GateEntryForm.clean_vehicle_no()` uppercases again server-side as a safety net for anything submitted without JS. Below: one or more **lots** (**company** — the coil's brand, grade, size, coil count — grade/size via the usual tap-to-pick overlay) rendered as collapsible rows, each summarized when collapsed; "+ Add Lot" appends another row (collapsing the rest) for a delivery mixing brands/grades/sizes, so the whole truck is logged in one go. Vendor and company are free text with autosuggest (`/gate-entry/autocomplete/?field=company|vendor&q=...`, `materials/views.py::material_field_autocomplete`) drawn from values already used in `Material` — keeps "Tata Steel" from also ending up as "TATA STEEL" and "Tata steel" across different gate entries. Each row past the first also has "✕ Remove This Lot" to undo one added by mistake before saving (client-side only — removing re-indexes the remaining rows so the formset's field names stay contiguous; at least one lot is required, so the button is hidden when only one remains). The GateEntry and every `GateEntryLot` row are created together, atomically (`materials/views.py::gate_entry_form`, backed by a `GateEntryLotFormSet`) — an invalid lot rolls back the whole submission rather than leaving a partial gate entry behind. Saving redirects to the gate entry's detail page.
   - Add Lot (later) → `/gate-entry/<pk>/add-lot/` — a single-lot version of the same form, for a delivery that turns out to have another grade/size/vendor beyond what was logged initially. Saving returns to the gate entry's detail page.
   - Gate Entry detail → `/gate-entry/<pk>/` — lists every lot logged so far with its progress, a "+ Add Another Lot" link, a link into coil registration for each lot that still has room, an "✏️ Edit" link to fix the gate entry's own top-level fields, and — for any lot with zero coils registered against it yet — a "✕ Remove (added by mistake)" action (`/gate-entry/lot/<lot_pk>/delete/`) to undo it after the fact. Once a lot has even one coil, the option disappears (`Material.lot` also uses `on_delete=PROTECT`, so a stray attempt would fail loudly rather than orphan real coils).
   - Edit Gate Entry → `/gate-entry/<pk>/edit/` (`materials/views.py::gate_entry_edit`) — the same date/vendor/vehicle no./invoice no./total weight fields as creation, reusing `GateEntryForm` bound to the existing instance. Unlike a lot or a registered coil, nothing here is locked once coils exist against it — these are paper/reference details, not something coil registration depends on being immutable — so it's editable at any point, before or after lots/coils are registered.
   - Select Gate Entry → `/gate-entry/select/` — lists every lot (across all gate entries) that still has coils left to register; "Add Another Coil" on the printed tag returns here once a lot is complete, or straight back into the same lot if it still has room.
   - Register a coil → `/gate-entry/lot/<lot_pk>/coil/` → prints QR tag. Vendor comes from the gate entry, company/grade/size from the lot — none of it resubmitted, so a tampered/stale form can't override them — shown read-only alongside the gate entry's `weight_per_coil()` (a rough average across every lot) for reference. The employee enters heat no. and the coil's **actual measured weight** (`quantity`) — deliberately separate from that paper `invoice_weight`, since real coils rarely weigh exactly the computed average, more so once a delivery mixes multiple grades/sizes. Once a lot's `no_of_coils` have been registered, it shows complete and blocks further entries against it — a new lot (or gate entry) is required for the rest.
2. Pick Coils for Order — an order isn't fulfilled by cutting a part from one coil; it's fulfilled by picking a number of whole (or partially-used) coils as raw material:
   - Select Order → `/select-order/` — lists confirmed orders (not yet started) and in-production orders separately.
   - Picking hub → `/order/<pk>/select-coil/` (`materials/views.py::select_coil_for_order`) — shows the order's fulfillment progress (`Order.picked_output_weight()` vs `Order.quantity`), a coil-number field for scanning (a barcode/QR scanner just types the coil's tag text into the field — no camera/JS scanning involved), and a browse list of coils matching the order's `AllowedCoilSpec`s (all coils with remaining weight if none are configured). The browse list is sorted **best-fit first** — coils whose remaining weight is closest to what the order still needs (converted through that coil's matching spec ratio via `_ratio_for_coil`, same helper `pick_coil_for_order` uses for its suggested weight) are listed above coils that would over- or under-shoot by more, rather than just showing newest coils first. Also lists every coil already picked for this order so far.
   - Confirm pick → `/order/<pk>/pick-coil/<coil_pk>/` (`materials/views.py::pick_coil_for_order`) — shown after scanning or clicking a coil from the browse list. Suggests a weight capped at both the coil's remaining weight and how much raw material the order still needs (converted via the matching `AllowedCoilSpec.raw_material_ratio`), but the employee can adjust it down for a partial pick. Product type comes from the order, never resubmitted. Confirming atomically creates the `OrderCoilPick`, a `ProductionJob` for it, and one pending `StepLog` per `ProcessStep` — an invalid submission creates none of them. Blocked (with a clear reason) if the coil is archived, exhausted, doesn't match the order's allowed specs, the order's requirement is already fully picked, or the order has no product type set.
3. Update Progress → `/production-board/` → `/job/<pk>/` to tick steps, one job per picked coil

## Order-first coil picking (important constraint)

When an order has a ProductType with AllowedCoilSpecs configured, only coils matching those grade/size specs can be picked (`_coil_matches_order_specs` in `materials/views.py`, used by both the scan lookup and the browse list). If no specs are configured, all coils with remaining weight are eligible. Orders **cannot be confirmed** without a product type set, and picking is blocked entirely without one.

## Quote form flow

Admin enters company name/email/phone in the Orders dashboard → sends a unique link via email → customer fills the form → order is created with status `pending` → admin reviews. The quote link is **single-use**: the token regenerates after submission, making the old URL a 404.

## REST API (`materials/api.py`, `materials/serializers.py`)

Read-only DRF API under `/api/` — `orders`, `coils`, `jobs`, `product-types`. Staff-only (`IsAdminUser`, session auth — same login as `/admin/`). Deliberately read-only: the state-transition rules (product type required to confirm, sequential step unlock, atomic pick creation) live in `materials/views.py` and aren't re-implemented here — this surface is for reading data out, not changing it. `coils` supports `?remaining=true` and `?include_archived=true` (archived coils excluded by default); `orders` and `jobs` support `?status=`; `jobs` also supports `?order=<id>` and exposes `coil_no`/`weight_allocated` (from its `OrderCoilPick`). Browsable API login at `/api-auth/`.

## Commit style

No `Co-Authored-By` trailers in commit messages.
