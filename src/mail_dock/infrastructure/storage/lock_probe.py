"""Child-process implementation for probing exclusive file locks."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

_LOCK_CONFLICT_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}


def run_lock_probe_child(path: str) -> int:
    """Try to acquire a non-blocking lock; return 0, 1, or 2 on outcome."""

    try:
        with Path(path).open("r+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                flock_name = "flock"
                lock_ex_name = "LOCK_EX"
                lock_nb_name = "LOCK_NB"
                flock = getattr(fcntl, flock_name)
                lock_ex = getattr(fcntl, lock_ex_name)
                lock_nb = getattr(fcntl, lock_nb_name)
                flock(handle.fileno(), lock_ex | lock_nb)
    except OSError as error:
        return 1 if error.errno in _LOCK_CONFLICT_ERRNOS else 2
    except Exception:
        return 2
    return 0


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if len(arguments) != 1:
        raise SystemExit(2)
    raise SystemExit(run_lock_probe_child(arguments[0]))
