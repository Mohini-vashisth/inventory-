"""Management commands: import_excel, backfill_options, backup_db (and its media mirror)."""

import pandas as pd
import tempfile
from io import StringIO

from decimal import Decimal
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from pathlib import Path
from unittest.mock import patch

from ..models import GradeOption, Material, SizeOption


class ImportExcelTests(TestCase):
    """Uses a small synthetic spreadsheet rather than the real (gitignored) one,
    so this runs the same in CI as it does locally."""

    def _write_sheet(self, rows, extra_columns=None):
        columns = [
            'SR. NO.', 'COIL NO.', 'DATE', 'GRADE', 'SIZE', 'COMPANY', 'VENDOR',
            'QTY (KGS)', 'HEAT NO.',
        ]
        if extra_columns:
            columns += extra_columns
        df = pd.DataFrame(rows, columns=columns)
        tmp = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
        df.to_excel(tmp.name, index=False)
        return tmp.name

    def test_imports_real_rows_and_skips_blank_ones(self):
        path = self._write_sheet([
            [1, 'WR0001', '2024-01-15', 'SAE 1008', 6, 'VSP', 'ADITYA STEEL', 1250.0, 'H001'],
            [2, None, None, None, None, None, None, None, None],  # blank template row
            # unit-suffixed size, oversized heat_no (9 chars, model max is 8)
            [3, 'WR0003', '2024-02-01', 'EN8D', '16.3 MM', 'TATA', 'XYZ TRADERS', 500.0, 'B30855015'],
        ])
        call_command('import_excel', f'--file={path}', '--yes')

        self.assertEqual(Material.objects.count(), 2)
        first = Material.objects.order_by('coil_no').first()
        self.assertEqual(first.grade, 'SAE 1008')
        self.assertEqual(first.quantity, 1250)

        second = Material.objects.order_by('coil_no').last()
        self.assertEqual(second.size, Decimal('16.3'))  # ' MM' suffix stripped
        self.assertEqual(second.heat_no, 'B3085501')  # truncated to 8 chars

    def test_malformed_date_does_not_crash_the_whole_import(self):
        """Real data had a typo like '11/058/2023' (no such day) that used to
        crash the entire import partway through, leaving a partial DB state."""
        path = self._write_sheet([
            [1, 'WR0001', '11/058/2023', 'SAE 1008', 6, 'VSP', 'ADITYA STEEL', 1250.0, 'H001'],
            [2, 'WR0002', '2024-02-01', 'EN8D', 6, 'TATA', 'XYZ TRADERS', 500.0, 'H002'],
        ])
        call_command('import_excel', f'--file={path}', '--yes')

        self.assertEqual(Material.objects.count(), 2)
        bad_row = Material.objects.get(heat_no='H001')
        self.assertIsNone(bad_row.date)
        good_row = Material.objects.get(heat_no='H002')
        self.assertIsNotNone(good_row.date)

    def test_crash_partway_through_leaves_no_partial_data(self):
        """The whole delete+import runs in one transaction — a failure partway
        through must roll back completely, not leave some rows imported."""
        Material.objects.create(grade='EXISTING', quantity=1)
        path = self._write_sheet([
            [1, 'WR0001', '2024-01-15', 'SAE 1008', 6, 'VSP', 'ADITYA STEEL', 1250.0, 'H001'],
        ])
        with patch(
            'materials.management.commands.import_excel.Command._clean_decimal',
            side_effect=RuntimeError('simulated failure'),
        ):
            with self.assertRaises(RuntimeError):
                call_command('import_excel', f'--file={path}', '--yes')

        # Rolled back to exactly the pre-import state — the old row is still there.
        self.assertEqual(Material.objects.count(), 1)
        self.assertEqual(Material.objects.first().grade, 'EXISTING')

    def test_dry_run_touches_nothing(self):
        path = self._write_sheet([
            [1, 'WR0001', '2024-01-15', 'SAE 1008', 6, 'VSP', 'ADITYA STEEL', 1250.0, 'H001'],
        ])
        call_command('import_excel', f'--file={path}', '--dry-run')
        self.assertEqual(Material.objects.count(), 0)

    def test_prompts_before_deleting_existing_rows(self):
        Material.objects.create(grade='OLD', quantity=1)
        path = self._write_sheet([
            [1, 'WR0001', '2024-01-15', 'SAE 1008', 6, 'VSP', 'ADITYA STEEL', 1250.0, 'H001'],
        ])
        # Simulate answering "no" at the confirmation prompt.
        with patch('builtins.input', return_value='n'):
            call_command('import_excel', f'--file={path}')
        self.assertEqual(Material.objects.count(), 1)
        self.assertEqual(Material.objects.first().grade, 'OLD')

    def test_issued_qty_columns_become_legacy_used_weight(self):
        """ISSUED QTY 1/2/3 track weight already used before the app existed —
        summed into legacy_used_weight so status reflects real-world usage."""
        path = self._write_sheet(
            [
                [1, 'WR0001', '2024-01-15', 'SAE 1008', 6, 'VSP', 'ADITYA STEEL',
                 1250.0, 'H001', 500.0, 250.0, None],
                [2, 'WR0002', '2024-02-01', 'EN8D', 6, 'TATA', 'XYZ TRADERS',
                 500.0, 'H002', None, None, None],
            ],
            extra_columns=['ISSUED QTY 1', 'ISSUED QTY 2', 'ISSUED QTY 3'],
        )
        call_command('import_excel', f'--file={path}', '--yes')

        used = Material.objects.get(heat_no='H001')
        self.assertEqual(used.legacy_used_weight, Decimal('750'))
        self.assertEqual(used.weight_used(), Decimal('750'))

        untouched = Material.objects.get(heat_no='H002')
        self.assertEqual(untouched.legacy_used_weight, Decimal('0'))

    def test_reset_sequence_renumbers_from_one(self):
        """Without --reset-sequence, coil_no keeps counting up from wherever
        deleted rows left off (SQLite doesn't rewind AUTOINCREMENT on delete).
        With it, the next imported coil starts at 1."""
        old = Material.objects.create(grade='OLD', quantity=1)
        old.delete()  # pushes SQLite's autoincrement counter past 1
        path = self._write_sheet([
            [1, 'WR0001', '2024-01-15', 'SAE 1008', 6, 'VSP', 'ADITYA STEEL', 1250.0, 'H001'],
        ])
        call_command('import_excel', f'--file={path}', '--yes', '--reset-sequence')
        self.assertEqual(Material.objects.get().coil_no, 1)

    def _write_multi_sheet(self):
        tmp = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
        with pd.ExcelWriter(tmp.name) as writer:
            pd.DataFrame({'Notes': ['not real data']}).to_excel(writer, sheet_name='Cover Page', index=False)
            pd.DataFrame({
                'SR. NO.': [1], 'COIL NO.': ['WR0001'], 'DATE': ['2024-01-15'],
                'GRADE': ['SAE 1008'], 'SIZE': [6], 'COMPANY': ['VSP'],
                'VENDOR': ['ADITYA STEEL'], 'QTY (KGS)': [1250.0], 'HEAT NO.': ['H001'],
            }).to_excel(writer, sheet_name='Stock 2024', index=False)
        return tmp.name

    def test_list_sheets_imports_nothing(self):
        path = self._write_multi_sheet()
        call_command('import_excel', f'--file={path}', '--list-sheets')
        self.assertEqual(Material.objects.count(), 0)

    def test_wrong_default_sheet_is_rejected_clearly(self):
        path = self._write_multi_sheet()
        with self.assertRaises(CommandError):
            call_command('import_excel', f'--file={path}', '--dry-run')

    def test_can_target_sheet_by_name_or_index(self):
        path = self._write_multi_sheet()
        call_command('import_excel', f'--file={path}', '--sheet=Stock 2024', '--yes')
        self.assertEqual(Material.objects.count(), 1)

        Material.objects.all().delete()
        call_command('import_excel', f'--file={path}', '--sheet=1', '--yes')
        self.assertEqual(Material.objects.count(), 1)


