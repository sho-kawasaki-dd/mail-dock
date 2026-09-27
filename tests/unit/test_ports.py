from __future__ import annotations

import ast
from pathlib import Path

from mail_dock.domain.ports import BaseCredentialStore
from mail_dock.usecases.register_account import register_account
from tests.support.in_memory_repository import InMemoryMessageRepository


class MemoryCredentialStore(BaseCredentialStore):
    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], str] = {}

    def set_password(self, account_id: str, password: str) -> None:
        self.set_secret(account_id, "password", password)

    def get_password(self, account_id: str) -> str | None:
        return self.get_secret(account_id, "password")

    def delete_password(self, account_id: str) -> None:
        self.delete_secret(account_id, "password")

    def set_secret(self, account_id: str, name: str, value: str) -> None:
        self.secrets[(account_id, name)] = value

    def get_secret(self, account_id: str, name: str) -> str | None:
        return self.secrets.get((account_id, name))

    def delete_secret(self, account_id: str, name: str) -> None:
        self.secrets.pop((account_id, name), None)

    def delete_all_secrets(self, account_id: str) -> None:
        for key in tuple(self.secrets):
            if key[0] == account_id:
                del self.secrets[key]


def test_register_account_uses_only_repository_and_credential_ports() -> None:
    repository = InMemoryMessageRepository()
    credentials = MemoryCredentialStore()

    account_id = register_account(
        repository,
        credentials,
        account_id="account",
        host="imap.example.com",
        port=993,
        username="user@example.com",
        password="secret",
        display_name="Example",
    )

    assert account_id == "account"
    assert credentials.secrets == {("account", "password"): "secret"}
    assert repository.list_accounts() == [
        {
            "id": "account",
            "provider_type": "imap",
            "display_name": "Example",
            "host": "imap.example.com",
            "port": 993,
            "username": "user@example.com",
            "is_enabled": 1,
            "tls_mode": "implicit",
            "ca_cert_path": None,
            "auth_type": "password",
            "oauth_provider": None,
            "oauth_client_id": None,
            "oauth_tenant": None,
        }
    ]


def test_usecases_do_not_import_provider_database_or_filesystem_apis() -> None:
    usecases_dir = Path(__file__).parents[2] / "src" / "mail_dock" / "usecases"
    forbidden = {
        "imaplib",
        "keyring",
        "pathlib",
        "shutil",
        "socket",
        "sqlite3",
        "ssl",
        "subprocess",
    }

    for source_path in usecases_dir.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        imports = {
            node.names[0].name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import) and node.names
        }
        imports.update(
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        )
        assert imports.isdisjoint(forbidden), source_path


def _imported_module_names(source_path: Path) -> set[str]:
    """Return the full dotted module name of every import in ``source_path``."""

    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)
    return imports


def _matches_forbidden(module_name: str, forbidden_prefixes: tuple[str, ...]) -> bool:
    return any(
        module_name == prefix or module_name.startswith(prefix + ".")
        for prefix in forbidden_prefixes
    )


def test_presentation_does_not_import_sqlite() -> None:
    presentation_root = Path(__file__).parents[2] / "src" / "mail_dock" / "presentation"

    for source_path in presentation_root.rglob("*.py"):
        imports = _imported_module_names(source_path)
        sqlite_imports = {name for name in imports if _matches_forbidden(name, ("sqlite3",))}
        assert not sqlite_imports, (source_path, sqlite_imports)


def test_domain_and_usecases_do_not_import_pyside6_or_infrastructure() -> None:
    src_root = Path(__file__).parents[2] / "src" / "mail_dock"
    forbidden = ("PySide6", "mail_dock.infrastructure", "webbrowser")

    for package_name in ("domain", "usecases"):
        package_dir = src_root / package_name
        for source_path in package_dir.rglob("*.py"):
            imports = _imported_module_names(source_path)
            offending = {name for name in imports if _matches_forbidden(name, forbidden)}
            assert not offending, (source_path, offending)
