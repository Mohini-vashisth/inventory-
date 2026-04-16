# Inventory Management System

A Django-based inventory tracking system for managing steel coils — from receipt to final product. Tracks coil entries, cut parts, production jobs, and manufacturing step progress.

---

## Features

- **Coil entry** — register incoming coils with grade, size, vendor, heat number and quantity
- **Auto-print tag** — after saving a coil, a printable tag with QR code opens automatically
- **Part tracking** — log pieces cut from each coil with weight and length
- **Production jobs** — assign a cut part to a product type and track it through manufacturing
- **Step-by-step progress** — operators update each manufacturing step; full audit trail kept
- **Admin dashboard** — Django admin with progress bars and status badges per job
- **Role-based entry** — separate employee and admin portals from the home screen

---

## Tech Stack

- **Backend** — Python 3, Django
- **Database** — SQLite (development)
- **Frontend** — Plain HTML/CSS (no framework)
- **Libraries** — `qrcode[pil]` for QR code generation

---

## Project Structure

```
inventory-/
├── inventory/                  # Django project config
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── materials/                  # Main app
│   ├── models.py               # Material, CoilPart, ProductType, ProcessStep, ProductionJob, StepLog
│   ├── views.py
│   ├── urls.py
│   ├── admin.py                # Custom admin with progress bars
│   ├── forms.py
│   ├── templatetags/
│   │   └── dict_extras.py      # |get_item filter
│   └── migrations/
├── templates/
│   ├── home.html               # Role selection (Employee / Admin)
│   ├── index.html              # Coil entry form
│   ├── admin_login.html
│   └── materials/
│       ├── employee_landing.html
│       ├── coil_tag.html       # Printable tag with QR code
│       ├── coil_parts.html     # Parts cut from a coil
│       ├── create_job.html     # Assign part to product type
│       ├── job_detail.html     # Operator step updater
│       ├── tracking_dashboard.html
│       ├── material_table.html
│       └── admin_dashboard.html
├── manage.py
├── requirements.txt
├── .gitignore
└── README.md
```

---

## Data Model

```
Material (coil)
  └── CoilPart (cut piece)
        └── ProductionJob (linked to a ProductType)
              ├── ProductType → ProcessStep (ordered steps)
              └── StepLog (status update per step, full history)
```

| Model | Purpose |
|-------|---------|
| `Material` | Incoming coil — grade, size, vendor, heat no. |
| `CoilPart` | A piece cut from a coil — weight, length, cut date |
| `ProductType` | Defines a product and its ordered manufacturing steps |
| `ProcessStep` | A named step belonging to a product type |
| `ProductionJob` | Links a part to a product type, holds overall status |
| `StepLog` | Every status change ever made — who, when, notes |

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/Mohini-vashisth/inventory-.git
cd inventory-
```

### 2. Create and activate a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows
```

### 3. Install dependencies

```bash
pip install django qrcode[pil]
```

### 4. Apply migrations

```bash
python manage.py migrate
```

### 5. Create a superuser (for admin access)

```bash
python manage.py createsuperuser
```

### 6. Run the development server

```bash
python manage.py runserver
```

Visit `http://127.0.0.1:8000/` in your browser.

---

## Usage

### Employee flow

1. Go to `/` → click **Employee**
2. Choose one of three actions:
   - **New Coil Entry** — fill the form, save → tag auto-prints
   - **Create New Part** — select a coil → log a cut piece → create a production job
   - **Update Progress** — select an active job → update step statuses

### Admin flow

1. Go to `/` → click **Admin** → log in
2. Visit `/admin/` to:
   - Set up **Product Types** and their manufacturing steps
   - View all **Production Jobs** with live progress bars
   - See the full **Step Log** audit trail
   - Bulk-update job statuses

---

## First-time admin setup

After running the server, go to `/admin/` and:

1. Create a **Product Type** (e.g. `Bracket A`)
2. Add **Process Steps** in order (e.g. `Blanking → Forming → Heat Treatment → QC`)

Once a product type exists, employees can create jobs and track progress.

---

## URL Reference

| URL | View | Description |
|-----|------|-------------|
| `/` | `home` | Role selection page |
| `/employee/` | `employee_landing` | Employee portal |
| `/material-form/` | `material_form` | New coil entry form |
| `/materials-table/` | `material_table` | All coils list |
| `/coil/<id>/tag/` | `coil_tag` | Printable coil tag |
| `/coil/<id>/parts/` | `coil_parts` | Parts for a coil |
| `/part/<id>/new-job/` | `create_job` | Create production job |
| `/job/<id>/` | `job_detail` | Step-by-step progress updater |
| `/tracking/` | `tracking_dashboard` | All jobs overview |
| `/admin-login/` | `admin_login` | Admin login page |
| `/admin/` | Django admin | Full admin panel |

---

## .gitignore

```
db.sqlite3
__pycache__/
*.pyc
.venv/
*.log
.DS_Store
```

---

## License

MIT