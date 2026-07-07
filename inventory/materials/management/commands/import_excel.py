import pandas as pd
from django.core.management.base import BaseCommand, CommandError
from materials.models import Material


class Command(BaseCommand):
    help = "Replace all Material rows with the contents of STOCK INVENTORY SHEET.xlsx"

    def add_arguments(self, parser):
        parser.add_argument(
            '--yes', action='store_true',
            help="Skip the confirmation prompt before deleting existing Material rows.",
        )

    def handle(self, *args, **options):
        file_path = "STOCK INVENTORY SHEET.xlsx"
        try:
            df = pd.read_excel(file_path)
        except FileNotFoundError:
            raise CommandError(f"'{file_path}' not found in the current directory.")

        existing = Material.objects.count()
        if existing and not options['yes']:
            confirm = input(
                f"This will delete all {existing} existing Material rows before importing. Continue? [y/N] "
            )
            if confirm.strip().lower() != 'y':
                self.stdout.write("Aborted.")
                return

        Material.objects.all().delete()

        df.columns = df.columns.str.strip()
        df = df.where(pd.notnull(df), None)

        for _, row in df.iterrows():
            Material.objects.create(
                date=row.get('Date'),
                grade=row.get('Grade'),
                size=row.get('Size'),
                company=row.get('Company'),
                vendor=row.get('Vendor'),
                quantity=row.get('Quantity') or 0,
                heat_no=row.get('Heat No'),
            )

        self.stdout.write(self.style.SUCCESS(f"Imported {len(df)} rows successfully."))
