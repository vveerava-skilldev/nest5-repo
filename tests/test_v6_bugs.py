"""v6 bug fixes: admin Projects page, owner PDF export for every timeline option."""
from pathlib import Path

from sqlalchemy import select

from app.models import Flat, LedgerPeriod
from tests.conftest import OWNER_PASSWORD, make_client

ROOT = Path(__file__).resolve().parents[1]


def _flats(db):
    return {f.number: f.id for f in db.scalars(select(Flat)).all()}


def test_templates_are_sane_size():
    for path in (ROOT / "app" / "templates").glob("*.html"):
        assert path.stat().st_size < 40_000, f"{path.name} is {path.stat().st_size} bytes"


def test_projects_page_renders_once(admin_client, owner_client, seeded_db):
    admin_client.post("/projects", data={"name": "Render once project", "total_cost": "5000"})
    seeded_db.expire_all()
    from app.models import Project
    proj = seeded_db.scalar(select(Project).where(Project.name == "Render once project"))
    try:
        for client in (admin_client, owner_client):
            r = client.get("/projects")
            assert r.status_code == 200
            assert len(r.text) < 40_000
            assert r.text.count("<h2>Building projects</h2>") == 1
            assert r.text.count("<thead><tr><th>Name</th>") == 1
        admin = admin_client.get("/projects").text
        assert 'action="/projects"' in admin and "Create project" in admin
        assert 'action="/projects"' not in owner_client.get("/projects").text
    finally:
        admin_client.post(f"/projects/{proj.id}/delete", data={"confirm": "DELETE"})


def _timeline_values(db):
    periods = db.scalars(select(LedgerPeriod).order_by(LedgerPeriod.year, LedgerPeriod.month)).all()
    return [None, "", "all"] + [str(p.id) for p in periods]


def _get_pdf(client, url, value):
    full = url if value is None else f"{url}?period_id={value}"
    r = client.get(full)
    assert r.status_code == 200, (full, r.status_code, r.text[:200])
    assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF"), full
    return r


def test_pdf_every_timeline_single_flat(seeded_db, owner_client):
    flats = _flats(seeded_db)
    for value in _timeline_values(seeded_db):
        r = _get_pdf(owner_client, f"/flats/{flats['201']}/statement.pdf", value)
        scope = "all-time" if not value or value == "all" else ""
        assert scope in r.headers["content-disposition"]
        _get_pdf(owner_client, "/owner/statement.pdf", value)
    assert owner_client.get(f"/flats/{flats['201']}/statement.pdf?period_id=abc").status_code == 400
    assert owner_client.get(f"/flats/{flats['201']}/statement.pdf?period_id=99999").status_code == 404


def test_pdf_every_timeline_multiple_flats(seeded_db, set_password, admin_client):
    flats = _flats(seeded_db)
    set_password(seeded_db, "manikyamba", OWNER_PASSWORD)
    multi = make_client("manikyamba", OWNER_PASSWORD)
    for value in _timeline_values(seeded_db):
        for number in ("101", "401"):
            _get_pdf(multi, f"/flats/{flats[number]}/statement.pdf", value)
        r = _get_pdf(multi, "/owner/statement.pdf", value)
        assert "flats-101-401-" in r.headers["content-disposition"]
        _get_pdf(admin_client, "/owner/statement.pdf", value)
    assert multi.get(f"/flats/{flats['201']}/statement.pdf?period_id=").status_code == 403
    page = multi.get(f"/flats/{flats['101']}").text
    assert '<option value="">All time</option>' in page and 'formaction="/owner/statement.pdf"' in page
