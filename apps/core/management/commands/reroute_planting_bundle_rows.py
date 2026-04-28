"""LIVE-17 (A): offline re-bucket of ``plantings.csv`` rows across ``year_YYYY/`` trees.

Reads a Stage-A-style bundle on disk (no Google APIs), moves rows whose Planned Plant Date
implies a different ``year_<Y>/`` folder using the same ±1 calendar-year shoulder rule as
LIVE-17 (C), and writes a JSON manifest. Rows more than one year from the folder year stay
put unless ``--move-beyond-one-calendar-year`` is set (listed as ``deferred_far_from_folder``).

Safe defaults: copy to ``--out-dir``; use ``--in-place`` only when explicitly intended.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.planting_planning_year_routing import (
    parse_planned_plant_calendar_year,
    parse_planned_plant_date,
    planting_target_planning_calendar_year,
)

_YEAR_DIR_RE = re.compile(r"^year_(\d{4})$")


def _iter_year_dirs(data_dir: Path) -> list[tuple[int, Path]]:
    out = []
    for child in sorted(data_dir.iterdir()):
        if not child.is_dir():
            continue
        m = _YEAR_DIR_RE.match(child.name)
        if m:
            out.append((int(m.group(1)), child))
    return out


def _read_plantings_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return fieldnames, rows


def _write_plantings_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


class Command(BaseCommand):
    help = (
        "LIVE-17 (A): move plantings.csv rows into year_<Planned Plant Date year>/ when within "
        "±1 calendar year of the source folder (optional wider moves). Writes a JSON manifest."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--data-dir",
            type=str,
            required=True,
            help="Root of the bundle (contains year_YYYY/ directories)",
        )
        parser.add_argument(
            "--out-dir",
            type=str,
            default="",
            help="Write a full copy of the bundle with plantings rerouted here (must not exist or be empty)",
        )
        parser.add_argument(
            "--manifest-json",
            type=str,
            default="",
            help="Write the move manifest to this path (default under out-dir or cwd for dry-run)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Do not modify any files; only compute moves and write manifest",
        )
        parser.add_argument(
            "--in-place",
            action="store_true",
            help="Rewrite plantings.csv under --data-dir (mutually exclusive with --out-dir)",
        )
        parser.add_argument(
            "--move-beyond-one-calendar-year",
            action="store_true",
            help=(
                "Also move rows when |Planned Plant Date year - folder year| > 1 (operator-trusted bundle)."
            ),
        )

    def handle(self, *args, **options):
        data_dir = Path(options["data_dir"]).resolve()
        out_dir_opt = (options.get("out_dir") or "").strip()
        manifest_opt = (options.get("manifest_json") or "").strip()
        dry_run = bool(options["dry_run"])
        in_place = bool(options["in_place"])
        move_far = bool(options["move_beyond_one_calendar_year"])

        if not data_dir.is_dir():
            raise CommandError(f"--data-dir is not a directory: {data_dir}")
        if in_place and out_dir_opt:
            raise CommandError("Use either --in-place or --out-dir, not both")
        if not in_place and not out_dir_opt:
            raise CommandError("Pass --out-dir (recommended) or --in-place")

        work_root = data_dir
        if out_dir_opt:
            out_dir = Path(out_dir_opt).resolve()
            if out_dir == data_dir:
                raise CommandError("--out-dir must differ from --data-dir")
            if not dry_run:
                if out_dir.exists() and any(out_dir.iterdir()):
                    raise CommandError(f"--out-dir exists and is not empty: {out_dir}")
                if out_dir.exists():
                    out_dir.rmdir()
                shutil.copytree(data_dir, out_dir)
                work_root = out_dir

        manifest_moves: list[dict] = []
        manifest_deferred: list[dict] = []
        manifest_skipped_blank: list[dict] = []

        # Collect moves: (source_folder_year, target_year, row_index_1based, row_dict, fieldnames)
        moves_out: list[tuple[int, int, int, dict[str, str], list[str]]] = []

        # Dry-run always reads the source bundle; apply with --out-dir mutates the copy only.
        scan_root = data_dir if dry_run else work_root
        for folder_year, ydir in _iter_year_dirs(scan_root):
            plantings = ydir / "plantings.csv"
            if not plantings.is_file():
                continue
            fieldnames, rows = _read_plantings_rows(plantings)
            if not fieldnames:
                continue
            for idx, row in enumerate(rows, start=1):
                raw_date = row.get("Planned Plant Date", "")
                d_year = parse_planned_plant_calendar_year(raw_date)
                if d_year is None:
                    manifest_skipped_blank.append(
                        {
                            "folder_year": folder_year,
                            "row_number": idx,
                            "planting_code": (row.get("ID") or "").strip() or None,
                            "reason": "blank_or_unparseable_planned_plant_date",
                        }
                    )
                    continue
                target_year, decision = planting_target_planning_calendar_year(
                    folder_year=folder_year,
                    planned_date_calendar_year=d_year,
                    planning_year_from_planned_date=True,
                    force_planned_date_past_threshold=move_far,
                )
                if target_year == folder_year:
                    continue
                if decision == "folder_wins_far_without_force":
                    pdate = parse_planned_plant_date(raw_date)
                    pdate_s = pdate.isoformat() if pdate else str(raw_date).strip()
                    manifest_deferred.append(
                        {
                            "from_folder_year": folder_year,
                            "would_target_year": d_year,
                            "row_number": idx,
                            "planting_code": (row.get("ID") or "").strip() or None,
                            "planned_plant_date": pdate_s,
                            "reason": "more_than_one_calendar_year_from_folder_use_move_beyond_one_calendar_year",
                        }
                    )
                    continue
                planting_code = (row.get("ID") or "").strip() or None
                pdate = parse_planned_plant_date(raw_date)
                pdate_s = pdate.isoformat() if pdate else str(raw_date).strip()
                manifest_moves.append(
                    {
                        "from_folder_year": folder_year,
                        "to_folder_year": target_year,
                        "row_number": idx,
                        "planting_code": planting_code,
                        "planned_plant_date": pdate_s,
                        "routing_decision": decision,
                    }
                )
                moves_out.append((folder_year, target_year, idx, dict(row), list(fieldnames)))

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry-run: {len(manifest_moves)} row(s) would move, "
                    f"{len(manifest_deferred)} deferred (far), "
                    f"{len(manifest_skipped_blank)} skipped blank date\n"
                )
            )
        else:
            mutate_root = work_root
            by_file: dict[Path, list[int]] = {}
            for folder_year, _target_year, idx, _row, _fn in moves_out:
                path = mutate_root / f"year_{folder_year}" / "plantings.csv"
                by_file.setdefault(path, []).append(idx)
            for path, indices in by_file.items():
                fieldnames, rows = _read_plantings_rows(path)
                drop = set(indices)
                new_rows = [r for i, r in enumerate(rows, start=1) if i not in drop]
                _write_plantings_csv(path, fieldnames, new_rows)

            by_target: dict[int, list[tuple[dict[str, str], list[str]]]] = {}
            for _sy, target_year, _idx, row, fn in moves_out:
                by_target.setdefault(target_year, []).append((dict(row), list(fn)))

            for target_year in sorted(by_target.keys()):
                pairs = by_target[target_year]
                fieldnames: list[str] = []
                for _r, fn in pairs:
                    for h in fn:
                        if h not in fieldnames:
                            fieldnames.append(h)
                new_rows = []
                for r, _fn in pairs:
                    new_rows.append({h: r.get(h, "") for h in fieldnames})

                dest_dir = mutate_root / f"year_{target_year}"
                dest = dest_dir / "plantings.csv"
                dest_dir.mkdir(parents=True, exist_ok=True)
                if dest.is_file():
                    existing_fn, existing_rows = _read_plantings_rows(dest)
                    merged_fn = list(existing_fn)
                    for h in fieldnames:
                        if h not in merged_fn:
                            merged_fn.append(h)
                    combined = existing_rows + new_rows
                    for r in combined:
                        for h in merged_fn:
                            r.setdefault(h, "")
                    _write_plantings_csv(dest, merged_fn, combined)
                else:
                    _write_plantings_csv(dest, fieldnames, new_rows)

            self.stdout.write(
                self.style.SUCCESS(
                    f"Moved {len(manifest_moves)} planting row(s); "
                    f"{len(manifest_deferred)} deferred (far); "
                    f"{len(manifest_skipped_blank)} skipped blank date\n"
                )
            )

        manifest = {
            "schema_version": "live17-planting-reroute-1",
            "data_dir": str(data_dir),
            "work_root": str(work_root),
            "scanned_root": str(scan_root),
            "dry_run": dry_run,
            "in_place": in_place,
            "move_beyond_one_calendar_year": move_far,
            "moves": sorted(manifest_moves, key=lambda m: (m["from_folder_year"], m["row_number"])),
            "deferred_far_from_folder": manifest_deferred,
            "skipped_blank_planned_plant_date": manifest_skipped_blank,
        }

        if manifest_opt:
            mpath = Path(manifest_opt).resolve()
        else:
            mpath = work_root / "_planting_reroute_manifest.json"

        mpath.parent.mkdir(parents=True, exist_ok=True)
        mpath.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.stdout.write(f"Wrote manifest: {mpath}\n")
