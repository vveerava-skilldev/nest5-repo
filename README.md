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

### Simple / Detailed dashboard, pay sheet, UPI button (v7)

- **Owner dashboard toggle:** a **Simple | Detailed** switch at the top of `/` (one click, any time). **Detailed is the v6 dashboard, unchanged.** **Simple** is a compact page for a phone: month navigation, Account balance / Effective balance / what you owe, this month's maintenance per own flat, and **Your dues to pay** (maintenance, corpus goals, projects with due dates; top 4, "+ N more dues" expands in the same view). Owners only ever see their own flats. The admin keeps the current dashboard (no toggle).
- **Default view:** the toggle choice is remembered for the login session; tick **Set as my default** to save it on your account (`users.dashboard_view`, migration `a7d2c4e9b108`; untick to clear). Without any choice a small screen (<= 600 px) opens Simple, a large one opens Detailed.
- **Pay sheet:** tap a due to open a small sheet (`/?view=simple&pay=...`) with flat, item and amount prefilled (part payments allowed for corpus and projects), date = today, mode, and an optional UPI reference / note. Submit uses the existing owner endpoints (`/owner/maintenance`, `/corpus/pay`, `/owner/projects/pay`), so the entry is "Submitted by owner" and the admin cancel-request flow is unchanged.
- **Pay via UPI (replaced by Pay now in v8):** a `upi://pay?pa=<UPI_VPA>&pn=<UPI_PAYEE_NAME>&am=<amount>&cu=INR&tn=<note>` link on each due and in the sheet (opens GPay / PhonePe / Paytm on a phone) and a QR code of the same link on desktop (`/upi/qr.svg`, server-side SVG via the `qrcode` package). **UPI never tells the portal that money moved:** nothing is marked paid until the owner submits the sheet with the UPI reference. Configure with `UPI_VPA` (default `9940255076@ybl`) and `UPI_PAYEE_NAME` (default `Sri Krishna Mani Flats`) in `.env`.
- New dependency: `qrcode==8.0` (pure Python; `pip install -r requirements.txt`).

### "Pay now" with a required UTR, and the in-portal Calendar (v8)

- **Pay now replaces the UPI link.** A generic `upi://pay` link opens WhatsApp on iPhones and blocks PhonePe / GPay, and the payee `UPI_VPA` is a personal UPI ID, so prefilled-amount links are unreliable. The **UPI** buttons on Simple-view dues are now **Pay now** buttons that open the pay sheet. The sheet has: **amount** and **UPI ID** each with a one-tap **Copy** button (`navigator.clipboard`, with a text-selection fallback), the payee name (`UPI_PAYEE_NAME`), best-effort buttons **PhonePe** (`phonepe://pay?...`), **Google Pay** (`tez://upi/pay?...`), **Paytm** (`paytmmp://pay?...`) and **Other UPI app** (`upi://pay?...`) with the same `pa/pn/am/cu/tn` parameters (URL-encoded) and a note that they may not open on every phone and **Copy is the reliable way**, a **Show QR** toggle (open by default on desktop, closed on phones; hint: screenshot it and use *Scan from gallery*), and the step "After paying, enter the UPI reference (UTR) and Submit". Part payments still update the amount in the copy text, the app links and the QR.
- **UTR is required for UPI.** When an **owner** submits with mode **UPI** (the default in Pay now), the reference field must be 8 to 30 letters or digits (no spaces): browser validation in the sheet and on the other owner forms (`static/js/paynow.js`) and the same rule on the server for `/owner/maintenance`, `/corpus/pay`, `/corpus/campaigns/{id}/pay`, `/owner/projects/pay` and `/owner/corpus/{id}/pay` (`app/services/upi.py`). It is stored upper-case in the existing `payment_note` column. **Cash, Bank transfer, Cheque and Other need no UTR**; the admin's own entries are unchanged. Nothing is ever marked paid automatically: the entry is still "Submitted by owner" with the same cancel-request flow.
- **Admin sees the UTR** (to match the UPI statement) in the Mode / note column on the maintenance, corpus-goal and project payment lists, in the Cancel requests queue, in the owner's My submissions and in the audit log. Owners only ever see their own flats' references.
- **Calendar** (`/calendar`, menu item **Calendar** for admin and owners; in-portal only, no ICS / phone subscription). Month grid on desktop; on phones an **agenda list grouped by date**. Previous / next month, **Today**, a month picker (mm-yyyy), dd-mm-yyyy dates, a "This month: N items - N events - N overdue" line, a colour legend with **type filter chips**, and a details panel (popover on desktop, bottom sheet on phones) with a link to the relevant page.
  - **Asset tasks** from the Asset schedule: the stored next-due date is the real occurrence (red when overdue); later occurrences in the viewed month are projected from the task's frequency (marked "expected", dashed; past projections are skipped). **Completed** maintenance logs show as *Maintenance done* history (dashed grey; costs are admin-only).
  - **Maintenance due** for the viewed month sits on the month's last day (the same rule as the reminders: overdue once the month has passed). Admin: one item with paid / pending counts and the pending flats. Owners: only their own flats, e.g. "Flat 102 maintenance pending / paid".
  - **Corpus goal** and **Project** due dates. Admin: collected / pending per flat. Owners: only goals and projects where their own flat has a share, with only their own flats' remaining amount.
  - **Events** (new table `calendar_events`, migration `b8e3f5a1d726`): admin-only add / edit / delete at `/calendar/events/new` and `/calendar/events/{id}/edit`: title, date, optional end date, optional start / end time, type (Meeting, AGM, Vendor visit, Reminder, Maintenance reminder, Other), notes and **Visible to owners** (default on). Every create / edit / delete is audited (`entity_type = calendar_event`). Events not visible to owners are never sent to owner browsers. Owners are read-only (403 on every event route).
  - The migration is idempotent (skips if app startup already created the table via `create_all`), SQLite and Postgres safe, and downgrades by dropping the table.
