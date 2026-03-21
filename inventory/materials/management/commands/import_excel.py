import pandas as pd
from django.core.management.base import BaseCommand
from materials.models import Material


class Command(BaseCommand):
    help = "Import materials from Excel"

    def handle(self, *args, **kwargs):

        file_path = "STOCK INVENTORY SHEET.xlsx"

        # ✅ Read Excel
        df = pd.read_excel(file_path)

        # ✅ Clean column names (remove spaces)
        df.columns = df.columns.str.strip()

        print("Columns found:", df.columns)
        print("Total rows in Excel:", len(df))

        # ✅ Rename columns to match Django model (VERY IMPORTANT)
        df = df.rename(columns={
            'DATE': 'Date',
            'GRADE': 'Grade',
            'SIZE': 'Size',
            'COMPANY': 'Company',
            'VENDOR': 'Vendor',
            'QTY (KGS)': 'Quantity',
            'HEAT NO.': 'Heat No'
        })

        # ✅ Remove rows without valid Date
        df = df[df['Date'].notna()]

        print("Rows with valid Date:", len(df))

        # ✅ Replace NaN → None
        df = df.where(pd.notnull(df), None)

        success_count = 0
        skip_count = 0

        # ✅ Insert data
        for i, row in df.iterrows():
            try:
                Material.objects.create(
                    date=row.get('Date'),
                    grade=row.get('Grade'),
                    size=row.get('Size'),
                    company=row.get('Company'),
                    vendor=row.get('Vendor'),
                    quantity=row.get('Quantity') or 0,
                    heat_no=row.get('Heat No')
                )
                success_count += 1

            except Exception as e:
                print(f"❌ Error in row {i}: {e}")
                skip_count += 1

        # ✅ Final report
        print("===================================")
        print(f"✅ Successfully added: {success_count}")
        print(f"⚠️ Skipped rows: {skip_count}")
        print("===================================")

        self.stdout.write(self.style.SUCCESS("🎉 Import completed successfully!"))