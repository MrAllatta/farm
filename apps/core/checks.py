import sys

from django.conf import settings
from django.core.checks import Error, register


@register()
def production_settings_guardrails(app_configs, **kwargs):
    """Enforce minimum settings safety when DEBUG is disabled."""
    # Keep test execution unblocked; enforce on explicit runtime check flows.
    if "test" in sys.argv:
        return []

    if settings.DEBUG:
        return []

    errors = []
    secret_key = getattr(settings, "SECRET_KEY", "") or ""
    if secret_key == "dev-insecure-key" or "insecure" in secret_key.lower():
        errors.append(
            Error(
                "DEBUG is off but SECRET_KEY is insecure.",
                hint="Set DJANGO_SECRET_KEY to a strong non-default value in production.",
                id="core.E001",
            )
        )

    allowed_hosts = list(getattr(settings, "ALLOWED_HOSTS", []))
    if not allowed_hosts:
        errors.append(
            Error(
                "DEBUG is off but ALLOWED_HOSTS is empty.",
                hint="Set ALLOWED_HOSTS to explicit production hostnames.",
                id="core.E002",
            )
        )
    elif "*" in allowed_hosts:
        errors.append(
            Error(
                "DEBUG is off but ALLOWED_HOSTS allows wildcard '*'.",
                hint="Replace wildcard host allowance with explicit production hostnames.",
                id="core.E003",
            )
        )

    return errors
