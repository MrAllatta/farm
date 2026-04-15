from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        # Register system checks for production-safety guardrails.
        from . import checks  # noqa: F401
