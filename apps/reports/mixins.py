from datetime import date, timedelta

from isoweek import Week

from core.planning_year import resolve_current_planning_year


REPORT_EXCLUDED_STATUSES = ("skipped", "failed", "revised")

ANALYZE_LINKS = [
    {"key": "plan_vs_actual", "label": "Plan vs Actual", "route": "reports:plan_vs_actual"},
    {
        "key": "crop_performance",
        "label": "Crop Performance",
        "route": "reports:crop_performance",
    },
    {
        "key": "channel_performance",
        "label": "Channel Performance",
        "route": "reports:channel_performance",
    },
    {
        "key": "block_utilization",
        "label": "Block Utilization",
        "route": "reports:block_utilization",
    },
    {"key": "season_summary", "label": "Season Summary", "route": "reports:season_summary"},
]


class ReportContextMixin:
    excluded_statuses = REPORT_EXCLUDED_STATUSES
    default_status_priority = ("active", "complete")

    def resolve_planning_year(self, status_priority=None):
        return resolve_current_planning_year(
            status_priority=status_priority or self.default_status_priority
        )

    @staticmethod
    def normalize_week(week_num):
        return max(1, min(int(week_num), 52))

    def resolve_week(self, explicit_week=None):
        return self.normalize_week(explicit_week or date.today().isocalendar()[1])

    def week_window(self, year, week_num):
        week_num = self.normalize_week(week_num)
        week_monday = Week(year, week_num).monday()
        return week_monday, week_monday + timedelta(days=6)

    def week_navigation(self, week_num):
        week_num = self.normalize_week(week_num)
        return {
            "week_num": week_num,
            "prev_week_num": 52 if week_num == 1 else week_num - 1,
            "next_week_num": 1 if week_num == 52 else week_num + 1,
            "show_week_navigation": True,
        }

    def parse_week_range(self, request, default_start, default_end):
        week_start = self.normalize_week(request.GET.get("start", default_start))
        week_end = self.normalize_week(request.GET.get("end", default_end))
        if week_end < week_start:
            week_end = week_start
        return week_start, week_end


class AnalyzeViewMixin(ReportContextMixin):
    analyze_page = ""
    page_title = ""

    def build_analyze_context(self, year_obj, page_subtitle="", empty_message=""):
        return {
            "year": year_obj,
            "analyze_page": self.analyze_page,
            "analyze_links": ANALYZE_LINKS,
            "page_title": self.page_title,
            "page_subtitle": page_subtitle,
            "empty_message": empty_message,
        }
