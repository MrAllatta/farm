"""Validate live-import lane JSON contracts without network access."""

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


ALLOWED_ROW_TRANSFORMS = {"split", "copy", "week_monday", "grid_unpivot"}
OUTPUT_PATH_PATTERN = re.compile(r"^(reference|year_\d{4}|year_\$\{YEAR\})/(?:reference/)?[^/]+\.csv$")
YEAR_TOKEN = "${YEAR}"


class Command(BaseCommand):
    help = "Validate farm/data/live_import lane config JSON files."

    def add_arguments(self, parser):
        parser.add_argument(
            "config_dir",
            nargs="?",
            default="farm/data/live_import",
            help="Directory containing live import JSON files (default: farm/data/live_import)",
        )

    def handle(self, *args, **options):
        config_dir = Path(options["config_dir"])
        if not config_dir.is_dir():
            raise CommandError(f"Config directory not found: {config_dir}")

        config_files = sorted(
            path for path in config_dir.glob("*.json") if path.name != "manifest.json"
        )
        if not config_files:
            raise CommandError(f"No config files found in {config_dir}")

        errors = []
        for config_path in config_files:
            errors.extend(self._validate_config_file(config_path))

        if errors:
            joined = "\n".join(f"- {error}" for error in errors)
            raise CommandError(f"live import validation failed:\n{joined}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Validated {len(config_files)} live-import config file(s) in {config_dir}"
            )
        )

    def _validate_config_file(self, config_path):
        errors = []
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return [f"{config_path.name}: invalid JSON ({exc})"]

        tabs = payload.get("tabs")
        if not isinstance(tabs, list) or not tabs:
            return [f"{config_path.name}: 'tabs' must be a non-empty list"]

        errors.extend(self._validate_year_schedule(config_path.name, payload, tabs))

        for index, tab in enumerate(tabs, 1):
            errors.extend(
                self._validate_tab(
                    config_path.name,
                    f"tab#{index}",
                    tab,
                    inherited_required_headers=None,
                    inherited_aliases=None,
                )
            )
        return errors

    def _tab_or_region_mentions_year_token(self, tab):
        for key in ("output_path", "worksheet_title", "spreadsheet_name"):
            val = tab.get(key)
            if isinstance(val, str) and YEAR_TOKEN in val:
                return True
        regions = tab.get("source_regions")
        if isinstance(regions, list):
            for region in regions:
                if isinstance(region, dict) and self._tab_or_region_mentions_year_token(region):
                    return True
        return False

    def _validate_year_schedule(self, config_name, payload, tabs):
        errors = []
        needs_years = any(self._tab_or_region_mentions_year_token(tab) for tab in tabs)
        if not needs_years:
            return errors

        has_list = "years" in payload and payload["years"] is not None
        has_range = payload.get("start_year") is not None or payload.get("end_year") is not None
        if has_list and has_range:
            errors.append(
                f"{config_name}: set either 'years' or start_year/end_year when tabs use {YEAR_TOKEN}, not both"
            )
            return errors

        if has_list:
            ys = payload["years"]
            if not isinstance(ys, list) or not ys:
                errors.append(f"{config_name}: 'years' must be a non-empty list when tabs use {YEAR_TOKEN}")
            else:
                for item in ys:
                    if not isinstance(item, int):
                        errors.append(f"{config_name}: each entry in 'years' must be an integer")
                        break
        elif has_range:
            start = payload.get("start_year")
            end = payload.get("end_year")
            if not isinstance(start, int) or not isinstance(end, int):
                errors.append(f"{config_name}: start_year and end_year must be integers when set")
            elif start > end:
                errors.append(f"{config_name}: start_year must be <= end_year")
        else:
            errors.append(
                f"{config_name}: tabs use {YEAR_TOKEN}; set top-level 'years' or 'start_year'/'end_year'"
            )
        return errors

    def _validate_tab(
        self,
        config_name,
        location,
        tab,
        inherited_required_headers,
        inherited_aliases,
        is_source_region=False,
    ):
        errors = []
        if not isinstance(tab, dict):
            return [f"{config_name} {location}: tab must be an object"]

        required_headers = tab.get("required_headers", inherited_required_headers)
        aliases = tab.get("aliases", inherited_aliases) or {}
        output_path = tab.get("output_path")
        worksheet_title = tab.get("worksheet_title")

        if not is_source_region and not worksheet_title and not tab.get("source_csv"):
            errors.append(
                f"{config_name} {location}: missing worksheet_title (or source_csv for offline configs)"
            )
        if not is_source_region and not output_path:
            errors.append(f"{config_name} {location}: missing output_path")
        elif not is_source_region and not OUTPUT_PATH_PATTERN.match(str(output_path)):
            errors.append(
                f"{config_name} {location}: output_path '{output_path}' must be reference/*.csv or year_YYYY/*.csv"
            )

        if not isinstance(required_headers, list) or not required_headers:
            errors.append(f"{config_name} {location}: required_headers must be a non-empty list")
            required_headers = []

        if aliases is not None and not isinstance(aliases, dict):
            errors.append(f"{config_name} {location}: aliases must be an object")
            aliases = {}

        errors.extend(
            self._validate_column_map(
                config_name=config_name,
                location=location,
                required_headers=required_headers,
                aliases=aliases,
                column_map=tab.get("column_map"),
            )
        )
        errors.extend(
            self._validate_row_transforms(
                config_name=config_name,
                location=location,
                row_transforms=tab.get("row_transforms"),
            )
        )

        source_regions = tab.get("source_regions")
        if source_regions is not None:
            if not isinstance(source_regions, list) or not source_regions:
                errors.append(f"{config_name} {location}: source_regions must be a non-empty list")
            else:
                for idx, region in enumerate(source_regions, 1):
                    errors.extend(
                        self._validate_tab(
                            config_name=config_name,
                            location=f"{location}.source_region#{idx}",
                            tab=region,
                            inherited_required_headers=required_headers,
                            inherited_aliases=aliases,
                            is_source_region=True,
                        )
                    )

        return errors

    def _validate_column_map(self, config_name, location, required_headers, aliases, column_map):
        errors = []
        if column_map is None:
            return errors
        if not isinstance(column_map, dict):
            return [f"{config_name} {location}: column_map must be an object"]

        allowed_sources = set(required_headers)
        allowed_sources.update(str(key) for key in aliases.keys())
        allowed_sources.update(str(value) for value in aliases.values())
        for target, source in column_map.items():
            if isinstance(source, int):
                continue
            if not isinstance(source, str):
                errors.append(
                    f"{config_name} {location}: column_map.{target} must be a header name or column index"
                )
                continue
            if source not in allowed_sources:
                errors.append(
                    f"{config_name} {location}: column_map.{target} source '{source}' not present in required_headers/aliases"
                )
        return errors

    def _validate_row_transforms(self, config_name, location, row_transforms):
        if row_transforms is None:
            return []
        if not isinstance(row_transforms, list):
            return [f"{config_name} {location}: row_transforms must be a list"]

        errors = []
        for idx, transform in enumerate(row_transforms, 1):
            if not isinstance(transform, dict):
                errors.append(
                    f"{config_name} {location}.row_transform#{idx}: transform must be an object"
                )
                continue
            transform_type = transform.get("type")
            if transform_type not in ALLOWED_ROW_TRANSFORMS:
                errors.append(
                    f"{config_name} {location}.row_transform#{idx}: unknown transform type '{transform_type}'"
                )
        return errors
