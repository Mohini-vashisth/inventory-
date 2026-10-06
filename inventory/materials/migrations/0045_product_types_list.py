"""The product types the business actually sells (decided 2026-10-06): Flat Bright Bar,
Square Bright Bar, Profile/Shaped Bright Bar, Cold Rolled Strip, Cold Rolled Profile,
Chamfer Steel, Triangle Bright Bar. Others can be added later in the admin.

"Shaped Bright Bar" and "Steel Chamfer" are renamed (their 3 letters change too, but
only if no product code has started with the old ones); Cold Rolled Profile is new; the
rest of the earlier 14 are removed.
"""
from django.db import migrations

# name -> (code, position)
KEEP = {
    'Flat Bright Bar': ('FBB', 1),
    'Square Bright Bar': ('SQB', 2),
    'Profile/Shaped Bright Bar': ('PSB', 3),
    'Cold Rolled Strip': ('CRS', 4),
    'Cold Rolled Profile': ('CRP', 5),
    'Chamfer Steel': ('CHS', 6),
    'Triangle Bright Bar': ('TBB', 7),
}
RENAMES = {'Shaped Bright Bar': 'Profile/Shaped Bright Bar', 'Steel Chamfer': 'Chamfer Steel'}


def reshape(apps, schema_editor):
    ProductCategory = apps.get_model('materials', 'ProductCategory')
    for old, new in RENAMES.items():
        category = ProductCategory.objects.filter(name=old).first()
        if category and not ProductCategory.objects.filter(name=new).exists():
            category.name = new
            category.save(update_fields=['name'])
    for name, (code, position) in KEEP.items():
        category, created = ProductCategory.objects.get_or_create(name=name, defaults={'code': code, 'position': position})
        update = ['position']
        category.position = position
        # Take the new letters only if nothing has started with the old ones (a printed code never changes).
        if (created or not category.product_codes.exists()) and category.code != code \
                and not ProductCategory.objects.filter(code=code).exclude(pk=category.pk).exists():
            category.code = code
            update.append('code')
        category.save(update_fields=update)
    for category in ProductCategory.objects.exclude(name__in=KEEP):
        if not category.product_codes.exists():   # never remove a type that codes already hang off
            category.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0044_width_and_thickness'),
    ]

    operations = [
        migrations.RunPython(reshape, migrations.RunPython.noop),
    ]
