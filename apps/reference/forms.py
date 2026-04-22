"""Forms for reference data editing (staff-only surfaces)."""

from __future__ import annotations

from decimal import Decimal

from django import forms
from django.forms import BaseInlineFormSet
from django.forms.models import inlineformset_factory

from reference.models import ProductRecipe, ProductRecipeComponent


class ProductRecipeForm(forms.ModelForm):
    class Meta:
        model = ProductRecipe
        fields = ("name", "output_unit", "effective_start", "effective_end", "is_active", "notes")


class ProductRecipeComponentForm(forms.ModelForm):
    class Meta:
        model = ProductRecipeComponent
        fields = (
            "source_crop",
            "source_product",
            "component_quantity",
            "component_unit",
            "component_percent",
            "sort_order",
            "notes",
        )


class ProductRecipeComponentFormSet(BaseInlineFormSet):
    """Inline formset with empty-row skipping and percent-total checks."""

    def clean(self):
        super().clean()
        if any(self.errors):
            return

        non_deleted_forms = [
            f
            for f in self.forms
            if not self.can_delete or not f.cleaned_data.get("DELETE", False)
        ]
        filled = []
        for f in non_deleted_forms:
            if not f.cleaned_data:
                continue
            # Skip forms that are entirely empty (extra blank rows)
            has_crop = bool(f.cleaned_data.get("source_crop"))
            has_product = bool(f.cleaned_data.get("source_product"))
            qty = f.cleaned_data.get("component_quantity")
            unit = (f.cleaned_data.get("component_unit") or "").strip()
            if not has_crop and not has_product and qty in (None, "") and not unit:
                continue
            filled.append(f)

        if not filled:
            raise forms.ValidationError("Add at least one recipe component.")

        percent_values = []
        all_have_percent = True
        for f in filled:
            pct = f.cleaned_data.get("component_percent")
            if pct is None:
                all_have_percent = False
            else:
                percent_values.append(pct)

        if all_have_percent and len(filled) == len(percent_values):
            total = sum(percent_values, Decimal("0"))
            if abs(total - Decimal("100.00")) > Decimal("0.01"):
                raise forms.ValidationError(
                    f"When all components use percentages, they must sum to 100.00 (got {total})."
                )


def make_product_recipe_component_formset():
    return inlineformset_factory(
        ProductRecipe,
        ProductRecipeComponent,
        form=ProductRecipeComponentForm,
        formset=ProductRecipeComponentFormSet,
        extra=3,
        can_delete=True,
    )
