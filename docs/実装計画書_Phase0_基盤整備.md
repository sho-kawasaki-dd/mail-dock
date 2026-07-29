# **Phase 0: 基盤整備 実装計画書**

対象: [ローカルメールバックアップ＆閲覧アプリ 開発計画書.md](./ローカルメールバックアップand閲覧アプリ開発計画書.md) の「6. 開発ロードマップ」における **Phase 0: 基盤整備**

本書と開発計画書に矛盾がある場合は、**開発計画書を正**とする。

---

## **1. 目的**

Phase 1 以降（IMAPコア / 検索 / GUI / 切断対応 / PSTアーカイブ）の実装が、**設計判断をやり直すことなく積み上げられる土台**を構築する。

具体的には、以下を満たすヘッドレス（UIを持たない）基盤一式を完成させる。

1. **一連の起動シーケンスが通ること** — 設定読み込み → ロギング初期化 → ストレージルート解決 → 多重起動ロック取得 → DBマイグレーション適用 → 正常終了。
2. **後から差し込むと全件再取得・全件再解析が必要になる要素を、最初から確定させること** — DBスキーマ（`source_item_key`・スレッド情報・IMAPフラグ・両一意インデックス）、例外階層、ログのマスキング規約、設定スキーマ。
3. **設計不変条件を守る「関門」をコード上に一箇所ずつ作ること** — I/O例外の分類（`detach.py`）、ルート同定（`.maildock_root` のUUID照合）、単一ライター前提の接続管理。
4. **開発サイクルを自動化すること** — `ruff` / `mypy` / `pytest` とCI、および Windows（モック）と WSL（Docker）に分離したテスト実行経路。

**Phase 0 のゴール判定:** 上記1の起動シーケンスが実機で通り、7章の検証項目がすべて成功し、CIが緑になること。

---

## **2. 要件**

### **2.1 前提となる意思決定（確定済み）**

| # | 項目 | 決定内容 |
| :--- | :---- | :---- |
| D-1 | パッケージ管理 | **uv に統一**。`uv.lock` をコミットし、CIは `uv sync --frozen` を使う |
| D-2 | `001_init.sql` の範囲 | **開発計画書3章の全テーブル（PST関連を除く）を一括作成**。`002_pst_import.sql` は Phase 4.5 |
| D-3 | テスト用IMAP環境 | **Windows は imaplib モックのみ / WSL(Linux) 上で Docker を用いた結合テスト**。pytestマーカーで分離 |
| D-4 | Dockerイメージ | Phase 0 は **GreenMail のみ**。Dovecot は Phase 1 冒頭で必要に応じ追加 |
| D-5 | CI | **GitHub Actions を Phase 0 で作成**（Windows: lint/型/単体、Linux: Docker結合） |
| D-6 | storage_root | **最小版を Phase 0 に含める**（`.maildock_root` のUUID照合・`.lock`・I/O例外分類）。状態機械・デバイス監視は Phase 4 |
| D-7 | 設定ファイル | `platformdirs` 配下に **JSON**（`config.json`、原子的書き込み、`schema_version` 付き） |
| D-8 | 静的解析 | ruff `E,F,I,N,UP,B,SIM,PTH,RUF` + formatter、line-length 100 / **mypy `strict = true`**（`presentation` 層のみ緩和） |
| D-9 | `metadata.db.bak` の内蔵ディスク複製 | **Phase 0 では `AppConfig` に設定項目の枠だけ定義**し、実処理は Phase 4。既定はOFF（C:のBitLocker有効時のみの明示的オプトイン） |
| D-10 | CLI | `--storage-root` / `--debug` / `--version` の最小 argparse に加え、**`migrate` / `verify` をサブコマンドとして常設**する（将来GUI起動が既定になっても残す） |

### **2.2 機能要件**

