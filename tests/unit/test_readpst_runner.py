from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from mail_dock.domain.errors import ConverterFailed, OperationCancelledError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.importer import ImportOptions
from mail_dock.infrastructure.importers.readpst_runner import ReadPstRunner


class _FakeProcess:
    def __init__(self, returncode: int | None, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None and self.terminated and not self.killed:
            raise subprocess.TimeoutExpired("readpst", timeout)
        return self.returncode or 0


def test_runner_builds_safe_readpst_command_and_reports_eml_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pst"
    source.write_bytes(b"pst")
    staging = tmp_path / "staging"
    process = _FakeProcess(0)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        calls.append((command, kwargs))
        (staging / "Inbox").mkdir(parents=True)
        (staging / "Inbox" / "1.eml").write_bytes(b"message")
        return process

    monkeypatch.setattr(
        "mail_dock.infrastructure.importers.readpst_runner.subprocess.Popen", fake_popen
    )

    progress: list[int] = []
    result = ReadPstRunner(
        tmp_path / "readpst.exe", poll_interval_seconds=0.001, is_windows=True
    ).run(
        source,
        staging,
        ImportOptions("Archive", "cp932", include_deleted=True),
        log_file=tmp_path / "logs" / "pstimp.log",
        cancel=CancelToken(),
        on_progress=progress.append,
    )

    command, kwargs = calls[0]
    assert command == [
        str((tmp_path / "readpst.exe").resolve()),
        "-e",
        "-t",
        "e",
        "-8",
        "-j",
        "0",
        "-q",
        "-C",
        "cp932",
        "-D",
        "-d",
        str((tmp_path / "logs" / "pstimp.log").resolve()),
        "-o",
        str(staging.resolve()),
        str(source.resolve()),
    ]
    assert kwargs["shell"] is False
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert result.file_count == 1
    assert progress[-1] == 1


def test_runner_includes_stdout_and_stderr_in_converter_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess(
        1,
        stdout=b"mk_separate_dir: Cannot create directory 'bad*name'\n",
        stderr=b"secondary diagnostic\n",
    )
    monkeypatch.setattr(
        "mail_dock.infrastructure.importers.readpst_runner.subprocess.Popen",
        lambda *args, **kwargs: process,
    )

    with pytest.raises(ConverterFailed) as raised:
        ReadPstRunner(tmp_path / "readpst", is_windows=False).run(
            tmp_path / "source.pst",
            tmp_path / "staging",
            cancel=CancelToken(),
            on_progress=lambda _count: None,
        )

    message = str(raised.value)
    assert "cannot be created on this Windows filesystem" in message
    assert "mk_separate_dir" in message
    assert "secondary diagnostic" in message


def test_runner_terminates_then_kills_unresponsive_process_on_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess(None)
    monkeypatch.setattr(
        "mail_dock.infrastructure.importers.readpst_runner.subprocess.Popen",
        lambda *args, **kwargs: process,
    )
    token = CancelToken()

    def cancel_after_first_sample(_count: int) -> None:
        token.cancel()

    with pytest.raises(OperationCancelledError):
        ReadPstRunner(
            tmp_path / "readpst", poll_interval_seconds=0.001, cancel_timeout_seconds=0
        ).run(
            tmp_path / "source.pst",
            tmp_path / "staging",
            cancel=token,
            on_progress=cancel_after_first_sample,
        )

    assert process.terminated is True
    assert process.killed is True