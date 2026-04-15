"""planning.urls"""

from django.urls import path
from . import views

app_name = "planning"

urlpatterns = [
    # Main matrix
    path("", views.PlanningMatrixView.as_view(), name="matrix"),
    path("date/<str:date>/", views.PlanningMatrixView.as_view(), name="matrix_date"),
    path("week/<int:week>/", views.PlanningMatrixView.as_view(), name="matrix_week"),
    # Planting CRUD
    path("planting/new/", views.PlantingCreateView.as_view(), name="planting_create"),
    path(
        "planting/new/block/<int:block_id>/week/<int:week>/",
        views.PlantingCreateView.as_view(),
        name="planting_create_prefilled",
    ),
    path("planting/<int:pk>/", views.PlantingDetailView.as_view(), name="planting_detail"),
    path("planting/<int:pk>/edit/", views.PlantingUpdateView.as_view(), name="planting_edit"),
    path("planting/<int:pk>/revise/", views.PlantingReviseView.as_view(), name="planting_revise"),
    path(
        "planting/<int:pk>/status/",
        views.PlantingStatusUpdateView.as_view(),
        name="planting_status",
    ),
    # Succession
    path("succession/new/", views.SuccessionCreateView.as_view(), name="succession_create"),
    path("succession/preview", views.SuccessionPreviewView.as_view(), name="succession_preview"),
    # Schedules
    path("nursery/", views.NurseryScheduleView.as_view(), name="nursery_schedule"),
    path("nursery/week/<int:week>/", views.NurseryScheduleView.as_view(), name="nursery_week"),
    path("harvest-calendar/", views.HarvestCalendarView.as_view(), name="harvest_calendar"),
    path("field-schedule/", views.FieldScheduleView.as_view(), name="field_schedule"),
    # HTMX helpers
    path(
        "htmx/crop-season-options/",
        views.CropSeasonOptionsView.as_view(),
        name="crop_season_options",
    ),
    path("htmx/harvest-dates/", views.HarvestDateCalcView.as_view(), name="harvest_date_calc"),
    path("htmx/bedfeet/", views.BedfeetCalcView.as_view(), name="bedfeet_calc"),
    path("htmx/week-to-date/", views.WeekToDateView.as_view(), name="week_to_date"),
    path("htmx/bed-conflicts/", views.BedConflictCheckView.as_view(), name="bed_conflict_check"),
    path(
        "htmx/planting-detail/<int:pk>/",
        views.PlantingDetailView.as_view(),
        name="planting_detail_htmx",
    ),
]