| # | 要件 | 根拠（開発計画書） |
| :--- | :---- | :---- |
| F-1 | `src` レイアウトで `domain / usecases / infrastructure / presentation` の依存方向が一方向に保たれていること。Phase 0 では実装するモジュールのみ作成し、空ディレクトリを先行生成しない | 2.2 |
| F-2 | ドメイン例外階層が定義され、`infrastructure` 層の生の例外（`OSError` / `sqlite3.Error`）が上位へ漏れないこと | 2.3 / 5.7.1-2a |
| F-3 | アプリ全般ログを `{config_dir}/logs/app.log` に `RotatingFileHandler`（5MB × 5世代）で出力し、**本文を出力せず**、件名は先頭20文字、メールアドレスはマスクすること | 5.5 |
| F-4 | ログ出力先を `{storage_root}/logs/` から `{config_dir}/logs/` へ切り替えるAPIが存在すること（切断時に内蔵ディスクへ退避するため） | 5.7.1-3-5 |
| F-5 | 設定を `platformdirs` 配下のJSONで原子的に読み書きし、未知キーを設定オブジェクト内で保持し、値の型・範囲・列挙値を検証し、`schema_version` の前方非互換を検出できること | 5.11 / D-7 |
| F-6 | ストレージルートの同定を **`.maildock_root` のUUID照合**で行い、`OK` / `MISSING` / `FOREIGN` の3値を返すこと。パスの存在のみを同定根拠にしないこと | 2.4-3 / 2.4-10 |
| F-7 | 設定の候補パスリストを順に照合し、**ドライブレター変更に自動追従**できること | 2.4-3 |
| F-8 | ルート配下に `eml/` `manifests/imap/` `manifests/pst/` `tmp/` `logs/` を作成できること（`tmp/` は必ずルート配下＝EMLと同一ボリューム） | 2.4-9 |
| F-9 | 空き容量を確認し、残20GB未満で警告、5GB未満で `InsufficientSpaceError` を送出すること | 2.4-5 |
| F-10 | ネットワークドライブを判定し、その場合 `journal_mode` を `DELETE` へフォールバックできること | 2.4-4 / 3.6 |
| F-11 | `.lock` による多重起動防止と、**ハートビート鮮度によるスタールロック回収**が動作すること | 3.6 |
| F-12 | DB接続時に開発計画書3.6のPRAGMAを適用し、接続をスレッド間で共有せず（`threading.local()`）、所有スレッドで協調的に全接続をクローズできること | 3.6 / 5.7.1-1 |
| F-13 | `PRAGMA user_version` による原子的なマイグレーションが動作し、未適用がある既存DB（非空v0を含む）の適用前に自動バックアップを取り、DBが新しすぎる場合は拒否すること | 3.6 |
| F-14 | `001_init.sql` により開発計画書3.1〜3.5（PST以外）＋ `app_state` のスキーマが構築され、FTS5の3トリガーが機能すること | 3.1〜3.5 / 5.7.1-4-4 |
| F-15 | `ruff` / `mypy strict` / `pytest` がローカルとCIの両方で実行でき、Docker必須テストがマーカーで分離されていること | 5.9 / 5.10 / D-3 |

### **2.3 非機能要件・制約**

* Python **3.13** 固定（`requires-python = ">=3.13"`）。
* 本体ライセンスは **GPL-3.0-or-later**。Phase 0 で `LICENSE` と `THIRD-PARTY-LICENSES.md` の枠を用意する。
* Phase 0 のコードは **PySide6 に依存しない**（`presentation` 層を作らないため、Linux CI でGUI関連のセットアップが不要）。
* `mypy strict` を通すこと。型無視（`# type: ignore`）を使う場合は理由をコメントで併記する。

---

## **3. タスク**

> 依存関係: **A → B → C → D**。同一グループ内の並行可否は各タスクに明記する。

### **3.1 グループA: プロジェクト骨格とツールチェーン**

#### **A-1. `pyproject.toml` の確定**

- [x] `[build-system]` に `hatchling` を指定し、`[tool.hatch.build.targets.wheel] packages = ["src/mail_dock"]` を設定する
- [x] `[project]` に `requires-python = ">=3.13"`、`license = "GPL-3.0-or-later"`（PEP 639 SPDX文字列）、`license-files = ["LICENSE"]`、`description`、`authors` を設定する
- [x] `dependencies` を開発計画書2.1の確定リストどおりに記述する（`PySide6>=6.8` / `keyring>=25` / `beautifulsoup4>=4.12` / `charset-normalizer>=3.3` / `platformdirs>=4`）
- [x] `[dependency-groups] dev` に `pytest` / `pytest-qt` / `pytest-cov` / `ruff` / `mypy` を記述し、mypy strict のため **`types-beautifulsoup4`** を追加する
- [x] `[project.scripts]` に `mail-dock = "mail_dock.__main__:main"` を定義する
- [x] `[tool.ruff]` を設定する（`target-version = "py313"`、`line-length = 100`、`lint.select = ["E","F","I","N","UP","B","SIM","PTH","RUF"]`、`tests/**` の per-file-ignores）
- [x] `[tool.mypy]` を設定する（`strict = true`、`python_version = "3.13"`、`files = ["src", "tests"]`、`mail_dock.presentation.*` と `PySide6.*` の overrides）
- [x] `[tool.pytest.ini_options]` を設定する（`testpaths = ["tests"]`、`addopts = "--strict-markers"`、`markers = ["docker: ...", "gui: ..."]`）
- [x] `[tool.coverage.run]` に `source = ["src/mail_dock"]` を設定する
- [x] `uv lock` を実行し、`uv.lock` を生成してコミット対象に含める
- [x] `uv sync` が成功することを確認する

#### **A-2. リポジトリ骨格の作成**

- [x] 以下のファイル・ディレクトリを作成する（**Phase 0 で実装するモジュールのみ**。空の `views/` `usecases/` 等は作らない）

```Plaintext
src/mail_dock/__init__.py
src/mail_dock/__main__.py
src/mail_dock/config.py
src/mail_dock/domain/__init__.py
src/mail_dock/domain/errors.py
src/mail_dock/infrastructure/__init__.py
src/mail_dock/infrastructure/logging_config.py
src/mail_dock/infrastructure/storage/__init__.py
src/mail_dock/infrastructure/storage/detach.py
src/mail_dock/infrastructure/storage/storage_root.py
src/mail_dock/infrastructure/database/__init__.py
src/mail_dock/infrastructure/database/connection.py
src/mail_dock/infrastructure/database/migrator.py
src/mail_dock/migrations/001_init.sql
tests/__init__.py
tests/conftest.py
tests/unit/ tests/integration/ tests/support/ tests/fixtures/eml/ tests/docker/
```

