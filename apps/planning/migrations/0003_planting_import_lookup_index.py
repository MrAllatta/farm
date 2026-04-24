from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("planning", "0002_variety_seed_order_planting_fk"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="planting",
            index=models.Index(
                fields=[
                    "planning_year",
                    "crop",
                    "block",
                    "bed_start",
                    "bed_end",
                    "planned_plant_date",
                ],
                name="planting_import_lookup_idx",
            ),
        ),
    ]