class BackfillOptionsCommandTests(TestCase):
    """Backfills GradeOption/SizeOption from whatever distinct grade/size
    values already exist in Material, as-is — duplicate spellings included.
    A migration (0013) seeds a small baseline set of options into every
    fresh database, so assertions check for specific values rather than
    the table being empty beforehand."""

    def test_adds_new_grades_and_sizes_found_in_material(self):
        Material.objects.create(grade='EN-8D', size='6.500', quantity=500)
        Material.objects.create(grade='EN-8D', size='6.500', quantity=500)  # duplicate, not double-added
        Material.objects.create(grade='SAE 1008', size='9.000', quantity=500)

        call_command('backfill_options')

        self.assertEqual(GradeOption.objects.filter(name='EN-8D').count(), 1)
        self.assertEqual(GradeOption.objects.filter(name='SAE 1008').count(), 1)
        self.assertEqual(SizeOption.objects.filter(value=Decimal('6.500')).count(), 1)
        self.assertEqual(SizeOption.objects.filter(value=Decimal('9.000')).count(), 1)

    def test_does_not_duplicate_existing_options(self):
        Material.objects.create(grade='EN8D', size='1.200', quantity=500)  # already seeded by 0013

        call_command('backfill_options')

        self.assertEqual(GradeOption.objects.filter(name='EN8D').count(), 1)
        self.assertEqual(SizeOption.objects.filter(value=Decimal('1.200')).count(), 1)

    def test_blank_and_null_grades_are_ignored(self):
        Material.objects.create(grade=None, size=None, quantity=500)
        Material.objects.create(grade='', size='6.000', quantity=500)

        call_command('backfill_options')

        self.assertFalse(GradeOption.objects.filter(name='').exists())
        self.assertEqual(SizeOption.objects.filter(value=Decimal('6.000')).count(), 1)

    def test_dry_run_changes_nothing(self):
        Material.objects.create(grade='EN-8D', size='6.500', quantity=500)
        call_command('backfill_options', '--dry-run')
        self.assertFalse(GradeOption.objects.filter(name='EN-8D').exists())
        self.assertFalse(SizeOption.objects.filter(value=Decimal('6.500')).exists())


