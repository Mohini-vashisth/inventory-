"""
Copies db.sqlite3 to a timestamped file under db_backups/, and deletes
backups older than --keep-days.

Schedule this — it does nothing by itself:
  cron (Mac/Linux):   0 2 * * *  cd /path/to/inventory && python3 manage.py backup_db
  Task Scheduler (Windows): run `python manage.py backup_db` daily.
"""
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Back up the SQLite database to db_backups/ with a timestamped filename."

    def add_arguments(self, parser):
        parser.add_argument(
            '--keep-days', type=int, default=30,
            help="Delete backups older than this many days (default: 30).",
        )

    def handle(self, *args, **options):
        db_path = Path(settings.DATABASES['default']['NAME'])
        if not db_path.exists():
            raise CommandError(f"Database file not found at {db_path}")

        backup_dir = settings.BASE_DIR / 'db_backups'
        backup_dir.mkdir(exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = backup_dir / f'db_{timestamp}.sqlite3'
        shutil.copy2(db_path, backup_path)
        self.stdout.write(self.style.SUCCESS(f"Backed up to {backup_path}"))

        cutoff = datetime.now() - timedelta(days=options['keep_days'])
        removed = 0
        for old_backup in backup_dir.glob('db_*.sqlite3'):
            if datetime.fromtimestamp(old_backup.stat().st_mtime) < cutoff:
                old_backup.unlink()
                removed += 1
        if removed:
            self.stdout.write(f"Removed {removed} backup(s) older than {options['keep_days']} days.")
