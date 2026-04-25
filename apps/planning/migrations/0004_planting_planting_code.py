# Generated manually for durable planting unit codes.

from django.db import migrations, models


def backfill_planting_codes(apps, schema_editor):
    Planting = apps.get_model("planning", "Planting")
    PlanningYear = apps.get_model("planning", "PlanningYear")
    year_by_id = {row.pk: row.year for row in PlanningYear.objects.all()}
    # Stable order: planning year id, then planting pk
    rows = list(Planting.objects.all().order_by("planning_year_id", "pk"))
    counters: dict[int, int] = {}
    for p in rows:
        yid = p.planning_year_id
        cal_year = year_by_id.get(yid)
        if cal_year is None:
            continue
        counters[yid] = counters.get(yid, 0) + 1
        seq = counters[yid]
        code = f"P-{int(cal_year)}-{seq:04d}"
        Planting.objects.filter(pk=p.pk).update(planting_code=code)


class Migration(migrations.Migration):

    dependencies = [
        ("planning", "0003_planting_import_lookup_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="planting",
            name="planting_code",
            field=models.CharField(max_length=20, null=True, blank=True, unique=False),
        ),
        migrations.RunPython(backfill_planting_codes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="planting",
            name="planting_code",
            field=models.CharField(max_length=20, unique=True),
        ),
    ]
