# Generated manually for 601 historical sales (H1a).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0006_alter_salesevent_ordering"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesevent",
            name="harvest_date",
            field=models.DateField(
                blank=True,
                help_text="Optional harvest anchor for this sale (e.g. 601 Market/Orders).",
                null=True,
            ),
        ),
    ]