- [x] `src/mail_dock/__init__.py` に `__version__` を定義する（`pyproject.toml` の version と一致させる）
- [x] `uv run python -c "import mail_dock"` が成功することを確認する

#### **A-3. ライセンス・ドキュメント（*A-1と並行可*）**

- [x] `LICENSE` に GPL-3.0 の全文を配置する
- [x] `THIRD-PARTY-LICENSES.md` を作成し、PySide6(Qt) / keyring / beautifulsoup4 / charset-normalizer / platformdirs の欄を用意する（readpst欄は Phase 4.5 で追記する旨をコメントで明記）
- [x] `README.md` に以下を記載する
  - [x] プロジェクトの目的と概要
  - [x] **前提条件: ストレージルートの BitLocker To Go による暗号化（5.3）**
  - [x] **バックアップ方針: 3-2-1ルールの推奨とドライブ丸ごとコピーで完結する構造（5.7）**
  - [x] 開発セットアップ手順（`uv sync` / `uv run ruff check .` / `uv run mypy` / `uv run pytest -m "not docker"`）
  - [x] WSL上でのDockerテスト手順
- [x] `.gitignore` に追記する（`vendor/readpst/` のバイナリ、`*.db` / `*.db-wal` / `*.db-shm` / `*.db.bak*`、`htmlcov/`）
- [x] `uv.lock` が `.gitignore` に含まれて**いない**ことを確認する

---

### **3.2 グループB: 横断基盤コード**

> *B-1〜B-4 は相互に並行可。B-5 は B-1 完了後。*

#### **B-1. `domain/errors.py` — ドメイン例外階層**

- [x] 外部依存ゼロで以下の階層を定義する

```Plaintext
MailDockError
├─ ConfigError
│   └─ ConfigVersionTooNewError
├─ StorageError
│   ├─ StorageDetachedError       # 稼働中の切断（5.7.1の分類先）
│   ├─ StorageForeignRootError    # .maildock_root のUUID不一致（MISSINGより危険）
│   ├─ StorageRootMissingError
│   ├─ StorageLockedError         # 他インスタンスが使用中
│   └─ InsufficientSpaceError
├─ DatabaseError
│   ├─ MigrationError
│   └─ SchemaVersionTooNewError   # 古いアプリで新しいDBを開かせない
├─ FetchError                     # Phase 1 で AuthenticationError / TransientError / PermanentError を追加
└─ OperationCancelledError
```

- [x] 各例外に用途を説明する docstring を付ける
- [x] モジュール docstring に「**infrastructure 層は生の例外をここへラップしてから上位へ渡す**」という規約を明記する
- [x] Phase 1 / 4.5 で葉を追加する箇所をコメントで示す

#### **B-2. `infrastructure/logging_config.py` — ロギング基盤（開発計画書5.5）**

- [x] `setup_logging(config_dir: Path, *, debug: bool) -> None` を実装する
  - [x] `{config_dir}/logs/app.log` に `RotatingFileHandler(maxBytes=5*1024*1024, backupCount=5, encoding="utf-8")` を設定する
  - [x] コンソールハンドラは `debug=True` または環境変数 `MAILDOCK_DEBUG` が設定されている場合のみ追加する（GUIでは stderr が失われるため、ファイル出力を必須とする）
- [x] `set_storage_log_target(path: Path | None) -> None` を実装する
  - [x] `path` 指定時は `{storage_root}/logs/sync-{YYYY-MM-DD}.log` ハンドラを追加する
  - [x] **`None` 指定時は当該ハンドラを確実に閉じて取り外す**（切断時に内蔵ディスク側へ退避するための必須API）
- [x] `MaskingFilter` を実装し、すべてのハンドラに装着する
  - [x] メールアドレスを `us***@example.com` 形式にマスクする
  - [x] `password` / `token` / `secret` を含むキーの値を `***` に置換する
- [x] `mask_subject(subject: str) -> str` を実装する（先頭20文字＋省略記号）
- [x] `purge_old_logs(log_dir: Path, days: int = 90) -> int` を実装する（Phase 0 では関数のみ。定期実行は Phase 4）
- [x] モジュール docstring に「**本文は絶対にログへ渡さない**」という呼び出し側の規約を明記する

#### **B-3. `config.py` — 設定管理（開発計画書5.11）**

- [x] 保存先を `platformdirs.user_config_dir("mail-dock", appauthor=False)` 配下の `config.json` として解決する関数を実装する
- [x] `@dataclass(frozen=True) class AppConfig` を定義し、**開発計画書5.11のPST以外の全項目**を初期から持たせる

