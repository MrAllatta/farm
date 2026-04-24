# DG-14: third fresh_or_storage choice + backfill from storage_weeks / can_hold_in_field.

from django.db import migrations, models


def forwards_backfill(apps, schema_editor):
    from reference.services.crop_category import derive_fresh_or_storage

    CropInfo = apps.get_model("reference", "CropInfo")
    for row in CropInfo.objects.all().iterator():
        nv = derive_fresh_or_storage(
            storage_weeks=row.storage_weeks,
            can_hold_in_field=row.can_hold_in_field,
        )
        if row.fresh_or_storage != nv:
            row.fresh_or_storage = nv
            row.save(update_fields=["fresh_or_storage"])


class Migration(migrations.Migration):

    dependencies = [
        ("reference", "0006_productrecipe_planning_year"),
    ]

    operations = [
        migrations.AlterField(
            model_name="cropinfo",
            name="fresh_or_storage",
            field=models.CharField(
                max_length=12,
                choices=[
                    ("fresh", "Fresh"),
                    ("fresh_holds", "Fresh holds"),
                    ("storage", "Storage"),
                ],
            ),
        ),
        migrations.RunPython(forwards_backfill, migrations.RunPython.noop),
    ]
