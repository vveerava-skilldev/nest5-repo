# Sri Krishna Mani Flat Owners Portal (Nest5 ledger)

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

Open **http://localhost:8000** — username **`admin`**, password **`nest5admin`** on first run (`ADMIN_PASSWORD`).

### Authentication (v2: user accounts)

- **admin** — everything (ledger edits, settlements, fee revision, audit log, users, export/restore).
- **owner** — read-only ledger (dashboard, payments, expenses, projects, schedule, statements), own flat page(s), raise complaints, change own password, and record payments/expenses for their own flat(s) (see below).
- First startup creates `admin` (password = `ADMIN_PASSWORD`) and one owner login per roster person with a temporary password in `instance/initial-passwords.txt`; owners must change it at first login. Admin can reset any password at `/admin/users`.
- **Forgot password** on the login page emails a single-use link (60 min). Admin recovery goes to `ADMIN_RECOVERY_EMAIL`. Without SMTP the link is printed in the terminal and written to `instance/outbox.log`. See `.env.example` for Gmail setup.
- Offline fallback: `python scripts/reset_admin.py`.
- **Several users at once:** each browser keeps its own login (session cookie), so many people can be signed in together. All tabs and windows of *one* browser share one login — signing in again in another tab switches them all. To test admin and an owner on one Mac, use a private/incognito window, a second browser, or open one at `http://127.0.0.1:8000` and the other at `http://localhost:8000` (separate cookies). Every page shows "Logged in as … (role)"; a form submitted from a tab that was opened as a different user is refused with a notice and nothing is saved.

### Data export & backup (admin)

`/admin/export`: Excel, CSV zip, JSON full dump, PDF summary (month / all time), raw SQLite copy, JSON restore (preview + type `RESTORE`, automatic backup first). The DB is copied to `backups/nest5-YYYYMMDD-HHMMSS.db` on every start (newest 30 kept).

### Owner self-service & corpus collections (v3)

