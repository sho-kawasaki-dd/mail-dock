"""Run the bundled readpst converter and monitor its staged output."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

from mail_dock.domain.errors import ConverterFailed, OperationCancelledError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.importer import ExtractResult, ImportOptions

_DEFAULT_POLL_INTERVAL_SECONDS = 0.25
_DEFAULT_CANCEL_TIMEOUT_SECONDS = 5.0
_TAIL_BYTES = 16 * 1024


class _TailBuffer:
    """Keep bounded process output without risking a full PIPE deadlock."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._chunks: list[bytes] = []
        self._size = 0

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._chunks.append(chunk)
        self._size += len(chunk)
        while self._size > self._limit and self._chunks:
            excess = self._size - self._limit
            first = self._chunks[0]
            if len(first) <= excess:
                self._chunks.pop(0)
                self._size -= len(first)
            else:
                self._chunks[0] = first[excess:]
                self._size -= excess

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


class ReadPstRunner:
    """Execute readpst as a bounded, cancellable subprocess."""

    def __init__(
        self,
        readpst_path: Path,
        *,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        cancel_timeout_seconds: float = _DEFAULT_CANCEL_TIMEOUT_SECONDS,
        is_windows: bool | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if cancel_timeout_seconds < 0:
            raise ValueError("cancel_timeout_seconds must not be negative")
        self._readpst_path = Path(readpst_path).expanduser().resolve()
        self._poll_interval_seconds = poll_interval_seconds
        self._cancel_timeout_seconds = cancel_timeout_seconds
        self._is_windows = os.name == "nt" if is_windows is None else is_windows

    def run(
        self,
        source: Path,
        staging: Path,
        options: ImportOptions | None = None,
        *,
        charset: str | None = None,
        include_deleted: bool | None = None,
        log_file: Path | None = None,
        cancel: CancelToken,
        on_progress: Callable[[int], None],
    ) -> ExtractResult:
        """Run readpst and return the output inventory count and output tails.

        ``options`` is the preferred API used by the archive importer.  The
        keyword overrides keep this infrastructure boundary convenient for
        callers that have not created an ``ImportOptions`` value yet.
        """

        selected_charset = options.charset if options is not None else (charset or "cp932")
        selected_deleted = (
            options.include_deleted if options is not None else bool(include_deleted)
        )
        if not selected_charset or "\x00" in selected_charset:
            raise ValueError("charset must be a non-empty string without NUL")

        source_path = Path(source).expanduser().resolve()
        staging_path = Path(staging).expanduser().resolve()
        staging_path.mkdir(parents=True, exist_ok=True)
        if log_file is not None:
            log_path = Path(log_file).expanduser().resolve()
            log_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            log_path = None

        command = self._build_command(
            source_path,
            staging_path,
            selected_charset,
            selected_deleted,
            log_path,
        )
        cancel.raise_if_cancelled()
        try:
            process = subprocess.Popen(
                command,
                cwd=self._readpst_path.parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0) if self._is_windows else 0
                ),
            )
        except OSError as error:
            raise ConverterFailed(f"Could not execute readpst: {self._readpst_path}") from error

        stdout_tail = _TailBuffer(_TAIL_BYTES)
        stderr_tail = _TailBuffer(_TAIL_BYTES)
        readers = self._start_output_readers(process, stdout_tail, stderr_tail)
        try:
            file_count = self._wait_for_process(process, staging_path, cancel, on_progress)
        except OperationCancelledError:
            self._stop_process(process)
            self._join_readers(readers)
            raise
        except BaseException:
            self._stop_process(process)
            self._join_readers(readers)
            raise

        self._join_readers(readers)
        stdout_text = stdout_tail.text()
        stderr_text = stderr_tail.text()
        return_code = process.returncode
        if return_code != 0:
            raise ConverterFailed(self._failure_message(return_code, stdout_text, stderr_text))

        final_count = self._count_eml_files(staging_path)
        on_progress(final_count)
        return ExtractResult(staging_path, final_count or file_count, stdout_text, stderr_text)

    def extract(
        self,
        source: Path,
        staging: Path,
        options: ImportOptions,
        *,
        cancel: CancelToken,
        on_progress: Callable[[int], None],
        log_file: Path | None = None,
    ) -> ExtractResult:
        """Compatibility adapter matching the archive importer operation."""

        return self.run(
            source,
            staging,
            options,
            cancel=cancel,
            on_progress=on_progress,
            log_file=log_file,
        )

    def _build_command(
        self,
        source: Path,
        staging: Path,
        charset: str,
        include_deleted: bool,
        log_file: Path | None,
    ) -> list[str]:
        command = [
            str(self._readpst_path),
            "-e",
            "-t",
            "e",
            "-8",
            "-j",
            "0",
            "-q",
            "-C",
            charset,
        ]
        if include_deleted:
            command.append("-D")
        if log_file is not None:
            command.extend(("-d", str(log_file)))
        command.extend(("-o", str(staging), str(source)))
        return command

    def _wait_for_process(
        self,
        process: subprocess.Popen[bytes],
        staging: Path,
        cancel: CancelToken,
        on_progress: Callable[[int], None],
    ) -> int:
        last_count = -1
        started_at = monotonic()
        while True:
            if cancel.is_cancelled:
                raise OperationCancelledError("readpst extraction cancelled")
            return_code = process.poll()
            current_count = self._count_eml_files(staging)
            if current_count != last_count:
                on_progress(current_count)
                last_count = current_count
            if return_code is not None:
                return current_count
            sleep(self._poll_interval_seconds)
            # Keep the elapsed-time read explicit: callers receive a fresh
            # progress sample even when a converter emits no files for a while.
            if monotonic() - started_at >= self._poll_interval_seconds:
                on_progress(current_count)
                started_at = monotonic()

    def _start_output_readers(
        self,
        process: subprocess.Popen[bytes],
        stdout_tail: _TailBuffer,
        stderr_tail: _TailBuffer,
    ) -> list[Thread]:
        readers: list[Thread] = []
        for stream, tail in ((process.stdout, stdout_tail), (process.stderr, stderr_tail)):
            if stream is None:
                continue
            reader = Thread(target=self._drain_stream, args=(stream, tail), daemon=True)
            reader.start()
            readers.append(reader)
        return readers

    @staticmethod
    def _drain_stream(stream: object, tail: _TailBuffer) -> None:
        read = getattr(stream, "read", None)
        if not callable(read):
            return
        while True:
            chunk = read(4096)
            if not chunk:
                return
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            tail.append(chunk)

    def _stop_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=self._cancel_timeout_seconds)
        except (OSError, subprocess.TimeoutExpired):
            with suppress(OSError):
                process.kill()
            with suppress(OSError):
                process.wait()

    @staticmethod
    def _join_readers(readers: list[Thread]) -> None:
        for reader in readers:
            reader.join()

    @staticmethod
    def _count_eml_files(staging: Path) -> int:
        try:
            return sum(
                1
                for path in staging.rglob("*")
                if path.is_file() and path.suffix.casefold() == ".eml"
            )
        except OSError:
            return 0

    @staticmethod
    def _failure_message(return_code: int | None, stdout: str, stderr: str) -> str:
        output = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)
        if "mk_separate_dir: Cannot create directory" in output:
            detail = "PST folder name cannot be created on this Windows filesystem"
        else:
            detail = "readpst failed"
        message = f"{detail} (exit code {return_code})"
        if output:
            message = f"{message}: {output}"
        return message


# Keep the spelling used by the module name available to early callers.
ReadpstRunner = ReadPstRunner


__all__ = ["ReadPstRunner", "ReadpstRunner"]