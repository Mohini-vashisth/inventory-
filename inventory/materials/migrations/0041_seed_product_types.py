from django.db import migrations

# The product types on the company website's Products menu, in menu order.
PRODUCT_TYPES = [
    'Key Steel', 'Steel Chamfer', 'Half Round Bright Bar', 'Flat Bright Bar',
    'Square Bright Bar', 'Round Bright Bar', 'Hexagonal Bright Bar', 'Special Profiles Bar',
    'Shaped Bright Bar', 'Triangle Bright Bar', 'Profile Wire', 'Shaped Wire',
    'Flat Wire', 'Cold Rolled Strip',
]


def seed(apps, schema_editor):
    ProductCategory = apps.get_model('materials', 'ProductCategory')
    for position, name in enumerate(PRODUCT_TYPES, start=1):
        ProductCategory.objects.get_or_create(name=name, defaults={'position': position})


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0040_product_types_and_codes'),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
