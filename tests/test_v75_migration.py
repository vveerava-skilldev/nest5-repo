"""v7.5 Alembic migration d6a3f9b1c428 (expenses.payment_source): from scratch, up/down/up, after the startup
safety net, and over a v7.4-shaped copy of live-like data (T18, T20)."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEAD, PREV = "d6a3f9b1c428", "b4e7c2a9d531"


def _env(tmp_path):
    return {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'm.db'}", "INSTANCE_DIR": str(tmp_path / "i"),
            "BACKUP_DIR": str(tmp_path / "b")}


def _alembic(tmp_path, *args):
    ran = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=_env(tmp_path), capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr[-2500:]
    return ran


def _con(tmp_path):
    return sqlite3.connect(tmp_path / "m.db")


def _cols(con, table="expenses"):
    return [r[1] for r in con.execute(f"pragma table_info({table})")]


def _version(con):
    return con.execute("select version_num from alembic_version").fetchone()[0]


def _app_numbers(tmp_path):
    """Account Balance, owed to owners and the expense split, computed by the application code itself."""
    code = (
        "import json\n"
        "from sqlalchemy import select\n"
        "from app.database import SessionLocal\n"
        "from app.services import ledger as L, balances as B\n"
        "db = SessionLocal()\n"
        "ov = B.overview(db)\n"
        "print('JSON=' + json.dumps({'bal': L.compute_fund_balance(db), 'owed': ov['owed'], 'total': L.expense_total(db),"
        " 'settled': L.expense_total(db, settled_only=True), 'by_flat': [[r['flat'].number, r['total']] for r in ov['by_flat']]}))\n"
    )
    ran = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=_env(tmp_path), capture_output=True, text=True)
    assert "JSON=" in ran.stdout, ran.stderr[-2500:]
    return json.loads(ran.stdout.split("JSON=", 1)[1])


def test_scratch_and_up_down_up_roundtrip(tmp_path):
    _alembic(tmp_path, "upgrade", "head")
    con = _con(tmp_path)
    assert _version(con) == HEAD and "payment_source" in _cols(con)
    before_cols = [c for c in _cols(con) if c != "payment_source"]
    con.close()

    _alembic(tmp_path, "upgrade", "head")  # already at head: nothing to do
    _alembic(tmp_path, "downgrade", PREV)
    con = _con(tmp_path)
    assert _version(con) == PREV and "payment_source" not in _cols(con)
    assert _cols(con) == before_cols
    con.close()

    _alembic(tmp_path, "upgrade", "head")
    con = _con(tmp_path)
    assert _version(con) == HEAD and "payment_source" in _cols(con)


def test_upgrade_when_the_startup_safety_net_already_created_the_column(tmp_path):
    """create_all()/add_missing_columns() run at app import; a later `alembic upgrade head` must still succeed."""
    boot = subprocess.run([sys.executable, "-c", "import app.main"], cwd=ROOT, env=_env(tmp_path), capture_output=True, text=True)
    assert boot.returncode == 0, boot.stderr[-1500:]
    con = _con(tmp_path)
    assert "payment_source" in _cols(con)
    con.close()
    _alembic(tmp_path, "stamp", PREV)
    _alembic(tmp_path, "upgrade", "head")
    con = _con(tmp_path)
    assert _version(con) == HEAD and _cols(con).count("payment_source") == 1
    con.close()
    _alembic(tmp_path, "upgrade", "head")
    _alembic(tmp_path, "downgrade", PREV)
    con = _con(tmp_path)
    assert "payment_source" not in _cols(con)


def test_over_a_v74_shaped_copy_of_live_like_data(tmp_path):
    _alembic(tmp_path, "upgrade", PREV)
    con = _con(tmp_path)
    con.execute("insert into flats (id, number) values (1,'101'),(2,'201'),(3,'301'),(4,'401'),(5,'501')")
    con.execute("insert into ledger_periods (id, year, month, opening_fund_balance, closing_fund_balance, corpus_opening, corpus_closing, is_closed) "
                "values (1, 2026, 9, 0, 4000, 0, 0, 1), (2, 2026, 10, 4000, null, 0, null, 0)")
    con.execute("insert into maintenance_payments (flat_id, period_id, amount, is_paid, paid_on, payment_mode) values "
                "(1, 1, 10000, 1, '2026-09-02', 'UPI'), (1, 2, 10000, 1, '2026-10-02', 'UPI')")
    expenses = [
        (1, "Watchman salary", "Salaries", 2000, "2026-09-05", 1, 1, 1),
        (2, "Common EB", "Utilities", 1500, "2026-10-10", 3, 2, 1),
        (3, "Lift repair", "Repairs", 1799, "2026-10-12", 1, 2, 1),
        (4, "Pump fuse", "Repairs", 900, "2026-10-14", 4, 2, 1),
        (5, "Water tank cleaning", "Common", 4000, "2026-09-20", None, 1, 1),
        (6, "Garden", "Common", 1000, "2026-10-15", 2, 2, 1),
        (7, "Notice boards", "Common", 545, "2026-10-19", 2, 2, 1),
        (8, "Tap washers", "Repairs", 200, "2026-10-18", 3, 2, 0),
    ]
    for eid, desc, cat, amt, day, flat, per, settled in expenses:
        con.execute("insert into expenses (id, description, category, amount, expense_date, paid_by_flat_id, period_id, is_settled, status) "
                    "values (?,?,?,?,?,?,?,?,'approved')", (eid, desc, cat, amt, day, flat, per, settled))
        if settled:
            con.execute("insert into settlements (expense_id, fund_type, amount, settled_on, note) values (?, 'MAINTENANCE', ?, ?, ?)",
                        (eid, amt, day, "Paid in cash" if eid == 5 else None))
    con.commit()
    assert con.execute("select count(*), sum(amount) from expenses").fetchone() == (8, 11944)
    assert con.execute("select sum(amount) from settlements").fetchone()[0] == 11744
    con.close()

    base = _app_numbers(tmp_path)
    assert base["total"] == 11944 and base["settled"] == 11744 and base["owed"] == 200
    assert base["bal"] == 20000 - 11744 == 8256

    con = _con(tmp_path)
    old_cols = _cols(con)
    snapshot = lambda c: {t: c.execute(f"select {', '.join(old_cols if t == 'expenses' else ['*'])} from {t} order by id").fetchall()  # noqa: E731
                          for t in ("expenses", "settlements", "ledger_periods", "maintenance_payments", "flats")}
    before = snapshot(con)
    con.close()

    _alembic(tmp_path, "upgrade", "head")
    _alembic(tmp_path, "upgrade", "head")
    con = _con(tmp_path)
    assert _version(con) == HEAD
    assert snapshot(con) == before  # every pre-existing column of every table is untouched
    sources = dict(con.execute("select id, payment_source from expenses order by id").fetchall())
    assert sources[5] is None  # the one with no flat is left for the admin to confirm
    assert all(v == "flat" for k, v in sources.items() if k != 5)
    con.close()
    after = _app_numbers(tmp_path)
    assert after == base

    _alembic(tmp_path, "downgrade", PREV)
    con = _con(tmp_path)
    assert _version(con) == PREV and "payment_source" not in _cols(con)
    assert snapshot(con) == before
    con.close()
    assert _app_numbers(tmp_path) == base
