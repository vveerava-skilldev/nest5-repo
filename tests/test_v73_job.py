"""v7.3 A end to end: the lazy catch-up on page loads and the lifespan background job, with a fake IST clock."""
from tests.test_v71_empty_and_review import run_scratch

CLOCK = r'''
import datetime as _dt, importlib
FAKE = {"d": _dt.date(2026, 11, 2)}
for _m in ("app.dates", "app.services.monthly", "app.services.reminders", "app.services.actions", "app.services.periods",
           "app.services.recurring", "app.routers.calendar", "app.routers.admin_settings"):
    importlib.import_module(_m).ist_today = lambda: FAKE["d"]
import app.services.monthly as monthly
from sqlalchemy import func
def period_row(y, m):
    return db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == y, LedgerPeriod.month == m))
'''


def test_page_load_creates_the_new_month_for_every_flat_and_dues_are_due_on_the_5th(tmp_path):
    code = r'''
db.expire_all()
out["before"] = period_row(2026, 11) is None
owner = login("sastry")
simple = owner.get("/?view=simple")
out["simple_status"] = simple.status_code
db.expire_all()
nov = period_row(2026, 11)
oct_ = period_row(2026, 10)
flats = db.scalar(select(func.count()).select_from(Flat))
rows = db.scalars(select(MaintenancePayment).where(MaintenancePayment.period_id == nov.id)).all()
out["rows"] = [len(rows), flats, sum(1 for r in rows if r.is_paid)]
out["opening"] = [nov.opening_fund_balance, oct_.opening_fund_balance, bool(nov.opening_carried), oct_.is_closed, nov.is_closed]
out["sept_closed"] = period_row(2026, 9).is_closed
out["periods"] = db.scalar(select(func.count()).select_from(LedgerPeriod))
out["due_text"] = "05-11-2026" in simple.text
out["audit"] = [(a.entity_type, a.action, a.user_label) for a in db.scalars(select(AuditLog).where(AuditLog.entity_type == "ledger_period")).all()
                if a.action == "auto_create"]
from app.services import ledger as ledger_svc
f1 = ledger_svc.compute_fund_balance(db)
for _ in range(3):
    admin.get("/payments/maintenance")
db.expire_all()
out["idempotent"] = [db.scalar(select(func.count()).select_from(MaintenancePayment).where(MaintenancePayment.period_id == nov.id)),
                     db.scalar(select(func.count()).select_from(LedgerPeriod)),
                     db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "auto_create"))]
out["fund_unchanged"] = ledger_svc.compute_fund_balance(db) == f1
# not overdue on the 5th itself, overdue from the 6th
from app.services import reminders as rem
flat_id = db.scalar(select(User).where(User.username == "sastry")).flats[0].id
def status():
    items = rem.reminders_for_flats(db, [flat_id])["items"]
    return [(i["label"], i["status"]) for i in items if i["kind"] == "Maintenance" and "November" in i["label"]]
res = {}
for day in (2, 5, 6):
    FAKE["d"] = _dt.date(2026, 11, day)
    monthly.reset_cache()
    res[day] = status()
out["status"] = {str(k): v for k, v in res.items()}
'''
    out = run_scratch(tmp_path, CLOCK + code)
    assert out["before"] is True and out["simple_status"] == 200
    assert out["rows"][0] == out["rows"][1] and out["rows"][2] == 0
    assert out["opening"][2] is True and out["opening"][3] is False and out["opening"][4] is False
    assert out["sept_closed"] is True and out["periods"] == 3  # nothing backfilled, nothing auto-closed
    assert out["due_text"] is True
    assert out["audit"] == [["ledger_period", "auto_create", "system"]]
    assert out["idempotent"] == [out["rows"][0], 3, 1] and out["fund_unchanged"] is True
    assert out["status"]["2"] and all(s != "overdue" for _, s in out["status"]["2"])
    assert out["status"]["5"] and all(s != "overdue" for _, s in out["status"]["5"])
    assert out["status"]["6"] and all(s == "overdue" for _, s in out["status"]["6"])


def test_lifespan_job_runs_at_startup_and_is_cancelled_cleanly(tmp_path):
    code = r'''
import time
from app.config import settings
FAKE["d"] = _dt.date(2026, 12, 1)
monthly.reset_cache()
with TestClient(app) as client:      # lifespan: startup hook + background job
    for _ in range(100):
        s = SessionLocal()
        try:
            if s.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 12)):
                break
        finally:
            s.close()
        time.sleep(0.1)
db.expire_all()
dec = period_row(2026, 12)
out["dec"] = dec is not None
out["rows"] = db.scalar(select(func.count()).select_from(MaintenancePayment).where(MaintenancePayment.period_id == dec.id)) == db.scalar(select(func.count()).select_from(Flat)) if dec else None
out["carried"] = bool(dec.opening_carried) if dec else None
out["audit_system"] = db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "auto_create", AuditLog.user_label == "system"))
out["tasks"] = db.scalar(select(func.count()).select_from(RecurringTask))
out["occurrences"] = db.scalar(select(func.count()).select_from(RecurringTaskOccurrence))
# disabled flag: nothing happens at startup
settings.monthly_job_enabled = False
FAKE["d"] = _dt.date(2027, 1, 1)
monthly.reset_cache()
with TestClient(app) as client2:
    time.sleep(0.6)
db.expire_all()
out["jan_when_disabled"] = period_row(2027, 1) is not None
'''
    out = run_scratch(tmp_path, CLOCK + code)
    assert out["dec"] is True and out["rows"] is True and out["carried"] is True
    assert out["audit_system"] == 1 and out["tasks"] == 2 and out["occurrences"] == 2
    assert out["jan_when_disabled"] is False


def test_december_to_january_and_a_revised_fee_flow_through_the_page_load(tmp_path):
    code = r'''
FAKE["d"] = _dt.date(2026, 12, 1)
monthly.reset_cache(); admin.get("/payments/maintenance")
FAKE["d"] = _dt.date(2027, 1, 1)
monthly.reset_cache(); admin.get("/payments/maintenance")
db.expire_all()
jan = period_row(2027, 1)
out["jan"] = [jan is not None, jan.year, jan.month, db.scalar(select(func.count()).select_from(MaintenancePayment).where(MaintenancePayment.period_id == jan.id))]
out["periods"] = sorted((p.year, p.month) for p in db.scalars(select(LedgerPeriod)).all())
out["tasks_jan"] = sorted(str(o.due_date) for o in db.scalars(select(RecurringTaskOccurrence).where(RecurringTaskOccurrence.year == 2027)).all())
'''
    out = run_scratch(tmp_path, CLOCK + code)
    assert out["jan"][:3] == [True, 2027, 1] and out["jan"][3] > 0
    assert out["periods"] == [[2026, 9], [2026, 10], [2026, 12], [2027, 1]]
    assert out["tasks_jan"] == ["2027-01-05", "2027-01-10"]
