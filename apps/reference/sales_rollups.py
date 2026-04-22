"""Rollup-level sales planning constants (301 workbook semantics).

Annual product-week grids attach to pseudo-channels that represent **Markets**, **Orders**,
or **CSA** planning surfaces — not individual outlets (KFM, BFM, …). Per-outlet weekly plans
use normal ``SalesChannel`` rows and the ``planning:sales_plan_by_channel`` view.
"""

from __future__ import annotations

from .models import SalesCategory

# Importer seeds these when ``year_*/product_week_plan.csv`` exists.
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

DEFAULT_ROLLUP_SLUG = "markets"

ROLLUP_TAB_LABELS: dict[str, str] = {
    "markets": "Markets",
    "orders": "Orders",
    "csa": "CSA",
}
