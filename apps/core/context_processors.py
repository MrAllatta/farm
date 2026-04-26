"""core/context_processors.py"""

from datetime import date, timedelta

from django.urls import reverse
from isoweek import Week

from core.planning_year import (
    get_effective_planning_year,
    operational_anchor_year,
    planning_years_in_clock_scope,
)
from core.operator_scope import first_operator_sales_channel

CROP_TYPE_COLORS = {
    "Tomatoes": "#fee2e2",
    "Greens": "#dcfce7",
    "Roots": "#fef3c7",
    "Brassica": "#dbeafe",
    "Allium": "#ede9fe",
    "Cucumbers": "#fef9c3",
    "Herbs": "#d1fae5",
    "Beans/Peas": "#e0e7ff",
    "Peppers": "#fce7f3",
    "Eggplant": "#f3e8ff",
    "Winter Squash": "#fed7aa",
    "Zucchini": "#fef08a",
    "Garlic": "#e9d5ff",
    "Lettuce": "#bbf7d0",
    "Salad Greens": "#a7f3d0",
    "Mix": "#f0fdf4",
}


def planning_context(request):
    """Add current planning year, week, and crop colors to every template."""
    year_obj = get_effective_planning_year(request)
    anchor = operational_anchor_year()

    today = date.today()
    iso = today.isocalendar()
    current_week = iso[1]
    # ISO week can be 53; week-ops URLs clamp to 1–52 to match HarvestEvent week helpers.
    operations_nav_week = max(1, min(52, current_week))
    iso_wy, iso_wn, _ = iso
    wk = Week(iso_wy, iso_wn)
    field_week_monday = wk.monday()
    field_week_sunday = field_week_monday + timedelta(days=6)

    first_channel = first_operator_sales_channel()
    weekly_order_url = None
    if first_channel is not None:
        weekly_order_url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": first_channel.id, "week": operations_nav_week},
        )

    return {
        "current_planning_year": year_obj,
        "planning_year_scope_choices": planning_years_in_clock_scope(anchor),
        "operational_anchor_year": anchor,
        "current_week": current_week,
        "operations_nav_week": operations_nav_week,
        "today": today,
        "field_week_monday": field_week_monday,
        "field_week_sunday": field_week_sunday,
        "crop_colors": CROP_TYPE_COLORS,
        "weekly_order_url": weekly_order_url,
    }
