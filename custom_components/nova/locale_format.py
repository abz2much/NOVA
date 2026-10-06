"""Country-aware spoken date formatting."""
from __future__ import annotations


MONTH_FIRST_COUNTRIES = frozenset({"US", "CA", "PH"})


def _country(hass) -> str:
    try:
        return str(hass.config.country or "").strip().upper()
    except Exception:
        return ""


def format_date(dt, hass, *, include_year: bool = False,
                pad_month_first_day: bool = False) -> str:
    """Format a spoken date, preserving the old US form when country is absent."""
    country = _country(hass)
    month_first = not country or country in MONTH_FIRST_COUNTRIES
    weekday = dt.strftime("%A")
    month = dt.strftime("%B")
    if month_first:
        day = f"{dt.day:02d}" if pad_month_first_day or include_year else str(dt.day)
        if include_year:
            return f"{weekday}, {month} {day}, {dt.year}"
        return f"{weekday} {month} {day}"
    year = f" {dt.year}" if include_year else ""
    return f"{weekday} {dt.day} {month}{year}"
