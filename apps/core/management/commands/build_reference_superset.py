"""Merge per-year historical reference CSVs into the reference lane bundle.

Reads ``reference/reference/crop_info.csv`` and ``crop_sales_formats.csv`` (current
lane pull) plus optional ``<historical_lane>/year_YYYY/reference/*.csv`` from Stage A2
pulls, then **rewrites** ``reference/reference/crop_info.csv`` and
``crop_sales_formats.csv`` in place.

``crop_sales_formats`` rows gain a ``Planning Year`` column so
``import_historical_data`` can persist prices into ``CropSalesFormatYear`` without
losing year-specific values.
"""

from __future__ import annotations

import csv
import os
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

YEAR_DIR_RE = re.compile(r"^year_(\d{4})$")

# Importer + reference pull contract (see ``reference.json`` / sample_import).
CROP_INFO_FIELDNAMES = [
    "Crop",
    "Type",
    "Botanical Family",
    "Fresh or Storage",
    "Storage Weeks",
    "Can Hold In Field",
    "Harvest Units",
    "Average Unit Weight",
    "Units Per Bin",
    "Harvest Bin",
    "Harvest Tools",
    "Harvest Rate (units per hour)",
    "Nursery Weeks",
    "Weeks Until Pot Up",
    "Pot Up Tray Size",
    "Seeded Tray Size",
    "Seeds Per Cell",
    "Thinned Plants",
    "Seeds Per Ounce",
]

CSF_FIELDNAMES = [
    "Crop Name",
    "Product Name",
    "Planning Year",
    "Sale Price",
    "Sale Unit",
    "Harvest Qty Per Sale Unit",
    "SKU",
    "Is Active",
]


def _read_dict_rows(path: Path) -> tuple[list[str], list[dict]]:
    if not path.exists():
        return [], []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return fieldnames, rows


def _year_from_year_dir(name: str) -> int | None:
    m = YEAR_DIR_RE.match(name)
    return int(m.group(1)) if m else None


def _merge_crop_info(sources: list[tuple[int, list[dict]]]) -> list[dict]:
    """Later calendar year wins for the same ``Crop`` name."""
    best: dict[str, tuple[int, dict]] = {}
    for year, rows in sources:
        for row in rows:
            crop = (row.get("Crop") or "").strip()
            if not crop:
                continue
            prev = best.get(crop)
            snapshot = {k: ("" if row.get(k) is None else str(row.get(k))) for k in CROP_INFO_FIELDNAMES}
            if prev is None or year >= prev[0]:
                best[crop] = (year, snapshot)
    out_rows = []
    for crop in sorted(best.keys()):
        _, raw = best[crop]
        out_rows.append({k: raw.get(k, "") for k in CROP_INFO_FIELDNAMES})
    return out_rows


def _merge_crop_sales_formats(sources: list[tuple[int, list[dict]]]) -> list[dict]:
    """Concatenate all sources; attach ``Planning Year`` from the source bucket year when missing."""
    dedup: dict[tuple[str, str, int], dict] = {}
    order_keys: list[tuple[str, str, int]] = []
    for year, rows in sources:
        for row in rows:
            crop = (row.get("Crop Name") or row.get("Crop") or "").strip()
            prod = (row.get("Product Name") or "").strip()
            py_raw = (row.get("Planning Year") or "").strip()
            if py_raw:
                try:
                    py_int = int(float(py_raw))
                except ValueError:
                    py_int = year
            else:
                py_int = year
            key = (crop, prod, py_int)
            out = {
                "Crop Name": crop,
                "Product Name": prod,
                "Planning Year": str(py_int),
                "Sale Price": str(row.get("Sale Price", "") or "").strip(),
                "Sale Unit": str(row.get("Sale Unit", "") or "").strip(),
                "Harvest Qty Per Sale Unit": str(row.get("Harvest Qty Per Sale Unit", "") or "").strip(),
                "SKU": str(row.get("SKU", "") or "").strip(),
                "Is Active": str(row.get("Is Active", "true") or "true").strip(),
            }
            if key not in dedup:
                order_keys.append(key)
            dedup[key] = out
    return [dedup[k] for k in order_keys]


