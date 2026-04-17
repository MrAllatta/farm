"""core/urls.py"""

from django.urls import path
from . import views

app_name = "core"

urlpatterns = [
    path(
        "planning-year/focus/",
        views.PlanningYearFocusView.as_view(),
        name="planning_year_focus",
    ),
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("clone/<int:source_year>/", views.ClonePlanUIView.as_view(), name="clone_plan_ui"),
    path("complete-season/", views.CompleteSeasonView.as_view(), name="complete_season"),
]
