"""Guards for operations that are limited to remote mail accounts."""

from __future__ import annotations

from mail_dock.domain.errors import PermanentError
from mail_dock.domain.repository import BaseMessageRepository, MessageRecord

PST_PROVIDER_TYPE = "pst_import"


def account_id(account: MessageRecord) -> str | None:
    """Return the stable account identifier from a repository record."""

    value = account.get("id", account.get("account_id"))
    return value if isinstance(value, str) and value else None


def is_pst_account(account: MessageRecord) -> bool:
    """Return whether an account represents a local PST archive."""

    return account.get("provider_type") == PST_PROVIDER_TYPE


def ensure_imap_account(repo: BaseMessageRepository, account_id_value: str) -> None:
    """Reject local archives before an operation that requires IMAP."""

    for account in repo.list_accounts():
        if account_id(account) != account_id_value:
            continue
        if is_pst_account(account):
            raise PermanentError(
                f"PST archive account cannot be used for an IMAP operation: {account_id_value}"
            )
        return


def ensure_imap_message(repo: BaseMessageRepository, message: MessageRecord) -> None:
    """Reject a message whose owning account is a local PST archive."""

    value = message.get("account_id")
    if isinstance(value, str) and value:
        ensure_imap_account(repo, value)
