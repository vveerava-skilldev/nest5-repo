"""v7.3 A: automatic monthly dues (fake clock, own scratch database for every test)."""
from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    AuditLog,
    Flat,
    LedgerPeriod,
    MaintenanceFeeRevision,
    MaintenancePayment,
)
from app.services import ledger as ledger_svc
from app.services import maintenance_fee as fee_svc
from app.services import monthly as monthly_svc
from app.services import reminders as reminders_svc
from app.services.flats import sync_flat_roster

FEE = 2000


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    sync_flat_roster(session)
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _period(db, y, m):
    return db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == y, LedgerPeriod.month == m))


def _pay_all(db, period, amount_each=None):
    for mp in db.scalars(select(MaintenancePayment).where(MaintenancePayment.period_id == period.id)).all():
        mp.is_paid, mp.paid_on = True, date(period.year, period.month, 3)
        if amount_each:
            mp.amount = amount_each
    db.commit()


def _rows(db, period):
    return db.scalars(select(MaintenancePayment).where(MaintenancePayment.period_id == period.id)).all()


@pytest.fixture
def world(db):
    """September closed, October open and fully paid (like the seed)."""
    sept = ledger_svc.get_or_create_period(db, 2026, 9)
    _pay_all(db, sept)
    ledger_svc.close_period(db, sept)
    db.commit()
    octo = _period(db, 2026, 10)
    ledger_svc.ensure_maintenance_rows(db, octo)
    _pay_all(db, octo)
    return db


def _flats(db):
    return db.scalar(select(func.count()).select_from(Flat))


# ---- clock: before / at / after the 1st ---------------------------------------------------------


def test_nothing_is_created_before_the_first(world):
    r = monthly_svc.ensure_current(world, date(2026, 10, 31))
    assert not r["period_created"] and _period(world, 2026, 11) is None
    assert world.scalar(select(func.count()).select_from(LedgerPeriod)) == 2


def test_created_on_the_first_for_every_flat_due_on_the_fifth(world):
    r = monthly_svc.ensure_current(world, date(2026, 11, 1))
    assert r["period_created"] and r["rows_added"] == _flats(world)
    nov = _period(world, 2026, 11)
    rows = _rows(world, nov)
    assert len(rows) == _flats(world) and all(not mp.is_paid and mp.amount == FEE for mp in rows)
    assert reminders_svc.maintenance_due_date(nov) == date(2026, 11, 5)
    assert reminders_svc.due_hint(reminders_svc.maintenance_due_date(nov)) == "due 05-11-2026"


def test_a_missed_run_heals_on_any_later_day(world):
    monthly_svc.ensure_current(world, date(2026, 11, 17))
    assert _period(world, 2026, 11) is not None


def test_december_rolls_into_january(world):
    monthly_svc.ensure_current(world, date(2026, 11, 1))
    monthly_svc.ensure_current(world, date(2026, 12, 1))
    jan = monthly_svc.ensure_current(world, date(2027, 1, 1))
    assert jan["period_created"]
    p = _period(world, 2027, 1)
    assert p is not None and len(_rows(world, p)) == _flats(world)
    assert reminders_svc.maintenance_due_date(p) == date(2027, 1, 5)


def test_rerun_is_idempotent_and_audits_once(world):
    monthly_svc.ensure_current(world, date(2026, 11, 1))
    again = monthly_svc.ensure_current(world, date(2026, 11, 1))
    later = monthly_svc.ensure_current(world, date(2026, 11, 20))
    assert not again["period_created"] and again["rows_added"] == 0 and later["rows_added"] == 0
    assert world.scalar(select(func.count()).select_from(LedgerPeriod).where(LedgerPeriod.month == 11)) == 1
    assert world.scalar(select(func.count()).select_from(MaintenancePayment)
                        .where(MaintenancePayment.period_id == _period(world, 2026, 11).id)) == _flats(world)
    audits = world.scalars(select(AuditLog).where(AuditLog.entity_type == "ledger_period", AuditLog.action == "auto_create")).all()
    assert len(audits) == 1 and audits[0].user_label == "system"
    assert "11-2026" in audits[0].after_json


def test_a_flat_added_mid_month_gets_its_row_without_a_new_period(world):
    monthly_svc.ensure_current(world, date(2026, 11, 1))
    before = _flats(world)
    world.add(Flat(number="777"))
    world.commit()
    r = monthly_svc.ensure_current(world, date(2026, 11, 12))
    nov = _period(world, 2026, 11)
    assert not r["period_created"] and r["rows_added"] == 1
    assert len(_rows(world, nov)) == before + 1
    assert world.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "auto_create")) == 1


def test_never_auto_closes_and_never_backfills_earlier_months(world):
    monthly_svc.ensure_current(world, date(2027, 2, 3))  # nothing ran for Nov, Dec, Jan
    assert _period(world, 2026, 11) is None and _period(world, 2026, 12) is None and _period(world, 2027, 1) is None
    feb = _period(world, 2027, 2)
    assert feb is not None
    assert not _period(world, 2026, 10).is_closed and not feb.is_closed


def test_does_not_create_a_period_older_than_an_existing_later_one(world):
    ledger_svc.get_or_create_period(world, 2026, 12)
    world.commit()
    r = monthly_svc.ensure_current(world, date(2026, 11, 2))
    assert not r["period_created"] and _period(world, 2026, 11) is None


