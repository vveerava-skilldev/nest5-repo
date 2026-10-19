import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.routers import accounts, admin_settings, admin_tools, calendar, data_export, owner, payee_report, reports, simple_dashboard, web
from app.services import backup as backup_svc
from app.services.access import access_guard
from app.services.corpus_campaigns import absorb_line_items
from app.services import expense_categories as expense_cats_svc
from app.services import expense_review as expense_review_svc
from app.services import monthly as monthly_svc
from app.services import payment_details as payment_details_svc
from app.services import projects as projects_svc
from app.services.flats import sync_flat_roster
from app.services.schema import add_missing_columns
from app.services.users import ensure_bootstrap_users

Base.metadata.create_all(bind=engine)
add_missing_columns(engine)

log = logging.getLogger("nest5")


def _sync_roster() -> None:
    db = SessionLocal()
    try:
        sync_flat_roster(db)
        db.commit()
        ensure_bootstrap_users(db)
        db.commit()
        if absorb_line_items(db):
            db.commit()
        changed = projects_svc.backfill(db)
        changed += expense_cats_svc.ensure_defaults(db)
        changed += expense_review_svc.repair_rejected_expenses(db)
        payment_details_svc.ensure_initial(db)
        payment_details_svc.backfill_payees(db)
        if changed:
            db.commit()
    finally:
        db.close()


def _startup_backup() -> None:
    try:
        dest = backup_svc.make_backup()
        if dest:
            print(f"[nest5] Safety backup: {dest}", flush=True)
    except Exception as exc:  # noqa: BLE001 — a failed backup must not stop the app
        log.warning("Startup backup failed: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    on_startup()
    job = None
    if settings.monthly_job_enabled:
        # Single uvicorn worker: one in-process loop creates the month's dues and recurring tasks.
        job = asyncio.create_task(monthly_svc.job_loop())
    try:
        yield
    finally:
        if job is not None:
            job.cancel()
            try:
                await job
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Nest5 Maintenance Ledger", dependencies=[Depends(access_guard)], lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)


@app.exception_handler(StarletteHTTPException)
async def forbidden_page(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 403:
        return web.templates.TemplateResponse(
            request, "forbidden.html", {"request": request, "detail": exc.detail}, status_code=403
        )
    return await http_exception_handler(request, exc)


def on_startup() -> None:
    _startup_backup()
    _sync_roster()

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.include_router(web.router)
app.include_router(accounts.router)
app.include_router(data_export.router)
app.include_router(owner.router)
app.include_router(admin_tools.router)
app.include_router(admin_settings.router)
app.include_router(payee_report.router)
app.include_router(reports.router)
app.include_router(simple_dashboard.router)
app.include_router(calendar.router)
