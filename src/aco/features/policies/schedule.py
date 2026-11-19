"""Recipient-local policy arithmetic using the standard IANA timezone database."""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class CalendarDecision:
    allowed: bool
    reason: str | None
    next_local: datetime | None


def unique_instant(candidate: datetime, zone: ZoneInfo) -> bool:
    # A nonexistent wall time cannot round-trip. A fold has two different UTC instants.
    first = candidate.replace(tzinfo=zone, fold=0)
    second = candidate.replace(tzinfo=zone, fold=1)
    return (
        first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == candidate
        and first.utcoffset() == second.utcoffset()
    )


def recipient_calendar(
    candidate: datetime,
    recipient_timezone: str | None,
    *,
    tenant_timezone: str | None = None,
    owner_confirmed: bool = False,
    contradictory: bool = False,
    opens_at: str = "09:00",
    closes_at: str = "17:00",
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4),
) -> CalendarDecision:
    zone_name = recipient_timezone or (tenant_timezone if owner_confirmed else None)
    if contradictory or not zone_name:
        return CalendarDecision(False, "timezone_review", None)
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return CalendarDecision(False, "timezone_review", None)
    opens, closes = time.fromisoformat(opens_at), time.fromisoformat(closes_at)
    if (
        not weekdays
        or any(day not in range(5) for day in weekdays)
        or not time(9) <= opens < closes <= time(17)
    ):
        raise ValueError("Invalid restricted contact window")
    local = candidate.astimezone(zone).replace(tzinfo=None) if candidate.tzinfo else candidate
    allowed = local.weekday() in weekdays and opens <= local.time() < closes and unique_instant(local, zone)
    if allowed:
        return CalendarDecision(True, None, local.replace(tzinfo=zone))
    future = local
    while True:
        if future.weekday() not in weekdays or future.time() >= closes:
            future = datetime.combine(future.date() + timedelta(days=1), opens)
            continue
        if future.time() < opens:
            future = datetime.combine(future.date(), opens)
        if unique_instant(future, zone):
            return CalendarDecision(False, "quiet_hours", future.replace(tzinfo=zone))
        # Only transition gaps/folds inside an otherwise permitted window need scanning.
        # Second precision selects the first unique valid instant, never a prior fold.
        future = future.replace(microsecond=0) + timedelta(seconds=1)
