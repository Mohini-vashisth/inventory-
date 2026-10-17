"""Rename the database tables and columns to match the model names (finishing the product code / product type rename).

Migrations 0058 and 0059 renamed the models and fields in the code only, keeping the old tables and columns through
`db_table` / `db_column`. This one makes the database match:

  tables   materials_producttype     (product CODES) -> materials_productcode
           materials_productcategory (product TYPES) -> materials_producttype
  columns  product_type_id (code)  -> product_code_id   (allowedcoilspec, processstep, productionjob, queryitem,
                                                          quotationlineitem, order)
           category_id / product_category_id (type) -> product_type_id   (productcode, quotationlineitem, queryitem)

The order matters, as in the first rename: a name is only used once the thing that held it has moved. The code table
and the code columns go first; then the type table and the type columns take the freed names. These are real schema
operations (SQLite remakes each affected table and copies its rows), so every row is kept. Reversible.
"""

from django.db import migrations, models
import django.db.models.deletion


def _code_fk(**kwargs):
    return models.ForeignKey(to='materials.productcode', **kwargs)


def _type_fk(**kwargs):
    return models.ForeignKey(to='materials.producttype', verbose_name='Product Type', **kwargs)


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0062_coil_thickness_readings'),
    ]

    operations = [
        # 1. The code table first (it is the one sitting on the name the type table is about to take).
        migrations.AlterModelTable(name='productcode', table=None),
        migrations.AlterModelTable(name='producttype', table=None),

        # 2. Every column that points at a product CODE: product_type_id -> product_code_id.
        migrations.AlterField('allowedcoilspec', 'product_code', _code_fk(
            on_delete=django.db.models.deletion.CASCADE, related_name='allowed_specs')),
        migrations.AlterField('processstep', 'product_code', _code_fk(
            on_delete=django.db.models.deletion.CASCADE, related_name='steps')),
        migrations.AlterField('productionjob', 'product_code', _code_fk(
            on_delete=django.db.models.deletion.PROTECT)),
        migrations.AlterField('queryitem', 'product_code', _code_fk(
            blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL)),
        migrations.AlterField('quotationlineitem', 'product_code', _code_fk(
            blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL)),
        migrations.AlterField('order', 'product_code', _code_fk(
            blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
            related_name='orders', verbose_name='Product Code')),

        # 3. Every column that points at a product TYPE: category_id / product_category_id -> product_type_id.
        migrations.AlterField('productcode', 'product_type', _type_fk(
            null=True, on_delete=django.db.models.deletion.PROTECT, related_name='product_codes')),
        migrations.AlterField('quotationlineitem', 'product_type', _type_fk(
            blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL)),
        migrations.AlterField('queryitem', 'product_type', _type_fk(
            blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL)),
    ]
