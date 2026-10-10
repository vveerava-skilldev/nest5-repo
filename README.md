# Nest5 Maintenance Ledger

Digital apartment maintenance ledger for Nest5 (flats **101, 102, 301, 401, 501**).

- **Monthly maintenance:** ₹2,000 per flat (configurable in `app/config.py`)
- **Project costs:** split equally across five flats
- **Stack:** FastAPI, SQLAlchemy, SQLite, Alembic, server-rendered HTML + responsive CSS

## Quick start

```bash
cd /agent/nest5-ledger
pip install -r requirements.txt
cp .env.example .env   # optional — defaults work for local dev

alembic upgrade head
python3 scripts/seed.py

uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open **http://localhost:8000** — default password **`nest5admin`** (`ADMIN_PASSWORD`).

### Authentication (v1)

One shared admin password (environment variable). No user accounts or roles — suitable for a single treasurer or household admin. **Role-based admin is planned for v2** (see roadmap below).

## Features (v1)

- **Dashboard** with month picker / prev-next navigation, opening & closing balances, scannable **alerts** (overdue asset tasks, pending payments, settlement blocked)
- **Payments (separate):** `/payments/maintenance` (monthly fees) and `/payments/corpus` (corpus fund) — tabbed, distinct forms
- **Asset schedule:** `/schedule` — preventive maintenance (tank, generator, lift), overdue / due-soon buckets
- **Expenses, settlements, projects, flats, complaints, audit, month rollover, printable statements**
- **Flat owners** on dashboard, pending lists, and flat detail; roster synced from `app/domain/flats.py` (legacy **201 → 102** migration on startup)

## Main URLs

| URL | Purpose |
|-----|---------|
| `/` | Dashboard |
| `/payments/maintenance` | Monthly maintenance fees |
| `/payments/corpus` | Corpus payments |
| `/schedule` | Building asset preventive schedule |
| `/expenses` | Common expenses (month filter) |
| `/settlements` | Owner reimbursements |
| `/projects` | Equal-share project payments |
| `/flats` | Owners & per-flat ledger |
| `/complaints` | Complaint log |
| `/audit` | Change history |
| `/periods/rollover` | Close month / carry forward |
| `/statements/{period_id}` | Printable statement |

Legacy `/maintenance` and `/corpus` redirect to the payments URLs.

## Flat owners (authoritative)

| Flat | Owner | Notes |
|------|--------|--------|
| 101 | Manikyamba | Tenant |
| 102 | Sastry | |
| 301 | Venkata Giridhar | |
| 401 | Manikyamba | |
| 501 | Pavan Kumar | |

Contact phone fields are optional placeholders (“Filled later” in UI) until updated.

## Seeded notebook snapshot

- **September 2026:** collections ₹10,000; expenses ₹5,045; fund balance ₹4,955
- **October 2026:** collections ₹10,000; watchman salary ₹4,000; lift-doors complaint
- **Carry-forward:** **₹10,955**

## Tests

```bash
python3 -m pytest -q
```

## v2 roadmap (not in v1)

| Area | Direction |
|------|-----------|
| **Admin user** | Dedicated admin account with full privileges (settlements, fee amount, edit/delete history, flats/owners, schedule, projects, corpus, audit) |
| **Other users** | TBD — e.g. read-only per flat or limited “mark my payment” (product not decided) |
| **Owner/contact** | Edit in UI instead of code roster only |
| **Reporting** | CSV/PDF exports, notifications |
| **Funds** | Corpus balance tracking, bank reconciliation |

Details: `/cursor/stores/self/docs/project-context.md` (section **V2**).
