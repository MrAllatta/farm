import csv
import json
from copy import deepcopy
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from googleapiclient.errors import HttpError

from core.google_sheets_connector import (
    DRIVE_READONLY_SCOPE,
    SHEETS_READONLY_SCOPE,
    build_google_service,
    extract_drive_folder_id,
    fetch_tab_rows,
    resolve_spreadsheet,
)
from core.management.commands.import_historical_data import Command as HistoricalImportCommand
from core.spreadsheet_connector import normalize_rows

YEAR_TOKEN = "${YEAR}"


def _tab_uses_year_token(tab):
    for key in ("output_path", "worksheet_title", "spreadsheet_name"):
        val = tab.get(key)
        if isinstance(val, str) and YEAR_TOKEN in val:
            return True
    return False


def _substitute_year(value, year):
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    if YEAR_TOKEN in value:
        if year is None:
            raise CommandError(
                f"Encountered {YEAR_TOKEN!r} but no active year (check config and --years / --start-year/--end-year)"
            )
        return value.replace(YEAR_TOKEN, str(year))
    return value


def _parse_cli_years(years_option):
    if years_option is None:
        return None
    raw = str(years_option).strip()
    if not raw:
        return None
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError as exc:
            raise CommandError(f"Invalid year in --years: {part!r}") from exc
    return sorted(set(out)) if out else None


def _years_from_config(config):
    has_list = "years" in config and config["years"] is not None
    has_range = config.get("start_year") is not None or config.get("end_year") is not None
    if has_list and has_range:
        raise CommandError("Set either config 'years' or start_year/end_year, not both")

    if has_list:
        raw = config["years"]
        if not isinstance(raw, list) or not raw:
            raise CommandError("Config 'years' must be a non-empty list of integers")
        years = []
        for item in raw:
            if not isinstance(item, int):
                raise CommandError("Config 'years' must contain only integers")
            years.append(item)
        return sorted(set(years))
    start = config.get("start_year")
    end = config.get("end_year")
    if start is not None or end is not None:
        if start is None or end is None:
            raise CommandError("Config must set both start_year and end_year when either is present")
        if not isinstance(start, int) or not isinstance(end, int):
            raise CommandError("start_year and end_year must be integers")
        if start > end:
            raise CommandError("start_year must be <= end_year")
        return list(range(start, end + 1))
    return None


def _resolve_year_list(cli_years_str, cli_start, cli_end, config, config_needs_years):
    years = _parse_cli_years(cli_years_str)
    if years is not None:
        return years
    if cli_start is not None or cli_end is not None:
        if cli_start is None or cli_end is None:
            raise CommandError("Use both --start-year and --end-year together")
        if not isinstance(cli_start, int) or not isinstance(cli_end, int):
            raise CommandError("--start-year and --end-year must be integers")
        if cli_start > cli_end:
            raise CommandError("--start-year must be <= --end-year")
        return list(range(cli_start, cli_end + 1))
    cfg_years = _years_from_config(config)
    if cfg_years is not None:
        return cfg_years
    if config_needs_years:
        raise CommandError(
            f"Tabs use {YEAR_TOKEN} in output_path, worksheet_title, or spreadsheet_name; "
            "supply years via config (years or start_year/end_year) or "
            "--years or --start-year/--end-year"
        )
    return [None]


def _prepare_tab_for_year(tab, year):
    """Return a shallow copy of tab with year tokens substituted for resolve/fetch paths."""
    t = deepcopy(tab)
    if "output_path" in t:
        t["output_path"] = _substitute_year(t["output_path"], year)
    if "worksheet_title" in t and t["worksheet_title"] is not None:
        t["worksheet_title"] = _substitute_year(t["worksheet_title"], year)
    if "spreadsheet_name" in t and t.get("spreadsheet_name") is not None:
        t["spreadsheet_name"] = _substitute_year(t["spreadsheet_name"], year)
    return t