| フィールド | 既定値 |
| :---- | :---- |
| `schema_version` | `1` |
| `storage_root_candidates: tuple[str, ...]` | `()` |
| `storage_root_uuid: str \| None` | `None` |
| `sync_on_startup: bool` | `True` |
| `sync_interval_minutes: int` | `60`（0で無効） |
| `max_message_bytes: int` | `50 * 1024 * 1024` |
| `remote_delete_mode: str` | `"trash"` |
| `remote_trash_folder: str \| None` | `None`（自動検出） |
| `delete_batch_limit: int` | `1000` |
| `trash_grace_days: int` | `30` |
| `purge_mode: str` | `"manual"`（A/B/C のA） |
| `block_remote_images: bool` | `True` |
| `startup_verification: str` | `"quick"` |
| `heartbeat_interval_sec: int` | `5` |
| `reprobe_attempts: int` | `3` |
| `sync_log_retention_days: int` | `90` |
| `db_backup_to_local_disk: bool` | `False` ※D-9。**枠のみ定義し Phase 0 では未使用** |

- [x] `load() -> AppConfig` を実装する（ファイル不在時は既定値を返す）
- [x] `save(config: AppConfig) -> None` を**原子的に**実装する（同一ディレクトリの一時ファイルへ書き込み → `flush` + `os.fsync` → `os.replace`）
- [x] `AppConfig` に未知キーを保持する `extra` フィールドを持たせ、モジュールグローバルへ退避せずに**未知キーを保持して書き戻す**仕組みを実装する（将来版が書いた設定を破壊しないため）
- [x] `schema_version` が現行より新しい場合 `ConfigVersionTooNewError` を送出する
- [x] `schema_version` が古い場合のアップグレード関数チェーンの枠を用意する（Phase 0 は v1 のみ）
- [x] JSONの構文・ルート型・既知キーの型・負数・許可されないモード値を検証し、`ConfigError` として報告する（不正な設定を既定値へ黙ってフォールバックしない）
- [x] `heartbeat_interval_sec` は `0` 以下を拒否し、正本の既定値である5秒に統一する
- [x] モジュール docstring に「**設定の読み書きのみを担当し、オブジェクト生成（DI）は行わない**」（開発計画書2.2）と明記する

#### **B-4. `infrastructure/storage/detach.py` — I/O例外の分類（開発計画書5.7.1-2a）**

- [x] `_DETACH_WINERRORS = frozenset({6, 21, 55, 433, 995, 1117, 1167})` を定義する
- [x] `classify_os_error(e: OSError) -> Exception` を実装する
  - [x] Windows: `winerror` が `_DETACH_WINERRORS` に含まれる場合 `StorageDetachedError` へ変換する
  - [x] **POSIX: `EIO(5)` / `ENXIO(6)` / `ENODEV(19)` / `ESTALE(116)` も分類する**（WSLでの切断試験を可能にするため）
  - [x] 該当しない例外はそのまま返す
- [x] `classify_sqlite_error(e: sqlite3.Error) -> Exception` を実装する（`sqlite_errorname` が `SQLITE_IOERR*` / `SQLITE_READONLY_DBMOVED` / `SQLITE_CANTOPEN`）
- [x] `@contextmanager storage_io()` を実装する（内部で例外を分類してから再送出するラッパ）
- [x] モジュール docstring に「**上位層に `winerror` やSQLiteエラーコードを漏らさない唯一の関門**である」と明記する

#### **B-5. `infrastructure/storage/storage_root.py` — ルート同定とロック（*B-1に依存*）**

**ルートマーカー**

- [x] `.maildock_root` の内容を `{"schema": 1, "root_uuid": "<uuid4>", "created_at": "<UTC ISO8601>", "app": "mail-dock"}` として定義する
- [x] `class RootProbe(StrEnum)` に `OK` / `MISSING` / `FOREIGN` を定義する
- [x] `initialize_root(path: Path) -> RootMarker` を実装する（既存マーカーがあれば読み込み、無ければ生成して fsync する）
- [x] `probe(path: Path, expected_uuid: str | None) -> RootProbe` を実装する
- [x] `RootResolution(path: Path | None, probe: RootProbe)` の結果型を定義し、`resolve_root(candidates: Sequence[Path], expected_uuid: str | None) -> RootResolution` を実装する（候補を順に照合し、**ドライブレター変更に自動追従**する）
- [x] `resolve_root()` はプローブ専用として書き込みを行わず、指定パスにマーカーがない初回だけコンポジションルートから明示的に `initialize_root()` を呼ぶ
- [x] 候補の正規化・重複排除・成功候補の先頭移動を定義し、一致候補がない場合に `FOREIGN` を `MISSING` より優先して返す
- [x] `FOREIGN` を `MISSING` より危険として扱う旨をコメントで明記する（別デバイスへの書き込み事故防止：開発計画書2.4-10）

**レイアウトと空き容量**

