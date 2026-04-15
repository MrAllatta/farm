"""core/context_processors.py"""

from datetime import date
from core.planning_year import resolve_current_planning_year

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
    year_obj = resolve_current_planning_year()

    today = date.today()
    current_week = today.isocalendar()[1]

    return {
        "current_planning_year": year_obj,
        "current_week": current_week,
        "today": today,
        "crop_colors": CROP_TYPE_COLORS,
    }
