# Inventory & Manufacturing System

A Django app for a steel coil manufacturing business, covering the full workflow: raw material receipt → coil registration → order management → coil picking → step-by-step production tracking → dispatch.

---

## Features

- **Gate Entry** — log a truck's delivery from its invoice (vendor, vehicle no., invoice no., total weight) as one or more brand/grade/size lots, before any coil is individually registered
- **New Coil Entry** — register each coil against an open gate entry lot; prints a QR tag automatically. Company/vendor/grade/size are locked from the gate entry/lot, capped at the lot's coil count
- **Coil picking** — fulfil an order by scanning/picking coils as raw material (weight need not be the coil's full remaining weight), with an all-or-nothing atomic write (never a pick with no job)
- **Order-first production** — customers submit requirements via a unique quote-form link; admin confirms an order, employees can then only pick coils matching that order's allowed grade/size specs. Staff edit an order right on its details page, and can also enter one by hand
- **Production jobs & step tracking** — each picked coil is assigned a product code with ordered manufacturing steps; employees tick off each step with a full audit trail (`StepLog`); material can be split into tracked parts
- **Queries, quotations & the WhatsApp bot** — inbound leads are logged as queries; a WhatsApp bot on the business number collects the details, and staff send an official PDF quotation by email. Confirming an order emails the customer (with a summary PDF) and sends the same confirmation on WhatsApp
- **Companies page** — a page per company (details, current and earlier orders, quoted rates, every quotation) and an approval list for detail changes companies request from their portal
- **Company portal** — one permanent link per company (`/portal/<token>/`): current and past orders, Order again, optional status tracking, an order summary PDF per confirmed order, My details (request a change) and About pages, installable as an app
- **Finance & coil tools** — finance uploads supplier bills (stored as PDF) with rates and extra charges for coil deliveries; the Coil Dashboard shows one coil's status, weights and the orders it was picked for
- **Admin dashboard** — Django admin (themed with Jazzmin) with progress bars, status badges, used/unused coil filters, and bulk actions (archive coils, mark jobs on hold, etc.)
- **Read-only REST API** — `orders`, `coils`, `jobs`, `product-codes` under `/api/`, staff-only
- **Access** — one login page where a 4-digit code opens the employee portal (tablet-friendly) or the finance page, plus a Django-auth admin/staff area; owners change the codes from their dashboard

See `CLAUDE.md` for the full architecture reference, deployment setup, and decision history.

---

## Tech Stack

- **Backend** — Python 3, Django 5.2 LTS, Django REST Framework
- **Database** — SQLite
- **Frontend** — Plain HTML/CSS/vanilla JS (no framework)
- **Admin theme** — django-jazzmin
- **Key libraries** — `qrcode`/`Pillow` (QR tags), `pandas`/`openpyxl` (spreadsheet import), `whitenoise` (static files), `reportlab` (PDF quotations, order summaries), `gunicorn` (Mac/Linux) / `waitress` (Windows) as the production WSGI server

---

## Project Structure

```
inventory-/
├── inventory/                       # Django project
│   ├── inventory/                   # settings.py, urls.py, wsgi.py, asgi.py
│   ├── materials/                   # the one Django app
│   │   ├── models.py                # GateEntry, GateEntryLot, Material, OrderCoilPick,
│   │   │                            # ProductType, ProductCode, ProcessStep, ProductionJob,
│   │   │                            # StepLog, Customer, Query, Quotation, Order, ...
│   │   ├── views/                   # one module per area (orders, portal, whatsapp, ...)
│   │   ├── forms.py
│   │   ├── admin.py
│   │   ├── api.py / serializers.py  # REST API
│   │   ├── management/commands/     # import_excel, backfill_options, backup_db, benchmark_queries
│   │   ├── templatetags/
│   │   ├── tests/                   # one test module per area
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
              └── OrderCoilPick (a coil picked against an order)
                    └── ProductionJob (linked to a ProductCode + Order)
                          ├── ProductCode → ProcessStep (ordered steps)
                          └── StepLog (status update per step, full history)

Customer → Order (quote/requirement) → OrderCoilPick → ProductionJob
```

| Model | Purpose |
|-------|---------|
| `GateEntry` | A truck delivery — vendor, vehicle no., invoice no., total weight |
| `GateEntryLot` | One brand/grade/size/coil-count batch within a gate entry |
| `Material` | A registered coil — grade, size, company, heat no., quantity |
| `OrderCoilPick` | A coil (or part of one) allocated to an order's raw-material requirement |
| `GradeOption` / `SizeOption` | Admin-managed valid grade/size options for the tap-to-pick pickers |
| `ProductType` | A family of product the business sells (e.g. Flat Bright Bar) |
| `ProductCode` | A product type in a particular grade (e.g. `FBB009`) with its ordered manufacturing steps |
| `ProcessStep` | A named step belonging to a product code |
| `ProductionJob` | Links a picked coil to a product code + order, holds overall status |
| `StepLog` | Every step status change ever made — append-only audit trail |
| `Customer` | A company: contact and GST details, a single-use quote-form link and a permanent portal link |
| `CompanyChangeRequest` | A change to its details that a company asked for, waiting for staff approval |
| `Query` / `Quotation` | An inbound lead, and the official quotation sent for it |
| `Order` | A customer's requirement — product code, quantity, delivery date, status |

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

### Employee flow (`/login/` with the employee code, then `/employee/`)

1. **Gate Entry** — log a truck's delivery and its lots in one page
2. **New Coil Entry** — pick an open lot, register its coils, tags print automatically
3. **Pick Coils for Order** — pick an order → scan/select matching coils until its raw-material requirement is met → a production job is created per picked coil
4. **Update Progress** — pick an active job, tick off manufacturing steps

### Admin/staff flow (`/admin-login/` → `/dashboard/`)

- Manage the orders pipeline: confirm, reject, edit or dispatch
- Log queries, send quotations by email, and copy order-form and portal links from the Companies page
- Configure `ProductCode`s, their `ProcessStep`s, and `AllowedCoilSpec`s
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
| `/` | `welcome` | Welcome page (start for every visitor) |
| `/login/` | `code_login` | One code box: employee PIN or finance code |
| `/employee/` | `employee_landing` | Employee portal |
| `/gate-entry/` | `gate_entry_form` | Log a truck delivery + its lots |
| `/gate-entry/select/` | `select_gate_entry` | Pick an open lot to register a coil against |
| `/gate-entry/lot/<pk>/coil/` | `material_form` | Register a coil, prints its QR tag |
| `/coil/<pk>/tag/` | `coil_tag` | Printable coil tag |
| `/select-order/` | `select_order` | Pick an order to pick coils for |
| `/order/<pk>/select-coil/` | `select_coil_for_order` | Order's picking hub — progress, scan, browse coils |
| `/order/<pk>/pick-coil/<coil_pk>/` | `pick_coil_for_order` | Confirm allocating a coil to the order |
| `/production-board/` | `production_board` | All in-production jobs |
| `/job/<pk>/` | `job_detail` | Step-by-step progress updater |
| `/orders/` | `order_dashboard` | Staff order pipeline |
| `/companies/` | `company_dashboard` | Companies, links and detail-change requests |
| `/companies/<pk>/` | `company_detail` | One company: details, orders, quoted rates |
| `/coil-lookup/` | `coil_lookup` | Coil Dashboard (read-only) |
| `/portal/<token>/` | `portal_home` | A company's portal (orders, Order again, My details, About) |
| `/quote/<token>/` | `quote_form` | Customer-facing, single-use quote request form |
| `/admin-login/` | `admin_login` | Staff login |
| `/admin/` | Django admin | Full admin panel |
| `/api/` | DRF router | Read-only `orders`/`coils`/`jobs`/`product-codes` |

---

## Deployment

This app runs on-site at the plant (not the cloud) so the database stays physically local, with Tailscale giving the owner remote access and Tailscale Funnel exposing only the public paths (`/quote`, `/portal`, `/webhooks/whatsapp`). See `CLAUDE.md` for the full reasoning and setup steps.

---

## License

MIT
