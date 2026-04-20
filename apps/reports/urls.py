"""reports/urls.py"""

from django.urls import path
from . import views

app_name = "reports"

urlpatterns = [
    # Crop map
    path("crop-map/", views.CropMapView.as_view(), name="crop_map"),
    path("crop-map/week/<int:week>/", views.CropMapView.as_view(), name="crop_map_week"),
    path(
        "crop-map/503/week-by-bed/",
        views.CropMapWeekByBedGridView.as_view(),
        name="crop_map_week_by_bed",
    ),
    path(
        "crop-map/503/week-by-block/",
        views.CropMapWeekByBlockGridView.as_view(),
        name="crop_map_week_by_block",
    ),
    path(
        "crop-map/503/successions/",
        views.CropMapSuccessionsByBlockView.as_view(),
        name="crop_map_successions",
    ),
    # Printable documents
    path(
        "harvest-list/week/<int:week>/",
        views.HarvestListPrintView.as_view(),
        name="harvest_list_print",
    ),
    path("pack-list/week/<int:week>/", views.PackListPrintView.as_view(), name="pack_list_print"),
    path(
        "weekly-schedule/week/<int:week>/",
        views.WeeklySchedulePrintView.as_view(),
        name="weekly_schedule_print",
    ),
    path(
        "crop-map/print/week/<int:week>/", views.CropMapPrintView.as_view(), name="crop_map_print"
    ),
    path(
        "nursery-schedule/print/",
        views.NurserySchedulePrintView.as_view(),
        name="nursery_schedule_print",
    ),
    path("seed-order/", views.SeedOrderReportView.as_view(), name="seed_order"),
    # Analysis
    path("crop-performance/", views.CropPerformanceView.as_view(), name="crop_performance"),
    path(
        "channel-performance/", views.ChannelPerformanceView.as_view(), name="channel_performance"
    ),
    path("block-utilization/", views.BlockUtilizationView.as_view(), name="block_utilization"),
    path("season-summary/", views.SeasonSummaryView.as_view(), name="season_summary"),
    path("plan-vs-actual/", views.PlanVsActualView.as_view(), name="plan_vs_actual"),
    # Export
    path("export/csv/", views.ExportCSVView.as_view(), name="export_csv"),
    path("export/season-archive/", views.ExportArchiveView.as_view(), name="export_archive"),
]
