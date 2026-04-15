"""reference/urls.py"""

from django.urls import path

from . import views

app_name = "reference"

urlpatterns = [
    path("", views.ReferenceHomeView.as_view(), name="index"),
]
