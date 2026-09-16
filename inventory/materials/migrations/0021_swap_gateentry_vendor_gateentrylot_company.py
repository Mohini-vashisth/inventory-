import django.db.models.deletion
from django.db import migrations, models


def copy_old_values_to_new_fields(apps, schema_editor):
    """vendor (the supplier who delivered the truck) was mistakenly put on
    GateEntryLot instead of GateEntry, and company (the coil's brand) was
    mistakenly put on GateEntry instead of GateEntryLot. Move each value to
    where it belongs before the old fields are dropped, so nothing entered
    on a real gate entry already is lost."""
    GateEntry = apps.get_model('materials', 'GateEntry')
    GateEntryLot = apps.get_model('materials', 'GateEntryLot')
    for entry in GateEntry.objects.all():
        entry.vendor = entry.company
        entry.save(update_fields=['vendor'])
    for lot in GateEntryLot.objects.all():
        lot.company = lot.vendor
        lot.save(update_fields=['company'])


def copy_new_values_back_to_old_fields(apps, schema_editor):
    GateEntry = apps.get_model('materials', 'GateEntry')
    GateEntryLot = apps.get_model('materials', 'GateEntryLot')
    for entry in GateEntry.objects.all():
        entry.company = entry.vendor
        entry.save(update_fields=['company'])
    for lot in GateEntryLot.objects.all():
        lot.vendor = lot.company
        lot.save(update_fields=['vendor'])


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0020_remove_placeholder_grade_size_options'),
    ]

    operations = [
        # 1. Add the new fields (nullable) alongside the old ones.
        migrations.AddField(
            model_name='gateentry',
            name='vendor',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name='gateentrylot',
            name='company',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        # 2. Copy every existing value across before the old fields disappear.
        migrations.RunPython(copy_old_values_to_new_fields, copy_new_values_back_to_old_fields),
        # 3. Drop the old, mislabeled fields.
        migrations.RemoveField(model_name='gateentry', name='company'),
        migrations.RemoveField(model_name='gateentrylot', name='vendor'),
    ]
