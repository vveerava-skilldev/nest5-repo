# Sri Krishna Mani Flat Owners Portal (Nest5 ledger)

Digital apartment maintenance ledger for Nest5 (flats **101, 201, 301, 401, 501**).

- **Monthly maintenance:** ₹2,000 per flat (configurable in `app/config.py`)
- **Project costs:** split equally across five flats
- **Stack:** FastAPI, SQLAlchemy, SQLite, Alembic, server-rendered HTML + responsive CSS

## Quick start

```bash
cd /agent/nest5-ledger
pip install -r requirements.txt
cp .env.example .env   # optional — defaults work for local dev

alembic upgrade head
python3 scripts/seed.py          # roster + assets + payment settings only; add --sample for demo months

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
- **Dashboard balances:** Account Balance (money held), Unsettled owner expenses, Effective balance after settling, Available to settle now (All / Some / None), plus (until the in-place v7.3 update removed it) a Settlement card listing pending reimbursements by flat with Settle buttons and a warning when settling all would go negative. Owners see what they are owed back on the dashboard and My flat.
- **Admin → Actions** (`/admin/actions`, summary on the dashboard): cancel requests, unsettled expenses, overdue maintenance / corpus / project dues by flat, overdue asset tasks, new owner submissions, users needing a password reset. Each row links to where you fix it.
- Migration `f2c6a8d4b301` (backup `backups/nest5-…-pre-v6.db` first).

### Simple / Detailed dashboard, pay sheet, UPI button (v7)

- **Owner dashboard toggle:** a **Simple | Detailed** switch at the top of `/` (one click, any time). **Detailed is the v6 dashboard, unchanged.** **Simple** is a compact page for a phone: month navigation, Account balance / Effective balance / what you owe, this month's maintenance per own flat, and **Your dues to pay** (maintenance, corpus goals, projects with due dates; top 4, "+ N more dues" expands in the same view). Owners only ever see their own flats. The admin keeps the current dashboard (no toggle).
- **Default view:** the toggle choice is remembered for the login session; tick **Set as my default** to save it on your account (`users.dashboard_view`, migration `a7d2c4e9b108`; untick to clear). Without any choice a small screen (<= 600 px) opens Simple, a large one opens Detailed.
- **Pay sheet:** tap a due to open a small sheet (`/?view=simple&pay=...`) with flat, item and amount prefilled (part payments allowed for corpus and projects), date = today, mode, and an optional UPI reference / note. Submit uses the existing owner endpoints (`/owner/maintenance`, `/corpus/pay`, `/owner/projects/pay`), so the entry is "Submitted by owner" and the admin cancel-request flow is unchanged.
- **Pay via UPI (replaced by Pay now in v8):** a `upi://pay?pa=<UPI_VPA>&pn=<UPI_PAYEE_NAME>&am=<amount>&cu=INR&tn=<note>` link on each due and in the sheet (opens GPay / PhonePe / Paytm on a phone) and a QR code of the same link on desktop (`/upi/qr.svg`, server-side SVG via the `qrcode` package). **UPI never tells the portal that money moved:** the payment is recorded when the owner submits the sheet with the UPI reference (it counts right away); the admin verifies the UTR against the UPI statement afterwards. Configure with `UPI_VPA` (default `9940255076@ybl`) and `UPI_PAYEE_NAME` (default `Sri Krishna Mani Flats`) in `.env`.
- New dependency: `qrcode==8.0` (pure Python; `pip install -r requirements.txt`).

### "Pay now" with a required UTR, and the in-portal Calendar (v8)

