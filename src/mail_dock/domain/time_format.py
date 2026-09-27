"""Pure UTC datetime helpers shared by parsing and use-case layers."""

from __future__ import annotations

from datetime import UTC, datetime


def to_utc(value: datetime | None) -> datetime | None:
    """Return ``value`` normalized to UTC, treating naive values as UTC."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_utc_iso8601(value: datetime) -> str:
    """Format a datetime as a UTC ISO 8601 timestamp without fractional seconds."""

    utc_value = to_utc(value)
    if utc_value is None:
        raise ValueError("datetime value is required")
    return (
        f"{utc_value.year:04d}-{utc_value.month:02d}-{utc_value.day:02d}"
        f"T{utc_value.hour:02d}:{utc_value.minute:02d}:{utc_value.second:02d}Z"
    )


__all__ = ["to_utc", "to_utc_iso8601"]
