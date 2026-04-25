"""Compare workbook 402 nursery CSV rows to in-app derived nursery ISO weeks."""

import json

from django.core.management.base import BaseCommand, CommandError

from planning.services.nursery_sheet_parity import run_nursery_parity


class Command(BaseCommand):
    help = (
        "Read-only: compare year_YYYY/nursery_events.csv wide rows (Nursery Plan 502 export) "
        "to nursery dates derived from Planting + CropInfo. Does not import nursery events."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "data_dir",
            type=str,
            help="Historical import bundle root (contains year_YYYY/nursery_events.csv)",
        )
        parser.add_argument(
            "--year",
            type=int,
            required=True,
            help="Planning year folder (e.g. 2026 for year_2026/)",
        )
        parser.add_argument(
            "--json-out",
            type=str,
            default="",
            help="Optional path to write the full JSON report",
        )
        parser.add_argument(
            "--fail-on-mismatch",
            action="store_true",
            help="Exit with code 1 when any wide row disagrees with derived weeks",
        )

    def handle(self, *args, **options):
        data_dir = options["data_dir"]
        year = int(options["year"])
        report = run_nursery_parity(data_dir, year)
        text = json.dumps(report, indent=2, default=str)
        self.stdout.write(text)
        out_path = (options.get("json_out") or "").strip()
        if out_path:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(text)
            self.stdout.write(self.style.SUCCESS(f"\nWrote report to {out_path}\n"))

        if options.get("fail_on_mismatch") and report.get("wide_rows_mismatch", 0) > 0:
            raise CommandError(
                f"nursery parity: {report['wide_rows_mismatch']} mismatch(es) "
                f"(see mismatches in report)"
            )
