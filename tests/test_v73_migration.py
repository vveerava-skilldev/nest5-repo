"""v7.3 Alembic migrations a3d9e6c1b852 and b4e7c2a9d531 (soft-deleted users): from scratch, up/down/up, after the startup safety net, and over live data."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V73, HEAD, PREV = "a3d9e6c1b852", "b4e7c2a9d531", "e5c1a7b9d204"
PAYEE_TABLES = ("maintenance_payments", "corpus_payments", "project_contributions", "submission_rejections")
PAYEE_COLS = {"payee_vpa", "payee_mobile", "payee_name", "payee_settings_id"}
NEW_TABLES = {"recurring_tasks", "recurring_task_occurrences", "payment_detail_changes"}


def _env(tmp_path):
    return {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'm.db'}", "INSTANCE_DIR": str(tmp_path / "i"),
            "BACKUP_DIR": str(tmp_path / "b")}


def _alembic(tmp_path, *args):
    ran = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=_env(tmp_path), capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr[-2500:]
    return ran


def _con(tmp_path):
    return sqlite3.connect(tmp_path / "m.db")


def _tables(con):
    return {r[0] for r in con.execute("select name from sqlite_master where type='table'")}


def _cols(con, table):
    return {r[1] for r in con.execute(f"pragma table_info({table})")}


def _version(con):
    return con.execute("select version_num from alembic_version").fetchone()[0]


def _assert_v73_schema(con):
    assert NEW_TABLES <= _tables(con)
    for t in PAYEE_TABLES:
        assert PAYEE_COLS <= _cols(con, t), t
    assert "opening_carried" in _cols(con, "ledger_periods")


def _assert_soft_delete_schema(con):
    assert {"deleted_at", "deleted_by"} <= _cols(con, "users")


def _assert_v73_data(con):
    tasks = con.execute("select seed_key, title, day_of_month, due_mode, interval_months, is_active from recurring_tasks order by id").fetchall()
    assert tasks == [("watchman_salary", "Watchman salary payment", 5, "by", 1, 1),
                     ("common_eb", "Common EB (electricity) payment", 10, "before", 1, 1)]
    hist = con.execute("select is_initial, new_upi_vpa, new_upi_mobile, old_upi_vpa from payment_detail_changes").fetchall()
    assert hist == [(1, "9940255076@ybl", "9940255076", None)]


def test_upgrade_from_scratch_is_idempotent_and_downgrade_round_trips(tmp_path):
    _alembic(tmp_path, "upgrade", "head")
    con = _con(tmp_path)
    assert _version(con) == HEAD
    _assert_v73_schema(con)
    _assert_soft_delete_schema(con)
    _assert_v73_data(con)
    uniq = con.execute("select sql from sqlite_master where name = 'recurring_task_occurrences'").fetchone()[0]
    assert "UNIQUE" in uniq.upper()
    con.close()

    _alembic(tmp_path, "upgrade", "head")  # already at head: nothing to do
    _alembic(tmp_path, "downgrade", PREV)
    con = _con(tmp_path)
    assert _version(con) == PREV
    assert not (NEW_TABLES & _tables(con))
    for t in PAYEE_TABLES:
        assert not (PAYEE_COLS & _cols(con, t)), t
    assert "opening_carried" not in _cols(con, "ledger_periods")
    assert not ({"deleted_at", "deleted_by"} & _cols(con, "users"))
    assert con.execute("select count(*) from app_settings where key in ('pay_upi_vpa','pay_upi_mobile','pay_payee_name','pay_payee_backfill_done')").fetchone()[0] == 0
    con.close()

    _alembic(tmp_path, "upgrade", "head")
    con = _con(tmp_path)
    assert _version(con) == HEAD
    _assert_v73_schema(con)
    _assert_v73_data(con)
    con.close()


def test_upgrade_when_the_startup_safety_net_already_created_everything(tmp_path):
    """create_all()/add_missing_columns() run at app import; a later `alembic upgrade head` must still succeed."""
    env = _env(tmp_path)
    boot = subprocess.run([sys.executable, "-c", "import app.main"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert boot.returncode == 0, boot.stderr[-1500:]
    con = _con(tmp_path)
    _assert_v73_schema(con)
    con.close()
    _alembic(tmp_path, "stamp", PREV)
    _alembic(tmp_path, "upgrade", "head")
    con = _con(tmp_path)
    assert _version(con) == HEAD
    _assert_v73_schema(con)
    _assert_v73_data(con)
    assert con.execute("select count(*) from recurring_tasks").fetchone()[0] == 2
    assert con.execute("select count(*) from payment_detail_changes").fetchone()[0] == 1
    con.close()
    _alembic(tmp_path, "upgrade", "head")
    con = _con(tmp_path)
    assert con.execute("select count(*) from recurring_tasks").fetchone()[0] == 2
    assert con.execute("select count(*) from payment_detail_changes").fetchone()[0] == 1


def test_upgrade_over_v72_data_backfills_payee_and_alters_nothing_else(tmp_path):
    _alembic(tmp_path, "upgrade", PREV)
    con = _con(tmp_path)
    con.execute("insert into flats (id, number) values (1, '101'), (2, '201')")
    con.execute("insert into ledger_periods (id, year, month, opening_fund_balance, closing_fund_balance, corpus_opening, corpus_closing, is_closed) "
                "values (1, 2026, 9, 0, 3000, 0, 0, 1), (2, 2026, 10, 3000, null, 0, null, 0)")
    con.execute("insert into maintenance_payments (id, flat_id, period_id, amount, is_paid, paid_on, payment_note, payment_mode) values "
                "(1, 1, 1, 1500, 1, '2026-09-03', 'UTR111111111111', 'UPI'), (2, 2, 1, 1500, 1, '2026-09-04', null, 'Cash'), "
                "(3, 1, 2, 1500, 0, null, null, null), (4, 2, 2, 1500, 1, '2026-10-02', 'bank', 'Bank')")
    con.execute("insert into corpus_payments (id, flat_id, amount, is_paid, paid_on, description, payment_mode) values "
                "(1, 1, 500, 1, '2026-10-01', 'Lift', 'UPI'), (2, 2, 500, 0, null, 'Lift', null)")
    con.execute("insert into submission_rejections (id, record_type, record_id, flat_id, amount, paid_on, reason, rejected_at, rejected_by, summary) "
                "values (1, 'maintenance_payment', 77, 1, 1500, '2026-10-02', 'No credit', '2026-10-03 10:00:00', 'admin', 'Maintenance')")
    con.commit()

    def other_columns(c):
        out = {}
        for t in PAYEE_TABLES + ("flats", "ledger_periods"):
            names = [n for n in _cols(c, t) if n not in PAYEE_COLS and n != "opening_carried"]
            rows = c.execute(f"select {', '.join(sorted(names))} from {t} order by id").fetchall() if names else []
            out[t] = (sorted(names), rows)
        return out

    before = other_columns(con)
    con.close()
    _alembic(tmp_path, "upgrade", "head")
    con = _con(tmp_path)
    assert _version(con) == HEAD
    assert other_columns(con) == before  # only the new columns were added; nothing else moved
    _assert_v73_schema(con)
    initial_id = con.execute("select id from payment_detail_changes where is_initial = 1").fetchone()[0]
    stamped = lambda t, where="1=1": con.execute(  # noqa: E731
        f"select count(*) from {t} where {where} and payee_vpa = '9940255076@ybl' and payee_mobile = '9940255076' "
        f"and payee_name is not null and payee_settings_id = {initial_id}").fetchone()[0]
    assert stamped("maintenance_payments", "is_paid = 1") == 3
    assert con.execute("select count(*) from maintenance_payments where is_paid = 0 and payee_vpa is not null").fetchone()[0] == 0
    assert stamped("corpus_payments", "is_paid = 1") == 1
    assert con.execute("select count(*) from corpus_payments where is_paid = 0 and payee_vpa is not null").fetchone()[0] == 0
    assert stamped("submission_rejections") == 1
    assert con.execute("select count(*) from ledger_periods where opening_carried is not null and opening_carried != 0").fetchone()[0] == 0
    first = con.execute("select fund_balance, total_balance from payment_detail_changes").fetchone()
    assert first[0] is not None and first[1] is not None
    con.close()

    _alembic(tmp_path, "downgrade", PREV)
    con = _con(tmp_path)
    assert other_columns(con) == before  # the round trip leaves the original data exactly as it was
    assert con.execute("select count(*) from maintenance_payments").fetchone()[0] == 4


def test_soft_delete_revision_up_down_up_on_a_v73_shaped_copy(tmp_path):
    """A database already at a3d9e6c1b852 (what live runs today) gains only the two nullable user columns."""
    _alembic(tmp_path, "upgrade", V73)
    con = _con(tmp_path)
    assert _version(con) == V73 and not ({"deleted_at", "deleted_by"} & _cols(con, "users"))
    con.execute("insert into users (id, username, password_hash, role, is_active, must_change_password, created_at) values "
                "(1, 'admin', 'x', 'admin', 1, 0, '2026-10-01 00:00:00'), (2, 'owner.one', 'y', 'owner', 0, 1, '2026-10-02 00:00:00')")
    con.execute("insert into flats (id, number) values (1, '101')")
    con.execute("insert into user_flats (user_id, flat_id) values (2, 1)")
    con.commit()
    before = con.execute("select id, username, password_hash, role, is_active, must_change_password, created_at from users order by id").fetchall()
    links = con.execute("select * from user_flats").fetchall()
    con.close()

    _alembic(tmp_path, "upgrade", "head")
    _alembic(tmp_path, "upgrade", "head")  # idempotent
    con = _con(tmp_path)
    assert _version(con) == HEAD
    _assert_soft_delete_schema(con)
    assert con.execute("select id, username, password_hash, role, is_active, must_change_password, created_at from users order by id").fetchall() == before
    assert con.execute("select deleted_at, deleted_by from users").fetchall() == [(None, None), (None, None)]
    assert con.execute("select * from user_flats").fetchall() == links
    con.execute("update users set deleted_at = '2026-10-19 10:00:00', deleted_by = 'admin' where id = 2")
    con.commit()
    con.close()

    _alembic(tmp_path, "downgrade", V73)
    con = _con(tmp_path)
    assert _version(con) == V73 and not ({"deleted_at", "deleted_by"} & _cols(con, "users"))
    assert con.execute("select id, username, is_active from users order by id").fetchall() == [(1, "admin", 1), (2, "owner.one", 0)]
    assert con.execute("select * from user_flats").fetchall() == links  # a deleted user stays disabled and linked
    con.close()
    _alembic(tmp_path, "upgrade", "head")
    con = _con(tmp_path)
    assert _version(con) == HEAD and con.execute("select count(*) from users").fetchone()[0] == 2


def test_soft_delete_columns_already_added_by_the_startup_safety_net(tmp_path):
    env = _env(tmp_path)
    boot = subprocess.run([sys.executable, "-c", "import app.main"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert boot.returncode == 0, boot.stderr[-1500:]
    con = _con(tmp_path)
    _assert_soft_delete_schema(con)
    con.close()
    _alembic(tmp_path, "stamp", V73)
    _alembic(tmp_path, "upgrade", "head")
    con = _con(tmp_path)
    assert _version(con) == HEAD