- Tests: `tests/test_v8_pay_now.py`, `tests/test_v8_calendar.py` (service, visibility, RBAC, migration from scratch / from a v7 database copy). `tests/test_v7_simple_dashboard.py` and the other owner-payment tests now send a UTR.

### Pay now: editable amount, three reliable ways to pay (v9)

- **Editable amount.** In the Pay now sheet the Amount in the top box (next to its **Copy** button) is an input for **corpus and project** dues (part payment allowed). It is the same value as the form's **Amount (₹)** field: editing either one updates the other live, and **Copy** copies the current plain number (e.g. `5000`). Amounts are whole rupees (the ledger stores integers): zero, empty, paise or more than the remaining amount shows an inline error, disables **Submit payment** and dims the QR; the server still rejects them too (`/owner/projects/pay`, `/corpus/pay`). **Fixed maintenance** stays read-only exactly as in v8.
- **Live hint** under the amount (part payments): `Paying ₹5,000 of ₹21,000 (24%) · Remaining after this payment: ₹16,000 (76%)`, or `Pays this off in full` when the amount equals the remaining. The percentages use the flat's total share; if something was already paid the hint adds `Already paid ₹X`. Data comes from `data-remaining` / `data-total` / `data-paid` on the Pay now block. The **QR** is regenerated for the edited amount.
- **PhonePe fix.** PhonePe declines payments started from `phonepe://pay` / `upi://pay` links that carry a payee and amount when the payee is a personal UPI ID ("declined for security reason, please try using mobile number, UPI ID or QR code"). This cannot be fixed in app code, so the Google Pay / Paytm / Other UPI app buttons and the prefilled PhonePe link were removed. The sheet now shows, in order: **UPI ID** (Copy), **or send to mobile number** (Copy; setting `UPI_MOBILE`, default `9940255076`) and **Show QR** (the QR still carries the `upi://pay` payload with payee, amount and note; PhonePe's scanner / *Scan from gallery* accepts it), plus a launch-only **Open PhonePe** button (`phonepe://`, no payee or amount). The real fix is a society / business UPI ID (merchant VPA): set it as `UPI_VPA`.
- **Version label:** `version: nest5-v7.0` (internal build 9.3) (v9.2: `nest5-v9.2`, v9.1: `nest5-v9.1`, v9: `nest5-v9`) at the bottom of the side menu (`APP_VERSION` in `app/config.py`).
- Tests: `tests/test_v9_pay_now.py` (the v7 / v8 pay tests were updated for the single launch button).

### Rejected owner expenses, corpus goal delete, asset alert clean-up (v9.1)

- **Root cause of "rejected expense still pending for settlement".** v9 had no way to reject an owner's common expense. An owner expense counts the moment it is recorded (`owner_expense_submit` in `app/routers/owner.py`), and the only "Reject" button in the app was on the **cancel-request** queue (`owner_submissions.reject`). That button means "no, keep the entry", so the expense stayed in every total: Unsettled owner expenses, Effective balance, Available to settle, Actions hub, the owner's "owed back" panel, Settlements, PDFs and alerts (all of them read `Expense.deleted_at IS NULL`, `app/services/ledger.py` and `app/services/balances.py`).
- **Reject an owner expense** (admin; `/expenses` and Payments › Owner expenses): optional reason, audited (`reject`). New columns `expenses.status` (`approved` / `rejected`), `rejection_reason`, `rejected_at`, `rejected_by` (migration `c9a2e5f7b140`). Every counting query now uses `Expense.counted()` (not deleted and not rejected), so a rejected expense is out of totals, balances, settlements, the settle button, the "Settlement blocked" alert, the Actions hub, dashboards (Detailed and Simple), flat statements and the PDF summary. It is hidden from every list (Expenses, Payments › Expenses / Owner expenses, flat page, owner "owed back" panel, Settlements, Actions hub) and from the Excel and CSV exports. The only place it appears is a collapsed admin-only **Rejected expenses (N)** panel on `/expenses`, with its reason, **Reinstate**, **Delete** and **Clear all rejected**. The owner's "My submissions" history still marks the entry as Rejected with the reason (it is a submission log, not an advance list). The JSON backup keeps every row (it is a full restore file). A rejected expense cannot be settled or edited; **Reinstate** brings it back. A settled expense cannot be rejected (revert the settlement first).
- **Clean-up** (admin only; owners get 403, from the Rejected expenses panel): **Delete** on a rejected row (confirm, audited `purge`) and **Clear all rejected (N)** (confirm, audited `clear_rejected`). Both remove the rows for good; the audit log keeps what they looked like. The cancel-request queue buttons now read "Approve cancel" / "Reject cancel (keep entry)".
- **Repair on upgrade.** The migration is idempotent, SQLite and Postgres safe, adds the columns, marks existing rows `approved`, and for any row already `rejected` un-settles it and reverts a live settlement (rows are never deleted). The same repair (`expense_review.repair_rejected_expenses`) also runs on every app start. Maintenance, corpus and project payments have no "reject the entry" action (admin Revert / Delete / approving a cancel remove them, already covered by `tests/test_cancel_everywhere.py`); rejecting a cancel request keeps them counted by design.
- **Corpus goal delete** (admin; any goal incl. General corpus): Corpus goal page › Delete goal. With payments the page says how many and how much will be removed, requires a ticked box and `DELETE`, takes an automatic SQLite backup (`backups/nest5-…-pre-delete-goal.db`) first, then removes payments, shares and line items, closes pending cancel requests for those payments, refreshes closed-month rollups and writes an audit entry with the removed payments. Nothing depends on a goal existing: startup does not recreate a deleted General corpus; it only appears again if the admin adds a one-off corpus amount.
- **Asset alerts clean-up.** `scripts/seed.py` no longer creates sample asset tasks or logs (the register still starts with Lift, Generator, Water tank, Motor/pump as assets), so a new install has no asset alerts. Existing databases: Asset schedule › **Clear all asset tasks & alerts** (admin; type `CLEAR`, automatic backup, audit `clear_all`) deletes every asset task and the maintenance-log entries that belong to a task, keeps the assets and general log notes. Never run automatically. CLI twin: `python scripts/clear_asset_alerts.py` (dry run) / `--yes` (backup, delete, audit user `cli`; `--no-backup` only for non-SQLite after your own backup).
- Tests: `tests/test_v91_rejected_expenses.py`, `tests/test_v91_corpus_delete.py`, `tests/test_v91_asset_cleanup.py`; `tests/test_v8_calendar.py` migration checks now expect head `c9a2e5f7b140`; `tests/test_asset_schedule.py` and the v6 goal-delete test were updated for the new behaviour.

### Tenants, owner edit, owed total, due hints, cleaner owner views (v9.3 code, shipped as v7.0)

- **Tenant role** (Admin › Users › role *Tenant*, linked to a flat). A tenant lands on **Payments › Maintenance** and sees only the pending payments of the flat(s) linked to them (an owner who also owns flat 401 is not affected: tenant of 101 never sees 401; an owner of two flats still sees both). No Dashboard, Expenses, Corpus, Projects, Schedule, Calendar, Complaints, Flats or Admin pages: those URLs return 403. The tenant keeps **My submissions** (to follow or cancel their own payment) and Change password. The tenant can record a maintenance payment for their own flat only.
- **Admin chooses what a tenant sees.** Users › Manage › **Tenant can see (payments)**: tick Maintenance, Corpus, Projects, Expenses at any time (default: Maintenance only). Unticked sections disappear from the tabs and return 403; ticked sections show only the tenant's flat(s). Stored in `users.tenant_payments` (migration `d4b8f1a3c627`, idempotent). Command line: `python scripts/create_tenant.py --flat 101 [--username tenant101] [--sections maintenance,corpus]` prints a one-time temporary password (must be changed at first login) and writes an audit entry.
- **Owner can edit a submitted expense** until the admin settles or rejects it (Payments › Expenses › Edit: flat, date, category, description, amount; audited `owner_edit`; blocked while a cancel request is pending, after rejection or settlement; own expenses only, own flats only).
- **Wrongly described expense, cancelled by the owner, rejected by the admin.** In Admin › Cancel requests an expense request now has **Approve cancel**, **Reject expense** (the expense is rejected: gone from Expenses, Pending for reimbursement, balances and totals; still visible in the owner's My submissions as Rejected) and **Keep entry** (the old "reject the cancel" behaviour). A request already decided as "rejected" whose expense still counts shows **Reject expense now**.
- **Simple view shows the owner's unsettled amount**: an "Unsettled - owed to you" tile and a "Your unsettled expenses" list (rejected items excluded).
- **Due dates in the description.** Maintenance (due on the last day of the month), project (its due date) and corpus items show "due dd-mm-yyyy" in reminders, the Simple view, the Pay now sheet, the month / project pickers and My submissions descriptions.
- **Owner views hide empty sections** ("Your corpus status - open goals", "Your corpus payments", project and expense cards, owed-back panel) when there is nothing to show; the admin still sees them.
- **Insights removed** for all users (the Insights card on the flat page). The data code stays; nothing is shown.
- **Version label:** `version: nest5-v7.0` at the bottom of the side menu (`APP_VERSION` in `app/config.py`).
- Tests: `tests/test_v93_features.py`; the insights test, the cancel-queue "keep" test (now `/keep`) and the migration head checks (`d4b8f1a3c627`) were updated.

### Upgrading from v9.x (keep your `nest5.db`, `instance/`, `backups/`, `.env`)

Unzip `nest5-ledger-v7.0.zip` into a new folder, copy `nest5.db`, `instance/`, `backups/` and `.env` over, then `./run-local.sh` (runs `alembic upgrade head`: from v9 two migrations, `c9a2e5f7b140` and `d4b8f1a3c627`; from v9.1 / v9.2 only `d4b8f1a3c627`). No new packages. Existing asset tasks are left alone: use **Clear all asset tasks & alerts** on the Asset schedule page (or `scripts/clear_asset_alerts.py --yes`) when you are ready to re-enter them.

### Upgrading from v8 (keep your `nest5.db`, `instance/`, `backups/`, `.env`)

Unzip `nest5-ledger-v9.zip` into a new folder, copy `nest5.db`, `instance/`, `backups/` and `.env` over, then `./run-local.sh`. No migration, no new packages. Optionally add `UPI_MOBILE=<number>` to `.env`.

### Upgrading from v7 (keep your `nest5.db`, `instance/`, `.env`)

Unzip `nest5-ledger-v8.zip` (or newer) into a new folder, copy `nest5.db`, `instance/`, `backups/` and `.env` over, then `./run-local.sh` (`alembic upgrade head` adds only the new `calendar_events` table; no existing rows change). No new Python packages.

### Upgrading from v6 (keep your `nest5.db`, `instance/`, `.env`)

Unzip `nest5-ledger-v7.zip` into a new folder, copy `nest5.db`, `instance/`, `backups/` and `.env` over, then `./run-local.sh` (it installs `qrcode` and runs `alembic upgrade head`, which only adds one nullable column to `users`).

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
| `/` | Dashboard (owners: `?view=simple` / `?view=detailed`) |
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
| `/upi/qr.svg` | UPI QR for the pay sheet (v7, own flats only) |
| `/calendar` | Calendar: tasks, dues, events (v8; admin + owners) |
| `/calendar/events/new`, `/calendar/events/{id}/edit` | Add / edit calendar events (admin only) |
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
