"""
Backs up db.sqlite3 to a timestamped file under db_backups/ (deleting ones
older than --keep-days), and mirrors uploaded files (MEDIA_ROOT: customer POs,
WhatsApp drawings) into db_backups/media/.

Schedule this — it does nothing by itself:
  cron (Mac/Linux):   0 2 * * *  cd /path/to/inventory && python3 manage.py backup_db
  Task Scheduler (Windows): run `python manage.py backup_db` daily.
"""
import shutil
import sqlite3
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
        # Online backup API, not a file copy: the DB runs in WAL mode, so recent
        # commits can live only in the -wal file and a raw copy would miss them.
        source = sqlite3.connect(db_path)
        dest = sqlite3.connect(backup_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        self.stdout.write(self.style.SUCCESS(f"Backed up to {backup_path}"))

        copied, total = self._mirror_media(backup_dir / 'media')
        if total:
            self.stdout.write(f"Media: {copied} new/changed file(s) copied, {total} total in the live folder.")

        cutoff = datetime.now() - timedelta(days=options['keep_days'])
        removed = 0
        for old_backup in backup_dir.glob('db_*.sqlite3'):
            if datetime.fromtimestamp(old_backup.stat().st_mtime) < cutoff:
                old_backup.unlink()
                removed += 1
        if removed:
            self.stdout.write(f"Removed {removed} backup(s) older than {options['keep_days']} days.")

    def _mirror_media(self, mirror_dir):
        """Incrementally copies MEDIA_ROOT into mirror_dir. Never deletes from
        the mirror: a file removed from the live folder by mistake should still
        be recoverable, and unlike DB snapshots these aren't pruned by age."""
        media_root = Path(settings.MEDIA_ROOT)
        if not media_root.is_dir():
            return 0, 0
        copied = total = 0
        for source in media_root.rglob('*'):
            if not source.is_file():
                continue
            total += 1
            target = mirror_dir / source.relative_to(media_root)
            if target.exists() and target.stat().st_size == source.stat().st_size \
                    and target.stat().st_mtime >= source.stat().st_mtime:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied += 1
        return copied, total
