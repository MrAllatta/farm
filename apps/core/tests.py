import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import TestCase

from reference.models import Block


class ImportHistoricalDataCommandTests(TestCase):
    def _write_blocks_csv(self, data_dir):
        blocks_csv = Path(data_dir) / "blocks.csv"
        blocks_csv.write_text(
            "\n".join(
                [
                    "Block,Block Type,# of Beds,Bed Width (feet),Bedfeet per Bed",
                    "Field 1,Field,10,3,100",
                ]
            ),
            encoding="utf-8",
        )

    def test_validate_only_does_not_write_and_reports_canonical_outcomes(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_blocks_csv(data_dir)
            summary_path = Path(output_dir) / "summary.json"

            call_command(
                "import_historical_data",
                data_dir,
                "--validate-only",
                "--summary-json",
                str(summary_path),
            )

            self.assertEqual(Block.objects.count(), 0)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            block_results = summary["results"]["models"]["Block"]
            self.assertEqual(set(block_results.keys()), {"created", "updated", "skipped", "error"})
            self.assertEqual(block_results["created"], 0)
            self.assertEqual(block_results["updated"], 0)
            self.assertEqual(block_results["skipped"], 1)
            self.assertEqual(block_results["error"], 0)

    def test_repeat_import_is_reported_as_updated(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_blocks_csv(data_dir)
            first_summary_path = Path(output_dir) / "summary-first.json"
            second_summary_path = Path(output_dir) / "summary-second.json"

            call_command(
                "import_historical_data",
                data_dir,
                "--summary-json",
                str(first_summary_path),
            )
            self.assertEqual(Block.objects.count(), 1)

            first_summary = json.loads(first_summary_path.read_text(encoding="utf-8"))
            self.assertEqual(first_summary["results"]["models"]["Block"]["created"], 1)
            self.assertEqual(first_summary["results"]["models"]["Block"]["updated"], 0)

            call_command(
                "import_historical_data",
                data_dir,
                "--summary-json",
                str(second_summary_path),
            )
            second_summary = json.loads(second_summary_path.read_text(encoding="utf-8"))
            self.assertEqual(second_summary["results"]["models"]["Block"]["created"], 0)
            self.assertEqual(second_summary["results"]["models"]["Block"]["updated"], 1)
