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

SALES_PLAN_302_FILENAME = "sales_plan_302.csv"


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


def _harvest_year_column_index(header_row):
    """Return 0-based column index for Harvest Year (case-insensitive), or None."""
    for i, cell in enumerate(header_row or []):
        if str(cell).strip().casefold() == "harvest year":
            return i
    return None


def _filter_csv_rows_by_harvest_year(rows_matrix, year):
    """Keep header + rows whose Harvest Year cell matches ``year`` (string compare).

    Used for Sales Plan 302 long tables: the same sheet is fetched for each calendar year
    folder, but only rows belonging to that harvest year should land in ``year_YYYY/``.
    """
    if not rows_matrix or len(rows_matrix) < 2 or year is None:
        return rows_matrix
    header = rows_matrix[0]
    idx = _harvest_year_column_index(header)
    if idx is None:
        return rows_matrix
    want = str(int(year))
    out = [header]
    for row in rows_matrix[1:]:
        cell = row[idx] if idx < len(row) else ""
        if str(cell).strip() == want:
            out.append(row)
    return out


def _split_rows_by_year_column(rows, year_column):
    """Group data rows by a year-column value for per-row year routing (H1a §5.6 / §6).

    Returns ``{year_str: [header, *matching_rows]}`` for each distinct non-empty integer
    year value found in *year_column*.  Rows whose cell is blank or un-parseable are
    collected under the ``""`` key so the caller can apply a loop-year fallback.
    """
    if not rows or len(rows) < 2:
        return {}
    header = rows[0]
    norm_target = str(year_column).strip().casefold()
    col_idx = None
    for i, h in enumerate(header):
        if str(h).strip().casefold() == norm_target:
            col_idx = i
            break
    groups: dict = {}
    for row in rows[1:]:
        raw = str(row[col_idx]).strip() if (col_idx is not None and col_idx < len(row)) else ""
        if raw:
            try:
                raw = str(int(float(raw)))
            except ValueError:
                raw = ""
        groups.setdefault(raw, []).append(row)
    return {k: [header] + v for k, v in groups.items()}


def _warn_date_year_mismatch(stdout, warning_fn, group_rows, year_col, date_col, expected_year, label):
    """Log H1a9.4 warning once per group when the date-column year disagrees with the
    routing year.  Returns the date-derived year when a mismatch is found so the caller
    can re-route to the correct directory; returns *expected_year* when they agree.
    """
    header = group_rows[0]
    date_idx = None
    norm_dc = str(date_col).strip().casefold()
    for i, h in enumerate(header):
        if str(h).strip().casefold() == norm_dc:
            date_idx = i
            break
    if date_idx is None:
        return expected_year
    for data_row in group_rows[1:]:
        date_val = str(data_row[date_idx]).strip() if date_idx < len(data_row) else ""
        if not date_val or len(date_val) < 4:
            continue
        try:
            date_year = int(date_val[:4])
        except ValueError:
            continue
        if date_year != expected_year:
            stdout.write(
                warning_fn(
                    f"H1a9.4: {label} — {year_col!r} ({expected_year}) disagrees with "
                    f"{date_col!r} year ({date_year}); routing by {date_col!r} year"
                )
            )
            return date_year
        return expected_year
    return expected_year


def _ensure_planning_year_csv(output_dir: Path, year: int) -> None:
    """Create ``year_<Y>/planning_year.csv`` when missing so ``import_historical_data`` can upsert.

    Live-import bundles are produced by ``pull_stage_a2_bundle`` (local Makefile or cloud pull
    job), not shipped as a static deploy artifact. Planning years are not pulled from Google
    Sheets in the crop-plan lane today; this stub prevents ``planning year not found`` on fresh
    databases when only plantings/nursery CSVs exist for a calendar year.
    """
    rel = Path(f"year_{year}") / "planning_year.csv"
    path = output_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Year", "Status", "Overplant Factor"])
        writer.writerow([str(year), "planning", "1.10"])


