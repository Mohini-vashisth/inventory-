"""Finish the product type rename in the WhatsApp bot's saved conversations.

The bot saves its place in a conversation as text, and that text named the product type question 'product_category'
(its old name): `last_asked_field` ('product_category@0'), `edit_field` ('item:1:product_category') and the held
change `pending_value` ({'_items': {'product_category': [pk]}}). The code now uses 'product_type'; this converts
what is already saved so a conversation in progress carries on. (The code also still reads the old spelling, so a
message handled between the deploy and this migration can't trip either.) Reversible.
"""

from django.db import migrations
from django.db.models import Value
from django.db.models.functions import Replace

OLD, NEW = 'product_category', 'product_type'


def _convert(apps, old, new):
    Query = apps.get_model('materials', 'Query')
    for field in ('last_asked_field', 'edit_field'):
        Query.objects.filter(**{f'{field}__contains': old}).update(**{field: Replace(field, Value(old), Value(new))})
    for query in Query.objects.exclude(pending_value={}).exclude(pending_value__isnull=True):
        items = query.pending_value.get('_items') if isinstance(query.pending_value, dict) else None
        if items and old in items:
            query.pending_value = {**query.pending_value, '_items': {(new if key == old else key): value for key, value in items.items()}}
            query.save(update_fields=['pending_value'])


def forwards(apps, schema_editor):
    _convert(apps, OLD, NEW)


def backwards(apps, schema_editor):
    _convert(apps, NEW, OLD)


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0063_real_table_and_column_names'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
