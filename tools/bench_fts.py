"""Generate deterministic synthetic EML/SQLite corpora for the FTS PoC.

This is a manual benchmark fixture generator, not a pytest test.  Each dataset
contains the EML files and a SQLite database populated through the same
normalization function used by the production message repository.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import sqlite3
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Final, cast

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
for import_root in (REPOSITORY_ROOT, REPOSITORY_ROOT / "src"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from mail_dock.domain.normalize import (  # noqa: E402, I001
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


@dataclass(frozen=True)
class TimingStats:
    """Timing distribution for one repeated benchmark operation."""

    p50_ms: float
    p95_ms: float


@dataclass(frozen=True)
class QuerySpec:
    """One synthetic search case used by the MATCH/LIKE measurements."""

    name: str
    path: str
    terms: tuple[str, ...]
    mode: str = "and"
    exclude_terms: tuple[str, ...] = ()


DEFAULT_WARMUPS: Final[int] = 2
DEFAULT_ITERATIONS: Final[int] = 7
DEEP_PAGE_OFFSET: Final[int] = 10_000
PAGE_SIZE: Final[int] = 200
A4_SYNTAX_TOKENS: Final[tuple[str, ...]] = (
    '"',
    "*",
    "^",
    "-",
    "(",
    ")",
    ":",
    "NEAR",
    "AND",
    "OR",
    "NOT",
)
A4_DETAIL_MODES: Final[tuple[str, ...]] = ("full", "column", "none")


def _positive_count(value: str) -> int:
    count = int(value)
    if count <= 0:
        raise argparse.ArgumentTypeError("count must be positive")
    return count


def _nonnegative_count(value: str) -> int:
    count = int(value)
    if count < 0:
        raise argparse.ArgumentTypeError("count must not be negative")
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


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[index]


def _time_query(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[object] = (),
    *,
    warmups: int,
    iterations: int,
) -> tuple[TimingStats, int]:
    for _ in range(warmups):
        connection.execute(sql, parameters).fetchall()

    durations: list[float] = []
    hit_count = 0
    for _ in range(iterations):
        started = time.perf_counter()
        rows = connection.execute(sql, parameters).fetchall()
        durations.append((time.perf_counter() - started) * 1000)
        hit_count = len(rows)
    return TimingStats(_percentile(durations, 0.50), _percentile(durations, 0.95)), hit_count


def _quote_fts_term(term: str) -> str:
    return f'"{term.replace(chr(34), chr(34) * 2)}"'


def _fts_match_sql(spec: QuerySpec) -> tuple[str, tuple[str, ...]]:
    ctes: list[str] = []
    parameters: list[str] = []
    include_names: list[str] = []
    for index, term in enumerate(spec.terms):
        name = f"include_{index}"
        include_names.append(name)
        ctes.append(f"{name} AS (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
        parameters.append(_quote_fts_term(term))

    included_sql = f" SELECT rowid FROM {include_names[0]}"
    operator = " INTERSECT " if spec.mode == "and" else " UNION "
    included_sql += "".join(f"{operator}SELECT rowid FROM {name}" for name in include_names[1:])
    ctes.append(f"included AS ({included_sql})")

    excluded_names: list[str] = []
    for index, term in enumerate(spec.exclude_terms):
        name = f"exclude_{index}"
        excluded_names.append(name)
        ctes.append(f"{name} AS (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
        parameters.append(_quote_fts_term(term))
    if excluded_names:
        excluded_sql = " SELECT rowid FROM " + excluded_names[0]
        excluded_sql += " UNION ".join(f" SELECT rowid FROM {name}" for name in excluded_names[1:])
        ctes.append(f"excluded AS ({excluded_sql})")
        sql = f"WITH {', '.join(ctes)} SELECT rowid FROM included EXCEPT SELECT rowid FROM excluded"
    else:
        sql = f"WITH {', '.join(ctes)} SELECT rowid FROM included"
    return sql, tuple(parameters)


def _escape_like(term: str) -> str:
    escaped = term.replace("\\", "\\\\")
    escaped = escaped.replace("%", "\\%")
    escaped = escaped.replace("_", "\\_")
    return f"%{escaped}%"


def _like_sql(term: str) -> tuple[str, tuple[str, ...]]:
    sql = """
        SELECT message_id
        FROM message_contents
        WHERE subject_norm LIKE ? ESCAPE '\\'
           OR sender_norm LIKE ? ESCAPE '\\'
           OR body_text LIKE ? ESCAPE '\\'
           OR attachment_names LIKE ? ESCAPE '\\'
    """
    pattern = _escape_like(term)
    return sql, (pattern, pattern, pattern, pattern)


def _count_match(connection: sqlite3.Connection, term: str) -> int:
    sql = "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH ?"
    parameter = _quote_fts_term(term)
    row = connection.execute(sql, (parameter,)).fetchone()
    if row is None:
        raise RuntimeError("SQLite did not return a MATCH count")
    return int(row[0])


def _query_plan(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[object] = (),
) -> tuple[str, ...]:
    return tuple(
        " | ".join(str(value) for value in row)
        for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters).fetchall()
    )


def _probe_short_match(connection: sqlite3.Connection) -> dict[str, Any]:
    term, _ = _find_rare_terms(connection, 2, use_like=True)
    match_count = _count_match(connection, term)
    like_count = _count_like(connection, term)
    return {
        "term": term,
        "match_count": match_count,
        "like_count": like_count,
        "match_returns_zero": match_count == 0,
        "like_finds_rows": like_count > 0,
    }


def _probe_syntax_escaping() -> dict[str, Any]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE a4_syntax_fts USING fts5(content, tokenize='trigram')"
        )
        connection.execute(
            "INSERT INTO a4_syntax_fts(content) VALUES (?)",
            ('literal * ^ - ( ) : NEAR AND OR NOT "',),
        )
        cases: list[dict[str, Any]] = []
        for raw_term in A4_SYNTAX_TOKENS:
            quoted_term = _quote_fts_term(raw_term)
            raw_error: str | None = None
            quoted_error: str | None = None
            try:
                connection.execute(
                    "SELECT rowid FROM a4_syntax_fts WHERE a4_syntax_fts MATCH ?",
                    (raw_term,),
                ).fetchall()
            except sqlite3.Error as error:
                raw_error = str(error)
            try:
                connection.execute(
                    "SELECT rowid FROM a4_syntax_fts WHERE a4_syntax_fts MATCH ?",
                    (quoted_term,),
                ).fetchall()
            except sqlite3.Error as error:
                quoted_error = str(error)
            cases.append(
                {
                    "raw_term": raw_term,
                    "quoted_term": quoted_term,
                    "raw_error": raw_error,
                    "quoted_error": quoted_error,
                }
            )
    finally:
        connection.close()

    return {
        "cases": cases,
        "raw_parse_error_count": sum(case["raw_error"] is not None for case in cases),
        "quoted_parse_error_count": sum(case["quoted_error"] is not None for case in cases),
        "all_quoted_terms_safe": all(case["quoted_error"] is None for case in cases),
    }


def _measure_like_target(
    connection: sqlite3.Connection,
    sql: str,
    pattern: str,
    *,
    warmups: int,
    iterations: int,
) -> dict[str, Any]:
    timing, hit_count = _time_query(
        connection,
        sql,
        (pattern,),
        warmups=warmups,
        iterations=iterations,
    )
    return {
        "hit_count": hit_count,
        "p50_ms": timing.p50_ms,
        "p95_ms": timing.p95_ms,
        "query_plan": _query_plan(connection, sql, (pattern,)),
    }


def _probe_like_reuse(
    connection: sqlite3.Connection,
    *,
    warmups: int,
    iterations: int,
) -> dict[str, Any]:
    short_term, _ = _find_rare_terms(connection, 2, use_like=True)
    long_term, _ = _find_rare_terms(connection, 3, use_like=True)
    measurements: dict[str, Any] = {}
    for label, term in (("2_characters", short_term), ("3_characters", long_term)):
        pattern = _escape_like(term)
        fts_sql = "SELECT rowid FROM messages_fts WHERE body_text LIKE ?"
        content_sql = "SELECT message_id FROM message_contents WHERE body_text LIKE ?"
        fts_measurement = _measure_like_target(
            connection,
            fts_sql,
            pattern,
            warmups=warmups,
            iterations=iterations,
        )
        content_measurement = _measure_like_target(
            connection,
            content_sql,
            pattern,
            warmups=warmups,
            iterations=iterations,
        )
        measurements[label] = {
            "term": term,
            "fts_table": fts_measurement,
            "message_contents": content_measurement,
            "trigram_candidate_length": len(term) >= 3,
            "fts_like_plan_mentions_virtual_index": any(
                "VIRTUAL TABLE INDEX" in plan
                for plan in cast(tuple[str, ...], fts_measurement["query_plan"])
            ),
        }
    return {
        "measurements": measurements,
        "two_character_reusable": False,
        "decision": (
            "Do not reuse the trigram index for two-character search: "
            "the two-character MATCH probe returns zero rows and trigram has no "
            "two-character token."
        ),
    }


def _detail_variant_connection(
    rows: Sequence[tuple[object, ...]],
    detail: str,
) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE a4_detail_contents(
            message_id INTEGER PRIMARY KEY,
            subject_norm TEXT,
            sender_norm TEXT,
            body_text TEXT,
            attachment_names TEXT
        )
        """
    )
    connection.execute(
        f"""
        CREATE VIRTUAL TABLE a4_detail_fts USING fts5(
            subject_norm,
            sender_norm,
            body_text,
            attachment_names,
            content='a4_detail_contents',
            content_rowid='message_id',
            tokenize='trigram',
            detail='{detail}'
        )
        """
    )
    connection.executemany(
        "INSERT INTO a4_detail_contents VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    connection.execute("INSERT INTO a4_detail_fts(a4_detail_fts) VALUES ('rebuild')")
    connection.commit()
    return connection


def _measure_detail_variants(
    connection: sqlite3.Connection,
    *,
    warmups: int,
    iterations: int,
) -> dict[str, Any]:
    rows = [
        tuple(row)
        for row in connection.execute(
            """
            SELECT message_id, subject_norm, sender_norm, body_text, attachment_names
            FROM message_contents
            ORDER BY message_id
            """
        ).fetchall()
    ]
    if not rows:
        raise RuntimeError("Synthetic database has no message contents")
    phrase_term = str(rows[0][1] or "")
    if not phrase_term:
        raise RuntimeError("Synthetic database has no subject for phrase probe")

    measurements: dict[str, Any] = {}
    for detail in A4_DETAIL_MODES:
        variant = _detail_variant_connection(rows, detail)
        try:
            match_sql = "SELECT rowid FROM a4_detail_fts WHERE a4_detail_fts MATCH ?"
            match_3, match_3_hits = _time_query(
                variant,
                match_sql,
                ("sen",),
                warmups=warmups,
                iterations=iterations,
            )
            match_10_timing: TimingStats | None = None
            match_10_hits = 0
            match_10_error: str | None = None
            try:
                match_10_timing, match_10_hits = _time_query(
                    variant,
                    match_sql,
                    ("sender0000",),
                    warmups=warmups,
                    iterations=iterations,
                )
            except sqlite3.Error as error:
                match_10_error = str(error)
            phrase_query = _quote_fts_term(phrase_term)
            phrase_error: str | None = None
            phrase_supported = True
            phrase_timing: TimingStats | None = None
            phrase_hits = 0
            try:
                phrase_timing, phrase_hits = _time_query(
                    variant,
                    match_sql,
                    (phrase_query,),
                    warmups=warmups,
                    iterations=iterations,
                )
            except sqlite3.Error as error:
                phrase_supported = False
                phrase_error = str(error)
            measurements[detail] = {
                "fts_page_bytes": _dbstat_bytes(variant, "a4_detail_fts*"),
                "match_3": {
                    "hit_count": match_3_hits,
                    "p50_ms": match_3.p50_ms,
                    "p95_ms": match_3.p95_ms,
                },
                "match_10": {
                    "supported": match_10_error is None,
                    "hit_count": match_10_hits,
                    "p50_ms": match_10_timing.p50_ms if match_10_timing else None,
                    "p95_ms": match_10_timing.p95_ms if match_10_timing else None,
                    "error": match_10_error,
                },
                "phrase": {
                    "query": phrase_query,
                    "supported": phrase_supported,
                    "error": phrase_error,
                    "hit_count": phrase_hits,
                    "p50_ms": phrase_timing.p50_ms if phrase_timing else None,
                    "p95_ms": phrase_timing.p95_ms if phrase_timing else None,
                },
            }
        finally:
            variant.close()
    return {
        "source_rows": len(rows),
        "phrase_probe": phrase_term,
        "variants": measurements,
        "phrase_requires_detail_full": all(
            bool(measurements[detail]["phrase"]["supported"]) == (detail == "full")
            for detail in A4_DETAIL_MODES
        ),
    }


def check_a4_dataset(
    count: int,
    output_root: Path,
    *,
    warmups: int,
    iterations: int,
) -> dict[str, Any]:
    """Run the manual A-4 behavior and tokenizer probes against one dataset."""

    database_path = output_root / str(count) / "messages.db"
    if not database_path.exists():
        raise FileNotFoundError(f"generated database does not exist: {database_path}")
    connection = sqlite3.connect(database_path)
    try:
        return {
            "count": count,
            "short_match": _probe_short_match(connection),
            "syntax_escaping": _probe_syntax_escaping(),
            "like_index_reuse": _probe_like_reuse(
                connection,
                warmups=warmups,
                iterations=iterations,
            ),
            "detail_comparison": _measure_detail_variants(
                connection,
                warmups=warmups,
                iterations=iterations,
            ),
        }
    finally:
        connection.close()


def _count_like(connection: sqlite3.Connection, term: str) -> int:
    sql, parameters = _like_sql(term)
    row = connection.execute(f"SELECT count(*) FROM ({sql})", parameters).fetchone()
    if row is None:
        raise RuntimeError("SQLite did not return a LIKE count")
    return int(row[0])


def _find_rare_terms(
    connection: sqlite3.Connection,
    length: int,
    *,
    use_like: bool,
) -> tuple[str, str]:
    row = connection.execute(
        """
        SELECT subject_norm, sender_norm, attachment_names
        FROM message_contents
        WHERE message_id = (SELECT min(message_id) FROM message_contents)
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("Synthetic database has no message contents")

    candidates: list[str] = []
    for value in row:
        text = str(value or "")
        candidates.extend(text[index : index + length] for index in range(len(text) - length + 1))

    count = connection.execute("SELECT count(*) FROM message_contents").fetchone()
    if count is None:
        raise RuntimeError("SQLite did not return a message count")
    corpus_count = int(count[0])
    max_rare_count = max(1, corpus_count // 100)
    selected: list[str] = []
    least_frequent: tuple[int, str] | None = None
    seen: set[str] = set()
    for candidate in candidates:
        if len(candidate) != length or candidate.isspace() or candidate in seen:
            continue
        seen.add(candidate)
        candidate_count = (
            _count_like(connection, candidate) if use_like else _count_match(connection, candidate)
        )
        if candidate_count > 0 and (least_frequent is None or candidate_count < least_frequent[0]):
            least_frequent = (candidate_count, candidate)
        if 0 < candidate_count <= max_rare_count:
            selected.append(candidate)
        if len(selected) == 2:
            return selected[0], selected[1]
    if selected:
        return selected[0], selected[0]
    if least_frequent is not None:
        return least_frequent[1], least_frequent[1]
    raise RuntimeError(f"Could not find rare {length}-character benchmark terms")


def _common_terms(length: int) -> tuple[str, str]:
    common = {
        3: ("exa", "amp"),
        5: ("xampl", "ample"),
        10: ("xample.tes", "ample.test"),
    }
    try:
        return common[length]
    except KeyError as error:
        raise ValueError(f"Unsupported MATCH benchmark length: {length}") from error


def _build_query_specs(connection: sqlite3.Connection) -> tuple[QuerySpec, ...]:
    specs: list[QuerySpec] = []
    for length in (3, 5, 10):
        common_first, common_second = _common_terms(length)
        rare_first, rare_second = _find_rare_terms(connection, length, use_like=False)
        specs.extend(
            (
                QuerySpec(f"match_{length}_single_high", "MATCH", (common_first,)),
                QuerySpec(f"match_{length}_single_low", "MATCH", (rare_first,)),
                QuerySpec(
                    f"match_{length}_and_high",
                    "MATCH",
                    (common_first, common_second),
                ),
                QuerySpec(
                    f"match_{length}_or_high",
                    "MATCH",
                    (common_first, common_second),
                    mode="or",
                ),
                QuerySpec(
                    f"match_{length}_exclude",
                    "MATCH",
                    (common_first,),
                    exclude_terms=(rare_first,),
                ),
                QuerySpec(
                    f"match_{length}_and_low",
                    "MATCH",
                    (rare_first, rare_second),
                ),
                QuerySpec(
                    f"match_{length}_or_low",
                    "MATCH",
                    (rare_first, rare_second),
                    mode="or",
                ),
            )
        )

    like_rare_first, _ = _find_rare_terms(connection, 2, use_like=True)
    specs.extend(
        (
            QuerySpec("like_2_single_high", "LIKE", ("ex",)),
            QuerySpec("like_2_single_low", "LIKE", (like_rare_first,)),
        )
    )
    return tuple(specs)


def _measure_queries(
    connection: sqlite3.Connection,
    *,
    warmups: int,
    iterations: int,
) -> dict[str, dict[str, Any]]:
    measurements: dict[str, dict[str, Any]] = {}
    for spec in _build_query_specs(connection):
        if spec.path == "MATCH":
            sql, parameters = _fts_match_sql(spec)
        else:
            sql, parameters = _like_sql(spec.terms[0])
        timing, hit_count = _time_query(
            connection,
            sql,
            parameters,
            warmups=warmups,
            iterations=iterations,
        )
        measurements[spec.name] = {
            "path": spec.path,
            "terms": spec.terms,
            "mode": spec.mode,
            "exclude_terms": spec.exclude_terms,
            "hit_count": hit_count,
            "p50_ms": timing.p50_ms,
            "p95_ms": timing.p95_ms,
        }
    return measurements


def _dbstat_bytes(connection: sqlite3.Connection, name_pattern: str) -> int | None:
    try:
        row = connection.execute(
            "SELECT sum(pgsize) FROM dbstat WHERE name GLOB ?",
            (name_pattern,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _measure_sizes(
    connection: sqlite3.Connection,
    database_path: Path,
) -> dict[str, int | float | None]:
    content_page_bytes = _dbstat_bytes(connection, "message_contents")
    fts_page_bytes = _dbstat_bytes(connection, "messages_fts*")
    payload_row = connection.execute(
        """
        SELECT sum(
            length(cast(coalesce(subject_norm, '') AS blob))
          + length(cast(coalesce(sender_norm, '') AS blob))
          + length(cast(coalesce(body_text, '') AS blob))
          + length(cast(coalesce(attachment_names, '') AS blob))
        )
        FROM message_contents
        """
    ).fetchone()
    content_payload_bytes = int(payload_row[0] or 0) if payload_row is not None else 0
    ratio = (
        fts_page_bytes / content_page_bytes
        if fts_page_bytes is not None and content_page_bytes
        else None
    )
    payload_ratio = (
        fts_page_bytes / content_payload_bytes
        if fts_page_bytes is not None and content_payload_bytes
        else None
    )
    return {
        "database_file_bytes": database_path.stat().st_size,
        "message_contents_payload_bytes": content_payload_bytes,
        "message_contents_page_bytes": content_page_bytes,
        "messages_fts_page_bytes": fts_page_bytes,
        "fts_to_message_contents_page_ratio": ratio,
        "fts_to_message_contents_payload_ratio": payload_ratio,
    }


def _measure_sorting(
    connection: sqlite3.Connection,
    *,
    warmups: int,
    iterations: int,
) -> dict[str, Any]:
    count_row = connection.execute("SELECT count(*) FROM messages").fetchone()
    if count_row is None:
        raise RuntimeError("SQLite did not return the message count")
    count = int(count_row[0])
    requested_offset = DEEP_PAGE_OFFSET
    deep_offset = requested_offset if count > requested_offset else max(0, count - PAGE_SIZE)
    cursor_row = connection.execute(
        """
        SELECT COALESCE(date_sent, internal_date, ''), id
        FROM messages
        ORDER BY COALESCE(date_sent, internal_date, '') DESC, id DESC
        LIMIT 1 OFFSET ?
        """,
        (deep_offset,),
    ).fetchone()
    if cursor_row is None:
        raise RuntimeError("SQLite did not return a keyset cursor")

    first_page_sql = """
        SELECT id
        FROM messages
        ORDER BY COALESCE(date_sent, internal_date, '') DESC, id DESC
        LIMIT ?
    """
    keyset_sql = """
        SELECT id
        FROM messages
        WHERE (COALESCE(date_sent, internal_date, ''), id) < (?, ?)
        ORDER BY COALESCE(date_sent, internal_date, '') DESC, id DESC
        LIMIT ?
    """
    measurements: dict[str, Any] = {
        "requested_deep_offset": requested_offset,
        "measured_deep_offset": deep_offset,
    }
    for enabled in (False, True):
        connection.execute("DROP INDEX IF EXISTS idx_bench_sort")
        if enabled:
            connection.execute(
                """
                CREATE INDEX idx_bench_sort
                ON messages(COALESCE(date_sent, internal_date, '') DESC, id DESC)
                """
            )
        connection.commit()
        suffix = "with_expression_index" if enabled else "without_expression_index"
        first_timing, first_hits = _time_query(
            connection,
            first_page_sql,
            (PAGE_SIZE,),
            warmups=warmups,
            iterations=iterations,
        )
        keyset_timing, keyset_hits = _time_query(
            connection,
            keyset_sql,
            (cursor_row[0], cursor_row[1], PAGE_SIZE),
            warmups=warmups,
            iterations=iterations,
        )
        measurements[suffix] = {
            "first_page": {
                "hit_count": first_hits,
                "p50_ms": first_timing.p50_ms,
                "p95_ms": first_timing.p95_ms,
            },
            "deep_keyset_page": {
                "hit_count": keyset_hits,
                "p50_ms": keyset_timing.p50_ms,
                "p95_ms": keyset_timing.p95_ms,
            },
        }
    connection.execute("DROP INDEX IF EXISTS idx_bench_sort")
    connection.commit()
    return measurements


def _measure_structured_filter(
    connection: sqlite3.Connection,
    *,
    warmups: int,
    iterations: int,
) -> dict[str, Any]:
    folder_row = connection.execute(
        "SELECT id FROM folders WHERE account_id = ? AND raw_name = ?",
        (ACCOUNT_ID, FOLDER_NAME),
    ).fetchone()
    if folder_row is None:
        raise RuntimeError("Synthetic folder was not created")
    sql = """
        SELECT m.id
        FROM messages AS m
        JOIN folders AS f ON f.id = m.folder_id
        WHERE m.account_id = ?
          AND m.folder_id = ?
          AND m.date_sent >= ?
          AND m.date_sent < ?
          AND m.has_attachment = ?
          AND m.local_state = ?
        ORDER BY COALESCE(m.date_sent, m.internal_date, '') DESC, m.id DESC
        LIMIT ?
    """
    timing, hit_count = _time_query(
        connection,
        sql,
        (
            ACCOUNT_ID,
            folder_row[0],
            "2026-01-01T00:00:00+00:00",
            "2027-01-01T00:00:00+00:00",
            1,
            "active",
            PAGE_SIZE,
        ),
        warmups=warmups,
        iterations=iterations,
    )
    return {
        "hit_count": hit_count,
        "p50_ms": timing.p50_ms,
        "p95_ms": timing.p95_ms,
    }


def _load_contents(connection: sqlite3.Connection, limit: int) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in connection.execute(
            """
            SELECT subject_norm, sender_norm, body_text, attachment_names
            FROM message_contents
            ORDER BY message_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    ]


def _measure_insert_variant(
    contents: Sequence[tuple[object, ...]],
    *,
    triggers_enabled: bool,
    iterations: int,
) -> TimingStats:
    durations: list[float] = []
    for _ in range(iterations):
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO accounts(id, provider_type) VALUES (?, ?)",
                (ACCOUNT_ID, "synthetic"),
            )
            folder_cursor = connection.execute(
                "INSERT INTO folders(account_id, raw_name, display_name) VALUES (?, ?, ?)",
                (ACCOUNT_ID, FOLDER_NAME, FOLDER_NAME),
            )
            folder_id = folder_cursor.lastrowid
            connection.executemany(
                """
                INSERT INTO messages(
                    account_id, folder_id, content_key, source_item_key, uid, uidvalidity
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (ACCOUNT_ID, folder_id, f"bench:{index}", f"bench:{index}", index, 1)
                    for index in range(1, len(contents) + 1)
                ],
            )
            connection.commit()
            if not triggers_enabled:
                connection.executescript(
                    "DROP TRIGGER mc_ai; DROP TRIGGER mc_ad; DROP TRIGGER mc_au;"
                )
            started = time.perf_counter()
            connection.executemany(
                """
                INSERT INTO message_contents(
                    message_id, subject_norm, sender_norm, body_text, attachment_names
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [(index, *values) for index, values in enumerate(contents, start=1)],
            )
            connection.commit()
            durations.append((time.perf_counter() - started) * 1000)
        finally:
            connection.close()
    return TimingStats(_percentile(durations, 0.50), _percentile(durations, 0.95))


def _measure_insert_throughput(
    connection: sqlite3.Connection,
    *,
    iterations: int,
) -> dict[str, Any]:
    count_row = connection.execute("SELECT count(*) FROM message_contents").fetchone()
    if count_row is None:
        raise RuntimeError("SQLite did not return the content count")
    contents = _load_contents(connection, min(1_000, int(count_row[0])))
    measurements: dict[str, Any] = {"rows": len(contents)}
    for enabled in (False, True):
        timing = _measure_insert_variant(
            contents,
            triggers_enabled=enabled,
            iterations=max(3, iterations // 2),
        )
        suffix = "with_triggers" if enabled else "without_triggers"
        measurements[suffix] = {
            "p50_ms": timing.p50_ms,
            "p95_ms": timing.p95_ms,
            "p50_rows_per_second": len(contents) / (timing.p50_ms / 1000),
            "p95_rows_per_second": len(contents) / (timing.p95_ms / 1000),
        }
    return measurements


def measure_dataset(
    count: int,
    output_root: Path,
    *,
    warmups: int,
    iterations: int,
) -> dict[str, Any]:
    """Measure every A-3 operation against one generated corpus."""

    dataset_root = output_root / str(count)
    database_path = dataset_root / "messages.db"
    if not database_path.exists():
        raise FileNotFoundError(f"generated database does not exist: {database_path}")
    connection = sqlite3.connect(database_path)
    try:
        sizes = _measure_sizes(connection, database_path)
        queries = _measure_queries(connection, warmups=warmups, iterations=iterations)
        sorting = _measure_sorting(connection, warmups=warmups, iterations=iterations)
        structured_filter = _measure_structured_filter(
            connection,
            warmups=warmups,
            iterations=iterations,
        )
        insert_throughput = _measure_insert_throughput(connection, iterations=iterations)
    finally:
        connection.close()
    return {
        "count": count,
        "sizes": sizes,
        "queries": queries,
        "sorting": sorting,
        "structured_filter": structured_filter,
        "insert_throughput": insert_throughput,
    }


def _linear_extrapolation(points: Sequence[tuple[int, float]], target: int) -> float:
    if len(points) == 1:
        return points[0][1]
    x_mean = sum(point[0] for point in points) / len(points)
    y_mean = sum(point[1] for point in points) / len(points)
    denominator = sum((point[0] - x_mean) ** 2 for point in points)
    if denominator == 0:
        return y_mean
    slope = sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator
    return max(0.0, y_mean + slope * (target - x_mean))


def _build_extrapolation(reports: Sequence[dict[str, Any]]) -> dict[str, Any]:
    target = 50_000

    def size_points(key: str) -> list[tuple[int, float]]:
        return [(int(report["count"]), float(report["sizes"][key])) for report in reports]

    extrapolated_sizes = {
        key: _linear_extrapolation(size_points(key), target)
        for key in (
            "database_file_bytes",
            "message_contents_payload_bytes",
            "message_contents_page_bytes",
            "messages_fts_page_bytes",
        )
    }
    content_pages = extrapolated_sizes["message_contents_page_bytes"]
    fts_pages = extrapolated_sizes["messages_fts_page_bytes"]
    ratio = fts_pages / content_pages if content_pages else None
    payload_points = size_points("message_contents_payload_bytes")
    payload_ratio = (
        _linear_extrapolation(size_points("messages_fts_page_bytes"), target)
        / _linear_extrapolation(payload_points, target)
        if _linear_extrapolation(payload_points, target)
        else None
    )

    query_names = reports[0]["queries"]
    query_extrapolation: dict[str, Any] = {}
    for name in query_names:
        query_extrapolation[name] = {
            field: _linear_extrapolation(
                [
                    (int(report["count"]), float(report["queries"][name][field]))
                    for report in reports
                ],
                target,
            )
            for field in ("p50_ms", "p95_ms")
        }
    match_p95 = max(
        float(values["p95_ms"])
        for name, values in query_extrapolation.items()
        if str(reports[0]["queries"][name]["path"]) == "MATCH"
    )
    like_p95 = max(
        float(values["p95_ms"])
        for name, values in query_extrapolation.items()
        if str(reports[0]["queries"][name]["path"]) == "LIKE"
    )
    return {
        "target_count": target,
        "sizes": {
            **extrapolated_sizes,
            "fts_to_message_contents_page_ratio": ratio,
            "fts_to_message_contents_payload_ratio": payload_ratio,
        },
        "queries": query_extrapolation,
        "targets": {
            "match_p95_ms_max": match_p95,
            "match_under_300ms": match_p95 <= 300,
            "like_p95_ms_max": like_p95,
            "like_under_3s": like_p95 <= 3_000,
            # external-content FTS can be smaller than the source payload; the
            # requirement is an upper bound, not a minimum duplication factor.
            "fts_ratio_at_most_5": payload_ratio is not None and 0 <= payload_ratio <= 5,
        },
    }


def _print_report(results: dict[str, Any]) -> None:
    print(f"\n=== {results['count']} messages ===")
    sizes = results["sizes"]
    print(
        "sizes: database={database_file_bytes:.1f} MiB, "
        "message_contents={message_contents_page_bytes} B, "
        "messages_fts={messages_fts_page_bytes} B, page ratio={page_ratio:.2f}x, "
        "payload ratio={payload_ratio:.2f}x".format(
            database_file_bytes=float(sizes["database_file_bytes"]) / 1024 / 1024,
            message_contents_page_bytes=sizes["message_contents_page_bytes"],
            messages_fts_page_bytes=sizes["messages_fts_page_bytes"],
            page_ratio=float(sizes["fts_to_message_contents_page_ratio"]),
            payload_ratio=float(sizes["fts_to_message_contents_payload_ratio"]),
        )
    )
    print("queries (p50/p95 ms, hits):")
    for name, measurement in results["queries"].items():
        print(
            f"  {name:28} {measurement['p50_ms']:8.3f}/{measurement['p95_ms']:8.3f} "
            f"hits={measurement['hit_count']}"
        )
    sorting = results["sorting"]
    print(
        f"sorting: deep offset={sorting['measured_deep_offset']} "
        f"(requested {sorting['requested_deep_offset']})"
    )
    for name in ("without_expression_index", "with_expression_index"):
        measurement = sorting[name]
        print(
            f"  {name:28} first={measurement['first_page']['p50_ms']:.3f}/"
            f"{measurement['first_page']['p95_ms']:.3f} ms, "
            f"keyset={measurement['deep_keyset_page']['p50_ms']:.3f}/"
            f"{measurement['deep_keyset_page']['p95_ms']:.3f} ms"
        )
    structured = results["structured_filter"]
    print(
        f"structured filter: {structured['p50_ms']:.3f}/{structured['p95_ms']:.3f} ms, "
        f"hits={structured['hit_count']}"
    )
    print("insert throughput:")
    for name in ("without_triggers", "with_triggers"):
        measurement = results["insert_throughput"][name]
        print(
            f"  {name:28} {measurement['p50_rows_per_second']:.1f} rows/s "
            f"(p50 {measurement['p50_ms']:.3f} ms)"
        )


def _print_a4_report(results: dict[str, Any]) -> None:
    print(f"\n=== A-4 behavior checks: {results['count']} messages ===")
    short_match = results["short_match"]
    print(
        f"2-character MATCH term={short_match['term']!r}: "
        f"MATCH hits={short_match['match_count']}, "
        f"LIKE hits={short_match['like_count']} "
        f"({'PASS' if short_match['match_returns_zero'] else 'FAIL'} MATCH=0)"
    )
    escaping = results["syntax_escaping"]
    print(
        f"FTS escaping: raw errors={escaping['raw_parse_error_count']}, "
        f"quoted errors={escaping['quoted_parse_error_count']} "
        f"({'PASS' if escaping['all_quoted_terms_safe'] else 'FAIL'} quoted)"
    )
    reuse = results["like_index_reuse"]
    print(
        f"LIKE reuse decision: {'REUSE' if reuse['two_character_reusable'] else 'DO NOT REUSE'} "
        "trigram index for 2-character terms"
    )
    for label, measurement in reuse["measurements"].items():
        print(
            f"  {label:14} term={measurement['term']!r}, "
            f"FTS p95={measurement['fts_table']['p95_ms']:.3f} ms, "
            f"message_contents p95={measurement['message_contents']['p95_ms']:.3f} ms"
        )
    details = results["detail_comparison"]
    print(
        f"detail phrase support: "
        f"({'PASS' if details['phrase_requires_detail_full'] else 'FAIL'} full only)"
    )
    for detail, measurement in details["variants"].items():
        match_10 = measurement["match_10"]
        match_10_text = (
            f"MATCH10 {match_10['p95_ms']:.3f} ms"
            if match_10["supported"]
            else f"MATCH10 unsupported ({match_10['error']})"
        )
        print(
            f"  detail={detail:6} FTS={measurement['fts_page_bytes']} B, "
            f"MATCH3 p95={measurement['match_3']['p95_ms']:.3f} ms, "
            f"{match_10_text}"
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
    parser.add_argument(
        "--measure",
        action="store_true",
        help="measure A-3 size, query, insert, sort, and filter benchmarks",
    )
    parser.add_argument(
        "--check-a4",
        action="store_true",
        help="run A-4 short MATCH, escaping, LIKE reuse, and detail probes",
    )
    parser.add_argument(
        "--warmups",
        type=_nonnegative_count,
        default=DEFAULT_WARMUPS,
        help=f"warmup runs before each measurement (default: {DEFAULT_WARMUPS})",
    )
    parser.add_argument(
        "--iterations",
        type=_positive_count,
        default=DEFAULT_ITERATIONS,
        help=f"timed runs for each measurement (default: {DEFAULT_ITERATIONS})",
    )
    parser.add_argument(
        "--results",
        type=Path,
        help="write the full measurements and 50,000-message extrapolation as JSON",
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

    if args.measure:
        reports: list[dict[str, Any]] = []
        for count in args.counts:
            dataset_root = output_root / str(count)
            if not dataset_root.exists():
                stats = generate_dataset(count, output_root, args.seed)
                print(
                    f"generated {stats.count} messages | "
                    f"EML {stats.eml_bytes / 1024 / 1024:8.1f} MiB | "
                    f"median body {stats.median_body_bytes:>6} bytes | "
                    f"long messages {stats.long_message_count:>4}"
                )
            report = measure_dataset(
                count,
                output_root,
                warmups=args.warmups,
                iterations=args.iterations,
            )
            reports.append(report)
            _print_report(report)

        extrapolation = _build_extrapolation(reports)
        print("\n=== 50,000-message linear extrapolation ===")
        extrapolated_sizes = extrapolation["sizes"]
        content_mib = extrapolated_sizes["message_contents_page_bytes"] / 1024 / 1024
        fts_mib = extrapolated_sizes["messages_fts_page_bytes"] / 1024 / 1024
        size_ratio = extrapolated_sizes["fts_to_message_contents_page_ratio"]
        print(
            f"sizes: message_contents={content_mib:.1f} MiB, "
            f"messages_fts={fts_mib:.1f} MiB, ratio={size_ratio:.2f}x"
        )
        targets = extrapolation["targets"]
        print(
            f"targets: MATCH p95 max={targets['match_p95_ms_max']:.3f} ms "
            f"({'PASS' if targets['match_under_300ms'] else 'FAIL'} <= 300 ms), "
            f"LIKE p95 max={targets['like_p95_ms_max']:.3f} ms "
            f"({'PASS' if targets['like_under_3s'] else 'FAIL'} <= 3 s), "
            f"FTS ratio {'PASS' if targets['fts_ratio_at_most_5'] else 'FAIL'} (<= 5x)"
        )
        results = {
            "sqlite_version": sqlite3.sqlite_version,
            "warmups": args.warmups,
            "iterations": args.iterations,
            "datasets": reports,
            "extrapolation": extrapolation,
        }
        if args.results is not None:
            results_path = args.results.resolve()
            results_path.parent.mkdir(parents=True, exist_ok=True)
            results_path.write_text(
                json.dumps(results, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"results_json={results_path}")
        return

    if args.check_a4:
        a4_reports: list[dict[str, Any]] = []
        for count in args.counts:
            dataset_root = output_root / str(count)
            if not dataset_root.exists():
                stats = generate_dataset(count, output_root, args.seed)
                print(
                    f"generated {stats.count} messages | "
                    f"EML {stats.eml_bytes / 1024 / 1024:8.1f} MiB | "
                    f"median body {stats.median_body_bytes:>6} bytes | "
                    f"long messages {stats.long_message_count:>4}"
                )
            report = check_a4_dataset(
                count,
                output_root,
                warmups=args.warmups,
                iterations=args.iterations,
            )
            a4_reports.append(report)
            _print_a4_report(report)
        if args.results is not None:
            results_path = args.results.resolve()
            results_path.parent.mkdir(parents=True, exist_ok=True)
            results_path.write_text(
                json.dumps(
                    {
                        "sqlite_version": sqlite3.sqlite_version,
                        "warmups": args.warmups,
                        "iterations": args.iterations,
                        "a4": a4_reports,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            print(f"results_json={results_path}")
        return

    for count in args.counts:
        stats = generate_dataset(count, output_root, args.seed)
        print(
            f"{stats.count:>6} messages | EML {stats.eml_bytes / 1024 / 1024:8.1f} MiB | "
            f"median body {stats.median_body_bytes:>6} bytes | "
            f"long messages {stats.long_message_count:>4}"
        )


if __name__ == "__main__":
    main()
