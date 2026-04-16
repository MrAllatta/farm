import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.management.commands.import_historical_data import Command as HistoricalImportCommand
from core.spreadsheet_connector import normalize_csv_file


class Command(BaseCommand):
    help = "Normalize local spreadsheet-tab CSV snapshots into an offline Stage A2 bundle"

    def add_arguments(self, parser):
        parser.add_argument("--config", required=True, help="JSON config describing source tabs")
        parser.add_argument("--output-dir", required=True, help="Directory for the normalized bundle")

    def handle(self, *args, **options):
        config_path = Path(options["config"]).resolve()
        output_dir = Path(options["output_dir"]).resolve()
        if not config_path.exists():
            raise CommandError(f"Config not found: {config_path}")

        config = json.loads(config_path.read_text(encoding="utf-8"))
        tabs = config.get("tabs", [])
        if not tabs:
            raise CommandError("Config must include at least one tab entry")

        output_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": HistoricalImportCommand.LIVE_SOURCE_NORMALIZER_CONTRACT["schema_version"],
            "source_id": config.get("source_id", "offline-stage-a2"),
            "connector_version": "offline-skeleton-1",
            "tabs": [],
        }

        for tab in tabs:
            source_csv = (config_path.parent / tab["source_csv"]).resolve()
            output_path = output_dir / tab["output_path"]
            normalized = normalize_csv_file(
                source_path=source_csv,
                output_path=output_path,
                required_headers=tab["required_headers"],
                aliases=tab.get("aliases"),
                max_scan_rows=tab.get(
                    "max_scan_rows",
                    HistoricalImportCommand.LIVE_SOURCE_NORMALIZER_CONTRACT["header_detection"]["max_scan_rows"],
                ),
                anchor_token=tab.get("anchor_token"),
                header_row_index=tab.get("header_row_index"),
                output_headers=tab.get("output_headers"),
                column_map=tab.get("column_map"),
                default_values=tab.get("default_values"),
                row_transforms=tab.get("row_transforms"),
                source_regions=tab.get("source_regions"),
                stop_on_blank_in=tab.get("stop_on_blank_in"),
            )
            manifest["tabs"].append(
                {
                    "source_csv": tab["source_csv"],
                    "output_path": tab["output_path"],
                    "header_row_index": normalized["header_row_index"],
                    "strategy": normalized["strategy"],
                    "rows_written": normalized["rows_written"],
                }
            )
            self.stdout.write(f"normalized {tab['source_csv']} -> {tab['output_path']}")

        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"wrote offline Stage A2 bundle manifest: {manifest_path}"))
