"""Delete every asset task (and the maintenance-log entries that belong to a task); keep the assets.

This removes all overdue / due asset alerts (dashboard, Actions hub, Calendar, reminders).
It is the command-line twin of Asset schedule -> "Clear all asset tasks & alerts".

Usage (from the nest5-ledger folder, venv active):
    python scripts/clear_asset_alerts.py            # shows what would be deleted, changes nothing
    python scripts/clear_asset_alerts.py --yes      # backs up the database, then deletes

The database is the one in DATABASE_URL (default sqlite:///./nest5.db). SQLite databases are copied to
BACKUP_DIR (default backups/) before anything is deleted (other databases need --no-backup after your own backup); the run is written to the audit log as user "cli".
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.services import audit as audit_svc  # noqa: E402
from app.services import backup as backup_svc  # noqa: E402
from app.services import schedule as schedule_svc  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Clear all asset tasks and their alerts (assets are kept).")
    parser.add_argument("--yes", action="store_true", help="Do it (without this flag nothing is changed)")
    parser.add_argument("--no-backup", action="store_true",
                        help="Only for non-SQLite databases (Postgres): you made your own backup")
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        counts = schedule_svc.task_counts(db)
        print(f"Database: {engine.url.render_as_string(hide_password=True)}")
        print(f"Asset tasks: {counts['tasks']}   task maintenance-log entries: {counts['logs']}   (assets are kept)")
        if not args.yes:
            print("Dry run. Re-run with --yes to back up the database and delete these.")
            return 0
        if not counts["tasks"] and not counts["logs"]:
            print("Nothing to clear.")
            return 0
        backup = backup_svc.make_backup("pre-clear-asset-tasks")
        if backup:
            print(f"Backup: {backup}")
        elif not args.no_backup:
            print("No SQLite file to back up (not SQLite?). Make your own backup, then add --no-backup.")
            return 1
        removed = schedule_svc.clear_all_tasks(db)
        audit_svc.log_audit(
            db, entity_type="asset_task", entity_id=None, action="clear_all", user_label="cli",
            after={**removed, "backup": backup.name if backup else None},
        )
        db.commit()
        print(f"Cleared {removed['tasks']} task(s) and {removed['logs']} log entr{'y' if removed['logs'] == 1 else 'ies'}.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
