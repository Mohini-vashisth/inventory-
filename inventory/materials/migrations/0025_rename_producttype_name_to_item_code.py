from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0024_remove_gateentry_bill_no'),
    ]

    operations = [
        migrations.RenameField(
            model_name='producttype',
            old_name='name',
            new_name='item_code',
        ),
        migrations.AlterField(
            model_name='producttype',
            name='item_code',
            field=models.CharField(max_length=100, verbose_name='Item Code'),
        ),
    ]