- [x] `ensure_layout(root: Path) -> None` を実装し、`eml/` `manifests/imap/` `manifests/pst/` `tmp/` `logs/` を作成する
- [x] `tmp/` が**必ずルート配下（EMLと同一ボリューム）**である理由（`os.replace` の原子性）をコメントで明記する
- [x] `free_space(path: Path) -> int` を `shutil.disk_usage` で実装する
- [x] `check_free_space(path: Path) -> SpaceStatus` を実装する（残20GB未満で警告、5GB未満で `InsufficientSpaceError`）
- [x] `drive_kind(path: Path) -> DriveKind` を実装する（Windows は `ctypes` で `GetDriveTypeW` を呼び `DRIVE_REMOTE` を検出、非Windowsは `LOCAL` 固定）

**多重起動ロック（開発計画書3.6）**

- [x] `class StorageLock` を実装する
  - [x] **ロック用ファイルと情報ファイルを分離する**: `.lock`（0バイト・排他ロック専用）と `.lock.meta.json`（`{pid, instance_uuid, machine_id, heartbeat_at}`）
        ※ `msvcrt.locking` は現在位置から N バイトをロックするため、同一ファイルへJSONを書くとロック範囲と衝突する
  - [x] Windows は `msvcrt.locking(fd, LK_NBLCK, 1)`、POSIX は `fcntl.flock(LOCK_EX | LOCK_NB)` を用いる（WSLテスト用）
  - [x] `touch_heartbeat() -> None` を実装する（駆動はPhase 3のUI側。Phase 0はAPIのみ）。ロックメタ情報の間隔は `heartbeat_interval_sec=5` と統一する
  - [x] スタール判定を実装する（開発計画書3.6の表）
        ロック取得不可 → `StorageLockedError` /
        取得可かつ `heartbeat_at` が新しい → 短時間リトライ後に中止 /
        取得可かつ `heartbeat_at` が古い・読めない → **前回異常終了として回収して続行**
  - [x] コンテキストマネージャとして使え、`release()` で `.lock` を確実に解放・削除できるようにする

---

### **3.3 グループC: DB基盤（*グループB完了後*）**

#### **C-1. `infrastructure/database/connection.py`**

- [x] `connect(db_path: Path, *, readonly: bool = False, network_drive: bool = False) -> sqlite3.Connection` を実装する
  - [x] 開発計画書3.6のPRAGMAをすべて適用する（`journal_mode=WAL` / `synchronous=NORMAL` / `foreign_keys=ON` / `busy_timeout=10000` / `temp_store=MEMORY` / `cache_size=-64000`）
  - [x] `network_drive=True` のとき `journal_mode` を **`DELETE` へフォールバック**する
  - [x] `readonly=True` のときSQLite URIの `mode=ro` と `PRAGMA query_only=ON` を使い、`journal_mode` の変更・マイグレーション・チェックポイントを実行しない（DBファイルを新規作成しない）
  - [x] `foreign_keys=ON` は**接続ごとに必須**である旨をコメントで明記する
- [x] `class ConnectionManager` を実装する
  - [x] `threading.local()` でスレッドローカル接続を保持する
  - [x] 生成した接続の所有スレッドを追跡し、各所有スレッドが `close_current_thread()` を呼ぶ協調停止プロトコルを実装する
  - [x] `request_close_all()` で新規接続を停止し、ワーカーの終了とjoin後に `assert_all_closed()` で漏れを検出する。別スレッドの接続を直接 `close()` してはならない
- [x] すべての `sqlite3.Error` を `detach.classify_sqlite_error` 経由で送出する
- [x] `checkpoint_truncate(conn)` を実装する（書き込み接続のみで `PRAGMA wal_checkpoint(TRUNCATE)`。定期実行は Phase 1、終了時実行は Phase 4）
- [x] モジュール docstring に「**接続をスレッド間で共有しない / `check_same_thread=False` を使わない / 書き込みは単一ライターに集約する**」方針を明記する

#### **C-2. `infrastructure/database/migrator.py`**

- [x] `migrations/` 内の `NNN_*.sql` を `importlib.resources.files()` で列挙し、番号昇順にソートする
- [x] `current_version(conn) -> int` を実装する（`PRAGMA user_version`）
- [x] `migrate(conn, db_path) -> int` を実装する
  - [x] `user_version` が最新マイグレーション番号より大きい場合 `SchemaVersionTooNewError` を送出する
  - [x] 未適用があり、かつ既存DBが非空の場合、`Connection.backup()` で `metadata.db.bak.{current_version}` を作成する。`user_version=0` の非空v0 DBも対象とし、新規作成直後の空DBは対象外とする
  - [x] バックアップ先を黙って上書きせず、同名がある場合はUTC時刻または連番で退避する。バックアップ後に `PRAGMA integrity_check` を実行して成功を確認する
  - [x] マイグレーション中はトランザクション開始前に `PRAGMA foreign_keys=OFF` とする
  - [x] 各SQLを1ファイル1トランザクションで適用する。`BEGIN IMMEDIATE;`、SQL本文、`PRAGMA user_version = N;`、`COMMIT;` を1つのスクリプトとして `executescript()` に渡し、失敗時は `ROLLBACK` する
  - [x] SQLファイル内の `BEGIN` / `COMMIT` / `ROLLBACK` / `PRAGMA user_version` を禁止し、採番とトランザクション境界をmigratorへ一元化する
  - [x] 成功・失敗を問わず `PRAGMA foreign_keys=ON` へ戻し、完了後に `PRAGMA foreign_key_check` を実行する。違反行があれば `MigrationError` とする
