"""Admin data export, backups and JSON restore."""
import json
import secrets
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy import delete as sa_delete, select, update as sa_update
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.config import settings
from app.database import get_db
from app.models import LedgerPeriod, ProjectContribution, ProjectPayment
from app.routers.web import templates
from app.services import expense_categories as expense_cats_svc
from app.services import projects as projects_svc
from app.services import corpus_campaigns as campaigns_svc
from app.services import audit as audit_svc
from app.services import backup as backup_svc
from app.services import export as export_svc
from app.services import ledger as ledger_svc
from app.services.pdf_report import build_summary_pdf

router = APIRouter()

RESTORE_CONFIRM_WORD = "RESTORE"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _download(content: bytes, filename: str, media_type: str) -> Response:
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _pending_path(token: str) -> Path:
    if not token.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "Bad restore token")
    return Path(settings.instance_dir) / f"pending-restore-{token}.json"


def _page(request: Request, db: Session, status_code: int = 200, **extra):
    periods = db.scalars(select(LedgerPeriod).order_by(LedgerPeriod.year.desc(), LedgerPeriod.month.desc())).all()
    ctx = {
        "request": request,
        "periods": [(p.id, ledger_svc.month_label(p)) for p in periods],
        "counts": export_svc.row_counts(db),
        "backups": [
            (p.name, p.stat().st_size, datetime.fromtimestamp(p.stat().st_mtime))
            for p in backup_svc.list_backups()[:10]
        ],
        "is_sqlite": backup_svc.sqlite_path() is not None,
        "error": None,
        "message": None,
        "preview": None,
        "confirm_word": RESTORE_CONFIRM_WORD,
        **extra,
    }
    return templates.TemplateResponse(request, "admin_export.html", ctx, status_code=status_code)


@router.get("/admin/export", response_class=HTMLResponse)
def export_page(request: Request, db: Session = Depends(get_db)):
    return _page(request, db)


@router.get("/admin/export/excel")
def export_excel(db: Session = Depends(get_db)):
    return _download(
        export_svc.build_xlsx(db),
        f"nest5-ledger-{_stamp()}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/admin/export/csv")
def export_csv(db: Session = Depends(get_db)):
    return _download(export_svc.build_csv_zip(db), f"nest5-ledger-csv-{_stamp()}.zip", "application/zip")


@router.get("/admin/export/json")
def export_json(db: Session = Depends(get_db)):
    return _download(export_svc.dump_json_bytes(db), f"nest5-ledger-dump-{_stamp()}.json", "application/json")


@router.get("/admin/export/pdf")
def export_pdf(period_id: str = "", db: Session = Depends(get_db)):
    period = None
    if period_id and period_id != "all":
        period = db.get(LedgerPeriod, int(period_id))
        if not period:
            raise HTTPException(404)
    suffix = f"{period.year}-{period.month:02d}" if period else "all-time"
    return _download(build_summary_pdf(db, period), f"nest5-summary-{suffix}.pdf", "application/pdf")


@router.get("/admin/export/sqlite")
def export_sqlite():
    if backup_svc.sqlite_path() is None:
        raise HTTPException(400, "Database is not SQLite")
    tmp = Path(tempfile.mkdtemp()) / "nest5.db"
    backup_svc.copy_sqlite(tmp)
    return FileResponse(
        tmp,
        media_type="application/vnd.sqlite3",
        filename=f"nest5-{_stamp()}.db",
        background=BackgroundTask(lambda: (tmp.unlink(missing_ok=True), tmp.parent.rmdir())),
    )


@router.post("/admin/export/backup-now", response_class=HTMLResponse)
def backup_now(request: Request, db: Session = Depends(get_db)):
    dest = backup_svc.make_backup("manual")
    msg = f"Backup saved: {settings.backup_dir}/{dest.name}" if dest else "No SQLite file to back up."
    return _page(request, db, message=msg)


@router.post("/admin/export/restore", response_class=HTMLResponse)
async def restore_upload(request: Request, dump: UploadFile = File(...), db: Session = Depends(get_db)):
    raw = await dump.read()
    try:
        data = json.loads(raw.decode("utf-8"))
        dump_counts = export_svc.validate_dump(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _page(request, db, 400, error="That file is not valid JSON.")
    except export_svc.RestoreError as exc:
        return _page(request, db, 400, error=str(exc))
    token = secrets.token_urlsafe(16)
    path = _pending_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    current = export_svc.row_counts(db)
    preview = {
        "token": token,
        "filename": dump.filename,
        "exported_at": data.get("exported_at"),
        "schema_version": data.get("schema_version"),
        "current_schema": export_svc.schema_version(db),
        "rows": [(name, current.get(name, 0), dump_counts.get(name)) for name in current],
    }
    return _page(request, db, preview=preview)


@router.post("/admin/export/restore/confirm", response_class=HTMLResponse)
def restore_confirm(
    request: Request,
    token: str = Form(...),
    confirm_text: str = Form(""),
    db: Session = Depends(get_db),
):
    path = _pending_path(token)
    if not path.exists():
        return _page(request, db, 400, error="Restore session expired — upload the dump again.")
    if confirm_text.strip() != RESTORE_CONFIRM_WORD:
        path.unlink(missing_ok=True)
        return _page(request, db, 400, error=f"Restore cancelled: you must type {RESTORE_CONFIRM_WORD} to confirm.")
    data = json.loads(path.read_text(encoding="utf-8"))
    backup = backup_svc.make_backup("pre-restore")
    actor = getattr(request.state, "username", None)
    try:
        restored = export_svc.restore_dump(db, data)
        campaigns_svc.absorb_line_items(db)
        if "project_contributions" not in data["tables"]:
            # Dump from before part payments: rebuild them from the restored paid/unpaid shares.
            db.execute(sa_delete(ProjectContribution))
            db.execute(sa_update(ProjectPayment).values(paid_amount=None))
        projects_svc.backfill(db)
        expense_cats_svc.ensure_defaults(db)
        audit_svc.log_audit(
            db,
            entity_type="database",
            entity_id=None,
            action="restore",
            user_label=actor,
            after={
                "dump_exported_at": data.get("exported_at"),
                "rows": restored,
                "backup": backup.name if backup else None,
            },
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return _page(request, db, 400, error=f"Restore failed, nothing changed: {exc}")
    finally:
        path.unlink(missing_ok=True)
    where = f"{settings.backup_dir}/{backup.name}" if backup else "(no SQLite file)"
    return _page(
        request, db,
        message=f"Restore complete ({sum(restored.values())} rows). Previous data was backed up to {where}.",
    )
