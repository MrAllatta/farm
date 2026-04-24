# Generated manually for executed mix batches (H1b).

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0002_alter_inventoryledger_options_packbatch_and_more"),
        ("planning", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="packbatch",
            name="planning_year",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="pack_batches",
                to="planning.planningyear",
            ),
        ),
    ]
