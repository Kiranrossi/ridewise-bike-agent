"""Time helpers. The database stores UTC text; users think in IST."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
DB_FORMAT = "%Y-%m-%d %H:%M:%S"


def parse_utc(value: str | None) -> datetime | None:
    """Parse a DB timestamp ('YYYY-MM-DD HH:MM:SS', UTC) into an aware datetime."""
    if not value:
        return None
    return datetime.strptime(value.strip(), DB_FORMAT).replace(tzinfo=UTC)


def to_db(dt: datetime) -> str:
    """Format an aware datetime as a DB timestamp string (UTC)."""
    return dt.astimezone(UTC).strftime(DB_FORMAT)


def fmt_ist(dt: datetime | None) -> str | None:
    """Human-readable IST time, e.g. 'Sat 10 Oct 2026, 08:48 IST'."""
    if dt is None:
        return None
    return dt.astimezone(IST).strftime("%a %d %b %Y, %H:%M IST")


def now() -> datetime:
    """Current time (UTC). Set RIDEWISE_NOW='YYYY-MM-DD HH:MM:SS' (UTC) to freeze it for tests/demos."""
    frozen = os.getenv("RIDEWISE_NOW")
    return parse_utc(frozen) if frozen else datetime.now(UTC)


def hours_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600
