# Inventory & Manufacturing System

A Django app for a steel coil manufacturing business, covering the full workflow: raw material receipt → coil registration → order management → part cutting → step-by-step production tracking → dispatch.

---

## Features

- **Gate Entry** — log a truck's delivery from its invoice (vendor, vehicle no., bill/invoice no., total weight) as one or more brand/grade/size lots, before any coil is individually registered
- **New Coil Entry** — register each coil against an open gate entry lot; prints a QR tag automatically. Company/vendor/grade/size are locked from the gate entry/lot, capped at the lot's coil count
- **Part tracking** — log pieces cut from a coil, with weight, length, and an all-or-nothing atomic write (never a part with no job)
- **Order-first production** — customers submit requirements via a unique quote-form link; admin confirms an order, employees can then only cut parts matching that order's allowed grade/size specs
- **Production jobs & step tracking** — a cut part is assigned a product type with ordered manufacturing steps; employees tick off each step with a full audit trail (`StepLog`)
- **Admin dashboard** — Django admin (themed with Jazzmin) with progress bars, status badges, used/unused coil filters, and bulk actions (archive coils, mark jobs on hold, etc.)
- **Read-only REST API** — `orders`, `coils`, `jobs`, `product-types` under `/api/`, staff-only
- **Two access paths** — a PIN-based employee portal (tablet-friendly) and a Django-auth admin/staff area, both reachable from the home screen

See `CLAUDE.md` for the full architecture reference, deployment setup, and decision history.

---

## Tech Stack

- **Backend** — Python 3, Django 4.2, Django REST Framework
- **Database** — SQLite
- **Frontend** — Plain HTML/CSS/vanilla JS (no framework)
- **Admin theme** — django-jazzmin
- **Key libraries** — `qrcode`/`Pillow` (QR tags), `pandas`/`openpyxl` (spreadsheet import), `whitenoise` (static files), `gunicorn` (Mac/Linux) / `waitress` (Windows) as the production WSGI server

---

## Project Structure

```
inventory-/
├── inventory/                       # Django project
│   ├── inventory/                   # settings.py, urls.py, wsgi.py, asgi.py
│   ├── materials/                   # the one Django app
│   │   ├── models.py                # GateEntry, GateEntryLot, Material, CoilPart,
│   │   │                            # ProductType, ProcessStep, ProductionJob, StepLog,
│   │   │                            # Customer, Order, GradeOption, SizeOption
│   │   ├── views.py
│   │   ├── forms.py
│   │   ├── admin.py
│   │   ├── api.py / serializers.py  # REST API
│   │   ├── management/commands/     # import_excel, backfill_options, backup_db, benchmark_queries
│   │   ├── templatetags/
│   │   ├── tests.py
│   │   └── migrations/
│   ├── templates/                   # home.html + templates/materials/*.html
│   ├── manage.py
│   └── .env.example
├── requirements.txt
├── CLAUDE.md                        # full architecture & deployment reference
└── .github/workflows/tests.yml      # CI: check, migration check, full test suite
```

---

## Data Model

```
GateEntry (one truck's delivery)
  └── GateEntryLot (one brand/grade/size batch within it)
        └── Material (a registered coil)
              └── CoilPart (a cut piece)
                    └── ProductionJob (linked to a ProductType + Order)
                          ├── ProductType → ProcessStep (ordered steps)
                          └── StepLog (status update per step, full history)

Customer → Order (quote/requirement) → ProductionJob
```

| Model | Purpose |
|-------|---------|
| `GateEntry` | A truck delivery — vendor, vehicle no., bill/invoice no., total weight |
| `GateEntryLot` | One brand/grade/size/coil-count batch within a gate entry |
| `Material` | A registered coil — grade, size, company, heat no., quantity |
| `CoilPart` | A piece cut from a coil — weight, length, cut date |
| `GradeOption` / `SizeOption` | Admin-managed valid grade/size options for the tap-to-pick pickers |
| `ProductType` | A product definition with preset grade/size and its ordered manufacturing steps |
| `ProcessStep` | A named step belonging to a product type |
| `ProductionJob` | Links a cut part to a product type + order, holds overall status |
| `StepLog` | Every step status change ever made — append-only audit trail |
| `Customer` | A company with a unique, single-use quote-form link |
| `Order` | A customer's requirement — product type, quantity, delivery date, status |

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/Mohini-vashisth/inventory-.git
cd inventory-
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows
```

### 3. Install dependencies

`requirements.txt` is at the repo root:

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

The Django project itself lives in the `inventory/` subfolder — `cd` into it for everything from here on:

```bash
cd inventory
cp .env.example .env
```

Local dev works with everything left blank except `EMPLOYEE_PIN` (defaults to `1234`). See `CLAUDE.md` for what each variable does and what's required in production.

### 5. Apply migrations

```bash
python3 manage.py migrate
```

### 6. Create a superuser (for admin access)

```bash
python3 manage.py createsuperuser
```

### 7. Run the development server

```bash
python3 manage.py runserver
```

Visit `http://127.0.0.1:8000/`.

---

## Usage

### Employee flow (`/employee/`, PIN-protected)

1. **Gate Entry** — log a truck's delivery and its lots in one page
2. **New Coil Entry** — pick an open lot, register its coils, tags print automatically
3. **Create New Part** — pick an order → pick a matching coil → log a cut piece → a production job is created
4. **Update Progress** — pick an active job, tick off manufacturing steps

### Admin/staff flow (`/admin-login/` → `/orders/` or `/admin/`)

- Manage the orders pipeline: confirm, reject, or dispatch
- Send/resend quote-form links to customers by email
- Configure `ProductType`s, their `ProcessStep`s, and `AllowedCoilSpec`s
- Manage `GradeOption`/`SizeOption` picker lists (or run `manage.py backfill_options` to seed them from existing data)
- Full Django admin at `/admin/` for everything else

---

## Running tests

```bash
python3 manage.py test materials
```

CI (`.github/workflows/tests.yml`) runs `manage.py check`, a migration-check, and the full suite on every push/PR to `main`.

---

## URL Reference

| URL | View | Description |
|-----|------|-------------|
| `/` | `home` | Landing page |
| `/employee/` | `employee_landing` | Employee portal |
| `/gate-entry/` | `gate_entry_form` | Log a truck delivery + its lots |
| `/gate-entry/select/` | `select_gate_entry` | Pick an open lot to register a coil against |
| `/gate-entry/lot/<pk>/coil/` | `material_form` | Register a coil, prints its QR tag |
| `/coil/<pk>/tag/` | `coil_tag` | Printable coil tag |
| `/coil/<pk>/parts/` | `coil_parts` | Parts cut from a coil |
| `/select-order/` | `select_order` | Pick an order to cut a part for |
| `/production-board/` | `production_board` | All in-production jobs |
| `/job/<pk>/` | `job_detail` | Step-by-step progress updater |
| `/orders/` | `order_dashboard` | Staff order pipeline |
| `/quote/<token>/` | `quote_form` | Customer-facing, single-use quote request form |
| `/admin-login/` | `admin_login` | Staff login |
| `/admin/` | Django admin | Full admin panel |
| `/api/` | DRF router | Read-only `orders`/`coils`/`jobs`/`product-types` |

---

## Deployment

This app runs on-site at the plant (not the cloud) so the database stays physically local, with Tailscale giving the owner remote access and a Cloudflare Tunnel exposing only the customer-facing quote form publicly. See `CLAUDE.md` for the full reasoning and setup steps.

---

## License

MIT