- [x] **`user_version` はSQLファイルに書かず、ファイル名の番号から migrator が設定する**（採番の一元管理）
- [x] Phase 5 で `messages.folder_id` を `message_folders` 中間テーブルへ移行する想定をコメントで残す（開発計画書3.6）

#### **C-3. `migrations/001_init.sql`**

DDLは開発計画書の記述をそのまま使用する。

- [x] `accounts` を作成する（3.1）
- [x] `folders` を作成する（3.2）
- [x] `messages` を作成する（3.3）
  - [x] `source_item_key` 列を**初期から**含める
  - [x] スレッド情報（`in_reply_to` / `references_ids` / `thread_key`）を**初期から**含める
  - [x] IMAPフラグ（`imap_flags` / `flags_seen_at`）を**初期から**含める
  - [x] `idx_msg_key` / `idx_msg_list` / `idx_msg_thread` / `idx_msg_trash` を作成する
  - [x] `uq_imap_message`（`uid IS NOT NULL`）と `uq_archive_message`（`uid IS NULL`）の部分一意インデックスを作成する
- [x] `message_contents` と `messages_fts`（`tokenize='trigram'`、external content）を作成する（3.4）
- [x] FTS同期トリガー `mc_ai` / `mc_ad` / `mc_au` の3本を作成する（3.4）
- [x] `sync_failures` と `audit_log` を作成する（3.5）
- [x] `app_state(key TEXT PRIMARY KEY, value TEXT)` を作成する（`clean_shutdown` フラグ用：5.7.1-4-4）
- [x] **CHECK制約を置かない**ことを確認する（`remote_state='no_remote'` 等はアプリ側で検証：開発計画書3.6）
- [x] `pst_imports` / `pst_import_items` を**含めない**ことを確認する（002 / Phase 4.5）

#### **C-4. `__main__.py` — コンポジションルート**

- [x] `argparse` で以下を実装する（D-10）
  - [x] 共通オプション: `--storage-root` / `--debug` / `--version`
  - [x] サブコマンド `migrate`: マイグレーションのみ実行して終了
  - [x] サブコマンド `verify`: 接続確認と `PRAGMA quick_check` / `foreign_key_check` を実行（Phase 0 の範囲。フル検証は Phase 4）
  - [x] サブコマンド省略時: 起動シーケンスを通して正常終了（GUI起動は Phase 3 で差し替え）
- [x] 起動シーケンスを実装する
  1. [x] `config.load()`
  2. [x] `setup_logging(config_dir, debug=...)`
  3. [x] `--storage-root` 指定時はプローブ後、マーカーがない場合だけ `initialize_root()` を明示的に実行し、再読込したUUIDを期待値と照合する。候補探索時は `resolve_root()` の結果型を利用する
  4. [x] `FOREIGN` の場合、**対象ルートへ一切書き込みを行わずに** `StorageForeignRootError` で中止する
  5. [x] `StorageLock` の取得
  6. [x] `ensure_layout(root)` と `check_free_space(root)`
  7. [x] `set_storage_log_target(root / "logs")`
  8. [x] `connect(root / "metadata.db", network_drive=...)` → `migrate()`
  9. [x] 起動成功後に `storage_root_uuid` と正規化した候補パスを設定へ原子的に保存する
  10. [x] 終了処理（新規接続停止 → ワーカーへキャンセル通知 → 各所有スレッドで接続close → join → `assert_all_closed()` → ストレージログ解除 → ロック解放）
- [x] **DI組み立てをこのファイルに集約**し、他モジュールでオブジェクト生成をしないことを確認する（開発計画書2.2）
- [x] 例外を捕捉して終了コードを分ける（正常:0 / 設定・ルート異常:2 / ロック競合:3 / DB異常:4）
- [x] `verify` は書き込みを伴うマイグレーションを行わず、読み取り専用接続で `quick_check` / `foreign_key_check` を実行する。`migrate` は書き込み接続を使用する

---

### **3.4 グループD: テスト基盤とCI（*グループC完了後。D-1〜D-3は並行可*）**

#### **D-1. pytest 基盤**

- [ ] `tests/conftest.py` に共通fixtureを実装する
  - [ ] `tmp_storage_root`: `tmp_path` 配下にマーカー付きルートを作成する
  - [ ] `db_conn`: **一時ディレクトリ上の実ファイルSQLite**を返す（FTSトリガーとWALの検証に必須のため `:memory:` は使わない）
  - [ ] `docker` マーカーの自動skip: 環境変数 `MAILDOCK_DOCKER=1` が無ければ skip する
