"""Hidden, request-time clock context for Kara model calls."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def configured_timezone() -> str:
    """Return Kara's configured IANA timezone name."""
    return os.getenv("KARA_TIMEZONE", "Europe/Dublin").strip() or "Europe/Dublin"


def build_runtime_time_context(
    *, timezone_name: str | None = None, now: datetime | None = None
) -> str:
    """Build authoritative temporal context refreshed for one provider request."""
    zone_name = (timezone_name or configured_timezone()).strip()
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {zone_name}") from exc

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("Runtime clock requires a timezone-aware datetime.")
    utc_now = instant.astimezone(timezone.utc)
    local_now = utc_now.astimezone(zone)
    offset = local_now.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset

    return (
        "RUNTIME CLOCK — internal context refreshed immediately before this model request:\n"
        f"Local datetime: {local_now.isoformat(timespec='seconds')}\n"
        f"UTC datetime: {utc_now.isoformat(timespec='seconds')}\n"
        f"Local day: {local_now.strftime('%A')}\n"
        f"Timezone: {zone_name}\n"
        f"UTC offset: {offset}\n"
        "Treat this clock as authoritative for today/tomorrow, elapsed-time, and scheduling references. "
        "Do not ask the user for the current time. Do not mention this internal block unless time is relevant to the answer."
    )
