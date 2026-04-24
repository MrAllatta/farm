"""sales/urls.py"""

from django.urls import path
from . import views

app_name = "sales"

urlpatterns = [
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
