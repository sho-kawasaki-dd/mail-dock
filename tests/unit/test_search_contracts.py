from __future__ import annotations

import ast
from pathlib import Path


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])
    return imported


def test_search_domain_has_no_external_dependencies() -> None:
    search_path = Path(__file__).parents[2] / "src" / "mail_dock" / "domain" / "search.py"
    assert _imports(search_path).issubset(
        {
            "__future__",
            "abc",
            "collections",
            "dataclasses",
            "datetime",
            "json",
            "typing",
            "mail_dock",
        }
    )


def test_search_and_message_ingestion_share_domain_normalizer() -> None:
    root = Path(__file__).parents[2] / "src" / "mail_dock"
    message_repository = root / "infrastructure" / "database" / "message_repository.py"
    search_query = root / "usecases" / "search_query.py"

    assert (
        "from mail_dock.domain.normalize import normalize_for_search"
        in message_repository.read_text(encoding="utf-8")
    )
    assert "from mail_dock.domain.normalize import normalize_for_search" in search_query.read_text(
        encoding="utf-8"
    )


def test_usecases_do_not_import_sqlite_or_infrastructure() -> None:
    usecases_dir = Path(__file__).parents[2] / "src" / "mail_dock" / "usecases"
    forbidden = {"sqlite3", "infrastructure", "keyring"}

    for source_path in usecases_dir.glob("*.py"):
        assert _imports(source_path).isdisjoint(forbidden), source_path