class Command(BaseCommand):
    help = "Fetch Google Sheets tabs and normalize them into a Stage A2 bundle"

    def add_arguments(self, parser):
        parser.add_argument("--config", required=True, help="JSON config describing live spreadsheet tabs")
        parser.add_argument("--output-dir", required=True, help="Directory for the normalized Stage A2 bundle")
        parser.add_argument(
            "--years",
            default=None,
            help="Comma-separated calendar years (e.g. 2022,2023,2024). Overrides config and --start-year/--end-year.",
        )
        parser.add_argument(
            "--start-year",
            type=int,
            default=None,
            help="Inclusive range start when expanding ${YEAR} (use with --end-year).",
        )
        parser.add_argument(
            "--end-year",
            type=int,
            default=None,
            help="Inclusive range end when expanding ${YEAR} (use with --start-year).",
        )

    def handle(self, *args, **options):
        config_path = Path(options["config"]).resolve()
        output_dir = Path(options["output_dir"]).resolve()
        if not config_path.exists():
            raise CommandError(f"Config not found: {config_path}")

        config = json.loads(config_path.read_text(encoding="utf-8"))
        tabs = config.get("tabs", [])
        if not tabs:
            raise CommandError("Config must include at least one tab entry")

        config_needs_years = any(_tab_uses_year_token(tab) for tab in tabs)
        years = _resolve_year_list(
            options.get("years"),
            options.get("start_year"),
            options.get("end_year"),
            config,
            config_needs_years,
        )
        multi_year_pass = not (len(years) == 1 and years[0] is None)
        first_year = years[0]

        folder_id = extract_drive_folder_id(config.get("drive_folder_id") or config.get("drive_folder_url"))
        drive_service = None
        if folder_id:
            drive_service = build_google_service("drive", "v3", [DRIVE_READONLY_SCOPE])
        sheets_service = build_google_service("sheets", "v4", [SHEETS_READONLY_SCOPE])

        output_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": HistoricalImportCommand.LIVE_SOURCE_NORMALIZER_CONTRACT["schema_version"],
            "source_id": config.get("source_id", folder_id or "google-sheets-stage-a2"),
            "connector_version": "google-sheets-stage-a2-1",
            "provider": "google_sheets",
            "drive_folder_id": folder_id,
            "tabs": [],
        }
        if multi_year_pass:
            manifest["years"] = [y for y in years if y is not None]

        default_scan_rows = HistoricalImportCommand.LIVE_SOURCE_NORMALIZER_CONTRACT["header_detection"][
            "max_scan_rows"
        ]

        for year in years:
            for tab in tabs:
                uses_year = _tab_uses_year_token(tab)
                if multi_year_pass and not uses_year and year != first_year:
                    continue

                worksheet_title = tab.get("worksheet_title")
                if not worksheet_title:
                    raise CommandError("Each tab entry must include worksheet_title")

                tab_run = _prepare_tab_for_year(tab, year)
                worksheet_title_run = tab_run["worksheet_title"]

                resolved = resolve_spreadsheet(tab_run, drive_service=drive_service, folder_id=folder_id)
                try:
                    rows = fetch_tab_rows(
                        spreadsheet_id=resolved["spreadsheet_id"],
                        worksheet_title=worksheet_title_run,
                        sheets_service=sheets_service,
                    )
                except HttpError as exc:
                    label = f"{resolved['spreadsheet_name']}:{worksheet_title_run}"
                    if year is not None:
                        label = f"{label} (year {year})"
                    self.stdout.write(
                        self.style.WARNING(f"skip tab (sheet unavailable or error): {label} — {exc}")
                    )
                    continue

                if not rows:
                    label = f"{resolved['spreadsheet_name']}:{worksheet_title_run}"
                    if year is not None:
                        label = f"{label} (year {year})"
                    self.stdout.write(self.style.WARNING(f"skip tab (no rows): {label}"))
                    continue

                normalized = normalize_rows(
                    rows,
                    required_headers=tab_run["required_headers"],
                    aliases=tab_run.get("aliases"),
                    max_scan_rows=tab_run.get("max_scan_rows", default_scan_rows),
                    anchor_token=tab_run.get("anchor_token"),
                    header_row_index=tab_run.get("header_row_index"),
                    output_headers=tab_run.get("output_headers"),
                    column_map=tab_run.get("column_map"),
                    default_values=tab_run.get("default_values"),
                    row_transforms=tab_run.get("row_transforms"),
                    source_regions=tab_run.get("source_regions"),
                    stop_on_blank_in=tab_run.get("stop_on_blank_in"),
                    prefer_anchor_token=tab_run.get("prefer_anchor_token", False),
                    grid_unpivot=tab_run.get("grid_unpivot"),
                )

                rel_output = tab_run["output_path"]
                output_path = output_dir / rel_output
                output_path.parent.mkdir(parents=True, exist_ok=True)
                append_without_header = tab_run.get("append_without_header", False)
                data_rows = normalized["rows"][1:]
                appended_data_only = append_without_header and output_path.exists()
                if appended_data_only:
                    with output_path.open("a", encoding="utf-8", newline="") as handle:
                        writer = csv.writer(handle)
                        writer.writerows(data_rows)
                    rows_written = len(data_rows)
                else:
                    with output_path.open("w", encoding="utf-8", newline="") as handle:
                        writer = csv.writer(handle)
                        writer.writerows(normalized["rows"])
                    rows_written = max(len(normalized["rows"]) - 1, 0)

                tab_manifest = {
                    "spreadsheet_id": resolved["spreadsheet_id"],
                    "spreadsheet_name": resolved["spreadsheet_name"],
                    "worksheet_title": worksheet_title_run,
                    "output_path": rel_output,
                    "header_row_index": normalized["header_row_index"],
                    "strategy": normalized["strategy"],
                    "rows_written": rows_written,
                    "modified_time": resolved.get("modified_time"),
                }
                if year is not None:
                    tab_manifest["year"] = year
                if append_without_header:
                    tab_manifest["append_without_header"] = True
                if tab_run.get("grid_unpivot"):
                    tab_manifest["grid_unpivot"] = True
                manifest["tabs"].append(tab_manifest)
                self.stdout.write(
                    f"pulled {resolved['spreadsheet_name']}:{worksheet_title_run} -> {rel_output}"
                )

        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"wrote Stage A2 bundle manifest: {manifest_path}"))
