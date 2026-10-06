"""Size becomes width + thickness on queries, quote lines and orders; product codes lose size.

A product code now depends only on product type + grade (width and thickness vary
per order). Existing single sizes become the width. Existing generated codes
(TYPE + grade number + 5 size digits) are rewritten to TYPE + grade number.
"""
from django.db import migrations, models


def shorten_generated_codes(apps, schema_editor):
    ProductType = apps.get_model('materials', 'ProductType')
    GradeOption = apps.get_model('materials', 'GradeOption')
    seen = {}
    for code in ProductType.objects.order_by('pk'):
        key = (code.category_id, (code.grade or '').strip().lower())
        if key in seen:
            raise RuntimeError(
                f"Product codes {seen[key]} and {code.item_code} are the same type + grade in different sizes. "
                "A code no longer includes size: delete or merge one of them before migrating.")
        seen[key] = code.item_code
        option = GradeOption.objects.filter(name__iexact=(code.grade or '').strip()).first()
        if code.category_id and code.category.code and option and option.number:
            old = f"{code.category.code}{option.number:03d}"
            if code.item_code.startswith(old) and code.item_code[len(old):].isdigit() and len(code.item_code) == len(old) + 5:
                code.item_code = old
                code.save(update_fields=['item_code'])


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0043_quotation_customer_gstin'),
    ]

    operations = [
        migrations.RenameField('query', 'size', 'width'),
        migrations.AlterField('query', 'width', models.DecimalField(blank=True, decimal_places=3, max_digits=10, null=True, verbose_name='Width (mm)')),
        migrations.AddField('query', 'thickness', models.DecimalField(blank=True, decimal_places=3, max_digits=10, null=True, verbose_name='Thickness (mm)')),
        migrations.RenameField('quotationlineitem', 'size', 'width'),
        migrations.AlterField('quotationlineitem', 'width', models.DecimalField(blank=True, decimal_places=3, max_digits=10, null=True, verbose_name='Width (mm)')),
        migrations.AddField('quotationlineitem', 'thickness', models.DecimalField(blank=True, decimal_places=3, max_digits=10, null=True, verbose_name='Thickness (mm)')),
        migrations.RenameField('order', 'size', 'width'),
        migrations.AlterField('order', 'width', models.DecimalField(blank=True, decimal_places=3, max_digits=10, null=True, verbose_name='Width (mm)')),
        migrations.AddField('order', 'thickness', models.DecimalField(blank=True, decimal_places=3, max_digits=10, null=True, verbose_name='Thickness (mm)')),
        migrations.RemoveConstraint('producttype', 'unique_code_per_type_grade_size'),
        migrations.RemoveConstraint('producttype', 'unique_untyped_code_per_grade_size'),
        migrations.RunPython(shorten_generated_codes, migrations.RunPython.noop),
        migrations.RemoveField('producttype', 'size'),
        migrations.AddConstraint('producttype', models.UniqueConstraint(fields=('category', 'grade'), name='unique_code_per_type_grade')),
        migrations.AddConstraint('producttype', models.UniqueConstraint(condition=models.Q(('category__isnull', True)), fields=('grade',), name='unique_untyped_code_per_grade')),
    ]
