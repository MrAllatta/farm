"""Backfill missing generated harvest and nursery events for plantings (OP-7 / LC-5)."""

from django.core.management.base import BaseCommand

from planning.models import PlanningYear
from planning.services.planting_events_repair import repair_planting_events


class Command(BaseCommand):
    help = (
        "Create missing HarvestEvent and NurseryEvent rows for plantings that have none. "
        "Idempotent: does not modify existing events."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--year",
            type=int,
            action="append",
            dest="years",
            help="Limit to planning years with this calendar year (repeatable).",
        )
        parser.add_argument(
            "--planning-year-id",
            type=int,
            action="append",
            dest="planning_year_ids",
            help="Limit to specific PlanningYear primary keys (repeatable).",
        )
        parser.add_argument(
            "--min-year",
            type=int,
            default=None,
            help="Minimum PlanningYear.year inclusive.",
        )
        parser.add_argument(
            "--max-year",
            type=int,
            default=None,
            help="Maximum PlanningYear.year inclusive.",
        )

    def handle(self, *args, **options):
        years = options.get("years") or []
        py_ids = list(options.get("planning_year_ids") or [])
        min_year = options.get("min_year")
        max_year = options.get("max_year")

        for y in years:
            pk = PlanningYear.objects.filter(year=y).values_list("id", flat=True).first()
            if pk is not None:
                py_ids.append(pk)
        planning_year_ids = sorted(set(py_ids)) if py_ids else None

        stats = repair_planting_events(
            planning_year_ids=planning_year_ids,
            min_year=min_year,
            max_year=max_year,
        )
        self.stdout.write(self.style.SUCCESS(str(stats.as_dict())))
