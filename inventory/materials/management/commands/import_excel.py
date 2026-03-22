import pandas as pd
from django.core.management.base import BaseCommand
from materials.models import Material

class Command(BaseCommand):
    help = "Import materials from Excel"

    def handle(self, *args, **kwargs):

        file_path = "STOCK INVENTORY SHEET.xlsx"

        # ✅ FIRST create df
        df = pd.read_excel(file_path)

        # ✅ THEN clean columns
        df.columns = df.columns.str.strip()

        # ✅ Debug (optional)
        print(df.columns)
        

        # ✅ Replace NaN with None
        df = df.where(pd.notnull(df), None)

        for _, row in df.iterrows():
            Material.objects.create(
                date=row.get('Date'),
                grade=row.get('Grade'),
                size=row.get('Size'),
                company=row.get('Company'),
                vendor=row.get('Vendor'),
                quantity=row.get('Quantity') or 0,
                heat_no=row.get('Heat No')
            )

        self.stdout.write(self.style.SUCCESS("Data imported successfully!"))