"""Locate and validate the bundled readpst converter."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mail_dock.domain.errors import ConverterNotFound

_VERSION_TIMEOUT_SECONDS = 10

# These are the non-system DLLs required by the MSYS2 UCRT64 readpst build.
_WINDOWS_READPST_DLLS = (
    "libbz2-1.dll",
    "libffi-8.dll",
    "libgcc_s_seh-1.dll",
    "libgio-2.0-0.dll",
    "libglib-2.0-0.dll",
    "libgmodule-2.0-0.dll",
    "libgobject-2.0-0.dll",
    "libgsf-1-114.dll",
    "libiconv-2.dll",
    "libintl-8.dll",
    "libpcre2-8-0.dll",
    "libpst-4.dll",
    "libstdc++-6.dll",
    "libsystre-0.dll",
    "libtre-5.dll",
    "libwinpthread-1.dll",
    "libxml2-16.dll",
    "zlib1.dll",
)


def default_vendor_dir() -> Path:
    """Return the repository's bundled readpst directory."""

    return Path(__file__).resolve().parents[4] / "vendor" / "readpst"


@dataclass(frozen=True)
class ReadPstInstallation:
    """Validated paths for the bundled converter and its runtime files."""

    vendor_dir: Path
    readpst_path: Path
    lspst_path: Path | None
    required_dlls: tuple[Path, ...]


class ReadPstLocator:
    """Resolve the bundled readpst tools without consulting ``PATH``."""

    def __init__(
        self,
        vendor_dir: Path | None = None,
        *,
        is_windows: bool | None = None,
    ) -> None:
        self._vendor_dir = (vendor_dir or default_vendor_dir()).expanduser().resolve()
        self._is_windows = os.name == "nt" if is_windows is None else is_windows

    @property
    def vendor_dir(self) -> Path:
        """Return the absolute directory containing the bundled tools."""

        return self._vendor_dir

    @property
    def readpst_path(self) -> Path:
        """Return the validated absolute path to readpst."""

        return self.resolve_readpst()

    @property
    def lspst_path(self) -> Path:
        """Return the validated absolute path to lspst."""

        return self.resolve_lspst()

    def resolve_readpst(self) -> Path:
        """Resolve readpst and verify its bundled Windows dependencies."""

        path = self._binary_path("readpst")
        self._require_file(path, "readpst executable")
        if self._is_windows:
            missing = [
                name
                for name in _WINDOWS_READPST_DLLS
                if not (self._vendor_dir / name).is_file()
            ]
            if missing:
                names = ", ".join(missing)
                raise ConverterNotFound(f"Bundled readpst dependencies are missing: {names}")
        return path

    def resolve_lspst(self) -> Path:
        """Resolve the optional inspection tool used by the probe stage."""

        path = self._binary_path("lspst")
        self._require_file(path, "lspst executable")
        return path

    def locate(self) -> Path:
        """Compatibility entry point returning the readpst executable path."""

        return self.resolve_readpst()

    def get_version(self) -> str:
        """Return the converter's ``-V`` output, or raise a domain error."""

        executable = self.resolve_readpst()
        command = [str(executable), "-V"]
        try:
            if self._is_windows:
                result = subprocess.run(
                    command,
                    cwd=self._vendor_dir,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    encoding="utf-8",
                    errors="replace",
                    text=True,
                    timeout=_VERSION_TIMEOUT_SECONDS,
                )
            else:
                result = subprocess.run(
                    command,
                    cwd=self._vendor_dir,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=False,
                    encoding="utf-8",
                    errors="replace",
                    text=True,
                    timeout=_VERSION_TIMEOUT_SECONDS,
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ConverterNotFound("Bundled readpst could not be executed") from error

        if result.returncode != 0:
            raise ConverterNotFound(
                f"Bundled readpst version check failed with exit code {result.returncode}"
            )
        output = result.stdout.strip() or result.stderr.strip()
        if not output:
            raise ConverterNotFound("Bundled readpst returned no version information")
        return output

    def version(self) -> str:
        """Return the converter version reported by ``readpst -V``."""

        return self.get_version()

    def installation(self) -> ReadPstInstallation:
        """Return all validated converter paths used by the importer."""

        readpst = self.resolve_readpst()
        lspst: Path | None
        try:
            lspst = self.resolve_lspst()
        except ConverterNotFound:
            lspst = None
        dlls = tuple(self._vendor_dir / name for name in _WINDOWS_READPST_DLLS)
        return ReadPstInstallation(self._vendor_dir, readpst, lspst, dlls)

    def _binary_path(self, name: str) -> Path:
        suffix = ".exe" if self._is_windows else ""
        return self._vendor_dir / f"{name}{suffix}"

    @staticmethod
    def _require_file(path: Path, description: str) -> None:
        try:
            is_file = path.is_file()
        except OSError as error:
            raise ConverterNotFound(f"Cannot inspect bundled {description}: {path}") from error
        if not is_file:
            raise ConverterNotFound(f"Bundled {description} was not found: {path}")


def locate_readpst(vendor_dir: Path | None = None) -> Path:
    """Resolve the bundled readpst executable."""

    return ReadPstLocator(vendor_dir).resolve_readpst()


def get_readpst_version(vendor_dir: Path | None = None) -> str:
    """Return the version reported by the bundled readpst executable."""

    return ReadPstLocator(vendor_dir).get_version()


__all__ = [
    "ReadPstInstallation",
    "ReadPstLocator",
    "default_vendor_dir",
    "get_readpst_version",
    "locate_readpst",
]