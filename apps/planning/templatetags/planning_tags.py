"""planning/templatetags/planning_tags.py"""

from django import template
from django.utils.safestring import mark_safe
from django.template.defaultfilters import slugify
from core.context_processors import CROP_TYPE_COLORS
from isoweek import Week as IsoWeek
from operations.planting_display import planting_unit_full_label

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Look up dict or list by key/index in template."""
    if isinstance(dictionary, dict):
        return dictionary.get(key)
    if isinstance(dictionary, (list, tuple)):
        try:
            return dictionary[int(key)]
        except (IndexError, ValueError):
            return None
    return None


@register.filter
def prev_item(lst, idx):
    """Return item before index idx in list."""
    try:
        if idx <= 0:
            return None
        return lst[idx - 1]
    except (IndexError, TypeError):
        return None


@register.filter
def crop_css(crop_type):
    """Convert crop type to CSS class name."""
    return f"crop-{slugify(crop_type)}"


@register.filter
def week_num(date_val):
    """Extract ISO week number from a date."""
    if not date_val:
        return ""
    try:
        return date_val.isocalendar()[1]
    except AttributeError:
        return ""


@register.filter
def week_range(first_date, last_date):
    """Format a date range as 'Wk N–M'."""
    if not first_date or not last_date:
        return ""
    w1 = first_date.isocalendar()[1]
    w2 = last_date.isocalendar()[1]
    if w1 == w2:
        return f"Wk {w1}"
    return f"Wk {w1}–{w2}"


@register.filter
def bedfeet_display(bedfeet):
    """Format bedfeet as '3,200 bf'."""
    if bedfeet is None:
        return "—"
    return f"{int(bedfeet):,} bf"


@register.filter
def qty_display(quantity, unit=""):
    """Format quantity with unit."""
    if quantity is None:
        return "—"
    if unit:
        return f"{float(quantity):,.0f} {unit}"
    return f"{float(quantity):,.0f}"


@register.filter
def variance_css(variance_pct):
    """Return CSS class for variance display."""
    if variance_pct is None:
        return ""
    if variance_pct > 10:
        return "positive"
    if variance_pct < -15:
        return "negative"
    return ""


@register.filter
def variance_display(variance_pct):
    """Format variance percentage with sign."""
    if variance_pct is None:
        return "—"
    sign = "+" if variance_pct >= 0 else ""
    return f"{sign}{variance_pct:.0f}%"


@register.filter
def days_display(days):
    """Format days as 'Xd' or 'X weeks'."""
    if days is None:
        return "—"
    days = int(days)
    if abs(days) < 7:
        return f"{days:+d}d"
    weeks = days // 7
    rem = abs(days) % 7
    if rem == 0:
        return f"{weeks:+d}wk"
    return f"{weeks:+d}wk {rem}d"


@register.simple_tag
def render_planting_bar(row, weeks, planning_year=None):
    """Render a planting as cells spanning correct columns in the matrix."""
    cells = []
    week_nums = [w["num"] for w in weeks]
    col_start = row["col_start"]
    col_span = row["col_span"]
    css = row["css_class"]
    planting = row["planting"]
    plan_year = planning_year or planting.planning_year.year

    # Empty cells before planting
    for i in range(col_start):
        wk = week_nums[i]
        cells.append(
            f'<td class="week-cell fallow" '
            f'data-block="{planting.block_id}" '
            f'data-week="{wk}" '
            f'data-block-type="{planting.block.block_type}" '
            f'hx-get="/planning/planting/new/block/{planting.block_id}/week/{wk}/" '
            f'hx-target="#planting-detail" '
            f'hx-swap="innerHTML" '
            f'hx-trigger="click">'
            f"</td>"
        )

    # The planting bar
    crop_css_class = f"crop-{slugify(planting.crop.crop_type)}"
    title = planting_unit_full_label(planting)

    cells.append(
        f'<td class="week-cell planting-bar {css} {crop_css_class}" '
        f'colspan="{col_span}" '
        f'data-planting="{planting.id}" '
        f'data-block-id="{planting.block_id}" '
        f'data-planning-year="{plan_year}" '
        f'draggable="true" '
        f'title="{title}" '
        f'hx-get="/planning/htmx/planting-detail/{planting.id}/" '
        f'hx-target="#planting-detail" '
        f'hx-swap="innerHTML" '
        f'hx-trigger="click">'
        f'<span class="planting-label">{row["label"]}</span>'
        f'<span class="planting-sublabel">{row["sublabel"]}</span>'
        f"</td>"
    )

    # Empty cells after planting
    remaining_start = col_start + col_span
    for i in range(len(week_nums) - remaining_start):
        wk = week_nums[remaining_start + i]
        cells.append(
            f'<td class="week-cell fallow" '
            f'data-block="{planting.block_id}" '
            f'data-week="{wk}" '
            f'data-block-type="{planting.block.block_type}" '
            f'hx-get="/planning/planting/new/block/{planting.block_id}/week/{wk}/" '
            f'hx-target="#planting-detail" '
            f'hx-swap="innerHTML" '
            f'hx-trigger="click">'
            f"</td>"
        )

    return mark_safe("".join(cells))


@register.inclusion_tag("planning/partials/nursery_event_row.html")
def nursery_event_row(event):
    """Render a single nursery event row."""
    return {"event": event}


@register.inclusion_tag("planning/partials/harvest_event_row.html")
def harvest_event_row(event):
    """Render a single harvest event row."""
    return {"event": event}


@register.simple_tag(takes_context=True)
def rotation_badge(context, planting):
    """Render a rotation warning badge if applicable."""
    from core.models import RotationRule, RotationHistory

    family = planting.crop.botanical_family
    if not family:
        return mark_safe("")

    year = planting.planning_year.year

    rule = RotationRule.objects.filter(botanical_family=family).first()
    if not rule:
        return mark_safe("")

    history = (
        RotationHistory.objects.filter(
            block=planting.block,
            botanical_family=family,
            year__gte=year - rule.min_gap_years,
            year__lt=year,
        )
        .order_by("-year")
        .first()
    )

    if not history:
        return mark_safe("")

    gap = year - history.year
    if gap >= rule.min_gap_years:
        return mark_safe("")

    return mark_safe(
        f'<span class="rotation-badge" '
        f'title="{family} in {planting.block.name} {history.year} '
        f'({gap}yr gap, min {rule.min_gap_years}yr)">⚠</span>'
    )


@register.filter
def crop_css_color(crop_type):
    """Return background color for a crop type."""
    return CROP_TYPE_COLORS.get(crop_type, "#f0f0ec")


@register.filter
def week_to_date(week_num, year):
    """Convert week number to Monday date for a given year."""
    try:
        return IsoWeek(int(year), int(week_num)).monday()
    except (ValueError, TypeError):
        return None


@register.filter
def mul(value, arg):
    """Multiply filter for template arithmetic."""
    try:
        return float(value) * float(arg)
    except (ValueError, TypeError):
        return 0


@register.filter
def div(value, arg):
    """Divide filter — returns None on zero division."""
    try:
        arg = float(arg)
        if arg == 0:
            return None
        return float(value) / arg
    except (ValueError, TypeError):
        return None


@register.filter
def pct(value, total):
    """Calculate percentage."""
    try:
        total = float(total)
        if total == 0:
            return 0
        return float(value) / total * 100
    except (ValueError, TypeError):
        return 0


@register.filter
def abs_val(value):
    """Absolute value filter."""
    try:
        return abs(value)
    except (TypeError, ValueError):
        return value


@register.filter
def subtract(value, arg):
    """Subtraction filter."""
    try:
        return float(value) - float(arg)
    except (ValueError, TypeError):
        return 0


@register.simple_tag
def planting_status_badge(planting):
    """Render a colored status badge."""
    colors = {
        "planned": ("#e0e7ff", "#4338ca"),
        "seeded": ("#f0fdf4", "#166534"),
        "planted": ("#d1fae5", "#065f46"),
        "growing": ("#a7f3d0", "#064e3b"),
        "harvesting": ("#fef3c7", "#92400e"),
        "complete": ("#f3f4f6", "#6b7280"),
        "failed": ("#fee2e2", "#991b1b"),
        "skipped": ("#f9fafb", "#9ca3af"),
        "revised": ("#f5f3ff", "#6d28d9"),
    }
    bg, fg = colors.get(planting.status, ("#f3f4f6", "#374151"))
    label = planting.get_status_display()

    return mark_safe(
        f'<span style="background:{bg}; color:{fg}; '
        f"padding:2px 8px; border-radius:4px; "
        f"font-size:0.75rem; font-weight:600; "
        f'text-transform:uppercase; letter-spacing:0.03em;">'
        f"{label}</span>"
    )


@register.simple_tag
def yield_variance_bar(actual, planned, width=100):
    """Render a small inline variance bar."""
    if not actual or not planned:
        return mark_safe('<span style="color:#ccc;">no data</span>')

    try:
        pct_val = float(actual) / float(planned) * 100
    except (ValueError, ZeroDivisionError):
        return mark_safe('<span style="color:#ccc;">—</span>')

    bar_width = min(pct_val, 150)  # cap at 150% for display

    if pct_val >= 100:
        color = "#22c55e"
        label_color = "#166534"
    elif pct_val >= 80:
        color = "#f59e0b"
        label_color = "#92400e"
    else:
        color = "#ef4444"
        label_color = "#991b1b"

    return mark_safe(
        f'<span style="display:inline-flex; align-items:center; gap:4px;">'
        f'<span style="display:inline-block; width:{width}px; height:8px; '
        f'background:#e5e7eb; border-radius:4px; overflow:hidden;">'
        f'<span style="display:block; width:{bar_width:.0f}%; height:100%; '
        f"background:{color}; "
        f'-webkit-print-color-adjust:exact; print-color-adjust:exact;">'
        f"</span></span>"
        f'<span style="color:{label_color}; font-size:0.75rem; font-weight:600;">'
        f"{pct_val:.0f}%</span>"
        f"</span>"
    )
