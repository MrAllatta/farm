"""Reference app tests."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from reference.models import CropInfo, CropSalesFormat, ProductRecipe
from reference.services import variety_scrape as variety_scrape_mod

FS_PREFIX = "components"


class JohnnySeedsVarietyScrapeTests(SimpleTestCase):
    def test_parse_product_json_ld(self):
        html = """
        <html><head><title>Ignored</title></head><body>
        <script type="application/ld+json">
        {"@type": "Product", "name": "Cherry Tomato", "description": "About 68 days to maturity in the field.",
         "offers": {"@type": "Offer", "price": "4.95", "priceCurrency": "USD"}}
        </script>
        </body></html>
        """
        out = variety_scrape_mod._parse_johnnyseeds(html)
        self.assertEqual(out["scraped_price"], Decimal("4.95"))
        self.assertIn("68", out["description"] + (str(out.get("scraped_dtm_days") or "")))
        merged = variety_scrape_mod._merge_scrape_results(
            out, variety_scrape_mod._parse_html_generic(html)
        )
        self.assertEqual(merged["scraped_price"], Decimal("4.95"))

    @patch("reference.services.variety_scrape.urlopen")
    def test_scrape_variety_page_dispatches_johnny_host(self, mock_urlopen):
        html = b"""<html><script type="application/ld+json">
        {"@type": "Product", "name": "X", "description": "Maturity 55 days from transplant.",
         "offers": {"price": "3.25"}}
        </script></html>"""
        cm = mock_urlopen.return_value.__enter__.return_value
        cm.read.return_value = html
        out = variety_scrape_mod.scrape_variety_page("https://www.johnnyseeds.com/vegetables/tomatoes/p/123")
        self.assertEqual(out["scraped_price"], Decimal("3.25"))
        self.assertIn("55", out.get("description", ""))


class ProductRecipeEditorTests(TestCase):
    """Staff mix recipe UI (handoff-safe)."""

    @classmethod
    def setUpTestData(cls):
        cls.crop_a = CropInfo.objects.create(
            name="Recipe Crop A",
            crop_type="Vegetables",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("1.00"),
        )
        cls.crop_b = CropInfo.objects.create(
            name="Recipe Crop B",
            crop_type="Vegetables",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("1.00"),
        )
        cls.product = CropSalesFormat.objects.create(
            crop=cls.crop_a,
            product_name="Salad Mix Bag",
            sale_price=Decimal("12.00"),
            sale_unit="bag",
            harvest_qty_per_sale_unit=Decimal("1"),
            is_active=True,
        )

    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user(
            username="mix-editor-staff",
            email="staff@example.com",
            password="pw-test-12345",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="mix-editor-viewer",
            email="viewer@example.com",
            password="pw-test-12345",
            is_staff=False,
        )

    def _post_recipe(self, rows: list[dict], recipe_extra: dict | None = None):
        recipe_extra = recipe_extra or {}
        data = {
            "name": recipe_extra.get("name", "Salad mix recipe"),
            "output_unit": recipe_extra.get("output_unit", "bag"),
            "effective_start": "",
            "effective_end": "",
            "is_active": "on",
            "notes": "",
            f"{FS_PREFIX}-TOTAL_FORMS": str(len(rows)),
            f"{FS_PREFIX}-INITIAL_FORMS": "0",
            f"{FS_PREFIX}-MIN_NUM_FORMS": "0",
            f"{FS_PREFIX}-MAX_NUM_FORMS": "1000",
        }
        data.update(recipe_extra)
        for i, row in enumerate(rows):
            data[f"{FS_PREFIX}-{i}-source_crop"] = str(row.get("source_crop") or "")
            data[f"{FS_PREFIX}-{i}-source_product"] = str(row.get("source_product") or "")
            data[f"{FS_PREFIX}-{i}-component_quantity"] = row["qty"]
            data[f"{FS_PREFIX}-{i}-component_unit"] = row["unit"]
            data[f"{FS_PREFIX}-{i}-component_percent"] = row.get("percent") or ""
            data[f"{FS_PREFIX}-{i}-sort_order"] = str(row.get("sort_order", i))
            data[f"{FS_PREFIX}-{i}-notes"] = row.get("notes", "")
        return self.client.post(
            reverse("reference:mix_edit", kwargs={"product_id": self.product.pk}),
            data,
        )

    def test_anonymous_redirects_from_mix_list(self):
        res = self.client.get(reverse("reference:mix_list"))
        self.assertEqual(res.status_code, 302)
        self.assertIn("login", res.url)

    def test_non_staff_forbidden(self):
        self.client.force_login(self.viewer)
        res = self.client.get(reverse("reference:mix_list"))
        self.assertEqual(res.status_code, 403)

    def test_staff_sees_product_on_list(self):
        self.client.force_login(self.staff)
        res = self.client.get(reverse("reference:mix_list"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Salad Mix Bag")

    def test_staff_creates_recipe_with_two_crop_components(self):
        self.client.force_login(self.staff)
        res = self._post_recipe(
            [
                {"source_crop": self.crop_a.pk, "qty": "1", "unit": "lb"},
                {"source_crop": self.crop_b.pk, "qty": "1", "unit": "lb"},
            ]
        )
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.url, reverse("reference:mix_list"))
        recipe = ProductRecipe.objects.get(product=self.product, is_active=True)
        self.assertEqual(recipe.components.count(), 2)

    def test_percent_rows_must_sum_to_100(self):
        self.client.force_login(self.staff)
        res = self._post_recipe(
            [
                {
                    "source_crop": self.crop_a.pk,
                    "qty": "1",
                    "unit": "lb",
                    "percent": "33.17",
                },
                {
                    "source_crop": self.crop_b.pk,
                    "qty": "1",
                    "unit": "lb",
                    "percent": "33.17",
                },
                {
                    "source_crop": self.crop_b.pk,
                    "qty": "1",
                    "unit": "lb",
                    "percent": "33.16",
                },
            ]
        )
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "must sum to 100")
        self.assertFalse(ProductRecipe.objects.filter(product=self.product).exists())

    def test_deactivate_then_create_new_active_recipe(self):
        self.client.force_login(self.staff)
        r1 = self._post_recipe([{"source_crop": self.crop_a.pk, "qty": "1", "unit": "lb"}])
        self.assertEqual(r1.status_code, 302)
        first = ProductRecipe.objects.get(product=self.product, is_active=True)
        self.assertEqual(first.name, "Salad mix recipe")

        dec = self.client.post(reverse("reference:mix_deactivate", kwargs={"product_id": self.product.pk}))
        self.assertEqual(dec.status_code, 302)
        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertFalse(ProductRecipe.objects.filter(product=self.product, is_active=True).exists())

        r2 = self._post_recipe(
            [{"source_crop": self.crop_b.pk, "qty": "2", "unit": "lb"}],
            recipe_extra={"name": "Second recipe"},
        )
        self.assertEqual(r2.status_code, 302)
        second = ProductRecipe.objects.get(product=self.product, is_active=True)
        self.assertEqual(second.name, "Second recipe")
        self.assertEqual(second.components.count(), 1)