class Command(BaseCommand):
    help = "Merge reference + per-year historical reference CSVs into reference/reference/"

    def add_arguments(self, parser):
        parser.add_argument(
            "bundle_dir",
            type=str,
            help="Live import bundle root (contains reference/reference/ and optional historical lane)",
        )
        parser.add_argument(
            "--historical-lane",
            default="historical-601",
            help="Lane directory under bundle with year_YYYY/reference/*.csv",
        )
        parser.add_argument(
            "--base-planning-year",
            type=int,
            default=None,
            help="Planning year label for rows from reference lane only (default LIVE_IMPORT_YEAR or 2026)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log merge stats only; do not write files",
        )

    def handle(self, *args, **options):
        bundle = Path(options["bundle_dir"]).resolve()
        if not bundle.is_dir():
            raise CommandError(f"Not a directory: {bundle}")

        base_py = options["base_planning_year"]
        if base_py is None:
            base_py = int(os.environ.get("LIVE_IMPORT_YEAR", "2026"))

        hist_lane = options["historical_lane"]
        dry_run = options["dry_run"]

        ref_ref = bundle / "reference" / "reference"
        crop_info_path = ref_ref / "crop_info.csv"
        csf_path = ref_ref / "crop_sales_formats.csv"
        hist_root = bundle / hist_lane

        if not ref_ref.is_dir():
            self.stdout.write(self.style.WARNING(f"No reference bundle at {ref_ref}; nothing to merge."))
            return

        # --- collect crop_info sources: historical years (asc) then base (max year wins in merge) ---
        info_sources: list[tuple[int, list[dict]]] = []
        if hist_root.is_dir():
            year_dirs = sorted(
                (d for d in hist_root.iterdir() if d.is_dir() and _year_from_year_dir(d.name)),
                key=lambda p: _year_from_year_dir(p.name) or 0,
            )
            for ydir in year_dirs:
                y = _year_from_year_dir(ydir.name)
                if y is None:
                    continue
                p = ydir / "reference" / "crop_info.csv"
                if p.exists():
                    _, rows = _read_dict_rows(p)
                    if rows:
                        info_sources.append((y, rows))
                        self.stdout.write(f"  crop_info: {p} ({len(rows)} rows, year={y})")

        base_info_rows: list[dict] = []
        if crop_info_path.exists():
            _, base_info_rows = _read_dict_rows(crop_info_path)
            if base_info_rows:
                info_sources.append((base_py, base_info_rows))
                self.stdout.write(
                    f"  crop_info: {crop_info_path} ({len(base_info_rows)} rows, year={base_py} base)"
                )

        merged_info = _merge_crop_info(info_sources) if info_sources else []

        # --- crop_sales_formats: historical then base (dedup key keeps last write) ---
        fmt_sources: list[tuple[int, list[dict]]] = []
        if hist_root.is_dir():
            year_dirs = sorted(
                (d for d in hist_root.iterdir() if d.is_dir() and _year_from_year_dir(d.name)),
                key=lambda p: _year_from_year_dir(p.name) or 0,
            )
            for ydir in year_dirs:
                y = _year_from_year_dir(ydir.name)
                if y is None:
                    continue
                p = ydir / "reference" / "crop_sales_formats.csv"
                if p.exists():
                    _, rows = _read_dict_rows(p)
                    if rows:
                        fmt_sources.append((y, rows))
                        self.stdout.write(f"  crop_sales_formats: {p} ({len(rows)} rows, year={y})")

        base_fmt_rows: list[dict] = []
        if csf_path.exists():
            _, base_fmt_rows = _read_dict_rows(csf_path)
            if base_fmt_rows:
                fmt_sources.append((base_py, base_fmt_rows))
                self.stdout.write(
                    f"  crop_sales_formats: {csf_path} ({len(base_fmt_rows)} rows, year={base_py} base)"
                )

        merged_fmt = _merge_crop_sales_formats(fmt_sources) if fmt_sources else []

        self.stdout.write(
            f"Superset: crop_info -> {len(merged_info)} rows, "
            f"crop_sales_formats -> {len(merged_fmt)} rows (Planning Year applied)\n"
        )

        if dry_run:
            return

        ref_ref.mkdir(parents=True, exist_ok=True)

        if merged_info:
            with crop_info_path.open("w", newline="", encoding="utf-8") as handle:
                w = csv.DictWriter(handle, fieldnames=CROP_INFO_FIELDNAMES, extrasaction="ignore")
                w.writeheader()
                w.writerows(merged_info)
            self.stdout.write(self.style.SUCCESS(f"Wrote {crop_info_path}"))

        if merged_fmt:
            with csf_path.open("w", newline="", encoding="utf-8") as handle:
                w = csv.DictWriter(handle, fieldnames=CSF_FIELDNAMES, extrasaction="ignore")
                w.writeheader()
                w.writerows(merged_fmt)
            self.stdout.write(self.style.SUCCESS(f"Wrote {csf_path}"))

        if not merged_info and not merged_fmt:
            self.stdout.write(self.style.WARNING("No source rows found; reference CSVs unchanged."))

