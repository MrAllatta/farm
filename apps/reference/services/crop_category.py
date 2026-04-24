"""DG-14: derive persisted ``CropInfo.fresh_or_storage`` from weeks + field-hold flag."""

from __future__ import annotations


def derive_fresh_or_storage(*, storage_weeks: int, can_hold_in_field: bool) -> str:
    """Return canonical category: ``fresh``, ``storage``, or ``fresh_holds``.

    Rules (LC-4 / ``docs/prototype-build-backlog.md``):

    - ``storage_weeks > 0`` and ``can_hold_in_field`` → ``fresh_holds``
    - ``storage_weeks > 0`` and not ``can_hold_in_field`` → ``storage``
    - else (including zero weeks) → ``fresh`` (fresh perishable)
    """
    try:
        sw = int(storage_weeks)
    except (TypeError, ValueError):
        sw = 0
    if sw < 0:
        sw = 0
    if sw > 0 and can_hold_in_field:
        return "fresh_holds"
    if sw > 0 and not can_hold_in_field:
        return "storage"
    return "fresh"