- [ ] 単体テスト（`tests/unit/`）を作成する
  - [ ] `test_config.py`: 往復・未知キー保持・原子的書き込み（書き込み途中で落ちても旧ファイルが壊れない）・`ConfigVersionTooNewError`
  - [ ] `test_config.py`: JSON構文・型・範囲・列挙値の検証、`heartbeat_interval_sec=0` の拒否
  - [ ] `test_logging.py`: メールアドレス／パスワードのマスキング、`mask_subject` の20文字打ち切り、`set_storage_log_target(None)` でハンドラが確実に外れること
  - [ ] `test_detach.py`: 各 winerror / POSIX errno / SQLite errorname が `StorageDetachedError` へ分類されること、無関係な `OSError` が素通しされること
  - [ ] `test_storage_root.py`: `OK` / `MISSING` / `FOREIGN` の3値、候補リストによるドライブレター追従、`ensure_layout`、空き容量閾値
  - [ ] `test_lock.py`: 2重取得で `StorageLockedError`、stale heartbeat の回収、正常解放後のファイル削除、`.lock` が残存していてもOSロック取得可なら起動できること
  - [ ] `test_connection.py`: readonly接続がDBを新規作成せず、journal modeを変更せず、ワーカースレッドが自身の接続を閉じた後に `assert_all_closed()` が成功すること
- [ ] 結合テスト（`tests/integration/`）を作成する
  - [ ] `test_migrator.py`: 空v0 DB→`user_version=1`、非空v0 DB→`.bak.0` 生成、`PRAGMA integrity_check`、冪等な再実行、テスト用v2適用前のバックアップ、version too new の拒否
  - [ ] `test_migrator.py`: 途中失敗時にDDLと `user_version` が更新されず、`foreign_keys` がONへ復帰すること
  - [ ] `test_fts_triggers.py`: `message_contents` への insert→FTS検索ヒット→update→delete→ヒットしないこと（3.4の3トリガー検証）
- [ ] `tests/fixtures/eml/` と `tests/support/` に **README のみ**を置く（EMLコーパスとimaplibモックの作り込みは Phase 1。`BaseMailFetcher` の設計に引きずられないため）

#### **D-2. Dockerテスト環境（WSL/Linux 専用）**

- [ ] `tests/docker/compose.yaml` を作成する（GreenMail standalone、IMAP 3143 / IMAPS 3993、`GREENMAIL_OPTS` でテストユーザーを作成）。healthcheckでIMAPポートの受付を確認する
- [ ] `tests/integration/test_imap_smoke.py` を作成する（`@pytest.mark.docker`）
  - [ ] `imaplib.IMAP4_SSL` で、総待機時間に上限を設けた接続リトライ後にLOGINできること
  - [ ] `LIST` が応答すること
  - [ ] テスト用自己署名証明書を使う場合は専用SSLコンテキストで検証し、本番コードの証明書検証無効化設定と共有しないこと
  - [ ] ※ UIDVALIDITY変化・フォルダ移動・接続切断の検証は Phase 1
- [ ] `README.md` にWSL手順を記載する（`docker compose -f tests/docker/compose.yaml up -d` → `MAILDOCK_DOCKER=1 uv run pytest -m docker`）
- [ ] Windowsでの既定実行が `uv run pytest -m "not docker"` であることをREADMEに明記する
- [ ] Dovecot の追加は Phase 1 冒頭で検討する旨をコメントに残す（SPECIAL-USE / UIDVALIDITY操作 / 同時接続数制限の再現用）

#### **D-3. `.github/workflows/ci.yml`**

- [ ] 共通ステップを定義する（`astral-sh/setup-uv`（キャッシュ有効）→ `uv sync --frozen`）
- [ ] ジョブ `lint`（windows-latest）: `uv run ruff format --check .` / `uv run ruff check .` / `uv run mypy`
- [ ] ジョブ `test-windows`（windows-latest）: `uv run pytest -m "not docker" --cov=mail_dock`
- [ ] ジョブ `test-linux`（ubuntu-latest）: GreenMail をhealthcheck付きの `services:` で起動し `MAILDOCK_DOCKER=1 uv run pytest -m docker`
- [ ] `concurrency` を設定して同一ブランチの重複実行をキャンセルする
- [ ] トリガーを `push`（main）と `pull_request` に設定する
- [ ] ※ Phase 0 時点では PySide6 依存のテストが無いため、Linux側の Xvfb 設定は不要

---

## **4. 主要成果物**

| パス | 内容 | タスク |
| :---- | :---- | :---- |
| `pyproject.toml` / `uv.lock` | 依存・ツール設定・エントリポイント | A-1 |
| `LICENSE` / `THIRD-PARTY-LICENSES.md` / `README.md` | GPL-3.0、前提条件、開発手順 | A-3 |
| `src/mail_dock/__main__.py` | コンポジションルート・起動シーケンス・CLI | C-4 |
| `src/mail_dock/config.py` | 設定の読み書き | B-3 |
| `src/mail_dock/domain/errors.py` | ドメイン例外階層 | B-1 |
| `src/mail_dock/infrastructure/logging_config.py` | ロガー・マスキング | B-2 |
| `src/mail_dock/infrastructure/storage/detach.py` | I/O例外の分類 | B-4 |
| `src/mail_dock/infrastructure/storage/storage_root.py` | ルート同定・レイアウト・空き容量・ロック | B-5 |
| `src/mail_dock/infrastructure/database/connection.py` | PRAGMA・スレッドローカル接続 | C-1 |
| `src/mail_dock/infrastructure/database/migrator.py` | `user_version` マイグレーション | C-2 |
| `src/mail_dock/migrations/001_init.sql` | 3章スキーマ（PST以外） | C-3 |
| `tests/conftest.py` ほか | テスト基盤・単体/結合テスト | D-1 |
| `tests/docker/compose.yaml` | GreenMail | D-2 |
| `.github/workflows/ci.yml` | CI | D-3 |

