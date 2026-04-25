"""Rollup-level sales planning constants (301 workbook semantics).

Annual product-week grids align to **SalesCategory** (Markets, Orders, CSA). Category-only
plan rows (workbook 302) use ``SalesEvent.sales_category`` with ``channel`` null.

Legacy ``product_week_plan`` imports may still attach to pseudo-channels named
``Markets (annual plan)``, etc.; ``plan_events_without_shadowed_rollups`` hides those when
operational outlet plan rows exist for the same category slice.
"""

from __future__ import annotations

from datetime import date

from .models import SalesCategory

# Importer seeds these when ``year_*/product_week_plan.csv`` exists (legacy 301 grids).
ANNUAL_PLAN_SALES_CHANNELS = (
    ("Markets (annual plan)", SalesCategory.CategoryName.MARKETS),
    ("Orders (annual plan)", SalesCategory.CategoryName.ORDERS),
    ("CSA (annual plan)", SalesCategory.CategoryName.CSA),
)

ROLLUP_SLUG_TO_CHANNEL_NAME: dict[str, str] = {
    "markets": "Markets (annual plan)",
    "orders": "Orders (annual plan)",
    "csa": "CSA (annual plan)",
}

ROLLUP_SLUG_TO_CATEGORY_NAME: dict[str, str] = {
    "markets": SalesCategory.CategoryName.MARKETS,
    "orders": SalesCategory.CategoryName.ORDERS,
    "csa": SalesCategory.CategoryName.CSA,
}

DEFAULT_ROLLUP_SLUG = "markets"

ROLLUP_TAB_LABELS: dict[str, str] = {
    "markets": "Markets",
    "orders": "Orders",
    "csa": "CSA",
}

ROLLUP_PLAN_CHANNEL_NAMES: frozenset[str] = frozenset(ROLLUP_SLUG_TO_CHANNEL_NAME.values())


def plan_events_without_shadowed_rollups(events):
    """Drop rollup-level plan rows when outlet-level plan rows own that category slice.

    Rollup-level means: pseudo ``SalesChannel`` name in ``ROLLUP_PLAN_CHANNEL_NAMES``, or
    a category-only row (``channel`` null, ``sales_category`` set).
    """
    rows = list(events)
    ops_coverage = set()
    for row in rows:
        if row.entry_kind != "plan":
            continue
        ch = row.channel
        if ch is None:
            continue
        if ch.name in ROLLUP_PLAN_CHANNEL_NAMES:
            continue
        cid = getattr(ch, "category_id", None)
        if not cid:
            continue
        wk = row.sale_date.isocalendar()[1]
        ops_coverage.add((row.product_id, wk, cid))

    out = []
    for row in rows:
        if row.entry_kind != "plan":
            out.append(row)
            continue
        wk = row.sale_date.isocalendar()[1]
        ch = row.channel

        if ch and ch.name in ROLLUP_PLAN_CHANNEL_NAMES:
            cid = getattr(ch, "category_id", None)
            if cid and (row.product_id, wk, cid) in ops_coverage:
                continue
            out.append(row)
            continue

        if ch is None and row.sales_category_id:
            cid = row.sales_category_id
            if (row.product_id, wk, cid) in ops_coverage:
                continue
            out.append(row)
            continue

        out.append(row)
    return out


def plan_week_iso_counts(
    events,
    iso_week: int,
    *,
    week_start: date | None = None,
    week_end: date | None = None,
) -> tuple[int, int]:
    """Count PLAN ``SalesEvent`` rows for the weekly-order window before vs after rollup shadowing.

    When ``week_start`` and ``week_end`` are set (inclusive Monday–Sunday for the viewed week),
    rows are counted by **calendar window** — same rule as per-channel PLAN filters and
    all-channel demand (LIVE-3). Otherwise falls back to ``sale_date.isocalendar()[1] == iso_week``
    for callers that only have a week index.
    """

    def in_target_week(row) -> bool:
        sd = row.sale_date
        if week_start is not None and week_end is not None:
            return week_start <= sd <= week_end
        return sd.isocalendar()[1] == iso_week

    raw = 0
    for row in events:
        if getattr(row, "entry_kind", None) != "plan":
            continue
        if not in_target_week(row):
            continue
        raw += 1
    filtered = plan_events_without_shadowed_rollups(events)
    visible = 0
    for row in filtered:
        if getattr(row, "entry_kind", None) != "plan":
            continue
        if not in_target_week(row):
            continue
        visible += 1
    return raw, visible
