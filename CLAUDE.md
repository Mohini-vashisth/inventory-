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

- Django 4.2, django-jazzmin (admin theme), qrcode + Pillow (coil QR tags), python-dotenv, djangorestframework

## Environment variables (`.env` in `inventory/`)

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | Django secret key |
| `EMPLOYEE_PIN` | Shared PIN for employee portal (default: `1234`) |
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_USE_TLS` | SMTP config |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | SMTP credentials |
| `DEFAULT_FROM_EMAIL` | Sender address for quote emails |

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
