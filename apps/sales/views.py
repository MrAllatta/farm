"""sales/views.py"""

from django.views.generic import TemplateView, FormView
from django.shortcuts import redirect
from django.urls import reverse
from django.contrib import messages
from django.db.models import Sum
from django.http import HttpResponse
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .models import SalesEvent, QuickSalesEntry
from reference.models import SalesChannel, CropSalesFormat
from operations.models import PackAllocation


class MarketSalesEntryView(TemplateView):
    """Record sales for a market day — quick or detailed mode."""

    template_name = "sales/market_entry.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # Find the most recent or upcoming market day
        today = date.today()
        channels = SalesChannel.objects.all()

        # Determine which channel and date we're recording for
        channel_id = self.request.GET.get("channel")
        sale_date_str = self.request.GET.get("date")

        if channel_id:
            channel = SalesChannel.objects.get(id=channel_id)
        else:
            # Default to first channel with a market day near today
            channel = channels.first()

        if sale_date_str:
            sale_date = date.fromisoformat(sale_date_str)
        else:
            sale_date = today

        # Check if quick entry already exists
        quick_entry = QuickSalesEntry.objects.filter(
            channel=channel,
            sale_date=sale_date,
        ).first()

        # Check if detailed entries exist
        detailed_entries = SalesEvent.objects.filter(
            entry_kind=SalesEvent.EntryKind.ACTUAL,
            channel=channel,
            sale_date=sale_date,
        ).select_related("product", "product__crop")

        # Get pack allocations for this channel/date (what was brought)
        pack_list = PackAllocation.objects.filter(
            channel=channel,
            pack_date=sale_date,
        ).select_related("product", "product__crop")

        # If no pack list, get all active sales formats as template
        if not pack_list.exists():
            products = (
                CropSalesFormat.objects.filter(is_active=True)
                .select_related("crop")
                .order_by("crop__crop_type", "crop__name")
            )
        else:
            products = None

        # Build entry list
        entry_items = []

        if pack_list.exists():
            # Use pack list as the template
            for pa in pack_list:
                existing = detailed_entries.filter(product=pa.product).first()
                entry_items.append(
                    {
                        "product": pa.product,
                        "brought": pa.quantity,
                        "existing_sold": existing.actual_quantity if existing else None,
                        "existing_revenue": existing.actual_revenue if existing else None,
                        "existing_returned": existing.returned_quantity if existing else None,
                    }
                )
        elif products:
            # Use all products as template
            for product in products:
                existing = detailed_entries.filter(product=product).first()
                entry_items.append(
                    {
                        "product": product,
                        "brought": None,
                        "existing_sold": existing.actual_quantity if existing else None,
                        "existing_revenue": existing.actual_revenue if existing else None,
                        "existing_returned": existing.returned_quantity if existing else None,
                    }
                )

        # Weekly target for this channel
        current_week = sale_date.isocalendar()[1]
        is_active_week = channel.start_week <= current_week <= channel.end_week

        ctx.update(
            {
                "channels": channels,
                "channel": channel,
                "sale_date": sale_date,
                "quick_entry": quick_entry,
                "detailed_entries": detailed_entries,
                "entry_items": entry_items,
                "has_pack_list": pack_list.exists(),
                "weekly_target": channel.weekly_target if is_active_week else 0,
                # For date navigation
                "prev_date": sale_date - timedelta(days=7),
                "next_date": sale_date + timedelta(days=7),
            }
        )
        return ctx

    def post(self, request, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not request.user.is_staff:
            return HttpResponse(status=403)
        channel_id = request.POST.get("channel_id")
        sale_date_raw = request.POST.get("sale_date")
        entry_mode = request.POST.get("mode", "quick")
        if not channel_id or not sale_date_raw:
            return HttpResponse(status=400)

        try:
            sale_date = date.fromisoformat(sale_date_raw)
            channel = SalesChannel.objects.get(id=channel_id)
        except (TypeError, ValueError, SalesChannel.DoesNotExist):
            return HttpResponse(status=400)

        if entry_mode == "quick":
            return self._save_quick(request, channel, sale_date)
        else:
            return self._save_detailed(request, channel, sale_date)

    def _save_quick(self, request, channel, sale_date):
        try:
            total_cash = Decimal(request.POST.get("total_cash", "0") or "0")
            total_card = Decimal(request.POST.get("total_card", "0") or "0")
        except (TypeError, InvalidOperation):
            return HttpResponse(status=400)
        notes = request.POST.get("notes", "")

        QuickSalesEntry.objects.update_or_create(
            channel=channel,
            sale_date=sale_date,
            defaults={
                "total_cash": total_cash,
                "total_card": total_card,
                "notes": notes,
            },
        )

        total = total_cash + total_card
        messages.success(
            request,
            f"Recorded: {channel.name} {sale_date.strftime('%b %d')} — "
            f"${total:,.0f} total (${total_cash:,.0f} cash + "
            f"${total_card:,.0f} card)",
        )

        return redirect(
            f"{reverse('sales:market_entry')}" f"?channel={channel.id}&date={sale_date.isoformat()}"
        )

    def _save_detailed(self, request, channel, sale_date):
        updated = 0
        total_revenue = Decimal("0")

        for key, value in request.POST.items():
            if key.startswith("sold_") and value:
                product_id = key.replace("sold_", "")

                try:
                    product = CropSalesFormat.objects.get(id=product_id)
                    sold_qty = Decimal(value)
                except (CropSalesFormat.DoesNotExist, ValueError, InvalidOperation):
                    continue

                # Get price — use actual price if overridden
                price_key = f"price_{product_id}"
                if price_key in request.POST and request.POST[price_key]:
                    try:
                        actual_price = Decimal(request.POST[price_key])
                    except (ValueError, InvalidOperation):
                        actual_price = product.sale_price
                else:
                    actual_price = product.sale_price

                revenue = sold_qty * actual_price

                brought_key = f"brought_{product_id}"
                brought_qty = None
                if brought_key in request.POST and request.POST[brought_key]:
                    try:
                        brought_qty = Decimal(request.POST[brought_key])
                    except (ValueError, InvalidOperation):
                        pass

                returned_qty = None
                if brought_qty is not None:
                    returned_qty = max(Decimal("0"), brought_qty - sold_qty)

                notes_key = f"notes_{product_id}"
                notes = request.POST.get(notes_key, "")
                pack_batch = (
                    PackAllocation.objects.filter(
                        channel=channel,
                        pack_date=sale_date,
                        product=product,
                        pack_batch__isnull=False,
                    )
                    .select_related("pack_batch")
                    .order_by("-id")
                    .values_list("pack_batch_id", flat=True)
                    .first()
                )

                SalesEvent.objects.update_or_create(
                    entry_kind=SalesEvent.EntryKind.ACTUAL,
                    channel=channel,
                    sale_date=sale_date,
                    product=product,
                    defaults={
                        "actual_quantity": sold_qty,
                        "actual_revenue": revenue,
                        "actual_price": actual_price,
                        "brought_quantity": brought_qty,
                        "returned_quantity": returned_qty,
                        "pack_batch_id": pack_batch,
                        "notes": notes,
                    },
                )

                total_revenue += revenue
                updated += 1

        messages.success(
            request,
            f"Recorded: {channel.name} {sale_date.strftime('%b %d')} — "
            f"{updated} products, ${total_revenue:,.0f} total revenue",
        )

        return redirect(
            f"{reverse('sales:market_entry')}" f"?channel={channel.id}&date={sale_date.isoformat()}"
        )
