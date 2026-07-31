"""Generate deterministic synthetic EML/SQLite corpora for the FTS PoC.

This is a manual benchmark fixture generator, not a pytest test.  Each dataset
contains the EML files and a SQLite database populated through the same
normalization function used by the production message repository.
"""

from __future__ import annotations

import argparse
import random
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Final

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
for import_root in (REPOSITORY_ROOT, REPOSITORY_ROOT / "src"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from mail_dock.infrastructure.parsing.normalize import (  # noqa: E402, I001
    normalize_for_search,
)
from tests.support.eml_builder import AttachmentSpec, build_eml  # noqa: E402


SCHEMA_PATH: Final[Path] = REPOSITORY_ROOT / "src" / "mail_dock" / "migrations" / "001_init.sql"
DEFAULT_OUTPUT: Final[Path] = REPOSITORY_ROOT / "tools" / ".bench_fts"
DEFAULT_COUNTS: Final[tuple[int, ...]] = (1_000, 5_000, 10_000)
MIN_TRIGRAM_SQLITE_VERSION: Final[tuple[int, int, int]] = (3, 34, 0)
ACCOUNT_ID: Final[str] = "synthetic@example.test"
FOLDER_NAME: Final[str] = "Synthetic"

_BODY_SENTENCES: Final[tuple[str, ...]] = (
    "本日の業務連絡と確認事項を共有します。",
    "各担当者は期限と変更点をご確認ください。",
    "関連する資料を確認し、必要な対応を返信してください。",
    "会議では進捗、課題、次の作業を順番に整理しました。",
    "保存した記録は後から検索できるように整理しています。",
    "この文章は日本語の検索性能を測るための合成本文です。",
    "お客様への回答内容と社内の確認結果をまとめています。",
    "予定に変更がある場合は、早めに関係者へお知らせください。",
)
_SUBJECTS: Final[tuple[str, ...]] = (
    "月次報告と確認事項",
    "プロジェクト進捗のお知らせ",
    "会議資料の共有",
    "請求書対応について",
    "運用手順の更新連絡",
)
_DISPLAY_NAMES: Final[tuple[str, ...]] = (
    "山田太郎",
    "佐藤花子",
    "鈴木一郎",
    "高橋美咲",
    "田中健一",
)


@dataclass(frozen=True)
class SyntheticMessage:
    """Source values used to build one EML and its searchable DB row."""

    subject: str
    sender: str
    recipient: str
    body: str
    attachment_name: str
    date: datetime


@dataclass(frozen=True)
class DatasetStats:
    """Summary printed after one corpus has been generated."""

    count: int
    eml_bytes: int
    median_body_bytes: int
    long_message_count: int


def _positive_count(value: str) -> int:
    count = int(value)
    if count <= 0:
        raise argparse.ArgumentTypeError("count must be positive")
    return count


def check_sqlite_environment() -> None:
    """Verify the SQLite version and actual availability of the trigram tokenizer."""

    print(f"sqlite3.sqlite_version={sqlite3.sqlite_version}")
    if sqlite3.sqlite_version_info < MIN_TRIGRAM_SQLITE_VERSION:
        required = ".".join(str(part) for part in MIN_TRIGRAM_SQLITE_VERSION)
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is unsupported; trigram requires {required} or newer"
        )

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE fts_environment_check USING fts5(content, tokenize='trigram')"
        )
    except sqlite3.Error as error:
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} does not provide the trigram tokenizer"
        ) from error
    finally:
        connection.close()
    print("trigram_tokenizer=available")


def _body_target_bytes(generator: random.Random) -> int:
    bucket = generator.random()
    if bucket < 0.80:
        return generator.randint(2_000, 8_000)
    if bucket < 0.97:
        return generator.randint(8_001, 32_000)
    return generator.randint(32_001, 256_000)


def _build_body(generator: random.Random) -> str:
    target_bytes = _body_target_bytes(generator)
    paragraphs: list[str] = []
    current_bytes = 0
    while current_bytes < target_bytes:
        paragraph = "".join(
            generator.choice(_BODY_SENTENCES) + "\n" for _ in range(generator.randint(3, 8))
        )
        paragraphs.append(paragraph)
        current_bytes += len(paragraph.encode("utf-8"))
    return "\n".join(paragraphs)


def _build_message(index: int, generator: random.Random) -> SyntheticMessage:
    sender_name = generator.choice(_DISPLAY_NAMES)
    sender = f'"{sender_name}" <sender{index:05d}@example.test>'
    date = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index)
    return SyntheticMessage(
        subject=f"{generator.choice(_SUBJECTS)} #{index:05d}",
        sender=sender,
        recipient=f"recipient{index:05d}@example.test",
        body=_build_body(generator),
        attachment_name=f"議事録_{index:05d}.pdf",
        date=date,
    )


