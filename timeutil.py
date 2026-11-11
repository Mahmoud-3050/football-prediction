"""Cairo-local display and calendar-day helpers.

Football-Data.org returns UTC timestamps. User-facing times and /matches days
use Africa/Cairo.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

CAIRO_TZ = ZoneInfo("Africa/Cairo")
UTC = timezone.utc


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_cairo(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(CAIRO_TZ)
    moment = parse_utc_timestamp(value if isinstance(value, str) else None)
    if moment is None:
        return None
    return moment.astimezone(CAIRO_TZ)


def format_kickoff_cairo(value: str | None) -> str | None:
    """Full kickoff line for prediction reports."""

    moment = to_cairo(value)
    if moment is None:
        return None
    return moment.strftime("%Y-%m-%d %H:%M Cairo")


def format_kickoff_short_cairo(value: str | None) -> str:
    """Short kickoff for fixture lists."""

    moment = to_cairo(value)
    if moment is None:
        return "----"
    return moment.strftime("%H:%M Cairo")


def format_datetime_cairo(value: str | datetime | None) -> str | None:
    """Dashboard-style full timestamp in Cairo."""

    moment = to_cairo(value)
    if moment is None:
        return None
    return moment.strftime("%d %b %Y · %H:%M Cairo")


def format_datetime_short_cairo(value: str | datetime | None) -> str | None:
    """Dashboard-style short timestamp in Cairo (match cards)."""

    moment = to_cairo(value)
    if moment is None:
        return None
    return moment.strftime("%d %b · %H:%M Cairo")


def format_clock_cairo(value: str | datetime | None) -> str | None:
    """Compact date+clock for job run stamps."""

    moment = to_cairo(value)
    if moment is None:
        return None
    return moment.strftime("%Y-%m-%d %H:%M Cairo")


def cairo_today() -> date:
    return datetime.now(CAIRO_TZ).date()


def cairo_day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """Inclusive Cairo day as [start, end) in UTC."""

    start_cairo = datetime.combine(day, time.min, tzinfo=CAIRO_TZ)
    end_cairo = start_cairo + timedelta(days=1)
    return start_cairo.astimezone(UTC), end_cairo.astimezone(UTC)


def api_date_window_for_cairo_day(day: date) -> tuple[str, str]:
    """YYYY-MM-DD range to request from Football-Data for one Cairo day.

    Football-Data.org `/v4/matches` treats `dateTo` as exclusive: a same-day
    `dateFrom`/`dateTo` returns nothing, and `dateFrom=D&dateTo=D+1` returns
    matches whose UTC calendar date is D. Widen `dateTo` by one day so every
    UTC date touched by the Cairo day is included, then filter locally.
    """

    start_utc, end_utc = cairo_day_bounds_utc(day)
    last_instant = end_utc - timedelta(seconds=1)
    date_from = start_utc.date()
    date_to = last_instant.date() + timedelta(days=1)
    return date_from.isoformat(), date_to.isoformat()


def falls_on_cairo_day(utc_value: str | None, day: date) -> bool:
    moment = to_cairo(utc_value)
    if moment is None:
        return False
    return moment.date() == day