- **Pay now replaces the UPI link.** A generic `upi://pay` link opens WhatsApp on iPhones and blocks PhonePe / GPay, and the payee `UPI_VPA` is a personal UPI ID, so prefilled-amount links are unreliable. The **UPI** buttons on Simple-view dues are now **Pay now** buttons that open the pay sheet. The sheet has: **amount** and **UPI ID** each with a one-tap **Copy** button (`navigator.clipboard`, with a text-selection fallback), the payee name (`UPI_PAYEE_NAME`), best-effort buttons **PhonePe** (`phonepe://pay?...`), **Google Pay** (`tez://upi/pay?...`), **Paytm** (`paytmmp://pay?...`) and **Other UPI app** (`upi://pay?...`) with the same `pa/pn/am/cu/tn` parameters (URL-encoded) and a note that they may not open on every phone and **Copy is the reliable way**, a **Show QR** toggle (open by default on desktop, closed on phones; hint: screenshot it and use *Scan from gallery*), and the step "After paying, enter the UPI reference (UTR) and Submit". Part payments still update the amount in the copy text, the app links and the QR.
- **UTR is required for UPI.** When an **owner** submits with mode **UPI** (the default in Pay now), the reference field must be 8 to 30 letters or digits (no spaces): browser validation in the sheet and on the other owner forms (`static/js/paynow.js`) and the same rule on the server for `/owner/maintenance`, `/corpus/pay`, `/corpus/campaigns/{id}/pay`, `/owner/projects/pay` and `/owner/corpus/{id}/pay` (`app/services/upi.py`). It is stored upper-case in the existing `payment_note` column. **Cash, Bank transfer, Cheque and Other need no UTR**; the admin's own entries are unchanged. Nothing is ever marked paid by UPI itself: the owner's submission is what records it, and it counts right away as "Submitted by owner" with the same cancel-request flow. The admin verifies the UTR afterwards and can **Reject** it (the due comes back and the owner sees "Rejected" in My submissions).
- **Admin sees the UTR** (to match the UPI statement) in the Mode / note column on the maintenance, corpus-goal and project payment lists, in the Cancel requests queue, in the owner's My submissions and in the audit log. Owners only ever see their own flats' references.
- **Calendar** (`/calendar`, menu item **Calendar** for admin and owners; in-portal only, no ICS / phone subscription). Month grid on desktop; on phones an **agenda list grouped by date**. Previous / next month, **Today**, a month picker (mm-yyyy), dd-mm-yyyy dates, a "This month: N items - N events - N overdue" line, a colour legend with **type filter chips**, and a details panel (popover on desktop, bottom sheet on phones) with a link to the relevant page.
  - **Asset tasks** from the Asset schedule: the stored next-due date is the real occurrence (red when overdue); later occurrences in the viewed month are projected from the task's frequency (marked "expected", dashed; past projections are skipped). **Completed** maintenance logs show as *Maintenance done* history (dashed grey; costs are admin-only).
  - **Maintenance due** for the viewed month sits on the month's last day (the same rule as the reminders: overdue once the month has passed). Admin: one item with paid / pending counts and the pending flats. Owners: only their own flats, e.g. "Flat 201 maintenance pending / paid".
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

### Cleaner admin views, reviewed submissions, rejected payments, Activity feed (v7.1)

