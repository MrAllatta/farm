# ProductRecipe scoped by planning year (H1b) + backfill.

import django.db.models.deletion
from decimal import Decimal

from django.db import migrations, models
from django.db.models import Q


def forwards_assign_planning_year(apps, schema_editor):
    ProductRecipe = apps.get_model("reference", "ProductRecipe")
    PlanningYear = apps.get_model("planning", "PlanningYear")

    def ensure_year(y: int):
        obj, _ = PlanningYear.objects.get_or_create(
            year=y,
            defaults={
                "status": "archived",
                "overplant_factor": Decimal("1.10"),
            },
        )
        return obj

    for recipe in ProductRecipe.objects.all().iterator():
        if recipe.planning_year_id:
            continue
        y = None
        if recipe.effective_start:
            y = recipe.effective_start.year
        recipe.planning_year = ensure_year(y) if y else ensure_year(2026)
        recipe.save(update_fields=["planning_year_id"])


def backwards_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("planning", "0001_initial"),
        ("reference", "0005_salescategory_saleschannel_category_salesplanbucket_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="productrecipe",
            name="planning_year",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="product_recipes",
                to="planning.planningyear",
            ),
        ),
        migrations.RunPython(forwards_assign_planning_year, backwards_noop),
        migrations.RemoveConstraint(
            model_name="productrecipe",
            name="product_recipe_single_active_per_product",
        ),
        migrations.AddConstraint(
            model_name="productrecipe",
            constraint=models.UniqueConstraint(
                condition=Q(is_active=True),
                fields=("planning_year", "product"),
                name="product_recipe_single_active_per_year_per_product",
            ),
        ),
        migrations.AlterModelOptions(
            name="productrecipe",
            options={
                "ordering": [
                    "planning_year",
                    "product__product_name",
                    "-effective_start",
                    "name",
                ]
            },
        ),
    ]
