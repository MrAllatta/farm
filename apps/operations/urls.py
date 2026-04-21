"""operations/urls.py"""

from django.urls import path
from . import views

app_name = "operations"

urlpatterns = [
    # Unified week-ops (canonical)
    path("week/<int:week>/walk/", views.FieldWalkView.as_view(), name="weekops_walk"),
    path("week/<int:week>/needs/", views.HarvestNeedsView.as_view(), name="weekops_needs"),
    path("week/<int:week>/record/", views.WeeklyHarvestEntryView.as_view(), name="weekops_record"),
    path("field-walk/print/<int:week>/", views.FieldWalkPrintView.as_view(), name="field_walk_print"),
    path(
        "harvest-needs/print/<int:week>/",
        views.HarvestNeedsPrintView.as_view(),
        name="harvest_needs_print",
    ),
    # Harvest entry (aliases + redirects)
    path("harvest/", views.HarvestEntryCurrentRedirect.as_view(), name="harvest_entry_current"),
    path(
        "harvest/week/<int:week>/",
        views.WeeklyHarvestEntryView.as_view(),
        name="harvest_entry_week",
    ),
    path(
        "harvest/planting/<int:pk>/", views.PlantingHarvestEntryView.as_view(), name="harvest_entry"
    ),
    # Field walk (aliases + redirects)
    path("field-walk/", views.FieldWalkCurrentRedirect.as_view(), name="field_walk_current"),
    path("field-walk/planting/<int:pk>/", views.FieldWalkNoteView.as_view(), name="field_walk"),
    # Inventory
    path("inventory/", views.InventoryDashboardView.as_view(), name="inventory"),
    path("inventory/add/", views.InventoryTransactionView.as_view(), name="inventory_add"),
    path(
        "inventory/harvest-in/<int:harvest_event_id>/",
        views.InventoryHarvestInView.as_view(),
        name="inventory_harvest_in",
    ),
    path("planting/<int:pk>/record/", views.PlantingRecordView.as_view(), name="planting_record"),
    path("harvest-needs/", views.HarvestNeedsCurrentRedirect.as_view(), name="harvest_needs_current"),
    path("harvest-needs/week/<int:week>/", views.HarvestNeedsView.as_view(), name="harvest_needs_week"),
    path("missing-plantings/", views.MissingPlantingsView.as_view(), name="missing_plantings"),
    path("print/planting-list/", views.PrintablePlantingListView.as_view(), name="planting_list_print"),
    path("print/seeding-todo/", views.PrintableSeedingTodoView.as_view(), name="seeding_todo_print"),
    path("pack/prep/", views.PackPrepView.as_view(), name="pack_prep"),
    path("pack/record/", views.PackBatchRecordView.as_view(), name="pack_batch_record"),
]
