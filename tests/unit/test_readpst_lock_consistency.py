import json
import re
from pathlib import Path
from typing import Any, cast

from mail_dock.infrastructure.importers.readpst_locator import _WINDOWS_READPST_DLLS

ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = ROOT / "packaging" / "readpst" / "msys2-packages.lock.json"


def _load_lock() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(LOCK_PATH.read_text(encoding="utf-8")))


def test_locked_readpst_dlls_match_runtime_locator() -> None:
    lock = _load_lock()
    locked_dlls = {
        file_record["destination"]
        for package in lock["packages"]
        for file_record in package["extracted_files"]
        if file_record["destination"].lower().endswith(".dll")
    }

    assert locked_dlls == set(_WINDOWS_READPST_DLLS)


def test_locked_packages_and_sources_are_pinned_and_referenced_once() -> None:
    lock = _load_lock()
    sources = {source["pkgbase"]: source for source in lock["sources"]}
    references = [package["source_ref"] for package in lock["packages"]]

    assert len(lock["packages"]) == 14
    assert set(references) == set(sources)
    assert len(sources) == 14
    assert len(lock["sources"]) == len(sources)
    for package in lock["packages"]:
        assert package["source_ref"] in sources
        assert package["binary"]["filename"].startswith(f"{package['name']}-{package['version']}-")
        assert re.fullmatch(r"[0-9A-F]{64}", package["binary"]["sha256"])
    for source in lock["sources"]:
        assert source["contains_upstream"] is True
        assert re.fullmatch(r"[0-9A-F]{64}", source["sha256"])
