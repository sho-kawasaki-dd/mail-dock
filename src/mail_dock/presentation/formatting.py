"""Shared display-formatting helpers for the presentation layer."""

from __future__ import annotations

from datetime import datetime

__all__ = ["format_local_datetime"]


def format_local_datetime(value: datetime | None) -> str:
    """Format a UTC-normalized datetime in the system's local timezone."""
    if value is None:
        return ""
    return value.astimezone().strftime("%Y-%m-%d %H:%M")
