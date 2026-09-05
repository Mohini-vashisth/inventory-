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
| `EMPLOYEE_PIN` | Shared PIN for employee portal (default: `1234`) |
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_USE_TLS` | SMTP config |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | SMTP credentials — Gmail needs an App Password, not the account password |
| `DEFAULT_FROM_EMAIL` | Sender address for quote emails |

## Deploying to a real machine (not local dev)

Local dev (`DEBUG=True`, `manage.py runserver`) skips several things a real deployment needs:

1. Set `.env` from `inventory/.env.example` — at minimum `DJANGO_DEBUG=False`, a real `DJANGO_SECRET_KEY`, and `DJANGO_ALLOWED_HOSTS` set to the machine's hostname/IP.
2. `python3 manage.py collectstatic --noinput` — with `DEBUG=False`, `runserver` no longer serves CSS/JS itself. WhiteNoise (already in `MIDDLEWARE`) serves whatever `collectstatic` gathers into `staticfiles/`. Re-run this after any static-asset change.
3. Run under `gunicorn`, not `runserver` — the dev server isn't hardened for unattended use:
   ```bash
   gunicorn inventory.wsgi:application --bind 0.0.0.0:8000
   ```
4. Schedule `python3 manage.py backup_db` (cron/Task Scheduler, e.g. nightly) — copies `db.sqlite3` to `db_backups/` with a timestamp and prunes anything older than `--keep-days` (default 30).

### Why `db.sqlite3` isn't tracked in git

It was committed and untracked twice before (`git log` shows both flips, each reverted within days) — untracking it broke a workflow where the database was being passed between machines via `git pull`/`push` in lieu of a real deployment. Now that the app runs as one persistent instance rather than being re-cloned onto different machines, that workflow no longer applies: `git pull` only touches code, and `db.sqlite3` sits on the deployed machine untouched by git, backed up separately via `backup_db`. **If you ever go back to syncing data between machines via git, this file needs to be tracked again** — the two reverts weren't accidents.

## Architecture

### Models (`materials/models.py`)

- **Material** — raw coil inventory (grade, size mm, vendor, quantity kg, heat no)
- **CoilPart** — a physical piece cut from a coil (part_no format: `COIL0001-A`)
- **GradeOption** / **SizeOption** — admin-managed lists of valid grades/sizes shown as tap-to-pick options on the New Coil Entry form (`/material-form/`); keeps the picker in sync without a code change
- **ProductType** — final product definition with preset grade + size; has ordered ProcessSteps and AllowedCoilSpecs. Grade + size is the identity of a product type — `unique_together` enforces one ProductType per grade/size combo
- **AllowedCoilSpec** — admin-configured coil grade/size that can be used as raw material for a ProductType
- **ProcessStep** — one manufacturing step belonging to a ProductType (ordered)
- **ProductionJob** — links a CoilPart to a ProductType + Order; tracks overall status
- **StepLog** — append-only log of step status changes for a job
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

**Employee** (PIN-based session):
- Access via `/employee/` — requires PIN (`EMPLOYEE_PIN` in `.env`)
- Can register coils, cut parts, update production step progress
- "Log out" button on the portal (`/employee-logout/`, POST-only) clears the session — the shared PIN stays valid, only that browser's login is ended

## Employee workflow

1. New Coil Entry → `/material-form/` → prints QR tag
   - Grade and size are chosen from a tap-to-pick overlay, populated from `GradeOption`/`SizeOption` (admin-managed). If either list is empty, the picker shows "not configured" and the form can't be submitted.
2. Create New Part → `/select-order/` → `/order/<pk>/select-coil/` → `/coil/<pk>/parts/`
   - Order determines product type; only AllowedCoilSpec-matching coils are shown
   - Product type is locked from the order — employees cannot override it
3. Update Progress → `/production-board/` → `/job/<pk>/` to tick steps

## Order-first part cutting (important constraint)

When an order has a ProductType with AllowedCoilSpecs configured, only coils matching those grade/size specs are shown in step 2. If no specs are configured, all coils with remaining weight are shown. Orders **cannot be confirmed** without a product type set.

## Quote form flow

Admin enters company name/email/phone in the Orders dashboard → sends a unique link via email → customer fills the form → order is created with status `pending` → admin reviews. The quote link is **single-use**: the token regenerates after submission, making the old URL a 404.

## REST API (`materials/api.py`, `materials/serializers.py`)

Read-only DRF API under `/api/` — `orders`, `coils`, `jobs`, `product-types`. Staff-only (`IsAdminUser`, session auth — same login as `/admin/`). Deliberately read-only: the state-transition rules (product type required to confirm, sequential step unlock, atomic part creation) live in `materials/views.py` and aren't re-implemented here — this surface is for reading data out, not changing it. `coils` supports `?remaining=true`; `orders` and `jobs` support `?status=`; `jobs` also supports `?order=<id>`. Browsable API login at `/api-auth/`.

## Commit style

No `Co-Authored-By` trailers in commit messages.