---

## **5. スコープ境界**

### **5.1 含むもの**

セクション3のグループA〜D。ヘッドレスで「設定 → ログ → ルート解決 → ロック → DBマイグレーション」が通る基盤一式。

### **5.2 含まないもの（明示的に除外）**

| 除外項目 | 実施フェーズ |
| :---- | :---- |
| `BaseMailFetcher` / `BaseMessageRepository` / EML解析 / EMLの原子的保存 | Phase 1 |
| 永続マニフェスト（`manifests/`）の**書き込み実装**。Phase 0 はディレクトリ作成のみ | Phase 1 |
| FTS5+trigram の性能PoC・検索ロジック・正規化 | Phase 2 |
| PySide6 の画面 / ViewModel / QThreadワーカー / HTMLサンドボックス | Phase 3 |
| 切断の状態機械・`WM_DEVICECHANGE` 監視・ハートビートタイマー駆動・範囲限定検証・フォールト注入試験 | Phase 4 |
| サーバー削除の安全装置・ゴミ箱・purge・整合性チェック | Phase 4 |
| PST関連一式・`vendor/readpst`・`002_pst_import.sql` | Phase 4.5 |
| PyInstaller / Inno Setup / リリースCIのGPL遵守チェック | リリース時（5.9） |

---

## **6. 検証**

各項目の完了を確認したうえで、対応するタスクのチェックボックスを埋めること。

- [ ] V-1. `uv sync` → `uv run ruff format --check .` → `uv run ruff check .` → `uv run mypy` がすべて成功する
- [ ] V-2. `uv run pytest -m "not docker"` が Windows で全緑になる（基盤モジュールのカバレッジ80%目安）
- [ ] V-3. `uv run mail-dock --storage-root <一時ディレクトリ>` を実行し、`.maildock_root` / `eml,manifests,tmp,logs` が生成され、設定へUUIDと候補パスが保存され、`metadata.db` の `user_version` が 1 になる。正常終了後に `.lock` / `.lock.meta.json` が残っていないことも確認する
- [ ] V-4. 生成された `metadata.db` に対し `PRAGMA integrity_check` と `PRAGMA foreign_key_check` が OK を返し、`PRAGMA journal_mode` が `wal` である
- [ ] V-5. FTSトリガー往復テスト: `message_contents` に日本語本文を挿入し、`messages_fts MATCH` で3文字語がヒットし、削除後にヒットしなくなる
- [ ] V-6. 多重起動検証: ロック取得後にイベント待機するテストヘルパーを起動し、その間に2つ目を起動して `StorageLockedError` 相当で停止する
- [ ] V-7. スタールロック検証: ロック保持プロセスを強制終了して古い `.lock.meta.json` を残し、`heartbeat_at` を過去日時に書き換えると、次回起動でOSロック取得可否を確認したうえでロックが回収され、正常に起動する
- [ ] V-8. `FOREIGN` 検証: `.maildock_root` の `root_uuid` を書き換えると起動が拒否され、**その際にルートへ一切書き込みが発生していない**
- [ ] V-9. 再実行検証: 2回目以降の起動でマイグレーションが冪等にスキップされる
- [ ] V-10. WSL: `docker compose -f tests/docker/compose.yaml up -d` → `MAILDOCK_DOCKER=1 uv run pytest -m docker` で GreenMail への LOGIN / LIST が成功する
- [ ] V-11. CI: プルリクエストを作成し、`lint` / `test-windows` / `test-linux` の3ジョブがすべて成功する

---

## **7. Phase 1 への引き継ぎ事項**

* Dovecot コンテナの追加要否（SPECIAL-USE / UIDVALIDITY操作 / 同時接続数制限の再現）を Phase 1 冒頭で判断する。
* `tests/fixtures/eml/` のコーパス（壊れたMIME、ISO-2022-JP / CP932 / EUC-JP、RFC2231分割ファイル名、Outlook非標準形式、Message-ID欠損、巨大添付、インライン画像）を Phase 1 で蓄積する。
* `manifests/` への追記実装（JSONL + 行末CRC32、fsync、末尾torn行の切り離し）を Phase 1 で行う。
* `AppConfig.db_backup_to_local_disk` の実処理（C:のBitLocker有効時のみのオプトイン）を Phase 4 で実装する。
* `set_storage_log_target(None)` / `ConnectionManager.request_close_all()` / `ConnectionManager.assert_all_closed()` / `checkpoint_truncate()` は、Phase 4 の「安全な取り外し」および `DETACHED` 遷移から呼び出す。
