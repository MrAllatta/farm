"""Post inventory ledger drawdown for mix pack batches (crop-backed components)."""

from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError

from operations.models import PackBatch


class Command(BaseCommand):
    help = (
        "Call PackBatch.post_component_consumption() for one or more pack batches. "
        "Creates negative InventoryLedger rows for crop-backed PackBatchComponent lines."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "pack_batch_ids",
            nargs="+",
            type=int,
            help="Primary key(s) of PackBatch records",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and print intent without writing ledger rows",
        )

    def handle(self, *args, **options):
        ids = options["pack_batch_ids"]
        dry_run = options["dry_run"]
        for pk in ids:
            try:
                batch = PackBatch.objects.select_related("product", "recipe").get(pk=pk)
            except PackBatch.DoesNotExist as e:
                raise CommandError(f"PackBatch id={pk} does not exist") from e
            self.stdout.write(
                f"PackBatch {pk}: {batch.product.product_name} @ {batch.pack_date} "
                f"({batch.packed_quantity} {batch.packed_unit})"
            )
            if dry_run:
                n = batch.components.count()
                self.stdout.write(self.style.WARNING(f"  dry-run: would post {n} component line(s)"))
                continue
            try:
                entries = batch.post_component_consumption()
            except ValidationError as e:
                raise CommandError(str(e)) from e
            self.stdout.write(
                self.style.SUCCESS(
                    f"  posted {len(entries)} inventory ledger entr(y/ies)"
                )
            )
