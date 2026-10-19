"""SQLite file backups (startup safety copies, pre-restore copies, raw download)."""
import sqlite3
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.database import engine


def sqlite_path() -> Path | None:
    if engine.url.get_backend_name() != "sqlite":
        return None
    db = engine.url.database
    if not db or db == ":memory:":
        return None
    return Path(db)


def copy_sqlite(dest: Path) -> Path:
    """Consistent copy via the SQLite backup API (safe while the app holds connections)."""
    src_path = sqlite_path()
    if src_path is None or not src_path.exists():
        raise FileNotFoundError("No SQLite database file to back up")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(src_path))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def prune_backups(keep: int | None = None) -> None:
    keep = settings.backup_keep if keep is None else keep
    files = sorted(Path(settings.backup_dir).glob("nest5-*.db"))
    for old in files[: max(len(files) - keep, 0)]:
        old.unlink(missing_ok=True)


def make_backup(label: str = "") -> Path | None:
    """Copy the DB to backups/nest5-YYYYMMDD-HHMMSS[-label].db and keep the newest N."""
    src_path = sqlite_path()
    if src_path is None or not src_path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"nest5-{stamp}{'-' + label if label else ''}.db"
    dest = Path(settings.backup_dir) / name
    n = 1
    while dest.exists():
        dest = Path(settings.backup_dir) / f"nest5-{stamp}-{n}{'-' + label if label else ''}.db"
        n += 1
    copy_sqlite(dest)
    prune_backups()
    return dest


def list_backups() -> list[Path]:
    return sorted(Path(settings.backup_dir).glob("nest5-*.db"), reverse=True)
