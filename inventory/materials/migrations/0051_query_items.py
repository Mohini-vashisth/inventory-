"""A query holds a list of products (QueryItem) instead of one set of product fields.

Each existing query's product type, code, grade, width, thickness, quantity and delivery form are
copied into its first item (when it has any of them), then the single-product columns are dropped. A
free-text quantity ("2 tons monthly") that never became a number is kept by appending it to the query's
notes rather than lost."""
from django.db import migrations, models
import django.db.models.deletion

import materials.models

FIELDS = ('product_category_id', 'product_type_id', 'grade', 'width', 'thickness', 'quantity', 'delivery_form')


def copy_to_items(apps, schema_editor):
    Query = apps.get_model('materials', 'Query')
    QueryItem = apps.get_model('materials', 'QueryItem')
    for query in Query.objects.all():
        values = {name: getattr(query, name) for name in FIELDS}
        if any(value not in (None, '') for value in values.values()):
            QueryItem.objects.create(query=query, position=1, **values)
        if query.quantity is None and (query.quantity_text or '').strip():
            note = f"Quantity (as typed): {query.quantity_text.strip()}"
            query.notes = f"{query.notes}\n{note}".strip()
            query.save(update_fields=['notes'])


def copy_back_to_query(apps, schema_editor):
    Query = apps.get_model('materials', 'Query')
    QueryItem = apps.get_model('materials', 'QueryItem')
    for item in QueryItem.objects.order_by('query_id', 'position', 'pk'):
        query = Query.objects.get(pk=item.query_id)
        if query.grade or query.product_category_id:
            continue   # only the first item goes back
        for name in FIELDS:
            setattr(query, name, getattr(item, name))
        query.save()


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0050_order_bar_length_and_coil_weight'),
    ]

    operations = [
        migrations.CreateModel(
            name='QueryItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('position', models.PositiveSmallIntegerField(default=1)),
                ('grade', materials.models.GradeField(blank=True, max_length=100)),
                ('width', models.DecimalField(blank=True, decimal_places=3, max_digits=10, null=True, verbose_name='Width (mm)')),
                ('thickness', models.DecimalField(blank=True, decimal_places=3, max_digits=10, null=True, verbose_name='Thickness (mm)')),
                ('quantity', models.DecimalField(blank=True, decimal_places=3, max_digits=10, null=True, verbose_name='Quantity (kg)')),
                ('delivery_form', models.CharField(blank=True, choices=[('Coil', 'Coil'), ('Bar', 'Bar')], max_length=10)),
                ('product_category', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='materials.productcategory', verbose_name='Product Type')),
                ('product_type', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='materials.producttype')),
                ('query', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='materials.query')),
            ],
            options={'ordering': ['position', 'pk']},
        ),
        migrations.RunPython(copy_to_items, copy_back_to_query),
        migrations.RemoveField('query', 'product_category'),
        migrations.RemoveField('query', 'product_type'),
        migrations.RemoveField('query', 'grade'),
        migrations.RemoveField('query', 'width'),
        migrations.RemoveField('query', 'thickness'),
        migrations.RemoveField('query', 'quantity'),
        migrations.RemoveField('query', 'delivery_form'),
        migrations.RemoveField('query', 'quantity_text'),
    ]
