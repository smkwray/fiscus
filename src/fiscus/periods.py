from __future__ import annotations

from calendar import monthrange


def month_end_iso(year: int, month: int) -> str:
    """Return the Gregorian month-end date for a validated year and month."""
    return f"{year:04d}-{month:02d}-{monthrange(year, month)[1]:02d}"
