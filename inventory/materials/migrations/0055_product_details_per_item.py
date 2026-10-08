# The drawing / sample, make-properties-process and end-use answers used to be asked once for a whole
# query. They are now per product (only the company details stay per query), so they move onto each
# QueryItem: every existing item gets a copy of its query's answers, and a query that has answers but
# no product yet gets one empty product to hold them. Reversing copies the first product's answers back.

from django.db import migrations, models

MOVED = ('drawing', 'drawing_notes', 'technical_requirements', 'end_use')


def copy_to_items(apps, schema_editor):
    Query = apps.get_model('materials', 'Query')
    QueryItem = apps.get_model('materials', 'QueryItem')
    for query in Query.objects.all():
        values = {name: getattr(query, name) for name in MOVED}
        if not any(values.values()):
            continue
        items = list(query.items.all())
        if not items:
            items = [QueryItem.objects.create(query=query, position=1)]
        for item in items:
            for name, value in values.items():
                setattr(item, name, value)
            item.save()


def copy_back_to_query(apps, schema_editor):
    Query = apps.get_model('materials', 'Query')
    for query in Query.objects.all():
        first = query.items.order_by('position', 'pk').first()
        if first:
            for name in MOVED:
                setattr(query, name, getattr(first, name))
            query.save()


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0054_spec_without_grade'),
    ]

    operations = [
        migrations.AddField(model_name='queryitem', name='drawing',
                            field=models.FileField(blank=True, null=True, upload_to='query_drawings/%Y/%m/')),
        migrations.AddField(model_name='queryitem', name='drawing_notes',
                            field=models.CharField(blank=True, max_length=255)),
        migrations.AddField(model_name='queryitem', name='end_use',
                            field=models.TextField(blank=True)),
        migrations.AddField(model_name='queryitem', name='technical_requirements',
                            field=models.TextField(blank=True, verbose_name='Make / properties / process')),
        migrations.RunPython(copy_to_items, copy_back_to_query),
        migrations.RemoveField(model_name='query', name='drawing'),
        migrations.RemoveField(model_name='query', name='drawing_notes'),
        migrations.RemoveField(model_name='query', name='end_use'),
        migrations.RemoveField(model_name='query', name='technical_requirements'),
    ]
