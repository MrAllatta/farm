"""Reference app tests."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase

from reference.services import variety_scrape as variety_scrape_mod


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
