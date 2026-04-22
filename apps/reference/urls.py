"""reference/urls.py"""

from django.urls import path

from . import views

app_name = "reference"

urlpatterns = [
    path("", views.ReferenceHomeView.as_view(), name="index"),
    path("mixes/", views.RecipeProductListView.as_view(), name="mix_list"),
    path(
        "mixes/product/<int:product_id>/",
        views.ProductRecipeEditView.as_view(),
        name="mix_edit",
    ),
    path(
        "mixes/product/<int:product_id>/deactivate/",
        views.ProductRecipeDeactivateView.as_view(),
        name="mix_deactivate",
    ),
]