- **Empty admin sections are hidden.** With no corpus goals, no projects or no (counted) expenses the admin no longer sees empty cards or tables for them: the dashboard (corpus column, settlement / Account Balance / unsettled cards), Corpus overview (Goals, Per-flat status, open goals), Projects, Expenses, the Payments › Corpus / Projects / Expenses tabs and the flat page. Each is replaced by one small **+ Add corpus goal / + Add project / + Add expense** link (the creation form stays available, opened by that link). A General corpus with no target, shares or payments counts as empty. Owners and tenants already hid empty cards; unchanged. **The "Add a one-off corpus amount (General corpus)" form and card are removed** (the General goal itself stays internal and can still be deleted like any other goal; `POST /corpus` is kept but has no screen).
- **Mark reviewed** (Actions board and dashboard card): each "New owner submission" has **Mark reviewed**, plus **Mark all reviewed**. Reviewed items leave the board and the dashboard / menu counts but stay in the audit log, Activity and the lists. Stored per submission in `submission_reviews` (migration `e5c1a7b9d204`), audited (`review`, `review_all`). Submissions already on the board stay un-reviewed after the upgrade; the older "Mark all reviewed" timestamp (`owner_submissions_reviewed_at`) is still honoured. A re-submission after a cancel or reject is a new, un-reviewed item. The board is one line per item (what · flat · amount, date, Open / Mark reviewed / Reject), grouped (Review, Money, Other), empty groups hidden and a friendly "All clear" when nothing is left. Every action type is kept (cancel requests, unsettled expenses, dues, overdue asset tasks, password resets).
- **Reject an owner payment** (admin): a **Reject** action (optional reason) on owner-submitted maintenance, corpus and project payments, in Payments lists and on the Actions board (expenses use the same endpoint, `POST /admin/submissions/{kind}/{id}/reject`). It reuses the cancel-approval path, so the payment disappears from every list, total, dashboard (admin / owner / Simple / tenant), the Actions board, statements, calendar, PDF, Excel and CSV, closed-month rollups recompute, a rejected maintenance payment makes that month's due reappear, and corpus progress % and project paid % drop. Nothing is deleted silently: the owner keeps a record in **My submissions** as **Rejected** with the reason (table `submission_rejections`); the JSON dump keeps every table (expenses carry `status`). A pending cancel request for the same payment is closed. Excel / CSV reports now also leave out cancelled / rejected expenses and reverted settlements (history sheets such as the audit log and cancel requests keep their lines). Cancel-request approval was re-checked in every view (tests in `tests/test_v71_empty_and_review.py`).
- **Activity** (admin only, menu item and `/admin/activity`): newest-first, human sentences from the audit log, e.g. "asrk.sastry (owner, Flat 201) submitted maintenance ₹2,000 for 10-2026" and "admin rejected expense “Bulbs” ₹640", with an Admin / Owner / Tenant badge and date-time (dd-mm-yyyy HH:MM). Filter chips (All / Owners / Admin / Tenants), Any time / Last 7 / Last 30 days, a From-To date form and a "Load more (older)" pager; cards, not tables, so it works on a phone. The dashboard has a compact **Recent activity** card (last 10 and **View all**; hidden while the audit log is empty). The **Audit log** page now shows the same readable line with the raw JSON collapsed under **Details**. Every audit action the app writes has a sentence; unknown ones fall back to "action on entity #id". Owners and tenants get 403 on both pages.
- **Migration** `e5c1a7b9d204` (after `d4b8f1a3c627`): creates `submission_reviews` and `submission_rejections` if absent (idempotent, SQLite and Postgres safe, compatible with the startup `create_all`). Downgrade drops both tables (review marks and owner-visible Rejected records are lost; the ledger itself is not touched).
- **Version label:** `version: nest5-v7.1` at the bottom of the side menu (`APP_VERSION` in `app/config.py`).
- Tests: `tests/test_v71_empty_and_review.py`, `tests/test_v71_activity.py` (also migration from scratch, up-down-up, with the startup safety net and on a v7.0-style database); a few older tests were adjusted for the hidden empty states, the new head and the cleaner Actions board.

### Owner "Expenses you paid" summary and IST clock (v7.2)

- **Consolidated metric for owners.** The "Expenses you paid — owed back to you" card (Detailed dashboard and flat page) and the Simple view's new "Expenses you paid" box show three tiles: **Paid by you**, **Settled** and **Owed back**, plus a count line ("5 expenses · 2 waiting to be settled · 3 settled"). Totals are summed across all of the owner's flats (e.g. manikyamba: 101 + 401); the Detailed card also lists paid / settled / owed per flat. Only counted expenses are included (`Expense.counted()`: rejected and cancelled ones are out). The section shows whenever the owner has any counted expense paid by their flats (fully settled ones show Owed back ₹0 / "All settled") and stays hidden when there are none. Simple view lists at most 3 waiting expenses with a "+ N more" link. Tenants and admin are unchanged. No migration. (`app/services/balances.py` `owner_advances`, `app/templates/_advance_summary.html`.)
- **IST date and time** under "Logged in as … (role)" on every logged-in page (all roles), e.g. `Mon 19-10-2026, 4:37 PM IST`. Rendered on the server with a fixed UTC+5:30 offset (India has no DST), so no tzdata package is needed; `app/static/js/ist-clock.js` refreshes it every minute with the same calculation. It is one non-wrapping line, so the phone header is unaffected.
- **Version label:** `version: nest5-v7.2`. Code-only update from v7.1: no new packages, no migration (head stays `e5c1a7b9d204`).
- Tests: `tests/test_v72_features.py`.

