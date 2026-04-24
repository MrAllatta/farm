"""Reference app views."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import ListView, TemplateView

from planning.models import PlanningYear
from reference.forms import ProductRecipeForm, make_product_recipe_component_formset
from reference.models import CropSalesFormat, ProductRecipe


class ReferenceHomeView(TemplateView):
    """Reference index (links to staff tools and admin)."""

    template_name = "reference/index.html"


class StaffReferenceMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Staff-only reference mutations (prototype gate)."""

    raise_exception = True

    def handle_no_permission(self):
        # With raise_exception=True, AccessMixin would return 403 for anonymous users.
        # Redirect unauthenticated users to login; keep 403 for authenticated non-staff.
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                self.get_login_url(),
                self.get_redirect_field_name(),
            )
        raise PermissionDenied(self.get_permission_denied_message())

    def test_func(self):
        u = self.request.user
        return u.is_authenticated and u.is_staff


class RecipeProductListView(StaffReferenceMixin, ListView):
    """Active sellable products with recipe summary (mix editor entry)."""

    model = CropSalesFormat
    template_name = "reference/mixes/list.html"
    context_object_name = "products"

    def get_queryset(self):
        active_output_sq = ProductRecipe.objects.filter(
            product_id=OuterRef("pk"),
            is_active=True,
        ).order_by("-planning_year__year", "-id").values("output_unit")[:1]
        return (
            CropSalesFormat.objects.filter(is_active=True)
            .select_related("crop")
            .annotate(
                active_recipe_count=Count("recipes", filter=Q(recipes__is_active=True)),
                component_count=Count("recipes__components", filter=Q(recipes__is_active=True)),
                active_output_unit=Subquery(active_output_sq),
            )
            .order_by("crop__name", "product_name")
        )


class ProductRecipeEditView(StaffReferenceMixin, View):
    """Create or edit the active ProductRecipe and its components for one product."""

    template_name = "reference/mixes/edit.html"

    def get_product(self):
        return get_object_or_404(
            CropSalesFormat.objects.select_related("crop"),
            pk=self.kwargs["product_id"],
            is_active=True,
        )

    def get(self, request, *args, **kwargs):
        product = self.get_product()
        recipe = (
            ProductRecipe.objects.filter(product=product, is_active=True)
            .order_by("-planning_year__year", "-id")
            .first()
        )
        if recipe is None:
            recipe = ProductRecipe(
                product=product,
                name=f"{product.product_name} mix",
                is_active=True,
            )
        FormSet = make_product_recipe_component_formset()
        form = ProductRecipeForm(instance=recipe)
        formset = FormSet(instance=recipe)
        return self._render(request, product, form, formset)

    def post(self, request, *args, **kwargs):
        product = self.get_product()
        recipe = (
            ProductRecipe.objects.filter(product=product, is_active=True)
            .order_by("-planning_year__year", "-id")
            .first()
        )
        if recipe is None:
            recipe = ProductRecipe(
                product=product,
                name=f"{product.product_name} mix",
                is_active=True,
            )
        FormSet = make_product_recipe_component_formset()
        form = ProductRecipeForm(request.POST, instance=recipe)
        formset = FormSet(request.POST, instance=recipe)
        if form.is_valid() and formset.is_valid():
            try:
                with transaction.atomic():
                    recipe_obj = form.save(commit=False)
                    recipe_obj.product = product
                    if recipe_obj.planning_year_id is None:
                        py, _ = PlanningYear.objects.get_or_create(
                            year=date.today().year,
                            defaults={
                                "status": "planning",
                                "overplant_factor": Decimal("1.10"),
                            },
                        )
                        recipe_obj.planning_year = py
                    recipe_obj.save()
                    formset.instance = recipe_obj
                    formset.save()
                    recipe_obj.refresh_from_db()
                    recipe_obj.validate_component_totals()
            except ValidationError as exc:
                if exc.error_dict:
                    for field, error_list in exc.error_dict.items():
                        for err in error_list:
                            form.add_error(field, err)
                else:
                    for message in exc.messages:
                        form.add_error(None, message)
            else:
                messages.success(
                    request,
                    f"Saved recipe for {product.product_name}.",
                )
                return redirect(reverse("reference:mix_list"))

        return self._render(request, product, form, formset)

    def _render(self, request, product, form, formset):
        return render(
            request,
            self.template_name,
            {
                "product": product,
                "form": form,
                "formset": formset,
            },
        )


class ProductRecipeDeactivateView(StaffReferenceMixin, View):
    """POST: deactivate the active recipe for a product (keep history)."""

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        product = get_object_or_404(CropSalesFormat, pk=kwargs["product_id"], is_active=True)
        recipe = (
            ProductRecipe.objects.filter(product=product, is_active=True)
            .order_by("-planning_year__year", "-id")
            .first()
        )
        if recipe:
            recipe.is_active = False
            recipe.save(update_fields=["is_active", "updated_at"])
            messages.success(request, f"Deactivated recipe for {product.product_name}.")
        else:
            messages.info(request, "No active recipe for this product.")
        return redirect(reverse("reference:mix_list"))
