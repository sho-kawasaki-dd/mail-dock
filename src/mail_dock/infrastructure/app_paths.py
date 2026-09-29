"""Resolve resources from the source tree or a frozen application bundle."""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """Return whether mail-dock is running from a frozen executable."""

    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Return the root used for bundled application resources."""

    if is_frozen():
        meipass_attribute = "_MEIPASS"
        return Path(getattr(sys, meipass_attribute))
    return Path(__file__).resolve().parents[3]
