"""Best-effort scrape of supplier catalog pages for Variety enrichment."""

import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from reference.models import Variety


def scrape_variety_page(url: str, timeout: int = 20) -> dict[str, Any]:
    """
    Fetch HTML and extract title, meta description, and loose price/DTM hints.
    No third-party HTML parser — regex only for portability.
    """
    out: dict[str, Any] = {
        "title": "",
        "description": "",
        "scraped_dtm_days": None,
        "scraped_price": None,
    }
    if not url or not url.startswith(("http://", "https://")):
        return out

    req = Request(url, headers={"User-Agent": "FarmPlanningVarietyScraper/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read(500_000)
    except (URLError, HTTPError, TimeoutError, ValueError):
        return out

    try:
        html = raw.decode("utf-8", errors="ignore")
    except Exception:
        return out

    title_m = re.search(r"<title[^>]*>([^<]{1,300})</title>", html, re.I)
    if title_m:
        out["title"] = title_m.group(1).strip()

    desc_m = re.search(
        r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']{1,500})["\']',
        html,
        re.I,
    )
    if desc_m:
        out["description"] = desc_m.group(1).strip()
    elif out["title"]:
        out["description"] = out["title"]

    dtm_m = re.search(
        r"(?:days?\s*to\s*maturity|DTM|maturity)[^0-9]{0,20}(\d{2,3})\s*(?:day|d)\b",
        html,
        re.I,
    )
    if dtm_m:
        try:
            out["scraped_dtm_days"] = int(dtm_m.group(1))
        except ValueError:
            pass

    price_m = re.search(r"\$\s*(\d+(?:\.\d{2})?)", html)
    if price_m:
        try:
            out["scraped_price"] = Decimal(price_m.group(1))
        except InvalidOperation:
            pass

    return out


def apply_scrape_to_variety(variety: Variety, data: dict[str, Any] | None = None) -> None:
    """Mutate and save variety with scraped fields."""
    from django.utils import timezone

    if data is None:
        data = scrape_variety_page(variety.source_url or "")

    if data.get("description"):
        variety.scraped_description = data["description"][:2000]
    if data.get("scraped_dtm_days") is not None:
        variety.scraped_dtm_days = data["scraped_dtm_days"]
    if data.get("scraped_price") is not None:
        variety.scraped_price = data["scraped_price"]
    variety.scraped_at = timezone.now()
    variety.save(
        update_fields=[
            "scraped_description",
            "scraped_dtm_days",
            "scraped_price",
            "scraped_at",
        ]
    )