- Owners record their own **maintenance payment** (`/payments/maintenance`: month, date, amount defaulting to that month's fee, mode, note), **common expenses they paid** (`/expenses`, lands as an unsettled owner advance) and **corpus payments** (Pay button, partial amounts allowed). Only their own flat(s) — checked on the server. Entries count immediately and show a "Submitted by owner" badge.
- **Cancel requests:** an owner can ask to cancel an entry they submitted; it is flagged "Cancel requested" and still counts until the admin approves (entry removed/reverted, audited) or rejects it at `/admin/cancel-requests`. Owners track status at `/owner/submissions`.
- **Corpus collections** (`/corpus/campaigns`): admin sets name, target, per-flat share (equal split by default, editable) and due date; flats pay in parts until their share is met; per-flat paid / remaining / due and totals. Older corpus line items are now part of General corpus (v4).
- **Reminders:** owners see a "Your dues" panel at the top of the dashboard and on their flat page (unpaid maintenance, corpus remaining with due date — overdue / due within 7 days highlighted — and project shares). Admin dashboard alerts list corpus dues per flat and waiting cancel requests.

### One corpus picture, Payments tabs, overview pages (v4)

- **Corpus is one picture.** Every corpus amount belongs to a collection. One-off amounts live in the open-ended **General corpus** collection (its target = sum of the flats' shares). A payment against any collection shows on Payments › Corpus, the flat page, the dashboard Corpus column, the corpus overview and the PDF, and is counted once. The migration `a6c4e2b8d013` folds the older corpus line items into General corpus (paid items become General corpus payments; unpaid items become share) after copying the DB to `backups/nest5-…-pre-general-corpus.db`.
- **Pay corpus** (`/payments/corpus`): pick the collection (any open one, including General corpus); each option shows that flat's share, paid and remaining; part payments allowed. Admin can add a one-off amount to a flat's General corpus share.
- **Payments** is one page with tabs: **Maintenance | Corpus | Projects | My expenses** (admin: Owner expenses) at `/payments/maintenance`, `/payments/corpus`, `/payments/projects`, `/payments/expenses`. Owners see only their own flats' rows and their own submitted expenses, with their forms on the matching tab; admin sees every flat.
- **Overview pages** in the menu: **Expenses** (`/expenses`), **Corpus** (`/corpus/campaigns`), **Projects** (`/projects`) — details plus building target / collected / pending / progress and per-flat status. Owners see building totals and the list of common expenses; other flats' individual payment rows are admin-only.
- Menu order: Dashboard, Payments, Expenses, Corpus, Projects, Asset schedule, Complaints, Flats / My flat, My submissions, then Admin and Account; the current page is highlighted. Tabs scroll sideways on phones.

### Cancels everywhere, Corpus goals, owner view, asset register (v5)

- **Cancelled = gone everywhere.** An approved cancel (maintenance, expense, corpus) or an admin revert (also projects) removes the amount from the dashboard, flat page, Payments tabs, overviews, reminders, alerts, Account Balance, statements and the PDF. A cancelled maintenance entry goes back to that month's fee (not the amount the owner typed). Edits in a **closed month** now update that month's closing balance and the next month's opening balance automatically. Raw data exports keep the history (cancelled expenses keep `deleted_at`, reverted maintenance shows unpaid).
- **Corpus** (`/corpus`) is one area: each **goal** has a title, category (General, Building repair, Lift, Painting; admin can add more), target, per-flat share, due date and optional **line items** (sub-goals). Owners pay by choosing the goal (and line item). The goal page (`/corpus/goals/{id}`) shows progress, line items, per-flat status and payments. Old `/corpus/campaigns…` URLs redirect. Migration `b5d7f1e3c920` (backup `…-pre-corpus-goals.db`) sets a category on existing collections.
- **Owner view:** owners only see their own flats. Menu: Dashboard → My flat → Payments → Complaints → Asset schedule → My submissions. Payments tabs: Maintenance | Corpus | Projects | Expenses. **View payments →** (`/payments/building`) shows every flat's payment status read-only; the admin can turn it off there (default on; off = link hidden, page 403). **My flat** shows charts (monthly payments, paid vs pending, corpus progress; drawn on the server, works offline) and **Export to PDF** (`/flats/{id}/statement.pdf`, `/owner/statement.pdf` for all my flats; any month or all time).
- **Asset schedule** (`/schedule`): overdue / due this week / upcoming at the top, then the **asset register** (Lift, Generator, Water tank, Motor/pump, …) with each asset's recurring tasks (daily / weekly / monthly / quarterly / yearly / custom days) and last log entry. The asset page (`/assets/{id}`) has details (type, location, vendor/AMC contact, install date, warranty/AMC expiry, cost, notes) and the full **maintenance log** (date, done by, notes, cost, next due). Admin adds/edits/deletes; owners view only. Dashboard alerts still use due/overdue tasks. Migration `e1f4b7a2c805` (backup `…-pre-asset-register.db`) turns existing schedule rows into assets + tasks and keeps each "last done" date as the first log entry.

### Project payments, expense categories, balances, Actions hub (v6)

- **Fixed:** the admin **Projects** page (its template was damaged in v4/v5) and owner **Export to PDF** with timeline **All time** (it failed with a validation error).
- **Owners pay project shares** on Payments › Projects: own flats, part payments allowed, counted at once, "Submitted by owner", Request cancel; refused once fully paid. Projects can have a due date (overdue shares show in reminders and the Actions hub).
- **Admin → Expense categories** (`/admin/expense-categories`): add, rename, enable/disable, choose which ones owners may use. Existing expenses keep their category.
- **Edit / delete projects** (project page) and **delete corpus goals** (goal page). Delete is **blocked while payments exist**: revert or delete the payments first. Typed DELETE + confirm, audited.
- **Dashboard balances:** Account Balance (money held), Unsettled owner expenses, Effective balance after settling, Available to settle now (All / Some / None), plus a Settlement card listing pending reimbursements by flat with Settle buttons and a warning when settling all would go negative. Owners see what they are owed back on the dashboard and My flat.
- **Admin → Actions** (`/admin/actions`, summary on the dashboard): cancel requests, unsettled expenses, overdue maintenance / corpus / project dues by flat, overdue asset tasks, new owner submissions, users needing a password reset. Each row links to where you fix it.
- Migration `f2c6a8d4b301` (backup `backups/nest5-…-pre-v6.db` first).

### Upgrading from v5 (keep your `nest5.db` and `instance/`)

1. Stop the running app.
2. Unzip `nest5-ledger-v6.zip` into a **new** folder (don't unzip over v5: the damaged `projects.html` must be replaced).
3. Copy `nest5.db` and the `instance/` folder (and `backups/` and `.env` if you have them) from the v5 folder into the new `nest5-ledger/` folder.
4. In the new folder run `bash verify-install.sh`, then `./run-local.sh`. This runs `alembic upgrade head`, which backs up the DB first and then migrates it. (Or run `python -m alembic upgrade head` yourself, then start uvicorn.) `run-local.sh` also runs `scripts/seed.py`, which only syncs the flat roster on an existing database.
5. Log in as admin: check **Projects**, **Admin → Actions** and **Admin → Expense categories**.

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
| `/payments/corpus` | Corpus payments (pick a collection, part payments) |
| `/payments/projects` | Project shares per flat |
| `/payments/expenses` | Owner-submitted common expenses (owner: record + my list) |
| `/schedule` | Asset schedule: due/overdue, asset register, tasks |
| `/assets/{id}` | Asset details & maintenance history |
| `/payments/building` | Building-wide payment status (owners: when enabled) |
| `/flats/{id}/statement.pdf` | Flat statement PDF (`?period_id=` for one month; empty / `all` / omitted = all time) |
| `/expenses` | Common expenses (month filter) |
| `/settlements` | Owner reimbursements |
| `/projects` | Equal-share projects; `/projects/{id}` detail, edit, delete |
| `/flats` | Owners & per-flat ledger |
| `/complaints` | Complaint log |
| `/audit` | Change history |
| `/periods/rollover` | Close month / carry forward |
| `/statements/{period_id}` | Printable statement |
| `/corpus` | Corpus goals: categories, progress, per-flat status, pay |
| `/corpus/goals/{id}` | Goal detail: line items, payments |
| `/owner/submissions` | Owner: my submitted entries & cancel requests |
| `/admin/cancel-requests` | Owner cancel requests queue (admin) |
| `/admin/actions` | Admin to-do hub (v6) |
| `/admin/expense-categories` | Manage expense categories (v6) |
| `/admin/users` | User accounts (admin) |
| `/admin/export` | Data export, backups, restore (admin) |
| `/account/password` | Change own password |
| `/forgot-password` | Email a reset link / admin recovery |

Legacy `/maintenance` and `/payments` redirect to the payments URLs; `/corpus/campaigns…` redirects to `/corpus…`; `/assets` redirects to `/schedule`.

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
| **Admin user** | Done — `admin` account with full privileges |
| **Other users** | Done — read-only owner accounts linked to flats |
| **Owner/contact** | Edit in UI instead of code roster only |
| **Reporting** | Exports done (Excel/CSV/JSON/PDF); notifications still open |
| **Funds** | Corpus collections done; bank reconciliation still open |

Details: `/cursor/stores/self/docs/project-context.md` (section **V2**).

## Deploying (EC2 / any Linux host)

Use **Python 3.12** — the pinned `pydantic-core` / `pillow` versions do not build on 3.14.

Updating an existing server:

1. **Back up first:** copy `nest5.db` (and `.env`) somewhere safe, e.g. `cp nest5.db backups/nest5-$(date +%Y%m%d-%H%M%S)-pre-update.db`.
2. **Upload the new code without** `.venv/`, `backups/`, `instance/`, `*.db`, or `.env` (e.g. `rsync -a --exclude .venv --exclude backups --exclude instance --exclude '*.db' --exclude .env ./ user@host:/path/nest5-ledger/`).
3. `pip install -r requirements.txt` inside the server's venv.
4. `alembic upgrade head`
5. `python scripts/seed.py`
6. Restart the service (e.g. `sudo systemctl restart nest5`).

Never overwrite the server's `nest5.db` or `.env`. Set a real `SECRET_KEY` and change the default `nest5admin` password on first login.
