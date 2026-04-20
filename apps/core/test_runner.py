from django.test.runner import DiscoverRunner


class ProjectDiscoverRunner(DiscoverRunner):
    """Ensure `manage.py test` runs project app suites by default."""

    default_project_labels = ["core", "planning", "operations", "reference", "sales", "reports"]

    def build_suite(self, test_labels=None, **kwargs):
        labels = list(test_labels or [])
        if not labels:
            labels = self.default_project_labels
        return super().build_suite(test_labels=labels, **kwargs)
