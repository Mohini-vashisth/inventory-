"""Rename step 2 of 2: the model that holds product TYPES gets the name step 1 freed up.

ProductCategory -> ProductType, and the fields that point at it (ProductCode.category,
QuotationLineItem.category, QueryItem.product_category) -> product_type. As in step 1 only the names inside the code
change: the table and columns keep their old names (`db_table` / `db_column`), so every schema operation is a
state-only one and the database is not touched. The content-type and permission records Django keeps for the admin
are renamed with the model so staff users' permissions carry over.
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
    _rename_records(apps, 'productcategory', 'producttype')


def backwards(apps, schema_editor):
    _rename_records(apps, 'producttype', 'productcategory')


def _type_field(column, **kwargs):
    """The renamed foreign key: new name in the code, old column name in the database."""
    return models.ForeignKey(db_column=column, to='materials.producttype', verbose_name='Product Type', **kwargs)


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0058_rename_producttype_to_productcode'),
        ('contenttypes', '0002_remove_content_type_name'),
        ('auth', '0001_initial'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(state_operations=[
            # The two uniqueness rules on the code name the field being renamed, so they are re-declared.
            migrations.RemoveConstraint(model_name='productcode', name='unique_code_per_type_grade'),
            migrations.RemoveConstraint(model_name='productcode', name='unique_untyped_code_per_grade'),
            migrations.RenameModel(old_name='ProductCategory', new_name='ProductType'),
            migrations.AlterModelTable(name='producttype', table='materials_productcategory'),
            migrations.RenameField('productcode', 'category', 'product_type'),
            migrations.AlterField('productcode', 'product_type', _type_field(
                'category_id', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='product_codes')),
            migrations.AddConstraint('productcode', models.UniqueConstraint(
                fields=('product_type', 'grade'), name='unique_code_per_type_grade')),
            migrations.AddConstraint('productcode', models.UniqueConstraint(
                condition=models.Q(('product_type__isnull', True)), fields=('grade',), name='unique_untyped_code_per_grade')),
            migrations.RenameField('quotationlineitem', 'category', 'product_type'),
            migrations.AlterField('quotationlineitem', 'product_type', _type_field(
                'category_id', blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL)),
            migrations.RenameField('queryitem', 'product_category', 'product_type'),
            migrations.AlterField('queryitem', 'product_type', _type_field(
                'product_category_id', blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL)),
        ]),
        migrations.RunPython(forwards, backwards),
    ]
