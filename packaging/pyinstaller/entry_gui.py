"""Windowed PyInstaller entrypoint for mail-dock."""

from __future__ import annotations

import io
import sys

_INTERNAL_LOCK_PROBE_FLAG = "--maildock-internal-lock-probe"


def _ensure_standard_streams() -> None:
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()


def main() -> int:
    _ensure_standard_streams()
    arguments = sys.argv[1:]
    if arguments and arguments[0] == _INTERNAL_LOCK_PROBE_FLAG:
        if len(arguments) != 2:
            return 2
        try:
            from mail_dock.infrastructure.storage.lock_probe import run_lock_probe_child

            return run_lock_probe_child(arguments[1])
        except Exception:
            return 2

    from mail_dock.__main__ import main as run_application

    return run_application()


if __name__ == "__main__":
    raise SystemExit(main())