class BackupDbCommandTests(SimpleTestCase):
    def test_backup_includes_rows_still_sitting_in_the_wal_file(self):
        import sqlite3
        from io import StringIO
        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / 'db.sqlite3'
            live = sqlite3.connect(db_file, isolation_level=None)
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("PRAGMA wal_autocheckpoint=0")
            live.execute("CREATE TABLE t (name TEXT)")
            live.execute("INSERT INTO t VALUES ('only-in-wal')")
            try:
                databases = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': db_file}}
                with override_settings(DATABASES=databases, BASE_DIR=Path(tmp), MEDIA_ROOT=Path(tmp) / 'no-media-here'):
                    call_command('backup_db', stdout=StringIO())
                backups = list((Path(tmp) / 'db_backups').glob('db_*.sqlite3'))
                self.assertEqual(len(backups), 1)
                restored = sqlite3.connect(backups[0])
                try:
                    rows = [r[0] for r in restored.execute("SELECT name FROM t")]
                finally:
                    restored.close()
            finally:
                live.close()
        self.assertEqual(rows, ['only-in-wal'])


class BackupMediaMirrorTests(SimpleTestCase):
    def setUp(self):
        import sqlite3
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.media = root / 'media'
        self.db = root / 'db.sqlite3'
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
        conn.close()
        override = override_settings(
            DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': self.db}},
            BASE_DIR=root, MEDIA_ROOT=self.media,
        )
        override.enable()
        self.addCleanup(override.disable)
        self.mirror = root / 'db_backups' / 'media'

    def _backup(self):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command('backup_db', stdout=out)
        return out.getvalue()

    def _upload(self, relpath, data=b'x'):
        f = self.media / relpath
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
        return f

    def test_copies_uploads_preserving_folder_layout(self):
        self._upload('purchase_orders/2026/10/po.pdf', b'PO')
        self._upload('query_drawings/2026/10/d.png', b'IMG')
        self._backup()
        self.assertEqual((self.mirror / 'purchase_orders/2026/10/po.pdf').read_bytes(), b'PO')
        self.assertEqual((self.mirror / 'query_drawings/2026/10/d.png').read_bytes(), b'IMG')

    def test_second_run_only_copies_new_files(self):
        self._upload('a.pdf')
        self.assertIn("1 new/changed", self._backup())
        self._upload('b.pdf')
        self.assertIn("1 new/changed file(s) copied, 2 total", self._backup())

    def test_changed_file_is_recopied(self):
        f = self._upload('a.pdf', b'old')
        self._backup()
        f.write_bytes(b'newer-content')
        self._backup()
        self.assertEqual((self.mirror / 'a.pdf').read_bytes(), b'newer-content')

    def test_file_deleted_from_live_folder_stays_in_the_mirror(self):
        f = self._upload('a.pdf', b'keep me')
        self._backup()
        f.unlink()
        self._backup()
        self.assertEqual((self.mirror / 'a.pdf').read_bytes(), b'keep me')

    def test_no_media_folder_yet_is_not_an_error(self):
        output = self._backup()
        self.assertIn("Backed up to", output)
        self.assertFalse(self.mirror.exists())


class MergeGradesCommandTests(TestCase):
    def setUp(self):
        GradeOption.objects.all().delete()
        self.keep = GradeOption.objects.create(name='EN-9', number=11)
        GradeOption.objects.create(name='EN9', number=12)
        Material.objects.create(grade='EN9', size='6.500', quantity=500)
        Material.objects.create(grade='en9', size='6.500', quantity=500)
        Material.objects.create(grade='EN-9', size='6.500', quantity=500)

    def test_dry_run_changes_nothing(self):
        call_command('merge_grades', 'EN9=EN-9', stdout=StringIO())
        self.assertEqual(Material.objects.filter(grade='EN9').count(), 1)
        self.assertTrue(GradeOption.objects.filter(name='EN9').exists())

    def test_apply_rewrites_every_record_and_drops_the_variant_option(self):
        call_command('merge_grades', 'EN9=EN-9', '--apply', stdout=StringIO())
        self.assertEqual(Material.objects.filter(grade='EN-9').count(), 3)
        self.assertEqual(list(GradeOption.objects.values_list('name', 'number')), [('EN-9', 11)])

    def test_the_kept_option_is_created_if_missing(self):
        call_command('merge_grades', 'EN9=EN 9', '--apply', stdout=StringIO())
        self.assertTrue(GradeOption.objects.filter(name='EN 9').exists())
        self.assertEqual(Material.objects.filter(grade='EN 9').count(), 2)

    def test_a_malformed_pair_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command('merge_grades', 'EN9', stdout=StringIO())
