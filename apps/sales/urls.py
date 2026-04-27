"""sales/urls.py"""

from django.urls import path
from . import views

app_name = "sales"

urlpatterns = [
    path(
        "weekly-order/channel/<int:channel_id>/product/<int:product_id>/year-summary/",
        views.ProductYearlyActualsSummaryView.as_view(),
        name="product_yearly_actuals_summary",
    ),
    path(
        "weekly-order/channel/<int:channel_id>/product/<int:product_id>/week/<int:week>/prior-context/",
        views.ProductPriorYearNeighborsView.as_view(),
        name="product_prior_year_neighbors",
    ),
    path(
        "weekly-order/channel/<int:channel_id>/week/<int:week>/",
        views.WeeklyChannelOrderView.as_view(),
        name="weekly_channel_order",
    ),
    path("", views.MarketSalesEntryView.as_view(), name="market_entry"),
    path(
        "channel/<int:channel_id>/",
        views.MarketSalesEntryView.as_view(),
        name="market_entry_channel",
    ),
    path(
        "channel/<int:channel_id>/date/<str:sale_date>/",
        views.MarketSalesEntryView.as_view(),
        name="market_entry_date",
    ),
    path(
        "channel/<int:channel_id>/week/<int:week>/print/",
        views.MarketListPrintView.as_view(),
        name="market_list_print",
    ),
]
