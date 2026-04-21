"""Append InventoryLedger rows with correct running balances (post-harvest loop)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from operations.models import InventoryLedger


def append_ledger_entry(
    crop,
    event_date: date,
    event_type: str,
    quantity: Decimal,
    *,
    harvest_event=None,
    notes: str = "",
) -> InventoryLedger:
    last = (
        InventoryLedger.objects.filter(crop=crop)
        .order_by("-event_date", "-created_at", "-id")
        .first()
    )
    prev_balance = last.running_balance if last else Decimal("0")
    running_balance = prev_balance + quantity
    return InventoryLedger.objects.create(
        crop=crop,
        harvest_event=harvest_event,
        event_date=event_date,
        event_type=event_type,
        quantity=quantity,
        running_balance=running_balance,
        notes=notes,
    )


def sync_harvest_event_ledger(harvest_event, old_actual_quantity) -> None:
    """Mirror HarvestEvent.actual_quantity changes into harvest_in / adjustment rows."""
    new_qty = harvest_event.actual_quantity
    if new_qty is None:
        return

    crop = harvest_event.planting.crop
    event_date = harvest_event.actual_date or date.today()

    old = old_actual_quantity if old_actual_quantity is not None else None
    if old is None:
        append_ledger_entry(
            crop,
            event_date,
            "harvest_in",
            abs(new_qty),
            harvest_event=harvest_event,
            notes=f"AUTO:harvest_in:harvest_event={harvest_event.pk}",
        )
        return

    delta = new_qty - old
    if delta == 0:
        return
    if delta > 0:
        append_ledger_entry(
            crop,
            event_date,
            "harvest_in",
            delta,
            harvest_event=harvest_event,
            notes=f"AUTO:harvest_in_delta:harvest_event={harvest_event.pk}",
        )
    else:
        append_ledger_entry(
            crop,
            event_date,
            "adjustment",
            delta,
            harvest_event=harvest_event,
            notes=f"AUTO:harvest_correction:harvest_event={harvest_event.pk}",
        )


def sync_sales_event_ledger(sales_event, old_actual_quantity, old_returned_quantity) -> None:
    """Mirror SalesEvent actual sold + returns into sale_out / return_in / adjustment rows."""
    if sales_event.entry_kind != sales_event.EntryKind.ACTUAL:
        return
    if sales_event.product_id is None:
        return

    crop = sales_event.product.crop
    event_date = sales_event.sale_date

    new_actual = sales_event.actual_quantity
    new_returned = sales_event.returned_quantity

    old_a = old_actual_quantity if old_actual_quantity is not None else Decimal("0")
    old_r = old_returned_quantity if old_returned_quantity is not None else Decimal("0")

    if new_actual is not None:
        delta_sold = new_actual - old_a
        if delta_sold > 0:
            append_ledger_entry(
                crop,
                event_date,
                "sale_out",
                -abs(delta_sold),
                notes=f"AUTO:sale_out_delta:sales_event={sales_event.pk}",
            )
        elif delta_sold < 0:
            append_ledger_entry(
                crop,
                event_date,
                "adjustment",
                abs(delta_sold),
                notes=f"AUTO:sale_correction:sales_event={sales_event.pk}",
            )

    if new_returned is not None:
        delta_ret = new_returned - old_r
        if delta_ret > 0:
            append_ledger_entry(
                crop,
                event_date,
                "return_in",
                abs(delta_ret),
                notes=f"AUTO:return_in_delta:sales_event={sales_event.pk}",
            )
        elif delta_ret < 0:
            append_ledger_entry(
                crop,
                event_date,
                "adjustment",
                -abs(delta_ret),
                notes=f"AUTO:return_correction:sales_event={sales_event.pk}",
            )