### Reports, settlement notes, optional sample data (v7.4)

- **Version label:** `version: nest5-v7.4`. **No migration** (alembic head stays `b4e7c2a9d531`); no new packages; code-only upgrade.
- **Reports (admin only, read-only): `/admin/reports`** (menu item "Reports"; owners and tenants get 403). Choose a report type (**Consolidated summary**, **Monthly trend**, **One category in detail**), a period (**All time**, a **financial year Apr-Mar**, or a **month range**), an optional **flat**, and whether to also list **rejected / cancelled / reverted** items. Those are always shown separately ("Not counted") and never added to a total.
  - **Consolidated summary:** plain-language tiles with a one-line definition under each number. *Maintenance* billed / collected / pending, per month. *Corpus* per goal (and sub-goal line item): target, shares, collected, pending, plus a total. *Projects* per project: budget, share per flat, shares, collected, pending, plus a grand total (there is no separate sub-project entity; open "Detail" for the per-flat breakdown). *Expenses* total / settled / unsettled / owed to owners, by category, paid by a flat vs from the Account Balance. *Settlements* paid out. A **Reconciliation** block ties opening + collected - paid out = closing for every month, checks the opening of each month against the previous closing, and cross-checks the report against the code the dashboard, Balances page and flat statements use (`compute_fund_balance`, `balances.overview`, `flat_balances`, `expense_total`, goal and project totals). A green check means it ties; any difference shows a warning and the figures that differ. Corpus and project money is shown as held separately from the Account Balance.
  - **Monthly trend:** a table by month (maintenance billed / collected / pending, corpus, projects, expenses, paid out, opening and closing balance) with inline SVG charts (no new JavaScript libraries) and a "Running totals" option.
  - **Drilldown:** every amount is a link to the entries behind it: date (dd-mm-yyyy), flat / owner, type, amount, mode, UTR, payee UPI ID, status, recorded by, recorded at (IST), note (a settlement's note appears here). The footer total equals the number you clicked (built from the same selection, so it cannot differ); pagination, breadcrumb and a back link included.
  - **Exports** of the current screen: CSV, **Excel** (sheets Parameters - filters, generated-at IST, app version, generated-by - then Summary, Trend, Details; dates are real dd-mm-yyyy cells) and **PDF** (titled, filters stated, page numbers). Each export writes an audit-log entry ("admin exported the ... report as CSV") with the report, format and filters.
  - **Counting rules** match the rest of the app: `Expense.counted()` for expenses, paid rows for payments, live settlements. Months: maintenance uses its ledger month; expenses and settlements the ledger month they belong to; corpus and project payments the month they were paid; shares and pending amounts are "as of today" (not narrowed by the month range, only by the flat). The reconciliation block always covers the whole society.
  - Phone-friendly: tables turn into cards under 768 px; empty states explain what to change; glossary tooltips on column headings.
- **Edit a settlement's note (admin only):** the "Recent settlements" table on `/settlements` has a Note box with Save note, and a settled expense's Edit panel on `/expenses` has the same field (`POST /settlements/{id}/note`). Audited as `settlement` / `edit_note` (shown in Activity as "admin edited settlement note", with the old and new text in the audit log). The amount, date, expense and balances are untouched; a reverted settlement cannot be edited; owners and tenants get 403. The note shows on the Settlements and Expenses pages, in the Reports drilldown and exports, and in the existing raw Excel / CSV / JSON exports and summary PDF.
- **Seed data is optional, and ships no sample expenses or settlements.** `scripts/seed.py` always sets up the flat roster, the standard asset register and the initial payment settings. The Sep-Oct 2026 demo months (every flat's maintenance marked paid, one open complaint) are created only with `python scripts/seed.py --sample` (or `SEED_SAMPLE_DATA=1`); even then there are no expenses and no settlements (the old "Flat 101 advance", "Common expense 1/2/3", "Watchman salary" and their "Reimbursed from maintenance fund" / "Paid from maintenance fund" settlement notes are gone). Running the seed against a database that already has ledger data changes nothing, so live data is not affected. Tests that relied on the old sample numbers (Sept closing 4,955, fund 10,955, Watchman salary) now build that data in `tests/legacy_seed.py`.
- **Deploy:** code-only; copy the new files over and restart with a single uvicorn worker (no `alembic upgrade` needed, none pending).
- Tests: `tests/test_v74_reports.py` (totals tie to dashboard / balances / flat statements, every clicked number equals its drilldown footer, trend months sum to the totals, reconciliation ties and warns when forced to mismatch, excluded items, closed months, sub-goal and project totals, exports match the screen and are audited, access control, empty states, flat / FY / month-range controls), `tests/test_v74_settlement_note.py`, `tests/test_v74_seed.py`; the dd-mm-yyyy / IST crawl in `tests/test_v73_date_format.py` also covers the Reports pages.

### Automatic monthly dues, recurring admin tasks, simpler Pay now, admin-set payment details (v7.3)

- **Version label:** `version: nest5-v7.3`. Two migrations, `a3d9e6c1b852` (revises `e5c1a7b9d204`) and `b4e7c2a9d531` (revises `a3d9e6c1b852`; head); no new packages.
- **A. Automatic monthly dues.** On the 1st of every month (IST) the app creates that month's ledger period and a maintenance due for every flat, at the current fee (`fee_for_period`, so revised fees apply). The due date is the **5th**; a due is overdue only **after** the 5th (reminders, Actions, Calendar, Simple view, owner My submissions). It is an in-app asyncio task started in the FastAPI lifespan: it runs at startup and then about hourly (`MONTHLY_JOB_INTERVAL_SECONDS`, default 3600; switch off with `MONTHLY_JOB_ENABLED=false`). Page loads also run a cheap, cached catch-up, so a missed run heals itself. It is idempotent, never closes or backfills earlier months and never creates a period older than an existing one; a new flat added mid-month gets its row for the current month. The new month opens at the previous month's closing balance (flagged `opening_carried`, so a late payment in the previous month still flows through and closing that month later finalises it), so nothing is double counted or lost. The creation is audited with actor `system` ("Monthly dues created automatically"). **Run a single uvicorn worker** (the job lock is process-local; with several workers the unique/exists checks still keep the data correct, but run one).
- **B. Recurring admin tasks (admin only).** `/admin/recurring` seeds "Watchman salary payment (by the 5th)" and "Common EB (electricity) payment (before the 10th, due on the 10th)". Admin can add, edit, disable/enable tasks (day of month, "by"/"before" wording, repeat every N months, notes) and mark an occurrence done (date, optional amount, note) or undo it. Occurrences are generated lazily and idempotently each month, show on the Calendar (own type, legend and filter), the Actions board ("Recurring tasks due": due within 5 days or overdue until done, with an inline Done form), the admin dashboard Actions card and the Schedule page; empty sections are hidden. Marking done never creates an expense. Done, undo, add, edit and enable/disable are audited. Owners and tenants never see them (403 on the routes).
- **C. Simpler Pay now sheet.** No QR and no copy buttons (also no top amount box). The sheet shows the UPI ID, mobile number and payee as large, select-all text, a one-line hint (amount, %, due date), the editable Amount (part payments), date, mode, UTR and Submit, and the Open PhonePe button. `/upi/qr.svg` still exists but nothing links to it.
- **D. Payment details set by the admin, with history.** The UPI ID, mobile and payee come from the database (`app_settings`); `UPI_VPA`, `UPI_MOBILE`, `UPI_PAYEE_NAME` in `.env`/config are only the initial seed. Admin page `/admin/payment-settings` (nav: "Payment settings") needs a **reason**, validates the UPI ID (`name@bank`) and a 10-digit Indian mobile, shows a **review and confirm** step, then appends a row to `/admin/payment-settings/history` (newest first; time in IST, who, old → new per field, reason, the app-computed Account Balance / Corpus / Projects / Total at that moment, and an optional bank balance exactly as typed). The first row is the seeded initial value, marked Initial. History rows cannot be edited or deleted (no route, and the ORM refuses updates/deletes). Admin only (owners and tenants get 403). Audited as `payment_settings` / `change`.
- **Past payments never change.** Every payment row (maintenance, corpus, project contribution, and rejected-submission records) stores the **payee UPI ID, mobile, name and the settings-history version id** in four new nullable columns at the moment it is recorded (owner submissions and admin-recorded payments alike; cash and bank rows carry the same snapshot, shown as "Paid to ..."). The migration backfills all existing paid/recorded rows with the initial seeded values, linked to the initial history row, touching no other column. The payee shows on the admin Maintenance / Corpus / Projects lists, flat page, statements and the flat statement PDF, the owner's My submissions, and in the Excel / CSV / JSON exports (`payee_vpa`, `payee_mobile`, `payee_name`, `payee_settings_id` columns). Changing the settings affects only Pay now sheets opened afterwards; amounts, balances, audit text, activity text and exports of earlier payments stay identical. Reverting/undoing/rejecting a payment clears the snapshot on the live row (the rejection record keeps it). Project share rows (`project_payments`) carry no payee: the contribution rows are the payments. Restoring a pre-v7.3 JSON dump re-derives the live values from the kept history and stamps restored rows with the initial payee.
- **E. Soft-delete users (in-place update, migration `b4e7c2a9d531`).** Admin Users page: **Delete** (type the username to confirm; you cannot delete yourself or the last active admin) marks the user `deleted_at` / `deleted_by` and inactive. Nothing is removed: flat links, payments, submissions and audit rows stay and keep the name. The user cannot log in, existing sessions stop working, reset links stop working, and they drop out of user lists, reminders, reset and e-mail lookups and the Excel / CSV user sheet (the JSON backup keeps them); ledger totals are unchanged. They appear in a collapsed **Deleted users** section with **Restore**, which brings back the same account, role and flats with a new temporary password and a forced change at first login. The username stays reserved while deleted. Audited and shown in Activity as "deleted user" / "restored user". Admin only (403 otherwise). The migration only adds two nullable columns on `users`, and is idempotent (safe after the startup safety net), with a working downgrade.
- **F. Flat 201 roster fix (in place).** The roster lists flat **201**, owner "A. Siva Rama Krishna Sastry" (was 102). A fresh install seeds it; a database already on 201 is left alone; an old database still on 102 (and no 201) is renumbered once, keeping the flat id and every link. Names you edited in the portal are never overwritten, and restarts change nothing. The owner login for flat 201 is **`asrk.sastry`** (set in the roster; other owners' logins are still derived from their names, e.g. `venkata.giridhar`). An existing database keeps whatever login it already has (the live `sastry` account and its flat links are not touched; rename it on the Users page if you want).
- **G. Payee / UPI audit report (admin only, read-only).** `/admin/payee-report` (nav: "Payee / UPI report"). Lists maintenance, corpus and project payments plus rejected owner submissions by the payee saved on each row (expenses and settlements are money paid out and carry no payee, so they are not covered). Filters: UPI ID (dropdown of every ID ever used, plus free text), mobile, payee name, settings version, date range, type, mode, status (paid / pending / rejected / reverted) and flat. Rejected and reverted rows are shown, marked "not counted" and left out of every total unless "Include rejected and reverted rows in totals" is ticked. Cash, bank-transfer and cheque payments and rows with no payee are grouped as "No UPI / cash-bank". The summary shows count and total per payee account, and, per settings-change period, "Under UPI X from <date> to <date>: ₹N received" next to the Account Balance / Total recorded in the history, so it can be reconciled. Cards on a phone, a table on a desktop, 25 rows per page, times in IST. Export the filtered view as CSV, Excel (Payments, By payee, By settings version sheets) or PDF. Owners and tenants get 403.
- **H. Admin dashboard layout (in-place update).** Order for the admin: period bar, top metrics and balance cards, **Action board** (directly below), **Alerts**, the **Actions** list, then **Recent activity**, which is collapsed by default (a `<details>` with the entry count in its summary line; **View all** inside). The old dashboard **Settlement** card (a per-expense table with Settle buttons) was removed because the Actions list already shows "Unsettled owner expenses to settle" and links to the Settlements page; the Unsettled / Effective balance / Available to settle now metrics, the Settlements menu item, the Settlements page and every settle route are unchanged. Owner and tenant dashboards keep their old order.
- **I. Due dates once under the column name (Action board, admin only).** The Action board shows each due date once, under its column heading, never on flat rows: **Maintenance** shows the 5th of the viewed month (so 2026-09 shows 05-09-2026), **Corpus** lists each open goal that has a due date ("Lift: due 15-12-2026"), each **project** column shows its own due date. A date that has passed while something is still unpaid reads "overdue since dd-mm-yyyy" in red; no due date, nothing shown. On a phone (cards, no column headings) the same facts appear once in a compact "Due" line above the cards.
- **J. All dates are dd-mm-yyyy.** Every date the app displays is zero-padded dd-mm-yyyy and date-times are dd-mm-yyyy HH:MM IST; the IST clock reads "Mon 19-10-2026, 4:37 PM IST" (generated-at stamps read "19-10-2026 16:37 IST"). One helper module, `app/dates.py` (`format_date`, `format_datetime`, `format_weekday_date`, `format_ist`, `format_ist_stamp`, `due_hint`, `display_dates_in_text`), is used by templates (filters), Activity / calendar / reminder / action text, PDF, Excel (real dates with a dd-mm-yyyy number format), CSV (dd-mm-yyyy) and the audit page's raw before / after data. ISO yyyy-mm-dd remains only in form values and date inputs, URLs / query strings, the database, and the **JSON backup** (needed for restore). Month-only labels such as 2026-10, "October 2026" and "Oct-2026" are unchanged. `tests/test_v73_date_format.py` crawls the admin, owner and tenant pages and the exports for ISO leaks and for times shown without "IST".
- **K. Every displayed time is IST.** Times are stored as naive UTC and converted only when shown or exported, by `app/dates.py` (`to_ist`, `format_datetime` -> `19-10-2026 16:37 IST`, `format_ist`, `format_ist_stamp`, `ist_today`, `ist_day_start_utc`): the Audit log and its raw before / after data, Activity (page and dashboard card), Actions rows, My submissions and cancel requests, payment-settings history, the payee report, the Users list (last login), the maintenance-fee history, "Recently done" recurring tasks (new *Recorded* column) and every generated-at stamp. Excel keeps real date-time cells in IST with the number format `dd-mm-yyyy hh:mm "IST"`; the CSV zip and the PDFs print `dd-mm-yyyy HH:MM IST` (the CSV README says so). Date-only values (due dates, paid dates, expense dates) are never converted. **"Today", "due today" and "overdue" use the IST date everywhere** (`ist_today()`, no `date.today()` left in `app/`), and the Activity date filter treats its dates as IST days. The **JSON backup stays UTC** (ISO, `exported_at` is UTC) so a restore is exact. Backup, export and report **file names** keep their `YYYYMMDD-HHMM(SS)` stamp, now in IST (they were server-local time). `tests/test_v73_ist.py` covers UTC 18:30 = 00:00 next day IST, the Audit / Activity pages and exports.
- **L. Pay now footer.** An owner's payment counts the moment it is submitted (it is recorded against the due, Submitted by owner); the admin then verifies the UTR against the UPI statement and can Reject it, which makes the due reappear and shows it to the owner as Rejected. The sheet footer says exactly that: "Your payment is recorded right away against this due. The admin then checks the UTR against the UPI statement; if it can't be matched, the admin rejects it and the due comes back."
- **Deploy:** unzip, keep `nest5.db`, `instance/`, `backups/`, `.env`, run `alembic upgrade head` (`./run-local.sh` does it) and restart with a single uvicorn worker.
- Tests: `tests/test_v73_monthly.py`, `test_v73_job.py`, `test_v73_recurring.py`, `test_v73_payment_settings.py`, `test_v73_user_delete.py`, `test_v73_payee_report.py`, `test_v73_dashboard_layout.py`, `test_v73_date_format.py`, `test_v73_ist.py`, `test_flats.py`, `test_v73_migration.py` (migration from scratch, up-down-up, after the startup safety net, over v7.2-style data); the Pay now and due-date tests were updated for the simpler sheet and the 5th.

### Upgrading from v7.3 (keep your `nest5.db`, `instance/`, `backups/`, `.env`)

Unzip `nest5-ledger-v7.4.zip` into a new folder, copy `nest5.db`, `instance/`, `backups/` and `.env` over, then `./run-local.sh` (no migration is pending; `alembic upgrade head` is a no-op) and run a single uvicorn worker. No new packages. Or, on a running server, copy only the changed files listed in the release notes and restart.

### Upgrading from v7.2 (keep your `nest5.db`, `instance/`, `backups/`, `.env`)

Unzip `nest5-ledger-v7.3.zip` into a new folder, copy `nest5.db`, `instance/`, `backups/` and `.env` over, then `./run-local.sh` (runs `alembic upgrade head`: two migrations, `a3d9e6c1b852` then `b4e7c2a9d531`) and run a single uvicorn worker. No new packages.

### Upgrading from v7.0 (keep your `nest5.db`, `instance/`, `backups/`, `.env`)

Unzip `nest5-ledger-v7.1.zip` into a new folder, copy `nest5.db`, `instance/`, `backups/` and `.env` over, then `./run-local.sh` (runs `alembic upgrade head`: one migration, `e5c1a7b9d204`). No new packages.

### Upgrading from v9.x (keep your `nest5.db`, `instance/`, `backups/`, `.env`)

Unzip `nest5-ledger-v7.0.zip` (or v7.1: one more migration, `e5c1a7b9d204`) into a new folder, copy `nest5.db`, `instance/`, `backups/` and `.env` over, then `./run-local.sh` (runs `alembic upgrade head`: from v9 two migrations, `c9a2e5f7b140` and `d4b8f1a3c627`; from v9.1 / v9.2 only `d4b8f1a3c627`). No new packages. Existing asset tasks are left alone: use **Clear all asset tasks & alerts** on the Asset schedule page (or `scripts/clear_asset_alerts.py --yes`) when you are ready to re-enter them.

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
- **Flat owners** on dashboard, pending lists, and flat detail; roster in `app/domain/flats.py`; the startup sync only creates missing flats, fills empty names and renumbers an old flat **102 → 201** once (keeping its id); it never overwrites an owner name the admin edited

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
| `/upi/qr.svg` | UPI QR (still served, no longer linked from the pay sheet since v7.3) |
| `/admin/recurring` | Recurring admin tasks: watchman salary, common EB, ... (v7.3, admin only) |
| `/admin/payment-settings`, `/admin/payment-settings/history` | UPI ID / mobile / payee settings and their append-only history (v7.3, admin only) |
| `/admin/reports` (+ `/drill`, `/export.csv`, `/export.xlsx`, `/export.pdf`) | Reports: consolidated summary, monthly trend, category detail, drilldown, exports (v7.4, admin only, read-only) |
| `/settlements/{id}/note` (POST) | Edit a settlement's note (v7.4, admin only) |
| `/admin/payee-report` (+ `/export.csv`, `/export.xlsx`, `/export.pdf`) | Payee / UPI audit report (v7.3, admin only, read-only) |
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
| 201 | A. Siva Rama Krishna Sastry | Renumbered from 102 |
| 301 | Venkata Giridhar | |
| 401 | Manikyamba | |
| 501 | Pavan Kumar | |

Contact phone fields are optional placeholders (“Filled later” in UI) until updated.

## Optional sample data (`python scripts/seed.py --sample`)

- **September 2026:** collections ₹10,000; no expenses; closing balance ₹10,000
- **October 2026:** collections ₹10,000; lift-doors complaint
- **Fund balance:** ₹20,000. No sample expenses or settlements are created (v7.4).

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
