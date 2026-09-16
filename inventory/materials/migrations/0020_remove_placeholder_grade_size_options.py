# Generated manually — removes the placeholder grade/size options seeded by
# 0013_seed_grade_size_options. Those were example values, not real ones —
# confirmed by having zero overlap with any grade/size actually used in
# Material once real client data was imported. See CLAUDE.md for the real
# ones backfilled from Material via `manage.py backfill_options`.

from django.db import migrations

GRADES = ['EN8D', 'EN8', 'RAW', '430']
SIZES = ['0.23', '0.3', '1.0', '1.2', '1.5']


def remove_placeholder_options(apps, schema_editor):
    GradeOption = apps.get_model('materials', 'GradeOption')
    SizeOption = apps.get_model('materials', 'SizeOption')
    GradeOption.objects.filter(name__in=GRADES).delete()
    SizeOption.objects.filter(value__in=SIZES).delete()


def restore_placeholder_options(apps, schema_editor):
    GradeOption = apps.get_model('materials', 'GradeOption')
    SizeOption = apps.get_model('materials', 'SizeOption')
    for name in GRADES:
        GradeOption.objects.get_or_create(name=name)
    for value in SIZES:
        SizeOption.objects.get_or_create(value=value)


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0019_gateentry_bill_no_gateentry_invoice_no'),
    ]

    operations = [
        migrations.RunPython(remove_placeholder_options, restore_placeholder_options),
    ]
