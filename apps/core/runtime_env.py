"""core/runtime_env.py — safe, staff-visible deployment snapshot."""

from __future__ import annotations

import os
from pathlib import Path

from django.conf import settings


def deployment_channel() -> str:
    if os.environ.get("K_SERVICE") or os.environ.get("K_REVISION"):
        return "cloud_run"
    env = os.environ.get("DJANGO_ENV", "").lower()
    if env in {"prod", "production", "staging"}:
        return "cloud"
    return "local"


def _bool_label(value: bool) -> str:
    return "yes" if value else "no"


def _google_creds_summary() -> str:
    raw = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not raw:
        return "not set"
    path = Path(raw)
    if path.is_file():
        return f"file:{path.name}"
    return "set (path not found on disk)"


def build_runtime_config_rows() -> list[tuple[str, str]]:
    """Return (label, value) pairs; no secrets."""
    db = settings.DATABASES["default"]
    engine = db.get("ENGINE", "")
    rows: list[tuple[str, str]] = [
        ("Deployment channel", deployment_channel()),
        ("DJANGO_ENV", os.environ.get("DJANGO_ENV", "—")),
        ("DEBUG", _bool_label(settings.DEBUG)),
        ("Database engine", engine.rsplit(".", maxsplit=1)[-1]),
    ]
    if "sqlite" in engine.lower():
        rows.append(("SQLite path", str(db.get("NAME", "—"))))
    else:
        rows.append(("Postgres host", str(db.get("HOST", "—"))))
        rows.append(("Postgres name", str(db.get("NAME", "—"))))

    rows.extend(
        [
            ("RUNNER_MODE", os.environ.get("RUNNER_MODE", "—")),
            ("Google credentials", _google_creds_summary()),
            ("PROJECT_ID", os.environ.get("PROJECT_ID", "—")),
            ("REGION", os.environ.get("REGION", "—")),
            ("JOB_REGION", os.environ.get("JOB_REGION", "—")),
            ("K_SERVICE", os.environ.get("K_SERVICE", "—")),
            ("K_REVISION", os.environ.get("K_REVISION", "—")),
            ("ALLOWED_HOSTS", ", ".join(settings.ALLOWED_HOSTS) or "—"),
            (
                "CSRF_TRUSTED_ORIGINS",
                ", ".join(settings.CSRF_TRUSTED_ORIGINS) or "—",
            ),
            ("Static storage", settings.STATICFILES_STORAGE),
            ("WhiteNoise", _bool_label(getattr(settings, "WHITENOISE_AVAILABLE", False))),
            ("TIME_ZONE", settings.TIME_ZONE),
        ]
    )

    job_keys = [
        "CLOUD_RUN_IMPORT_REFERENCE_JOB",
        "CLOUD_RUN_IMPORT_PREFLIGHT_JOB",
        "CLOUD_RUN_IMPORT_APPLY_JOB",
        "CLOUD_RUN_IMPORT_PULL_STAGE_A2_JOB",
    ]
    for key in job_keys:
        rows.append((key, "set" if os.environ.get(key) else "not set"))

    return rows
