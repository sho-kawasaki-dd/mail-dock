"""Helpers for forcing a Dovecot Maildir to create a new UIDVALIDITY."""

from __future__ import annotations

import subprocess
from pathlib import Path


def force_uidvalidity_change(mailbox_path: Path) -> None:
    """Remove Dovecot UID state so the next mailbox open creates a new generation.

    ``mailbox_path`` must be the Maildir directory for the mailbox inside the
    Dovecot container or a bind-mounted test volume. Dovecot recreates both
    files when the mailbox is opened again. Existing messages remain in place,
    but their UID generation is intentionally reset for integration tests.
    """

    for state_file in ("dovecot-uidvalidity", "dovecot-uidlist"):
        state_path = mailbox_path / state_file
        if state_path.exists():
            state_path.unlink()


def force_uidvalidity_change_in_container(
    compose_file: Path,
    *,
    service: str = "dovecot",
    mailbox_path: str = "/var/mail/vmail/testuser/Maildir",
) -> None:
    """Reset UID state in a running Dovecot Compose service.

    This variant supports the named volume used by the test Compose file.
    ``mailbox_path`` is restricted to an absolute container path so test code
    cannot accidentally pass a host-side relative path to ``docker exec``.
    """

    if not mailbox_path.startswith("/"):
        raise ValueError("mailbox_path must be an absolute container path")

    state_paths = [f"{mailbox_path}/{name}" for name in ("dovecot-uidvalidity", "dovecot-uidlist")]
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "exec",
            "-T",
            service,
            "rm",
            "-f",
            *state_paths,
        ],
        check=True,
    )
