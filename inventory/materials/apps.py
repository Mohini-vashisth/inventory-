from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _enable_sqlite_wal_mode(sender, connection, **kwargs):
    """The default SQLite journal mode locks the whole database file for
    the duration of a write and blocks readers while it does. WAL mode lets
    readers proceed without waiting on an in-progress writer — the
    difference that actually matters once two plants and QA's extra
    per-step log rows are both writing concurrently, well before this app
    would ever need a heavier database engine.

    journal_mode is a persistent property of the database file itself (set
    once, it stays set), but re-asserting it on every new connection is
    cheap and guards against ever silently losing it — e.g. a fresh
    db.sqlite3 on a newly set up machine that nobody remembered to flip."""
    if connection.vendor != 'sqlite':
        return
    with connection.cursor() as cursor:
        cursor.execute('PRAGMA journal_mode=WAL;')


class MaterialsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'materials'

    def ready(self):
        connection_created.connect(_enable_sqlite_wal_mode)
