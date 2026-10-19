#!/usr/bin/env bash
# Run from nest5-ledger/:  bash verify-install.sh
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
echo "Install folder: $ROOT"
echo ""
FILES=(
  requirements.txt alembic.ini alembic/env.py
  app/main.py scripts/seed.py run-local.sh
  scripts/reset_admin.py scripts/create_tenant.py app/services/tenant.py alembic/versions/d4b8f1a3c627_tenant_role.py app/routers/accounts.py app/routers/data_export.py
  alembic/versions/e7b3c9d2f4a1_users_rbac.py
  app/routers/owner.py alembic/versions/f3a8d1c6b2e9_owner_submissions_corpus_campaigns.py
  alembic/versions/a6c4e2b8d013_general_corpus_collection.py
  app/templates/payments_projects.html app/templates/payments_expenses.html
  alembic/versions/b5d7f1e3c920_corpus_goals.py alembic/versions/d3a1c5e7f902_app_settings.py
  alembic/versions/e1f4b7a2c805_asset_register.py
  app/templates/_corpus_pay_form.html app/templates/payments_building.html
  app/templates/asset_detail.html app/templates/_asset_macros.html
  app/services/insights.py app/services/app_settings.py
  alembic/versions/f2c6a8d4b301_project_payments_categories.py
  app/services/projects.py app/services/balances.py app/services/actions.py
  app/services/expense_categories.py app/routers/admin_tools.py
  app/templates/admin_actions.html app/templates/_actions_list.html
  app/templates/admin_expense_categories.html app/templates/_my_advances.html
  alembic/versions/a7d2c4e9b108_user_dashboard_view.py app/services/dashboard_view.py
  app/services/upi.py app/routers/simple_dashboard.py
  app/templates/dashboard_simple.html app/templates/_view_toggle.html
  alembic/versions/b8e3f5a1d726_calendar_events.py app/services/portal_calendar.py
  app/routers/calendar.py app/templates/calendar.html app/templates/calendar_event_form.html
  app/static/js/paynow.js app/static/js/calendar.js
  alembic/versions/c9a2e5f7b140_expense_review_status.py app/services/expense_review.py
  app/templates/_expense_review.html scripts/clear_asset_alerts.py
)
OK=0
for f in "${FILES[@]}"; do
  if [[ -e "$f" ]]; then echo "  OK  $f"; else echo "  MISSING  $f"; OK=1; fi
done
# v5 shipped a damaged projects.html (~1.9 MB); every template should be a few KB.
for f in app/templates/*.html; do
  size=$(wc -c < "$f")
  if (( size > 40000 )); then echo "  DAMAGED  $f ($size bytes) — unzip a fresh copy"; OK=1; fi
done
echo ""
if [[ $OK -eq 0 ]]; then echo "All required files present. Run: ./run-local.sh"; else echo "Re-download docs/nest5-ledger.zip and unzip fresh."; fi
exit "$OK"
