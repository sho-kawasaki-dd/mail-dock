"""Collect runtime dependency and Qt license materials for release bundles."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import posixpath
import re
import shutil
import sys
import tarfile
from collections import deque
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict

_LICENSE_NAME = re.compile(r"(?i)^(?:license|licence|copying|copyright|notice|unlicense)")
_PACKAGE_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


class CollectionError(RuntimeError):
    """Raised when required license material is missing or inconsistent."""


class _PackageRecord(TypedDict):
    name: str
    version: str
    license: str
    project_url: str
    license_files: list[str]


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _marker_environment() -> dict[str, str]:
    version = platform.python_version()
    return {
        "implementation_name": sys.implementation.name,
        "implementation_version": platform.python_version(),
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_python_implementation": platform.python_implementation(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": version,
        "python_version": ".".join(version.split(".")[:2]),
        "sys_platform": sys.platform,
        "extra": "",
    }


def _evaluate_marker_node(node: ast.AST, environment: dict[str, str]) -> Any:
    if isinstance(node, ast.Expression):
        return _evaluate_marker_node(node.body, environment)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in environment:
        return environment[node.id]
    if isinstance(node, ast.BoolOp):
        values = [_evaluate_marker_node(value, environment) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _evaluate_marker_node(node.operand, environment)
    if isinstance(node, ast.Compare):
        left = _evaluate_marker_node(node.left, environment)
        for operator, comparator in zip(node.ops, node.comparators, strict=True):
            right = _evaluate_marker_node(comparator, environment)
            version_marker = isinstance(node.left, ast.Name) and node.left.id in {
                "implementation_version",
                "python_full_version",
                "python_version",
            }
            if version_marker and isinstance(left, str) and isinstance(right, str):
                left = tuple(int(part) for part in left.split("."))
                right = tuple(int(part) for part in right.split("."))
            if isinstance(operator, ast.Eq):
                matched = left == right
            elif isinstance(operator, ast.NotEq):
                matched = left != right
            elif isinstance(operator, ast.In):
                matched = left in right
            elif isinstance(operator, ast.NotIn):
                matched = left not in right
            elif isinstance(operator, ast.Lt):
                matched = left < right
            elif isinstance(operator, ast.LtE):
                matched = left <= right
            elif isinstance(operator, ast.Gt):
                matched = left > right
            elif isinstance(operator, ast.GtE):
                matched = left >= right
            else:
                raise CollectionError("Unsupported dependency marker operator")
            if not matched:
                return False
            left = right
        return True
    raise CollectionError("Unsupported dependency marker syntax")


def _requirement_applies(requirement: str, environment: dict[str, str]) -> bool:
    _, separator, marker = requirement.partition(";")
    if not separator:
        return True
    try:
        expression = ast.parse(marker.strip(), mode="eval")
    except SyntaxError as error:
        raise CollectionError("Could not parse a dependency environment marker") from error
    return bool(_evaluate_marker_node(expression, environment))


def _requirement_name(requirement: str) -> str:
    match = _PACKAGE_NAME.match(requirement)
    if match is None:
        raise CollectionError("Could not parse a dependency name")
    return match.group(1)


def _runtime_distributions() -> list[metadata.Distribution]:
    try:
        root = metadata.distribution("mail-dock")
    except metadata.PackageNotFoundError as error:
        raise CollectionError("mail-dock distribution metadata is not installed") from error

    environment = _marker_environment()
    queue = deque(root.requires or ())
    discovered: dict[str, metadata.Distribution] = {}
    while queue:
        requirement = queue.popleft()
        if not _requirement_applies(requirement, environment):
            continue
        name = _requirement_name(requirement)
        canonical = _canonical_name(name)
        if canonical in discovered or canonical == "mail-dock":
            continue
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError as error:
            raise CollectionError(f"Runtime dependency is not installed: {name}") from error
        discovered[canonical] = distribution
        queue.extend(
            child
            for child in (distribution.requires or ())
            if _requirement_applies(child, environment)
        )
    return sorted(discovered.values(), key=lambda item: _canonical_name(item.metadata["Name"]))


def _license_paths(distribution: metadata.Distribution) -> list[Path]:
    paths = []
    for entry in distribution.files or ():
        if _LICENSE_NAME.match(PurePosixPath(str(entry)).name):
            source = Path(str(distribution.locate_file(entry)))
            if source.is_file():
                paths.append(source)
    return sorted(set(paths), key=lambda path: path.as_posix().lower())


def _license_metadata(distribution: metadata.Distribution) -> str:
    value = (
        distribution.metadata.get("License-Expression")
        or distribution.metadata.get("License")
        or "See included license text"
    )
    return str(value)


def _project_url(distribution: metadata.Distribution) -> str:
    for value in distribution.metadata.get_all("Project-URL", []):
        label, separator, url = value.partition(",")
        if separator and label.strip().lower() in {"homepage", "source", "repository"}:
            return str(url.strip())
    return str(distribution.metadata.get("Home-page", ""))


def _copy_python_licenses(output_dir: Path) -> list[_PackageRecord]:
    destination_root = output_dir / "python"
    records: list[_PackageRecord] = []
    for distribution in _runtime_distributions():
        name = distribution.metadata["Name"]
        version = distribution.version
        sources = _license_paths(distribution)
        if not sources:
            raise CollectionError(f"No license file found for {name}=={version}")
        package_dir = destination_root / f"{name}-{version}"
        package_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        distribution_root = Path(str(distribution.locate_file("."))).resolve()
        for source in sources:
            try:
                relative = source.resolve().relative_to(distribution_root)
            except ValueError:
                relative = Path(source.name)
            target = package_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied.append(target.relative_to(output_dir).as_posix())
        records.append(
            _PackageRecord(
                name=name,
                version=version,
                license=_license_metadata(distribution),
                project_url=_project_url(distribution),
                license_files=copied,
            )
        )
    return records


def _find_python_license(explicit_path: Path | None) -> Path:
    candidates = [explicit_path] if explicit_path else []
    candidates.extend(
        Path(prefix) / filename
        for prefix in dict.fromkeys((sys.base_prefix, sys.prefix))
        for filename in ("LICENSE.txt", "LICENSE")
    )
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    raise CollectionError("Python LICENSE.txt was not found; pass --python-license")


def _find_pyinstaller_copying(explicit_path: Path | None) -> Path:
    if explicit_path is not None:
        if explicit_path.is_file():
            return explicit_path
        raise CollectionError(f"PyInstaller bootloader license not found: {explicit_path}")
    try:
        distribution = metadata.distribution("pyinstaller")
    except metadata.PackageNotFoundError as error:
        raise CollectionError(
            "PyInstaller bootloader COPYING was not found; pass --pyinstaller-bootloader-copying"
        ) from error
    candidates = [
        Path(str(distribution.locate_file(entry)))
        for entry in distribution.files or ()
        if PurePosixPath(str(entry)).name.lower() in {"copying", "copying.txt"}
        and "bootloader" in str(entry).lower()
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise CollectionError(
        "PyInstaller bootloader COPYING was not found; pass --pyinstaller-bootloader-copying"
    )


def _safe_tar_path(name: str) -> PurePosixPath | None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def _archive_members(archive: tarfile.TarFile) -> dict[PurePosixPath, tarfile.TarInfo]:
    members: dict[PurePosixPath, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        path = _safe_tar_path(member.name)
        if path is not None and member.isfile():
            members[path] = member
    return members


def _copy_tar_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    destination: Path,
) -> None:
    stream = archive.extractfile(member)
    if stream is None:
        raise CollectionError(f"Could not read source archive member: {member.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with stream, destination.open("wb") as output:
        shutil.copyfileobj(stream, output)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _collect_qt_licenses(sources: list[Path], output_dir: Path) -> None:
    destination_root = output_dir / "qt"
    found = {"gpl": False, "lgpl": False}
    for source_path in sources:
        try:
            with tarfile.open(source_path, mode="r:*") as archive:
                for path, member in _archive_members(archive).items():
                    filename = path.name.lower()
                    if not _LICENSE_NAME.match(path.name) and not re.match(
                        r"(?i)^(?:gpl|lgpl)-3(?:\.0)?(?:[-.]|$)", path.name
                    ):
                        continue
                    if "gpl" in filename and "lgpl" not in filename and "3" in filename:
                        found["gpl"] = True
                    if "lgpl" in filename and "3" in filename:
                        found["lgpl"] = True
                    _copy_tar_member(archive, member, destination_root / path)
        except (OSError, tarfile.TarError) as error:
            raise CollectionError(f"Could not open Qt source archive: {source_path}") from error
    missing = [name.upper() for name, present in found.items() if not present]
    if missing:
        names = ", ".join(missing)
        raise CollectionError(f"Qt source archives are missing full license texts: {names}")


def _webengine_license_members(
    archive: tarfile.TarFile,
    members: dict[PurePosixPath, tarfile.TarInfo],
) -> tuple[set[PurePosixPath], list[str]]:
    webengine_roots = [
        path
        for path in members
        if path.name == "CHROMIUM_VERSION" and "qtwebengine-everywhere-src-" in "/".join(path.parts)
    ]
    if not webengine_roots:
        raise CollectionError("QtWebEngine source archive has no CHROMIUM_VERSION marker")
    root = webengine_roots[0].parent
    chromium_root = root / "src" / "3rdparty" / "chromium"
    selected: set[PurePosixPath] = set()
    missing_references: list[str] = []
    for path in members:
        relative_to_root = path.relative_to(root) if path.is_relative_to(root) else None
        if path.name in {"LICENSE.Chromium", "CHROMIUM_VERSION"}:
            selected.add(path)
        if relative_to_root is not None and (
            relative_to_root.parts[:1] == ("LICENSES",)
            or relative_to_root.as_posix()
            in {
                "src/core/doc/src/qwebengine-licensing.qdoc",
                "src/pdf/doc/src/qtpdf-licensing.qdoc",
                "src/3rdparty/chromium/LICENSE",
            }
        ):
            selected.add(path)
        if _LICENSE_NAME.match(path.name):
            selected.add(path)
        if relative_to_root is None:
            continue
        if path.name != "README.chromium" or not path.is_relative_to(chromium_root):
            continue
        selected.add(path)
        member_stream = archive.extractfile(members[path])
        if member_stream is None:
            continue
        text = member_stream.read().decode("utf-8", errors="replace")
        for reference in re.findall(r"(?mi)^\s*License File:\s*(.*?)\s*$", text):
            reference = reference.strip().replace("\\", "/")
            if not reference:
                continue
            base = chromium_root if reference.startswith("//") else path.parent
            relative = PurePosixPath(reference.lstrip("/"))
            normalized = posixpath.normpath((base / relative).as_posix())
            target = _safe_tar_path(normalized)
            if target is None or normalized == "." or normalized.startswith("../"):
                missing_references.append(f"{path.as_posix()}: {reference}")
            elif target in members:
                selected.add(target)
            else:
                missing_references.append(f"{path.as_posix()}: {reference}")
    return selected, missing_references


def _collect_qtwebengine(
    source: Path,
    expected_sha256: str,
    output_dir: Path,
) -> tuple[str, list[str]]:
    digest = _sha256_file(source)
    if digest != expected_sha256.upper():
        raise CollectionError(f"QtWebEngine source SHA-256 mismatch: {source.name}")
    try:
        with tarfile.open(source, mode="r:*") as archive:
            members = _archive_members(archive)
            selected, missing = _webengine_license_members(archive, members)
            for path in sorted(selected, key=lambda item: item.as_posix().lower()):
                member = members[path]
                root_index = next(
                    index
                    for index, part in enumerate(path.parts)
                    if part.startswith("qtwebengine-everywhere-src-")
                )
                relative = PurePosixPath(*path.parts[root_index + 1 :])
                _copy_tar_member(archive, member, output_dir / "qtwebengine" / relative)
            required = (
                output_dir / "qtwebengine" / "LICENSE.Chromium",
                output_dir / "qtwebengine" / "src" / "3rdparty" / "chromium" / "LICENSE",
            )
            absent = [path.name for path in required if not path.is_file()]
            if absent:
                raise CollectionError(
                    f"QtWebEngine license source is incomplete: {', '.join(absent)}"
                )
    except (OSError, tarfile.TarError) as error:
        raise CollectionError(f"Could not open QtWebEngine source archive: {source}") from error
    return digest, missing


def _check_inventory(manifest_path: Path, inventory_path: Path) -> None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        inventory = inventory_path.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as error:
        raise CollectionError("Could not read the package manifest or license inventory") from error
    rows = {
        _canonical_name(cells[0].strip().strip("`"))
        for line in inventory.splitlines()
        if line.strip().startswith("|")
        for cells in [line.strip().strip("|").split("|")]
        if cells and cells[0].strip().lower() != "dependency"
    }
    missing = sorted(
        package["name"]
        for package in manifest.get("packages", [])
        if _canonical_name(package["name"]) not in rows
    )
    if missing:
        raise CollectionError(f"Dependencies missing from {inventory_path}: {', '.join(missing)}")


def _write_qtwebengine_source_record(
    output_dir: Path,
    source: Path,
    qtwebengine_sha256: str,
    missing: list[str],
) -> None:
    version = metadata.version("PySide6")
    qt_branch = ".".join(version.split(".")[:2])
    chromium_version_path = output_dir / "qtwebengine" / "CHROMIUM_VERSION"
    chromium_version = chromium_version_path.read_text(encoding="utf-8").strip()
    record = {
        "qt_version": version,
        "chromium_version": chromium_version,
        "source_archive": source.name,
        "source_url": (
            f"https://download.qt.io/official_releases/qt/{qt_branch}/{version}/submodules/"
            f"qtwebengine-everywhere-src-{version}.tar.xz"
        ),
        "sha256": qtwebengine_sha256,
        "missing_license_references": missing,
    }
    (output_dir / "qtwebengine-source.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def collect(args: argparse.Namespace) -> None:
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    packages = _copy_python_licenses(output_dir)
    manifest = {"schema_version": 1, "packages": packages}
    (output_dir / "python-packages.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    python_license = _find_python_license(args.python_license)
    shutil.copyfile(python_license, output_dir / "python" / "Python-LICENSE.txt")
    pyinstaller_copying = _find_pyinstaller_copying(args.pyinstaller_bootloader_copying)
    pyinstaller_dir = output_dir / "pyinstaller"
    pyinstaller_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pyinstaller_copying, pyinstaller_dir / "bootloader-COPYING.txt")

    if not args.qt_license_source:
        raise CollectionError("Provide Qt GPL/LGPL source archives with --qt-license-source")
    _collect_qt_licenses(args.qt_license_source, output_dir)
    webengine_sha256, missing = _collect_qtwebengine(
        args.qtwebengine_source,
        args.qtwebengine_sha256,
        output_dir,
    )
    _write_qtwebengine_source_record(
        output_dir,
        args.qtwebengine_source,
        webengine_sha256,
        missing,
    )
    if missing:
        (output_dir / "qtwebengine-missing-license-references.txt").write_text(
            "\n".join(missing) + "\n", encoding="utf-8"
        )
        print(
            f"Warning: {len(missing)} QtWebEngine license references were not present "
            "in the source archive"
        )
    if args.inventory:
        _check_inventory(output_dir / "python-packages.json", args.inventory)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("build/licenses"))
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--python-license", type=Path)
    parser.add_argument("--pyinstaller-bootloader-copying", type=Path)
    parser.add_argument("--qt-license-source", type=Path, action="append", default=[])
    parser.add_argument("--qtwebengine-source", type=Path)
    parser.add_argument("--qtwebengine-sha256")
    parser.add_argument("--check-inventory", type=Path)
    args = parser.parse_args()
    if args.check_inventory:
        return args
    if args.qtwebengine_source is None:
        parser.error("--qtwebengine-source is required unless --check-inventory is used")
    if args.qtwebengine_sha256 is None:
        parser.error("--qtwebengine-sha256 is required for license collection")
    return args


def main() -> int:
    args = _parse_args()
    try:
        if args.check_inventory:
            _check_inventory(args.output_dir / "python-packages.json", args.check_inventory)
        else:
            collect(args)
    except (CollectionError, OSError, metadata.PackageNotFoundError) as error:
        print(f"License collection failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
