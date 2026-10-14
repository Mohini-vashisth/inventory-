"""Rename step 1 of 2: the model that holds product CODES gets its right name.

ProductType -> ProductCode, and the fields that point at it product_type -> product_code. Only the names inside
the code change: the table and the columns keep their old names (`db_table` / `db_column` on the model), so every
operation on the schema is a state-only one and the database itself is not touched. The only data touched are the
content-type and permission records Django keeps for the admin, which are renamed along with the model so any
staff user's permissions carry over.
"""

from django.db import migrations, models
import django.db.models.deletion


def _rename_records(apps, old_model, new_model):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    content_type = ContentType.objects.filter(app_label='materials', model=old_model).first()
    if content_type is None or ContentType.objects.filter(app_label='materials', model=new_model).exists():
        return   # a fresh database has none yet, or the new records are already there
    content_type.model = new_model
    content_type.save(update_fields=['model'])
    for permission in Permission.objects.filter(content_type=content_type):
        permission.codename = permission.codename.replace(old_model, new_model)
        permission.save(update_fields=['codename'])


def forwards(apps, schema_editor):
    _rename_records(apps, 'producttype', 'productcode')


def backwards(apps, schema_editor):
    _rename_records(apps, 'productcode', 'producttype')


def _code_field(**kwargs):
    """The renamed foreign key: new name in the code, old column name in the database."""
    return models.ForeignKey(db_column='product_type_id', to='materials.productcode', **kwargs)


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0057_gate_bills_and_non_coil_entries'),
        ('contenttypes', '0002_remove_content_type_name'),
        ('auth', '0001_initial'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(state_operations=[
            migrations.RenameModel(old_name='ProductType', new_name='ProductCode'),
            migrations.AlterModelTable(name='productcode', table='materials_producttype'),
            migrations.RenameField('allowedcoilspec', 'product_type', 'product_code'),
            migrations.AlterField('allowedcoilspec', 'product_code', _code_field(
                on_delete=django.db.models.deletion.CASCADE, related_name='allowed_specs')),
            migrations.RenameField('processstep', 'product_type', 'product_code'),
            migrations.AlterField('processstep', 'product_code', _code_field(
                on_delete=django.db.models.deletion.CASCADE, related_name='steps')),
            migrations.RenameField('productionjob', 'product_type', 'product_code'),
            migrations.AlterField('productionjob', 'product_code', _code_field(
                on_delete=django.db.models.deletion.PROTECT)),
            migrations.RenameField('queryitem', 'product_type', 'product_code'),
            migrations.AlterField('queryitem', 'product_code', _code_field(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL)),
            migrations.RenameField('quotationlineitem', 'product_type', 'product_code'),
            migrations.AlterField('quotationlineitem', 'product_code', _code_field(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL)),
            migrations.RenameField('order', 'product_type', 'product_code'),
            migrations.AlterField('order', 'product_code', _code_field(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='orders', verbose_name='Product Code')),
        ]),
        migrations.RunPython(forwards, backwards),
    ]
