"""Every grade is stored as capital letters and digits only: EN-8D -> EN8D, SAE 1008 ->
SAE1008, EN-8D CR -> EN8DCR (decided 2026-10-06). Rewrites the grade on every record that
has one, and the grade list itself (two list entries that normalize to the same grade are
merged, keeping the lower id and its product-code number)."""
import re

from django.db import migrations

import materials.models


def normalize(value):
    return re.sub(r'[^A-Za-z0-9]', '', value).upper()


def rewrite(apps, schema_editor):
    GradeOption = apps.get_model('materials', 'GradeOption')
    seen, keep = set(), []
    for option in GradeOption.objects.order_by('pk'):
        new = normalize(option.name)
        if new in seen:   # the same grade spelled another way: keep the first
            option.delete()
        else:
            seen.add(new)
            keep.append((option, new))
    for option, new in keep:   # after the duplicates are gone, so no rename can collide
        if option.name != new:
            option.name = new
            option.save(update_fields=['name'])
    for model_name in ('Material', 'GateEntryLot', 'AllowedCoilSpec', 'ProductType', 'Order', 'Query', 'QuotationLineItem'):
        model = apps.get_model('materials', model_name)
        for old in model.objects.exclude(grade__isnull=True).exclude(grade='').values_list('grade', flat=True).distinct():
            new = normalize(old)
            if new != old:
                model.objects.filter(grade=old).update(grade=new)


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0045_product_types_list'),
    ]

    operations = [
        migrations.RunPython(rewrite, migrations.RunPython.noop),
        migrations.AlterField('gradeoption', 'name', materials.models.GradeField(max_length=20, unique=True)),
        migrations.AlterField('gateentrylot', 'grade', materials.models.GradeField(blank=True, max_length=10, null=True)),
        migrations.AlterField('material', 'grade', materials.models.GradeField(blank=True, max_length=10, null=True)),
        migrations.AlterField('allowedcoilspec', 'grade', materials.models.GradeField(blank=True, max_length=10, verbose_name='Grade')),
        migrations.AlterField('producttype', 'grade', materials.models.GradeField(blank=True, max_length=20, verbose_name='Grade')),
        migrations.AlterField('query', 'grade', materials.models.GradeField(blank=True, max_length=100)),
        migrations.AlterField('quotationlineitem', 'grade', materials.models.GradeField(blank=True, max_length=100)),
        migrations.AlterField('order', 'grade', materials.models.GradeField(blank=True, max_length=100, verbose_name='Grade of Material')),
    ]
