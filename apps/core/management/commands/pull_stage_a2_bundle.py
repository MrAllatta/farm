import csv
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

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


class Command(BaseCommand):
    help = "Fetch Google Sheets tabs and normalize them into a Stage A2 bundle"

    def add_arguments(self, parser):
        parser.add_argument("--config", required=True, help="JSON config describing live spreadsheet tabs")
        parser.add_argument("--output-dir", required=True, help="Directory for the normalized Stage A2 bundle")

    def handle(self, *args, **options):
        config_path = Path(options["config"]).resolve()
        output_dir = Path(options["output_dir"]).resolve()
        if not config_path.exists():
            raise CommandError(f"Config not found: {config_path}")

        config = json.loads(config_path.read_text(encoding="utf-8"))
        tabs = config.get("tabs", [])
        if not tabs:
            raise CommandError("Config must include at least one tab entry")

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

        default_scan_rows = HistoricalImportCommand.LIVE_SOURCE_NORMALIZER_CONTRACT["header_detection"][
            "max_scan_rows"
        ]

        for tab in tabs:
            worksheet_title = tab.get("worksheet_title")
            if not worksheet_title:
                raise CommandError("Each tab entry must include worksheet_title")

            resolved = resolve_spreadsheet(tab, drive_service=drive_service, folder_id=folder_id)
            rows = fetch_tab_rows(
                spreadsheet_id=resolved["spreadsheet_id"],
                worksheet_title=worksheet_title,
                sheets_service=sheets_service,
            )
            if not rows:
                raise CommandError(
                    f"Worksheet '{worksheet_title}' in spreadsheet '{resolved['spreadsheet_name']}' returned no rows"
                )

            normalized = normalize_rows(
                rows,
                required_headers=tab["required_headers"],
                aliases=tab.get("aliases"),
                max_scan_rows=tab.get("max_scan_rows", default_scan_rows),
                anchor_token=tab.get("anchor_token"),
                header_row_index=tab.get("header_row_index"),
            )

            output_path = output_dir / tab["output_path"]
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerows(normalized["rows"])

            rows_written = max(len(normalized["rows"]) - 1, 0)
            manifest["tabs"].append(
                {
                    "spreadsheet_id": resolved["spreadsheet_id"],
                    "spreadsheet_name": resolved["spreadsheet_name"],
                    "worksheet_title": worksheet_title,
                    "output_path": tab["output_path"],
                    "header_row_index": normalized["header_row_index"],
                    "strategy": normalized["strategy"],
                    "rows_written": rows_written,
                    "modified_time": resolved.get("modified_time"),
                }
            )
            self.stdout.write(
                f"pulled {resolved['spreadsheet_name']}:{worksheet_title} -> {tab['output_path']}"
            )

        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"wrote Stage A2 bundle manifest: {manifest_path}"))
