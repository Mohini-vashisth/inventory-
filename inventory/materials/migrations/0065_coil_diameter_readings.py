"""The five measured readings on a coil are its actual DIAMETER, not its thickness: thickness_1..5 -> diameter_1..5.

A pure rename (SQLite renames each column and keeps its values). No coil had any reading yet when this ran. The
product's own thickness (on orders, queries and quote lines) is a different thing and is untouched."""

from django.db import migrations, models


def _diameter(number):
    return models.DecimalField(blank=True, decimal_places=3, max_digits=7, null=True,
                               verbose_name=f'actual diameter, reading {number} (mm)')


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0064_whatsapp_saved_keys'),
    ]

    operations = (
        [migrations.RenameField('material', f'thickness_{n}', f'diameter_{n}') for n in range(1, 6)]
        + [migrations.AlterField('material', f'diameter_{n}', _diameter(n)) for n in range(1, 6)]
    )