def _drive_folder_id_for_year(config, year, default_folder_id):
    """Optional per-calendar-year Drive root for resolve (see drive_folder_id_by_year in lane JSON)."""
    mapping = config.get("drive_folder_id_by_year")
    if not mapping or year is None:
        return default_folder_id
    if not isinstance(mapping, dict):
        raise CommandError("drive_folder_id_by_year must be an object mapping year to folder id or URL")
    raw = mapping.get(str(year))
    if raw is None or raw == "":
        return default_folder_id
    resolved = extract_drive_folder_id(raw)
    if not resolved:
        raise CommandError(f"drive_folder_id_by_year[{year!r}] could not be resolved to a folder id")
    return resolved


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
        search_descendants = bool(config.get("drive_search_subfolders"))
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

                resolve_folder_id = _drive_folder_id_for_year(config, year, folder_id)
                resolved = resolve_spreadsheet(
                    tab_run,
                    drive_service=drive_service,
                    folder_id=resolve_folder_id,
                    search_descendants=search_descendants,
                )
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
                    fold_into_notes=tab_run.get("fold_into_notes"),
                    constant_columns=tab_run.get("constant_columns"),
                )

                rel_output = tab_run["output_path"]
                rows_matrix = normalized["rows"]
                if (
                    year is not None
                    and isinstance(rel_output, str)
                    and rel_output.replace("\\", "/").endswith(SALES_PLAN_302_FILENAME)
                ):
                    rows_matrix = _filter_csv_rows_by_harvest_year(rows_matrix, year)
                    normalized["rows"] = rows_matrix

                append_without_header = tab_run.get("append_without_header", False)
                row_year_routing = tab.get("row_year_routing")

                if row_year_routing and len(rows_matrix) > 1:
                    # Per-row year routing (H1a §5.6 / §6): split by year column and write
                    # each year-group to its own year_YYYY/ directory.
                    year_col = row_year_routing.get("year_column", "Planning Year")
                    date_col = row_year_routing.get("date_column")
                    original_path_template = tab.get("output_path", rel_output)

                    year_groups = _split_rows_by_year_column(rows_matrix, year_col)

                    # Rows with blank year fall back to the loop year
                    fallback_rows = year_groups.pop("", None)
                    if fallback_rows and year is not None:
                        fallback_key = str(year)
                        if fallback_key in year_groups:
                            year_groups[fallback_key] = (
                                [year_groups[fallback_key][0]]
                                + year_groups[fallback_key][1:]
                                + fallback_rows[1:]
                            )
                        else:
                            year_groups[fallback_key] = fallback_rows
                        self.stdout.write(
                            self.style.WARNING(
                                f"row_year_routing: {len(fallback_rows) - 1} rows in "
                                f"{resolved['spreadsheet_name']}:{worksheet_title_run} "
                                f"have blank {year_col!r} — routing to loop year {year!r}"
                            )
                        )

                    sheet_label = f"{resolved['spreadsheet_name']}:{worksheet_title_run}"
                    for row_year_str, group_rows in sorted(year_groups.items()):
                        try:
                            row_year_int = int(row_year_str)
                        except (ValueError, TypeError):
                            row_year_int = year

                        # H1a9.4: warn (and re-route) when date-column year disagrees
                        if date_col and row_year_int is not None:
                            row_year_int = _warn_date_year_mismatch(
                                self.stdout,
                                self.style.WARNING,
                                group_rows,
                                year_col,
                                date_col,
                                row_year_int,
                                sheet_label,
                            )

                        group_rel_path = _substitute_year(original_path_template, row_year_int)
                        group_abs_path = output_dir / group_rel_path
                        group_abs_path.parent.mkdir(parents=True, exist_ok=True)

                        data_rows_group = group_rows[1:]
                        appended_group = append_without_header and group_abs_path.exists()
                        if appended_group:
                            with group_abs_path.open("a", encoding="utf-8", newline="") as fh:
                                writer = csv.writer(fh)
                                writer.writerows(data_rows_group)
                        else:
                            with group_abs_path.open("w", encoding="utf-8", newline="") as fh:
                                writer = csv.writer(fh)
                                writer.writerows(group_rows)
                        group_rows_written = len(data_rows_group)

                        group_manifest = {
                            "spreadsheet_id": resolved["spreadsheet_id"],
                            "spreadsheet_name": resolved["spreadsheet_name"],
                            "worksheet_title": worksheet_title_run,
                            "output_path": group_rel_path,
                            "header_row_index": normalized["header_row_index"],
                            "strategy": normalized["strategy"],
                            "rows_written": group_rows_written,
                            "modified_time": resolved.get("modified_time"),
                            "row_year": row_year_int,
                        }
                        if year is not None:
                            group_manifest["year"] = year
                        if append_without_header:
                            group_manifest["append_without_header"] = True
                        if tab_run.get("grid_unpivot"):
                            group_manifest["grid_unpivot"] = True
                        manifest["tabs"].append(group_manifest)

                        if multi_year_pass and row_year_int is not None:
                            _ensure_planning_year_csv(output_dir, row_year_int)

                    self.stdout.write(
                        f"pulled {resolved['spreadsheet_name']}:{worksheet_title_run} "
                        f"-> {len(year_groups)} year bucket(s) via row_year_routing "
                        f"({year_col!r})"
                    )

                else:
                    output_path = output_dir / rel_output
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    data_rows = normalized["rows"][1:]
                    appended_data_only = append_without_header and output_path.exists()
                    if appended_data_only:
                        with output_path.open("a", encoding="utf-8", newline="") as fh:
                            writer = csv.writer(fh)
                            writer.writerows(data_rows)
                        rows_written = len(data_rows)
                    else:
                        with output_path.open("w", encoding="utf-8", newline="") as fh:
                            writer = csv.writer(fh)
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

            if multi_year_pass and year is not None:
                _ensure_planning_year_csv(output_dir, year)

        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"wrote Stage A2 bundle manifest: {manifest_path}"))