def _create_database(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO accounts(id, provider_type, display_name) VALUES (?, ?, ?)",
            (ACCOUNT_ID, "synthetic", "Synthetic benchmark account"),
        )
        connection.execute(
            """
            INSERT INTO folders(account_id, raw_name, display_name, is_sync_target)
            VALUES (?, ?, ?, 1)
            """,
            (ACCOUNT_ID, FOLDER_NAME, FOLDER_NAME),
        )
        connection.commit()
    except Exception:
        connection.close()
        raise
    return connection


def _insert_message(
    connection: sqlite3.Connection,
    folder_id: int,
    index: int,
    message: SyntheticMessage,
    raw_eml: bytes,
) -> None:
    date_value = message.date.isoformat()
    cursor = connection.execute(
        """
        INSERT INTO messages(
            account_id, folder_id, content_key, source_item_key, uid, uidvalidity,
            subject, sender, recipient, date_sent, internal_date, size_bytes,
            has_attachment, remote_state, local_state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'present', 'active')
        """,
        (
            ACCOUNT_ID,
            folder_id,
            f"synthetic:{index:05d}",
            f"synthetic:{index:05d}",
            index,
            1,
            message.subject,
            message.sender,
            message.recipient,
            date_value,
            date_value,
            len(raw_eml),
        ),
    )
    message_id = cursor.lastrowid
    if message_id is None:
        raise RuntimeError("SQLite did not return the synthetic message id")
    normalized_values = (
        normalize_for_search(message.subject),
        normalize_for_search(message.sender),
        normalize_for_search(message.body),
        normalize_for_search(message.attachment_name),
    )
    connection.execute(
        """
        INSERT INTO message_contents(
            message_id, subject_norm, sender_norm, body_text, attachment_names
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (message_id, *normalized_values),
    )


def generate_dataset(count: int, output_root: Path, seed: int) -> DatasetStats:
    """Generate one EML directory and SQLite database for ``count`` messages."""

    dataset_root = output_root / str(count)
    if dataset_root.exists():
        raise FileExistsError(f"output already exists; use --force: {dataset_root}")
    eml_root = dataset_root / "eml"
    eml_root.mkdir(parents=True)
    database_path = dataset_root / "messages.db"

    generator = random.Random(seed + count)
    body_sizes: list[int] = []
    eml_bytes = 0
    long_message_count = 0
    connection = _create_database(database_path)
    try:
        folder_row = connection.execute(
            "SELECT id FROM folders WHERE account_id = ? AND raw_name = ?",
            (ACCOUNT_ID, FOLDER_NAME),
        ).fetchone()
        if folder_row is None:
            raise RuntimeError("Synthetic folder was not created")
        folder_id = int(folder_row[0])

        for index in range(1, count + 1):
            message = _build_message(index, generator)
            raw_eml = build_eml(
                subject=message.subject,
                sender=message.sender,
                recipient=message.recipient,
                body=message.body,
                message_id=f"<synthetic-{index:05d}@example.test>",
                date=format_datetime(message.date),
                attachments=(
                    AttachmentSpec(
                        filename=message.attachment_name,
                        content=b"synthetic attachment",
                        content_type="application/pdf",
                    ),
                ),
            )
            (eml_root / f"{index:05d}.eml").write_bytes(raw_eml)
            _insert_message(connection, folder_id, index, message, raw_eml)
            body_size = len(message.body.encode("utf-8"))
            body_sizes.append(body_size)
            eml_bytes += len(raw_eml)
            if body_size >= 32_000:
                long_message_count += 1
            if index % 500 == 0:
                connection.commit()
        connection.commit()
    finally:
        connection.close()

    body_sizes.sort()
    median_body_bytes = body_sizes[len(body_sizes) // 2]
    return DatasetStats(count, eml_bytes, median_body_bytes, long_message_count)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="directory for generated EML files and SQLite databases",
    )
    parser.add_argument(
        "--counts",
        type=_positive_count,
        nargs="+",
        default=DEFAULT_COUNTS,
        metavar="N",
        help="message counts to generate (default: 1000 5000 10000)",
    )
    parser.add_argument("--seed", type=int, default=20260731)
    parser.add_argument(
        "--force",
        action="store_true",
        help="remove the selected generated output before creating it",
    )
    parser.add_argument(
        "--check-environment",
        action="store_true",
        help="check SQLite trigram support without generating a dataset",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    check_sqlite_environment()
    if args.check_environment:
        return

    output_root = args.output.resolve()
    if output_root.exists() and args.force:
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    for count in args.counts:
        stats = generate_dataset(count, output_root, args.seed)
        print(
            f"{stats.count:>6} messages | EML {stats.eml_bytes / 1024 / 1024:8.1f} MiB | "
            f"median body {stats.median_body_bytes:>6} bytes | "
            f"long messages {stats.long_message_count:>4}"
        )


if __name__ == "__main__":
    main()
