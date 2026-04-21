"""Fetch supplier catalog pages and cache scraped fields on Variety."""

from django.core.management.base import BaseCommand, CommandError

from reference.models import Variety
from reference.services.variety_scrape import apply_scrape_to_variety, scrape_variety_page


class Command(BaseCommand):
    help = "Scrape Variety.source_url and update scraped_* fields."

    def add_arguments(self, parser):
        parser.add_argument("--variety-id", type=int, help="Single variety primary key")
        parser.add_argument(
            "--all-with-url",
            action="store_true",
            help="Process every variety that has a non-empty source_url",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print scrape results without saving",
        )

    def handle(self, *args, **options):
        vid = options.get("variety_id")
        all_urls = options.get("all_with_url")
        dry_run = options.get("dry_run")
        if bool(vid) == bool(all_urls):
            raise CommandError("Specify exactly one of --variety-id or --all-with-url")

        if vid:
            varieties = Variety.objects.filter(pk=vid)
        else:
            varieties = Variety.objects.exclude(source_url="").exclude(source_url__isnull=True)

        count = 0
        for v in varieties:
            data = scrape_variety_page(v.source_url or "")
            if dry_run:
                self.stdout.write(f"{v.pk} {v}: {data}")
            else:
                apply_scrape_to_variety(v, data)
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Processed {count} variety record(s)."))
