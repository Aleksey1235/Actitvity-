from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc

SEGMENTS = {
    "morning": (time(6, 0), time(12, 0)),
    "day": (time(12, 0), time(18, 0)),
    "evening": (time(18, 0), time(23, 59, 59, 999999)),
    "night": (time(0, 0), time(6, 0)),
}

SEGMENT_ALIASES = {
    "утро": "morning",
    "morning": "morning",
    "день": "day",
    "day": "day",
    "вечер": "evening",
    "evening": "evening",
    "ночь": "night",
    "night": "night",
    "плавающий": "floating",
    "floating": "floating",
    "flex": "floating",
}

SEGMENT_LABELS = {
    "morning": "🌅 Утро",
    "day": "☀️ День",
    "evening": "🌆 Вечер",
    "night": "🌙 Ночь",
    "floating": "🔄 Плавающий",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(UTC).isoformat()


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def validate_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {name}") from exc


def parse_local_datetime(value: str, tz_name: str) -> datetime:
    """Parse YYYY-MM-DD HH:MM in guild local time and return UTC."""
    tz = validate_timezone(tz_name)
    try:
        naive = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError("Use format YYYY-MM-DD HH:MM, e.g. 2026-08-20 21:00") from exc
    return naive.replace(tzinfo=tz).astimezone(UTC)


def parse_local_date(value: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Use date format YYYY-MM-DD, e.g. 2026-08-20") from exc


def local_date(dt: datetime, tz_name: str) -> date:
    return dt.astimezone(validate_timezone(tz_name)).date()


def local_segment(dt: datetime, tz_name: str) -> str:
    local = dt.astimezone(validate_timezone(tz_name)).time()
    if time(0, 0) <= local < time(6, 0):
        return "night"
    if time(6, 0) <= local < time(12, 0):
        return "morning"
    if time(12, 0) <= local < time(18, 0):
        return "day"
    return "evening"


def day_group(dt: datetime, tz_name: str) -> str:
    local = dt.astimezone(validate_timezone(tz_name))
    return "weekend" if local.weekday() >= 5 else "weekday"


def next_local_monday(now: datetime, tz_name: str) -> datetime:
    tz = validate_timezone(tz_name)
    local = now.astimezone(tz)
    days = (7 - local.weekday()) % 7
    if days == 0:
        days = 7
    target = (local + timedelta(days=days)).date()
    return datetime.combine(target, time.min, tzinfo=tz).astimezone(UTC)


def current_week_bounds(now: datetime, tz_name: str) -> tuple[datetime, datetime]:
    tz = validate_timezone(tz_name)
    local = now.astimezone(tz)
    monday = local.date() - timedelta(days=local.weekday())
    start = datetime.combine(monday, time.min, tzinfo=tz)
    end = start + timedelta(days=7)
    return start.astimezone(UTC), end.astimezone(UTC)


def previous_complete_week(now: datetime, tz_name: str) -> tuple[datetime, datetime]:
    current_start, _ = current_week_bounds(now, tz_name)
    return current_start - timedelta(days=7), current_start


def comparable_previous_period(
    start: datetime, end: datetime
) -> tuple[datetime, datetime]:
    duration = end - start
    prev_start = start - timedelta(days=7)
    return prev_start, prev_start + duration


def parse_segments(value: str) -> set[str]:
    raw = [p.strip().lower() for p in value.replace(";", ",").split(",") if p.strip()]
    if not raw:
        raise ValueError("Specify at least one segment")
    result: set[str] = set()
    for item in raw:
        mapped = SEGMENT_ALIASES.get(item)
        if not mapped:
            raise ValueError(
                f"Unknown segment '{item}'. Use: утро, день, вечер, ночь, плавающий"
            )
        result.add(mapped)
    if "floating" in result and len(result) > 1:
        raise ValueError("'плавающий' cannot be combined with fixed segments")
    return result


def dt_to_discord_timestamp(dt: datetime, style: str = "R") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"
