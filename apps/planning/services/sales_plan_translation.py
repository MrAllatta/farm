from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db.models import Q

from planning.models import PlanningYear
from reference.models import CropBySeason, SalesCategory, SalesChannel
from reference.sales_rollups import plan_events_without_shadowed_rollups
from sales.models import SalesEvent


def build_demand_to_supply_draft(
    planning_year: PlanningYear,
    *,
    channel: SalesChannel | None = None,
    sales_category: SalesCategory | None = None,
) -> dict:
    """Build draft planting recommendations from planned product-week demand."""
    if (channel is None) == (sales_category is None):
        raise ValueError("Provide exactly one of channel or sales_category")

    qs = SalesEvent.objects.filter(
        entry_kind=SalesEvent.EntryKind.PLAN,
        planning_year=planning_year,
    ).select_related("product", "product__crop", "channel", "channel__category", "sales_category")

    if channel is not None:
        qs = qs.filter(channel=channel)
    else:
        qs = qs.filter(
            Q(sales_category=sales_category) | Q(channel__category=sales_category),
        )

    planned_events = plan_events_without_shadowed_rollups(list(qs.order_by("sale_date", "product__crop__name", "product__product_name")))

    rows = []
    skipped = []
    total_bedfeet = Decimal("0")

    for event in planned_events:
        if not event.product:
            skipped.append(
                {
                    "week": event.sale_date.isocalendar()[1],
                    "product": "",
                    "reason": "missing_product",
                }
            )
            continue

        crop = event.product.crop
        crop_season = (
            CropBySeason.objects.filter(crop=crop, block_type="field").order_by("id").first()
            or CropBySeason.objects.filter(crop=crop).order_by("id").first()
        )
        if not crop_season:
            skipped.append(
                {
                    "week": event.sale_date.isocalendar()[1],
                    "product": event.product.product_name,
                    "reason": "missing_crop_season",
                }
            )
            continue

        quantity = event.planned_quantity or Decimal("0")
        harvest_qty_per_unit = event.product.harvest_qty_per_sale_unit or Decimal("1")
        harvest_units_required = quantity * harvest_qty_per_unit

        if crop_season.total_yield_per_bedfoot <= 0:
            skipped.append(
                {
                    "week": event.sale_date.isocalendar()[1],
                    "product": event.product.product_name,
                    "reason": "invalid_yield_per_bedfoot",
                }
            )
            continue

        planned_bedfeet = harvest_units_required / crop_season.total_yield_per_bedfoot
        total_bedfeet += planned_bedfeet

        planned_plant_date = event.sale_date - timedelta(days=crop_season.dtm_days)
        rows.append(
            {
                "week": event.sale_date.isocalendar()[1],
                "product": event.product.product_name,
                "crop": crop.name,
                "block_type": crop_season.block_type,
                "demand_quantity": str(quantity),
                "planned_bedfeet": planned_bedfeet,
                "planned_plant_date": planned_plant_date.isoformat(),
            }
        )

    return {
        "rows": rows,
        "skipped": skipped,
        "counts": {
            "rows": len(rows),
            "skipped": len(skipped),
            "total_planned_bedfeet": total_bedfeet,
        },
    }
