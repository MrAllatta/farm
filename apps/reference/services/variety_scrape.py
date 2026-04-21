"""Best-effort scrape of supplier catalog pages for Variety enrichment."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from reference.models import Variety


def _empty_scrape_dict() -> dict[str, Any]:
    return {
        "title": "",
        "description": "",
        "scraped_dtm_days": None,
        "scraped_price": None,
    }


def _parse_html_generic(html: str) -> dict[str, Any]:
    """Title, meta description, and loose price/DTM hints (regex only)."""
    out = _empty_scrape_dict()

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


def _extract_dtm_from_text(text: str) -> int | None:
    if not text:
        return None
    dtm_m = re.search(
        r"(?:days?\s*to\s*maturity|DTM|maturity)[^0-9]{0,20}(\d{2,3})\s*(?:day|d)\b",
        text,
        re.I,
    )
    if dtm_m:
        try:
            return int(dtm_m.group(1))
        except ValueError:
            return None
    return None


def _parse_johnnyseeds(html: str) -> dict[str, Any]:
    """
    Johnny's Selected Seeds product pages often expose schema.org Product in JSON-LD.
    """
    out = _empty_scrape_dict()
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    ):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            types = item.get("@type")
            type_ok = types in ("Product", "IndividualProduct")
            if isinstance(types, list):
                type_ok = bool(set(types) & {"Product", "IndividualProduct"})
            if not type_ok:
                continue
            name = item.get("name")
            if name and not out["title"]:
                out["title"] = str(name).strip()[:300]
            desc = item.get("description")
            if desc and not out["description"]:
                plain = re.sub(r"<[^>]+>", " ", str(desc))
                plain = re.sub(r"\s+", " ", plain).strip()
                out["description"] = plain[:2000]
            offers = item.get("offers")
            if isinstance(offers, dict) and out["scraped_price"] is None:
                price = offers.get("price") or offers.get("lowPrice")
                if price is not None:
                    try:
                        out["scraped_price"] = Decimal(str(price))
                    except (InvalidOperation, TypeError):
                        pass
            if out["description"] or out["title"]:
                break
        if out["description"] or out["title"]:
            break

    blob = (out["description"] or "") + html[:50_000]
    dtm = _extract_dtm_from_text(blob)
    if dtm is not None:
        out["scraped_dtm_days"] = dtm

    return out


def _parse_supplier_by_host(host: str, html: str) -> dict[str, Any]:
    h = host.lower()
    if h == "www.johnnyseeds.com" or h.endswith(".johnnyseeds.com"):
        return _parse_johnnyseeds(html)
    return _empty_scrape_dict()


def _merge_scrape_results(primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    out = _empty_scrape_dict()
    for key in out:
        pv, fv = primary.get(key), fallback.get(key)
        if key in ("scraped_dtm_days", "scraped_price"):
            out[key] = pv if pv is not None else fv
        else:
            out[key] = pv or fv or ""
    return out


def scrape_variety_page(url: str, timeout: int = 20) -> dict[str, Any]:
    """
    Fetch HTML and extract catalog hints. Host-specific parsers run first;
    generic regex fill covers unknown suppliers.
    """
    empty = _empty_scrape_dict()
    if not url or not url.startswith(("http://", "https://")):
        return empty

    req = Request(url, headers={"User-Agent": "FarmPlanningVarietyScraper/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read(500_000)
    except (URLError, HTTPError, TimeoutError, ValueError):
        return empty

    try:
        html = raw.decode("utf-8", errors="ignore")
    except Exception:
        return empty

    host = urlparse(url).netloc or ""
    specialized = _parse_supplier_by_host(host, html)
    generic = _parse_html_generic(html)
    return _merge_scrape_results(specialized, generic)


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
