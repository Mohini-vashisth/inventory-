import django.db.models.deletion
import uuid
from django.db import migrations, models


def give_every_company_its_own_link(apps, schema_editor):
    """A new unique column can't take one default for existing rows, so each company gets its own value first."""
    Customer = apps.get_model('materials', 'Customer')
    for customer in Customer.objects.all():
        customer.portal_token = uuid.uuid4()
        customer.save(update_fields=['portal_token'])


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0069_coil_bill_rates_and_charges'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='portal_token',
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(give_every_company_its_own_link, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='customer',
            name='portal_token',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddField(
            model_name='order',
            name='customer_can_track',
            field=models.BooleanField(default=False, verbose_name='Customer can track this order'),
        ),
        migrations.AddField(
            model_name='order',
            name='reordered_from',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reorders', to='materials.order'),
        ),
    ]