def test_revised_fee_with_an_effective_month_applies(world):
    world.add(MaintenanceFeeRevision(effective_date=date(2026, 12, 1), amount=2500, note="revised"))
    world.commit()
    monthly_svc.ensure_current(world, date(2026, 11, 1))
    monthly_svc.ensure_current(world, date(2026, 12, 1))
    assert {mp.amount for mp in _rows(world, _period(world, 2026, 11))} == {FEE}
    assert {mp.amount for mp in _rows(world, _period(world, 2026, 12))} == {2500}
    assert fee_svc.fee_for_period(world, _period(world, 2026, 12)) == 2500


# ---- fund / opening / closing maths ------------------------------------------------------------


def test_auto_created_month_does_not_wipe_or_double_count_the_balance(world):
    fund = ledger_svc.compute_fund_balance(world)
    assert fund > 0
    monthly_svc.ensure_current(world, date(2026, 11, 1))
    nov = _period(world, 2026, 11)
    octo = _period(world, 2026, 10)
    assert ledger_svc.compute_fund_balance(world) == fund  # nothing lost, nothing counted twice
    assert nov.opening_fund_balance == fund and nov.opening_carried is True
    assert ledger_svc.period_fund_summary(world, octo)["closing"] == fund
    first = _rows(world, nov)[0]
    first.is_paid, first.paid_on = True, date(2026, 11, 3)
    world.commit()
    assert ledger_svc.compute_fund_balance(world) == fund + FEE


def test_late_october_payment_flows_into_the_open_novembers_opening(world):
    octo = _period(world, 2026, 10)
    late = _rows(world, octo)[0]
    late.is_paid, late.paid_on = False, None
    world.commit()
    fund_without = ledger_svc.compute_fund_balance(world)
    monthly_svc.ensure_current(world, date(2026, 11, 1))
    nov = _period(world, 2026, 11)
    assert nov.opening_fund_balance == fund_without
    late.is_paid, late.paid_on = True, date(2026, 11, 2)
    world.commit()
    world.refresh(nov)
    assert nov.opening_fund_balance == fund_without + FEE  # still following October until it is closed
    assert ledger_svc.compute_fund_balance(world) == fund_without + FEE


def test_closing_the_previous_month_later_carries_the_opening_correctly(world):
    monthly_svc.ensure_current(world, date(2026, 11, 1))
    nov = _period(world, 2026, 11)
    for mp in _rows(world, nov)[:3]:
        mp.is_paid, mp.paid_on = True, date(2026, 11, 3)
    world.commit()
    before = ledger_svc.compute_fund_balance(world)
    octo = _period(world, 2026, 10)
    closing = ledger_svc.close_period(world, octo)
    world.commit()
    world.refresh(nov)
    assert nov.opening_fund_balance == closing and not nov.opening_carried
    assert octo.is_closed and octo.closing_fund_balance == closing
    assert ledger_svc.compute_fund_balance(world) == before  # same money, now finalised
    # a late change in the closed month still follows through (existing behaviour kept)
    octo_row = _rows(world, octo)[0]
    octo_row.is_paid = False
    world.commit()
    world.refresh(nov)
    assert nov.opening_fund_balance == closing - FEE
    assert ledger_svc.compute_fund_balance(world) == before - FEE


def test_month_created_after_a_closed_month_starts_from_its_closing(world):
    octo = _period(world, 2026, 10)
    closing = ledger_svc.close_period(world, octo)
    world.commit()
    nov = _period(world, 2026, 11)  # close_period already opened November
    assert nov is not None and nov.opening_fund_balance == closing
    r = monthly_svc.ensure_current(world, date(2026, 11, 1))
    assert not r["period_created"] and r["rows_added"] == 0 and nov.opening_fund_balance == closing
    monthly_svc.ensure_current(world, date(2026, 12, 1))
    dec = _period(world, 2026, 12)
    assert dec.opening_fund_balance == ledger_svc.compute_fund_balance(world, nov) and dec.opening_carried is True
    assert ledger_svc.compute_fund_balance(world) == closing


def test_closing_a_carried_month_keeps_the_chain_consistent(world):
    fund = ledger_svc.compute_fund_balance(world)
    monthly_svc.ensure_current(world, date(2026, 11, 1))
    monthly_svc.ensure_current(world, date(2026, 12, 1))
    nov, dec = _period(world, 2026, 11), _period(world, 2026, 12)
    for mp in _rows(world, nov):
        mp.is_paid, mp.paid_on = True, date(2026, 11, 4)
    world.commit()
    world.refresh(dec)
    assert dec.opening_fund_balance == fund + FEE * _flats(world)
    assert ledger_svc.compute_fund_balance(world) == fund + FEE * _flats(world)
    ledger_svc.close_period(world, _period(world, 2026, 10))
    ledger_svc.close_period(world, nov)
    world.commit()
    world.refresh(dec)
    assert dec.opening_fund_balance == fund + FEE * _flats(world) and not dec.opening_carried
    assert ledger_svc.compute_fund_balance(world) == fund + FEE * _flats(world)


def test_first_ever_month_opens_at_zero(db):
    r = monthly_svc.ensure_current(db, date(2026, 11, 1))
    p = _period(db, 2026, 11)
    assert r["period_created"] and p.opening_fund_balance == 0 and len(_rows(db, p)) == _flats(db)
