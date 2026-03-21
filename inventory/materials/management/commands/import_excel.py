import pandas as pd
from django.core.management.base import BaseCommand
from materials.models import Material

class Command(BaseCommand):
    help = "Import materials from Excel"

    def handle(self, *args, **kwargs):

        file_path = "STOCK INVENTORY SHEET.xlsx"

        # Read Excel
        df = pd.read_excel(file_path)

        # 🔧 Clean column names (VERY IMPORTANT)
        df.columns = df.columns.str.strip().str.lower()

        # 🔧 Replace NaN with None
        df = df.where(pd.notnull(df), None)

        print("Columns detected:", df.columns)
        print("Total rows:", len(df))

        for i, row in df.iterrows():
            try:
                Material.objects.create(
                    date=row.get('date'),
                    grade=row.get('grade'),
                    size=row.get('size'),
                    company=row.get('company'),
                    vendor=row.get('vendor'),
                    quantity=row.get('quantity') or 0,
                    heat_no=row.get('heat no')
                )
            except Exception as e:
                print(f"❌ Error at row {i}: {e}")

        self.stdout.write(self.style.SUCCESS("✅ Data imported successfully!"))