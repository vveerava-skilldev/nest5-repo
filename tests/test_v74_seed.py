"""v7.4: scripts/seed.py ships no sample expenses or settlements; demo data is optional (--sample)."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(tmp_path, *args, name="s.db", env_extra=None):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / name}", "INSTANCE_DIR": str(tmp_path / "i"),
           "BACKUP_DIR": str(tmp_path / "b")}
    env.pop("SEED_SAMPLE_DATA", None)
    env.update(env_extra or {})
    ran = subprocess.run([sys.executable, "scripts/seed.py", *args], cwd=ROOT, env=env, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr
    return ran.stdout, sqlite3.connect(tmp_path / name)


def _count(con, table):
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_default_seed_has_no_ledger_data_at_all(tmp_path):
    out, con = _run(tmp_path)
    assert "no sample ledger data" in out
    assert _count(con, "flats") == 5 and _count(con, "assets") >= 1
    for table in ("expenses", "settlements", "ledger_periods", "maintenance_payments", "complaints"):
        assert _count(con, table) == 0, table
    assert con.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0] >= 0


def test_sample_seed_is_opt_in_and_has_no_expenses_or_settlements(tmp_path):
    for i, (args, extra) in enumerate([(("--sample",), {}), ((), {"SEED_SAMPLE_DATA": "1"})]):
        out, con = _run(tmp_path, *args, name=f"s{i}.db", env_extra=extra)
        assert "sample ledger" in out and "₹20000" in out
        assert _count(con, "expenses") == 0 and _count(con, "settlements") == 0
        assert _count(con, "ledger_periods") == 2 and _count(con, "maintenance_payments") == 10
        closing = con.execute("SELECT closing_fund_balance, is_closed FROM ledger_periods WHERE month = 9").fetchone()
        assert closing == (10000, 1)
        assert con.execute("SELECT opening_fund_balance FROM ledger_periods WHERE month = 10").fetchone()[0] == 10000


def test_seed_text_has_no_sample_expense_names_or_notes():
    text = (ROOT / "scripts" / "seed.py").read_text()
    for needle in ("Common expense", "Reimbursed from maintenance fund", "Watchman salary", "Flat 101 advance", "Settlement("):
        assert needle not in text, needle


def test_seeding_again_never_touches_existing_data(tmp_path):
    _run(tmp_path, "--sample")
    con = sqlite3.connect(tmp_path / "s.db")
    con.execute("INSERT INTO expenses (description, category, amount, expense_date, is_settled, status) VALUES ('Mine','Common',10,'2026-10-02',0,'approved')")
    con.commit()
    con.close()
    out, con = _run(tmp_path, "--sample")
    assert "already has data" in out and _count(con, "expenses") == 1 and _count(con, "ledger_periods") == 2
    out, con = _run(tmp_path)
    assert "already has data" in out and _count(con, "expenses") == 1
