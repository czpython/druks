from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown IANA timezone: {value!r}") from exc
    return value


def operator_local_day(timezone_name: str, now: datetime) -> tuple[ZoneInfo, datetime]:
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")
    local_start = now.astimezone(timezone).replace(hour=0, minute=0, second=0, microsecond=0)
    return timezone, local_start
