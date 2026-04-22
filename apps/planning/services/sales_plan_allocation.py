"""Split category-level plan totals across outlet channels."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def even_split_sale_units(total: Decimal, n: int) -> list[Decimal]:
    """Divide ``total`` sale-units across ``n`` channels as evenly as possible.

    Uses cent rounding with remainder assigned to the first outlets so sums match ``total``.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    total = Decimal(total)
    if total == 0:
        return [Decimal("0")] * n
    cents = int((total * 100).to_integral_value(rounding=ROUND_HALF_UP))
    base = cents // n
    rem = cents % n
    return [Decimal(base + (1 if i < rem else 0)) / Decimal(100) for i in range(n)]
