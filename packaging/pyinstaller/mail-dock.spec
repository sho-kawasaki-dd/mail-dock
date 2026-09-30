from pathlib import Path
import sys

from PyInstaller.building.build_main import Analysis
from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.utils.hooks import copy_metadata
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)


ROOT = Path(SPECPATH).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mail_dock import __version__


def version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdecimal() for part in parts):
        raise ValueError(f"Unsupported application version: {version}")
    return tuple(int(part) for part in parts) + (0,)


version_values = version_tuple(__version__)
version_resource = VSVersionInfo(
    FixedFileInfo(
        filevers=version_values,
        prodvers=version_values,
        mask=0x3F,
        flags=0,
        OS=0x40004,
        fileType=1,
        subtype=0,
        date=(0, 0),
    ),
    [
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", "mail-dock contributors"),
                        StringStruct("FileDescription", "Local mail backup and viewer"),
                        StringStruct("FileVersion", __version__),
                        StringStruct("LegalCopyright", "GPL-3.0-or-later"),
                        StringStruct("ProductName", "mail-dock"),
                        StringStruct("ProductVersion", __version__),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)


datas = [
    (str(path), "mail_dock/migrations")
    for path in sorted((ROOT / "src" / "mail_dock" / "migrations").glob("*.sql"))
]
readpst_root = ROOT / "vendor" / "readpst"
for pattern in ("*.exe", "*.dll"):
    datas.extend((str(path), "vendor/readpst") for path in sorted(readpst_root.glob(pattern)))
for filename in (
    "COPYING",
    "readpst.exe.manifest",
    "readpst-artifacts.json",
    "SHA256SUMS",
):
    path = readpst_root / filename
    if path.is_file():
        datas.append((str(path), "vendor/readpst"))
for filename in ("README.md", "LICENSE", "THIRD-PARTY-LICENSES.md"):
    datas.append((str(ROOT / filename), "."))
datas += copy_metadata("keyring")
licenses_root = ROOT / "build" / "licenses"
if licenses_root.is_dir():
    datas.extend(
        (
            str(path),
            (Path("licenses") / path.relative_to(licenses_root)).parent.as_posix(),
        )
        for path in sorted(licenses_root.rglob("*"))
        if path.is_file()
    )


a = Analysis(
    [str(ROOT / "packaging" / "pyinstaller" / "entry_gui.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=["keyring.backends.Windows"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mail-dock",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    version=version_resource,
    manifest=str(ROOT / "packaging" / "pyinstaller" / "mail-dock.manifest"),
    icon=None,
)
COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="mail-dock",
)