import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

from tools.collect_licenses import (
    CollectionError,
    _check_inventory,
    _collect_qtwebengine,
    _copy_python_licenses,
    _marker_environment,
    _parse_args,
    _requirement_applies,
    _write_qtwebengine_source_record,
    main,
)


def _add_file(archive: tarfile.TarFile, name: str, content: str) -> None:
    data = content.encode("utf-8")
    member = tarfile.TarInfo(name)
    member.size = len(data)
    archive.addfile(member, io.BytesIO(data))


def test_dependency_markers_exclude_extras_and_evaluate_environment_versions() -> None:
    environment = _marker_environment()
    environment.update(sys_platform="win32", python_version="3.13", extra="")

    assert _requirement_applies(
        "pywin32-ctypes>=0.2; sys_platform == 'win32' and python_version >= '3.12'",
        environment,
    )
    assert not _requirement_applies("pytest; extra == 'test'", environment)
    assert not _requirement_applies('old-runtime; python_version < "3.9"', environment)


def test_collect_python_licenses_copies_every_runtime_package(tmp_path: Path) -> None:
    records = _copy_python_licenses(tmp_path)

    names = {record["name"].lower().replace("_", "-") for record in records}
    assert "beautifulsoup4" in names
    assert "pytest" not in names
    assert "soupsieve" in names
    assert all(
        (tmp_path / license_file).is_file()
        for record in records
        for license_file in record["license_files"]
    )
    manifest = tmp_path / "python-packages.json"
    manifest.write_text(json.dumps({"packages": records}), encoding="utf-8")
    inventory = Path(__file__).resolve().parents[2] / "THIRD-PARTY-LICENSES.md"
    _check_inventory(manifest, inventory)


def test_collect_qtwebengine_preserves_notices_and_resolves_readme_references(
    tmp_path: Path,
) -> None:
    source = tmp_path / "qtwebengine.tar.gz"
    root = "qtwebengine-everywhere-src-6.11.1"
    chromium = f"{root}/src/3rdparty/chromium"
    with tarfile.open(source, "w:gz") as archive:
        _add_file(archive, f"{root}/LICENSE.Chromium", "Chromium license")
        _add_file(archive, f"{root}/CHROMIUM_VERSION", "140.0.7339.264")
        _add_file(archive, f"{chromium}/LICENSE", "Chromium third-party license index")
        _add_file(
            archive,
            f"{chromium}/third_party/zlib/README.chromium",
            "License File: LICENSE\nLicense File: //third_party/extra/LICENSE\n",
        )
        _add_file(archive, f"{chromium}/third_party/zlib/LICENSE", "zlib license")
        _add_file(archive, f"{chromium}/third_party/extra/LICENSE", "extra license")
        _add_file(
            archive, f"{chromium}/third_party/absent/README.chromium", "License File: MISSING"
        )

    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    digest, missing = _collect_qtwebengine(source, expected_hash, tmp_path / "licenses")

    assert digest == expected_hash.upper()
    assert (tmp_path / "licenses/qtwebengine/LICENSE.Chromium").is_file()
    assert (
        tmp_path / "licenses/qtwebengine/src/3rdparty/chromium/third_party/zlib/LICENSE"
    ).read_text() == "zlib license"
    assert (
        tmp_path / "licenses/qtwebengine/src/3rdparty/chromium/third_party/extra/LICENSE"
    ).is_file()
    assert len(missing) == 1
    _write_qtwebengine_source_record(tmp_path / "licenses", source, digest, missing)
    provenance = json.loads((tmp_path / "licenses/qtwebengine-source.json").read_text())
    assert provenance["qt_version"] == "6.11.1"
    assert provenance["chromium_version"] == "140.0.7339.264"
    assert provenance["sha256"] == expected_hash.upper()
    assert provenance["missing_license_references"] == missing


def test_collect_qtwebengine_rejects_hash_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "qtwebengine.tar.gz"
    source.write_bytes(b"not the locked archive")

    with pytest.raises(CollectionError, match="SHA-256 mismatch"):
        _collect_qtwebengine(source, "0" * 64, tmp_path / "licenses")


def test_check_inventory_fails_for_an_unlisted_dependency(tmp_path: Path) -> None:
    manifest = tmp_path / "python-packages.json"
    inventory = tmp_path / "THIRD-PARTY-LICENSES.md"
    manifest.write_text(json.dumps({"packages": [{"name": "unlisted-package"}]}), encoding="utf-8")
    inventory.write_text(
        "| Dependency | License | Project |\n| --- | --- | --- |\n", encoding="utf-8"
    )

    with pytest.raises(CollectionError, match="unlisted-package"):
        _check_inventory(manifest, inventory)


def test_inventory_cli_returns_success_for_the_runtime_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    packages = _copy_python_licenses(tmp_path)
    (tmp_path / "python-packages.json").write_text(
        json.dumps({"packages": packages}), encoding="utf-8"
    )
    inventory = Path(__file__).resolve().parents[2] / "THIRD-PARTY-LICENSES.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collect_licenses.py",
            "--check-inventory",
            str(inventory),
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert main() == 0


def test_inventory_cli_returns_failure_for_an_unlisted_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "python-packages.json").write_text(
        json.dumps({"packages": [{"name": "unlisted-package"}]}), encoding="utf-8"
    )
    inventory = tmp_path / "THIRD-PARTY-LICENSES.md"
    inventory.write_text("| Dependency | License | Project |\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collect_licenses.py",
            "--check-inventory",
            str(inventory),
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert main() == 1


def test_inventory_cli_does_not_require_qt_archive_or_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["collect_licenses.py", "--check-inventory", "THIRD-PARTY-LICENSES.md"],
    )

    args = _parse_args()

    assert args.check_inventory == Path("THIRD-PARTY-LICENSES.md")


def test_collection_cli_requires_a_locked_qtwebengine_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["collect_licenses.py", "--qtwebengine-source", "qtwebengine.tar.xz"],
    )

    with pytest.raises(SystemExit, match="2"):
        _parse_args()
