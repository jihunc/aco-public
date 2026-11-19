"""Validated calendar cutoff, preserving unresolved natural-language dates."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def end_of_date_cutoff(due_date: date | None, timezone: str | None) -> datetime | None:
    if due_date is None or timezone is None:
        return None
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Unknown recipient timezone") from None
    local = datetime.combine(due_date + timedelta(days=1), time.min)
    a = local.replace(tzinfo=zone, fold=0)
    b = local.replace(tzinfo=zone, fold=1)
    if a.utcoffset() != b.utcoffset() or a.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != local:
        raise ValueError("Ambiguous or nonexistent cutoff needs clarification")
    return a.astimezone(UTC)
