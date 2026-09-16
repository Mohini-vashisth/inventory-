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

- **GateEntry** — one truck's delivery, logged from its invoice (company, vehicle no., bill no., invoice no., total weight) before any of its coils are individually registered. A truck can carry a mixed load — different grades, sizes, even different vendors within one delivery — so all of that lives on `GateEntryLot`, not here; `total_weight` is the figure for the *entire* delivery. `no_of_coils()` sums every lot's coil count; `weight_per_coil()` is a rough average (`total_weight ÷ total coils across all lots`) — a paper reference, not a measurement, and less precise the more a delivery's lots vary in size. `coils_registered()`/`coils_remaining()`/`is_complete()` roll up the same stats from every lot
- **GateEntryLot** — one vendor/grade/size batch within a gate entry (e.g. "3 coils of EN8D 1.2mm from Vendor A"). Vendor lives here rather than on GateEntry, since a single truck can carry material sourced from more than one vendor. Its own `coils_registered()`/`coils_remaining()`/`is_complete()` cap how many coils can be registered against *that lot specifically* — New Coil Entry always targets a lot, never a gate entry directly, and always requires an open (incomplete) one — see Employee workflow below
- **Material** — raw coil inventory (grade, size mm, vendor, quantity kg, heat no). `weight_used()`/`weight_remaining()`/`is_used_up()` are the single source of truth for how much of a coil has been cut — admin, the REST API, and the part-cutting form all call these rather than each computing their own aggregate. `weight_used()` = weight cut into `CoilPart`s via the app **plus** `legacy_used_weight` (usage that happened before this coil was tracked in the app, imported from the spreadsheet's `ISSUED QTY 1/2/3` columns — see the import section below). `archived_at` marks a mistaken entry as archived (hidden from normal admin/employee views, excluded from part-cutting) without ever deleting it or renumbering `coil_no` — that number may already be on a physical QR tag, so it's never reused or reassigned. `lot` links a coil to the gate entry lot (vendor/grade/size batch within a truck delivery) it physically came from (null for historical/imported coils, which predate gate entries); `invoice_weight` is that lot's parent gate entry's rough average per-coil figure, copied onto the coil at registration time — `quantity` remains the actual measured weight and is what every usage calculation (`weight_used()` etc.) is based on, never `invoice_weight`
- **CoilPart** — a physical piece cut from a coil (part_no format: `COIL0001-A`)
- **GradeOption** / **SizeOption** — admin-managed lists of valid grades/sizes shown as tap-to-pick options on the Gate Entry form's lot rows (`/gate-entry/`); keeps the picker in sync without a code change. `manage.py backfill_options` (one-off, re-runnable) seeds these from every distinct grade/size already in `Material`, as-is — it doesn't try to merge differently-spelled duplicates (e.g. `EN8D` vs `EN-8D`), that's left for an admin to clean up afterward via the option admin pages
- **ProductType** — final product definition with preset grade + size; has ordered ProcessSteps and AllowedCoilSpecs. Grade + size is the identity of a product type — `unique_together` enforces one ProductType per grade/size combo
- **AllowedCoilSpec** — admin-configured coil grade/size that can be used as raw material for a ProductType
- **ProcessStep** — one manufacturing step belonging to a ProductType (ordered)
- **ProductionJob** — links a CoilPart to a ProductType + Order. `status` (`pending`/`in_progress`/`on_hold`/`completed`) is a rollup of its steps' latest StepLog, computed by `recalculate_status()` — the single source of truth, called from the employee step-update view and from `StepLogAdmin` (add/change/delete) so it stays correct no matter where a StepLog came from. A step logged `failed` (only possible via the admin — the employee portal only ever logs `in_progress`/`completed`) always puts the job on `on_hold`, shown to employees as a red banner on the job detail page, so a failed step can't silently look pending/in-progress forever
- **StepLog** — append-only log of step status changes for a job. `status` includes `failed`, settable only through the admin (`/admin/materials/steplog/`) — there's no "mark failed" action in the employee portal
- **Customer** — company name, email, phone, UUID quote token (regenerated after each form submission)
- **Order** — customer requirement: product_type FK, grade, size, quantity, delivery date, status

### Order statuses
`pending` → `confirmed` → `in_production` → `completed` (or `cancelled`)

- **pending**: submitted via customer quote form, awaiting admin review
- **confirmed**: admin approved, ready for employees to cut parts
- **in_production**: first part cut against this order
- **completed**: admin dispatched

### Two user roles

**Admin/staff** (`is_staff=True` Django user):
- Access via `/admin-login/` → Django admin (`/admin/`) or `/orders/` dashboard
- Can confirm/reject/dispatch orders, manage product types, view all data
- Materials list shows a Used/Unused status badge and filter (computed from cut weight, not stored). A mistaken coil entry should be **archived** (bulk action in the admin), not deleted — archiving hides it from the materials list and from employee coil-selection/part-cutting, but keeps its `coil_no` intact. Archived coils are hidden by default; `?archived=yes` or `?archived=all` in the admin URL shows them

**Employee** (PIN-based session):
- Access via `/employee/` — requires PIN (`EMPLOYEE_PIN` in `.env`)
- Can register coils, cut parts, update production step progress
- "Log out" button on the portal (`/employee-logout/`, POST-only) clears the session — the shared PIN stays valid, only that browser's login is ended

## Employee workflow

1. New Coil Entry — always starts with a gate entry, never a bare coil form:
   - Gate Entry → `/gate-entry/` — one page, one submission, for the whole truck. Top: date/vehicle no./company/bill no./invoice no./total weight. Below: one or more **lots** (vendor, grade, size, coil count — grade/size via the usual tap-to-pick overlay) rendered as collapsible rows, each summarized when collapsed; "+ Add Lot" appends another row (collapsing the rest) for a mixed-grade/vendor delivery, so the whole truck is logged in one go. Company and vendor are free text with autosuggest (`/gate-entry/autocomplete/?field=company|vendor&q=...`, `materials/views.py::material_field_autocomplete`) drawn from values already used in `Material` — keeps "Tata Steel" from also ending up as "TATA STEEL" and "Tata steel" across different gate entries. Each row past the first also has "✕ Remove This Lot" to undo one added by mistake before saving (client-side only — removing re-indexes the remaining rows so the formset's field names stay contiguous; at least one lot is required, so the button is hidden when only one remains). The GateEntry and every `GateEntryLot` row are created together, atomically (`materials/views.py::gate_entry_form`, backed by a `GateEntryLotFormSet`) — an invalid lot rolls back the whole submission rather than leaving a partial gate entry behind. Saving redirects to the gate entry's detail page.
   - Add Lot (later) → `/gate-entry/<pk>/add-lot/` — a single-lot version of the same form, for a delivery that turns out to have another grade/size/vendor beyond what was logged initially. Saving returns to the gate entry's detail page.
   - Gate Entry detail → `/gate-entry/<pk>/` — lists every lot logged so far with its progress, a "+ Add Another Lot" link, a link into coil registration for each lot that still has room, and — for any lot with zero coils registered against it yet — a "✕ Remove (added by mistake)" action (`/gate-entry/lot/<lot_pk>/delete/`) to undo it after the fact. Once a lot has even one coil, the option disappears (`Material.lot` also uses `on_delete=PROTECT`, so a stray attempt would fail loudly rather than orphan real coils).
   - Select Gate Entry → `/gate-entry/select/` — lists every lot (across all gate entries) that still has coils left to register; "Add Another Coil" on the printed tag returns here once a lot is complete, or straight back into the same lot if it still has room.
   - Register a coil → `/gate-entry/lot/<lot_pk>/coil/` → prints QR tag. Company comes from the gate entry, vendor/grade/size from the lot — none of it resubmitted, so a tampered/stale form can't override them — shown read-only alongside the gate entry's `weight_per_coil()` (a rough average across every lot) for reference. The employee enters heat no. and the coil's **actual measured weight** (`quantity`) — deliberately separate from that paper `invoice_weight`, since real coils rarely weigh exactly the computed average, more so once a delivery mixes multiple grades/sizes. Once a lot's `no_of_coils` have been registered, it shows complete and blocks further entries against it — a new lot (or gate entry) is required for the rest.
2. Create New Part → `/select-order/` → `/order/<pk>/select-coil/` → `/coil/<pk>/parts/`
   - Order determines product type; only AllowedCoilSpec-matching coils are shown
   - Product type is locked from the order — employees cannot override it
3. Update Progress → `/production-board/` → `/job/<pk>/` to tick steps

## Order-first part cutting (important constraint)

When an order has a ProductType with AllowedCoilSpecs configured, only coils matching those grade/size specs are shown in step 2. If no specs are configured, all coils with remaining weight are shown. Orders **cannot be confirmed** without a product type set.

## Quote form flow

Admin enters company name/email/phone in the Orders dashboard → sends a unique link via email → customer fills the form → order is created with status `pending` → admin reviews. The quote link is **single-use**: the token regenerates after submission, making the old URL a 404.

## REST API (`materials/api.py`, `materials/serializers.py`)

Read-only DRF API under `/api/` — `orders`, `coils`, `jobs`, `product-types`. Staff-only (`IsAdminUser`, session auth — same login as `/admin/`). Deliberately read-only: the state-transition rules (product type required to confirm, sequential step unlock, atomic part creation) live in `materials/views.py` and aren't re-implemented here — this surface is for reading data out, not changing it. `coils` supports `?remaining=true` and `?include_archived=true` (archived coils excluded by default); `orders` and `jobs` support `?status=`; `jobs` also supports `?order=<id>`. Browsable API login at `/api-auth/`.

## Commit style

No `Co-Authored-By` trailers in commit messages.
