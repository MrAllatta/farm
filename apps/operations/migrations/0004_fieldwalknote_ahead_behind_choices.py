# Generated manually for FieldWalkNote condition choices.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0003_packbatch_planning_year"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fieldwalknote",
            name="condition",
            field=models.CharField(
                choices=[
                    ("ahead", "Ahead of plan"),
                    ("behind", "Behind plan"),
                    ("good", "Good"),
                    ("fair", "Fair"),
                    ("poor", "Poor"),
                    ("failed", "Failed"),
                ],
                max_length=10,
            ),
        ),
    ]
