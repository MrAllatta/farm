"""Read-only comparison of sheet ``nursery_events.csv`` (402 wide rows) vs derived nursery dates."""

from __future__ import annotations

import csv
from pathlib import Path

from planning.models import Planting

from . import nursery_plan_sheet


def run_nursery_parity(data_dir: str, year: int) -> dict:
    """Compare wide-tab nursery rows to dates derived from ``Planting`` + ``CropInfo``.

    Does not modify the database. Expects ``year_YYYY/nursery_events.csv`` next to other
    historical bundle files (same layout as ``import_historical_data``).
    """
    path = Path(data_dir) / f"year_{year}" / "nursery_events.csv"
    if not path.is_file():
        return {
            "status": "skipped",
            "reason": f"missing {path}",
            "year": year,
            "rows_checked": 0,
        }

    mismatches: list[dict] = []
    skipped: list[dict] = []
    matched = 0

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, 1):
            if not nursery_plan_sheet.nursery_row_is_workbook_402_plan_tab_shape(row):
                continue

            planting = nursery_plan_sheet.resolve_nursery_planting_from_plan_tab(row, year)
            if not planting:
                skipped.append({"csv_row": i, "reason": "planting_not_resolved"})
                continue

            planting = Planting.objects.select_related("crop").get(pk=planting.pk)
            crop = planting.crop

            row_issues: list[str] = []

            seed_y = (row.get("Nursery Seeding Year") or "").strip()
            seed_w = (row.get("Nursery Seeding Week") or "").strip()
            derived_seed = nursery_plan_sheet.derived_nursery_seed_date(planting)
            if seed_y and seed_w:
                sheet_seed = nursery_plan_sheet.date_from_plan_year_week(seed_y, seed_w)
                if sheet_seed and derived_seed:
                    if sheet_seed.isocalendar()[:2] != derived_seed.isocalendar()[:2]:
                        row_issues.append(
                            f"seed_week sheet={sheet_seed.isocalendar()[:2]} "
                            f"derived={derived_seed.isocalendar()[:2]}"
                        )
                elif sheet_seed and not derived_seed:
                    row_issues.append("sheet_has_seed_week_but_crop_nursery_weeks_is_zero")
                elif not sheet_seed:
                    row_issues.append("invalid_nursery_seeding_year_week")

            pot_y = (row.get("Nursery Pot Up Year") or "").strip()
            pot_w = (row.get("Nursery Pot Up Week") or "").strip()
            derived_pot = nursery_plan_sheet.derived_nursery_pot_up_date(planting)
            if pot_y and pot_w:
                sheet_pot = nursery_plan_sheet.date_from_plan_year_week(pot_y, pot_w)
                if sheet_pot and derived_pot:
                    if sheet_pot.isocalendar()[:2] != derived_pot.isocalendar()[:2]:
                        row_issues.append(
                            f"pot_up_week sheet={sheet_pot.isocalendar()[:2]} "
                            f"derived={derived_pot.isocalendar()[:2]}"
                        )
                elif sheet_pot and not derived_pot:
                    row_issues.append("sheet_has_pot_week_but_no_derived_pot_up")
                elif not sheet_pot:
                    row_issues.append("invalid_nursery_pot_up_year_week")

            if row_issues:
                mismatches.append(
                    {
                        "csv_row": i,
                        "planting_id": planting.pk,
                        "crop": crop.name,
                        "issues": row_issues,
                    }
                )
            else:
                matched += 1

    return {
        "status": "ok",
        "year": year,
        "csv_path": str(path),
        "wide_rows_matched": matched,
        "wide_rows_mismatch": len(mismatches),
        "wide_rows_skipped": len(skipped),
        "mismatches": mismatches[:200],
        "skipped_sample": skipped[:50],
    }
