"""LIVE-12: block accidental apply of committed ``sample_import`` into default ``farm/db.sqlite3``.

When ``FARM_SQLITE_PATH`` is unset, SQLite uses ``BASE_DIR / db.sqlite3``. Operators expect
``make import-sample`` / a sandbox path for fixture trees; raw ``manage.py`` against the
committed sample directory should not silently pollute the dev DB.
"""

from __future__ import annotations

import os
from pathlib import Path


def live12_block_message_for_sample_into_dev_sqlite(
    *,
    data_dir: str,
    validate_only: bool,
    dry_run: bool,
    farm_sqlite_env: str,
    db_engine: str,
    db_name,
    base_dir: Path,
    allow_escape: bool = False,
) -> str | None:
    """Return a ``CommandError`` message if apply should be blocked; otherwise ``None``."""
    if allow_escape:
        return None
    if validate_only or dry_run:
        return None
    if farm_sqlite_env.strip():
        return None
    if (db_engine or "").lower() != "django.db.backends.sqlite3":
        return None

    sample_root = (base_dir / "data" / "sample_import").resolve()
    raw = Path(data_dir).expanduser()
    resolved = raw.resolve() if raw.is_absolute() else (Path.cwd() / raw).resolve()
    try:
        if resolved != sample_root:
            return None
    except OSError:
        return None

    try:
        default_db = (base_dir / "db.sqlite3").resolve()
        active = Path(db_name).expanduser().resolve()
    except (OSError, TypeError):
        return None
    if active != default_db:
        return None

    return (
        "Refusing to apply committed farm/data/sample_import into the default farm/db.sqlite3 "
        "while FARM_SQLITE_PATH is unset (LIVE-12). Use `make import-sample`, set FARM_SQLITE_PATH "
        "to a sandbox file, or pass --allow-sample-into-repo-dev-sqlite if you intend to write "
        "sample channels into this database."
    )
