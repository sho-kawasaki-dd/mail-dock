"""Helpers for forcing a Dovecot Maildir to create a new UIDVALIDITY."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path


def force_uidvalidity_change(mailbox_path: Path) -> None:
    """Change the Dovecot uidlist generation for a local Maildir mailbox.

    Dovecot stores the effective UIDVALIDITY in the ``V`` field of
    ``dovecot-uidlist``. Existing messages remain in place while their UID
    generation is intentionally changed for integration tests.
    """

    uidlist = mailbox_path / "dovecot-uidlist"
    contents = uidlist.read_text(encoding="ascii")
    lines = contents.splitlines(keepends=True)
    if not lines or " V" not in lines[0]:
        raise ValueError(f"Dovecot uidlist has no UIDVALIDITY field: {uidlist}")
    prefix, _, suffix = lines[0].partition(" V")
    _, _, remainder = suffix.partition(" ")
    lines[0] = f"{prefix} V{int(time.time()) + 100} {remainder}"
    uidlist.write_text("".join(lines), encoding="ascii")


def force_uidvalidity_change_in_container(
    compose_file: Path,
    *,
    service: str = "dovecot",
    mailbox_path: str = "/var/mail/vmail/testuser/Maildir",
) -> None:
    """Change the uidlist generation in a running Dovecot Compose service.

    This variant supports the named volume used by the test Compose file.
    ``mailbox_path`` is restricted to an absolute container path so test code
    cannot accidentally pass a host-side relative path to ``docker exec``.
    """

    if not mailbox_path.startswith("/"):
        raise ValueError("mailbox_path must be an absolute container path")

    uidlist_path = f"{mailbox_path}/dovecot-uidlist"
    replacement = f"s/^3 V[0-9]*/3 V{int(time.time()) + 100}/"
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "exec",
            "-T",
            service,
            "sed",
            "-i",
            replacement,
            uidlist_path,
        ],
        check=True,
    )
