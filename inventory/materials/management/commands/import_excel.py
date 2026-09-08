"""
Replace all Material rows with the contents of STOCK INVENTORY SHEET.xlsx.

The real sheet has 22 columns (issuance history, balances, etc.) and ~5000
rows, most of which are blank template rows. Only the columns that map to
Material fields are read; blank rows are skipped entirely.
"""
import re
from decimal import Decimal, InvalidOperation

import pandas as pd
from django.core.management.base import BaseCommand, CommandError
from materials.models import Material

# Spreadsheet column -> Material field, and that field's max length (None = no limit / not a CharField)
COLUMN_MAP = {
    'DATE': ('date', None),
    'GRADE': ('grade', 10),
    'SIZE': ('size', None),
    'COMPANY': ('company', 100),
    'VENDOR': ('vendor', 50),
    'QTY (KGS)': ('quantity', None),
    'HEAT NO.': ('heat_no', 8),
}


class Command(BaseCommand):
    help = "Replace all Material rows with the contents of STOCK INVENTORY SHEET.xlsx"

    def add_arguments(self, parser):
        parser.add_argument(
            '--yes', action='store_true',
            help="Skip the confirmation prompt before deleting existing Material rows.",
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Parse and report what would be imported without touching the database.",
        )
        parser.add_argument(
            '--file', default="STOCK INVENTORY SHEET.xlsx",
            help="Path to the spreadsheet (default: STOCK INVENTORY SHEET.xlsx in the current directory).",
        )

    def handle(self, *args, **options):
        file_path = options['file']
        try:
            df = pd.read_excel(file_path)
        except FileNotFoundError:
            raise CommandError(f"'{file_path}' not found.")

        df.columns = df.columns.str.strip()
        missing = [c for c in COLUMN_MAP if c not in df.columns]
        if missing:
            raise CommandError(f"Expected column(s) not found in the spreadsheet: {', '.join(missing)}")

        # Drop fully-blank template rows — keep only rows with at least one real value.
        sheet_cols = list(COLUMN_MAP.keys())
        df = df[df[sheet_cols].notna().any(axis=1)]

        if options['dry_run']:
            self.stdout.write(f"Would import {len(df)} rows from '{file_path}' (dry run — nothing was changed).")
            if len(df):
                preview = df[sheet_cols].head(5).to_string(index=False)
                self.stdout.write(f"\nFirst 5 rows:\n{preview}")
            return

        existing = Material.objects.count()
        if existing and not options['yes']:
            confirm = input(
                f"This will delete all {existing} existing Material rows before importing "
                f"{len(df)} rows from '{file_path}'. Continue? [y/N] "
            )
            if confirm.strip().lower() != 'y':
                self.stdout.write("Aborted.")
                return

        Material.objects.all().delete()

        created = 0
        for _, row in df.iterrows():
            Material.objects.create(
                date=self._clean_date(row.get('DATE')),
                grade=self._clean_str(row.get('GRADE'), 10),
                size=self._clean_size(row.get('SIZE')),
                company=self._clean_str(row.get('COMPANY'), 100),
                vendor=self._clean_str(row.get('VENDOR'), 50),
                quantity=self._clean_decimal(row.get('QTY (KGS)')),
                heat_no=self._clean_str(row.get('HEAT NO.'), 8),
            )
            created += 1

        self.stdout.write(self.style.SUCCESS(f"Imported {created} rows successfully."))

    # ── Cell cleaning ────────────────────────────────────────

    def _clean_str(self, value, max_len):
        if pd.isna(value):
            return None
        s = str(value).strip()
        return s[:max_len] if s else None

    def _clean_date(self, value):
        if pd.isna(value):
            return None
        return value.date() if hasattr(value, 'date') else value

    def _clean_decimal(self, value):
        if pd.isna(value):
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None

    def _clean_size(self, value):
        """SIZE is mostly numeric but a few rows have units, e.g. '16.3 MM'."""
        if pd.isna(value):
            return None
        digits = re.sub(r'[^0-9.]', '', str(value))
        try:
            return Decimal(digits) if digits else None
        except InvalidOperation:
            return None
