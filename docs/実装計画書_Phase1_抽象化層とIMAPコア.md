# **Phase 1: 抽象化層 & IMAPコア 実装計画書**

対象: [ローカルメールバックアップ＆閲覧アプリ 開発計画書.md](./ローカルメールバックアップand閲覧アプリ開発計画書.md) の「6. 開発ロードマップ」における **Phase 1: 抽象化層 & IMAPコア**

前提: [Phase 0: 基盤整備 実装計画書](./実装計画書_Phase0_基盤整備.md) の成果物（設定・ロギング・ストレージルート・DB接続・マイグレーション・`001_init.sql`）が完成していること。

本書と開発計画書に矛盾がある場合は、**開発計画書を正**とする。

---

## **1. 目的**

**ヘッドレス（UIを持たない）状態で、IMAPサーバーからメールを取得し、設計不変条件を守って永続化できる中核**を完成させる。

具体的には、以下を満たす。

1. **プロトコル抽象化の確定** — `BaseMailFetcher` と例外階層により、上位層が `imaplib` / `ssl` / `socket` の存在を一切知らない状態にする。Phase 5 の Gmail/OAuth2 実装時に、この境界を変更せずに済むこと。
2. **設計不変条件2（書き込み順序）を、コード上の唯一の経路として実装すること** — `tmp/` へ書き込み+fsync → `os.replace` → 永続マニフェスト追記+fsync → `BEGIN IMMEDIATE` でDBコミット。EML保存はこの1関数以外から行えないようにする。
3. **後から入れると全件再取得・全件再解析が必要になる要素を、最初から確定させること** — 文字コードのフォールバック順序、RFC 2231分割ファイル名、スレッドヘッダ（`in_reply_to` / `references_ids` / `thread_key`）、検索用正規化テキスト、`content_key`。
4. **100GB規模の初回同期に耐える運用特性を備えること** — 最新メールを先に使える二カーソル方式（新着の高水位 `last_seen_uid`＋履歴の下向きカーソル `backfill_next_uid`）、バッチコミット、キャンセル、1通の失敗で全体を止めない失敗記録と自動再試行。
5. **実機（お名前.com）で仮定を検証すること** — フォルダ区切り文字、modified UTF-7、同時接続数制限、タイムアウト挙動。

**Phase 1 のゴール判定:** `mail-dock account add` → `mail-dock folders --enable` → `mail-dock sync` の一連がお名前.com 実アカウントの小規模フォルダで完走し、中断・再開・削除検知が動作し、6章の検証項目がすべて成功し、CIが緑になること。

---

## **2. 要件**

### **2.1 前提となる意思決定（確定済み）**

| # | 項目 | 決定内容 |
| :--- | :---- | :---- |
| D-1 | アカウント登録の導線 | **CLIサブコマンド `account add`**。`getpass` で対話入力し、資格情報は **`keyring` のみ**に保管、接続情報のみ `accounts` テーブルへ登録する。Phase 3 のGUIは同じユースケースを再利用する |
| D-2 | Phase 1 のCLI範囲 | `account add` / `account list` / `folders`（一覧・同期対象のトグル）/ `sync` / `reparse` を追加する。Phase 0 の `migrate` / `verify` は維持 |
| D-3 | 永続マニフェスト | **書き込み（JSONL + 行末CRC32 + fsync）＋読み取り＋末尾torn行の切り離し**まで実装する。**EML+マニフェストからのDB完全再構築は Phase 4** |
| D-4 | マニフェストのファイル分割 | **月次ローテーション** `manifests/imap/{account_id}/events-{YYYYMM}.jsonl`。復旧時の検証範囲を限定するため |
| D-5 | `delete_remote_message()` | **IMAP実装（trash MOVE / expunge）まで書く**。ただし usecase / CLI からの呼び出し導線は作らず、**GreenMail/Dovecot 結合テストからのみ検証**する。ドライラン・件数手入力確認・監査ログ・レート制限は Phase 4 |
| D-6 | EMLの重複排除 | 同一アカウント内に**完全な SHA-256 が一致する**EMLが既に存在する場合、既存実体を再検証して `relative_path` を共有し、`messages` 行のみ追加する。ファイル名の先頭32桁だけで同一性を判定しない。アカウントをまたぐ共有は行わない |
| D-7 | 前倒しする付随機能 | `normalize_for_search()` / 添付ファイル名サニタイズ / HTML→テキスト抽出 / `sync_failures` 記録と自動再試行 / 削除・移動検知 は **すべて Phase 1 に含める** |
| D-8 | テスト用IMAPサーバー | **Dovecot を追加**（SPECIAL-USE `\Trash`、UIDVALIDITY強制変更、modified UTF-7 の日本語フォルダ名の再現）。GreenMail は既存のまま併用 |
| D-9 | 実機検証 | **お名前.com 実アカウントの小規模フォルダで実施**し、Phase 1 の完了条件とする |
| D-10 | `iter_message_refs()` のチャンク | **UID 500件ずつ** メタ情報を FETCH し、本文は1通ずつ順次ダウンロードする（開発計画書 4.2 の帯域方針） |
| D-11 | 実行モデル | **単一プロセス・単一スレッド**で完結させる。`CancelToken` は将来のQThreadワーカーのために定義するが、Phase 1 では SIGINT ハンドラから駆動する |
| D-12 | スキーマ変更 | **`001_init.sql` は変更しない**。Phase 1 の二カーソル同期とUIDVALIDITY別失敗管理に必要な変更だけを `002_sync_cursor.sql` として追加する。それ以外の過不足は本書に記録し、Phase 2 以降のマイグレーションで扱う |
| D-13 | 初回同期カーソル | `folders.last_seen_uid` は完了済み新着範囲の高水位、`backfill_next_uid` は次に取得する履歴UIDの上限とする。初回／UIDVALIDITY変更時にサーバー最大UIDを両方へ設定する。新着・履歴とも最新優先で降順取得し、新着高水位は開始時に固定した範囲をすべて処理した後だけ進める |
| D-14 | レイヤー境界 | usecase は `domain` のポートだけに依存する。keyring、SQLite、EMLファイルI/O、マニフェストI/Oの具象実装は `infrastructure` に置き、`__main__.py` で注入する |

### **2.2 機能要件**

| # | 要件 | 根拠（開発計画書） |
| :--- | :---- | :---- |
| F-1 | `BaseMailFetcher`（ABC）と `RemoteFolder` / `RemoteMessageRef` / `CancelToken` が `domain` 層に定義され、外部依存がゼロであること | 4.1 / 2.2 |
| F-2 | `FetchError` 配下に `AuthenticationError` / `TransientError` / `PermanentError` / `UidValidityChanged` / `OversizeError` が定義され、`infrastructure` 層が `imaplib.IMAP4.error` / `ssl.SSLError` / `socket.timeout` をこれらへラップし、上位層へ生の例外を漏らさないこと | 4.1 / 5.7.1-2a |
| F-3 | `OnamaeImapFetcher` が IMAP over SSL (993) で接続し、modified UTF-7 のフォルダ名をデコードし、SPECIAL-USE 属性と区切り文字を取得できること。**1アカウント＝1接続**を守ること | 4.1 / 4.2 |
| F-4 | `iter_message_refs()` がUID範囲と昇降順を明示して遅延生成できること。新着は同期開始時の最大UIDから `last_seen_uid + 1` まで、履歴は `backfill_next_uid` 以下をUID降順で取得し、`CancelToken` で中断できること | 4.1 / 4.2 |
| F-5 | 文字コードのフォールバックが `宣言charsetの正規化 → iso-2022-jp → cp932 → euc_jp → utf-8 → charset-normalizer → errors="replace"` の順で行われ、**最終手段でも例外を投げず警告ログを残す**こと | 4.7 |
| F-6 | 添付ファイル名が **RFC 2231分割形式**（`filename*0*=`）と **Outlook非標準形式**（`filename=` に RFC2047）の双方で取得できること。標準ライブラリの `get_filename()` に依存しないこと | 4.7 |
| F-7 | 本文抽出が `text/plain` > `text/html`（タグ除去）の優先順で行われ、`multipart/alternative` は plain 優先、`multipart/related` は本体を辿り、`Content-ID` 付きインライン画像を添付名リストから除外すること | 4.7 |
| F-8 | `Date` ヘッダの解析が try/except で保護され、失敗時・未来日時（現在+1日超）時は `INTERNALDATE` へフォールバックし、すべての日時が UTC ISO8601（`YYYY-MM-DDTHH:MM:SSZ`）でDBに保存されること | 4.7 |
| F-9 | 解析に失敗しても **EMLは必ず保存**され、`message_contents` を空で登録し、`sync_failures` に `'parse'` として記録され、`reparse` で復旧できること | 4.7 / 5.6 |
| F-10 | `in_reply_to` / `references_ids` / `thread_key` が同期時に算出され、`messages` へ保存されること | 3.2 |
| F-11 | EMLのファイル名が `sha256(eml_bytes)` の先頭32桁であり、保存先の年月ディレクトリが **`INTERNALDATE`**（不明時は `unknown/`）で決まること | 2.4 |
| F-12 | EMLの保存が `tmp/`（**ストレージルート配下**）への書き込み+fsync → `os.replace` の順で行われ、この経路以外からEMLが配置されないこと | 1.3 / 2.4-9 / 4.7 |
| F-13 | 永続マニフェストが `manifests/imap/{account_id}/events-{YYYYMM}.jsonl` へ append-only で追記され、各行末に CRC32 が付与され、バッチ境界で fsync されること。読み取り時に末尾のtorn行を切り離せること | 2.4 / 4.8 |
| F-14 | **100通または50MB**を1バッチとし、マニフェストのfsync後、メッセージ・失敗状態・二カーソルを同じ `BEGIN IMMEDIATE` トランザクションでコミットすること。10バッチごとにWALをチェックポイントし、1通ごとのコミットを行わないこと | 4.2 / 3.6 |
| F-15 | `UIDVALIDITY` の変化を検出したら二カーソルを新世代向けに初期化して当該フォルダを再同期すること。旧世代のメッセージと失敗履歴は保持するが再試行対象から外し、同一アカウント内の完全な `file_hash` 一致時だけ既存EMLを再利用すること | 4.2 / D-6 |
| F-16 | 同期完了後にサーバーUID集合と**現在のUIDVALIDITY世代**のローカルUID集合を比較すること。消失UIDは、他フォルダの `present` な候補のうち `content_key` と完全な `file_hash` が一致する候補が一意な場合だけ `'moved'` とし、候補なしは `'deleted'`、複数候補またはハッシュ不明は `'unknown'` とすること。**いずれの場合もEMLを削除しないこと** | 4.2 |
| F-17 | `TransientError` に対して指数バックオフ（1s → 2s → 4s、最大3回、jitter付き）でリトライし、**リトライがユースケース層にのみ存在する**こと | 5.6 |
| F-18 | エラー種別ごとにUIDVALIDITYを含めて `sync_failures` へ記録され（`transient` / `permanent` / `auth` / `oversize` / `parse`）、現在世代の失敗だけが次回同期時に自動再試行され、`attempt_count` が加算されること | 5.6 |
| F-19 | 1通あたり `AppConfig.max_message_bytes`（既定50MB）を超えるメールは本文をダウンロードせず、ヘッダだけを取得して `relative_path` / `file_hash` がNULLの `messages` 行を登録し、`oversize` として記録すること | 4.2 |
| F-20 | 進捗が **転送バイト数を主指標**として通知され（副指標: 通数・推定残り時間）、キャンセルが**バッチコミット境界で**成立すること | 4.2 |
| F-21 | `message_contents` へ投入するテキストが `normalize_for_search()`（NFKC → casefold → 連続空白圧縮）を通っていること。**投入時と検索時で同一関数**を使うこと | 4.5 |
| F-22 | 添付ファイル名のサニタイズ（パス成分除去・NTFS禁止文字・末尾ドット/空白・Windows予約名・NFC正規化・長さ制限・実行可能拡張子の警告・保存直前の `resolve()` 再検証）が純粋関数として実装され、単体テストされていること | 4.6-4 |
| F-23 | 資格情報が `keyring` のみに保管され、DB・設定ファイル・ログに出力されないこと | 5.3 / 5.5 |
| F-24 | 新規フォルダを検出した場合 `is_sync_target=0` で登録し、**自動で同期を開始しない**こと | 4.2 |

### **2.3 非機能要件・制約**

* Phase 1 のコードは **PySide6 に依存しない**（`presentation` 層を作らない）。
* `mypy strict` を通すこと。`# type: ignore` を使う場合は理由をコメントで併記する。
* レイヤーの依存方向を厳守する（`domain` ← `usecases` ← `presentation`、`infrastructure` は `domain` にのみ依存）。`domain` に `imaplib` / `sqlite3` / `bs4` を import しない。
* `BaseMessageRepository` は「**ユースケースの単体テストをインメモリ実装へ差し替えるため**」だけに存在する。目的を超えたメソッドを足さない。
* usecase は `BaseMessageRepository` / `BaseCredentialStore` / `BaseEmlStorage` / `BaseManifestWriter` のみに依存し、`keyring` や infrastructure の具象クラスを import しない。
* EMLの生バイト列をログに出力しない。件名は先頭20文字、メールアドレスはマスク（Phase 0 の `MaskingFilter` を利用）。
* 1通の解析・保存失敗で同期全体を停止させない。

---

## **3. タスク**

> 依存関係: **A → B → (C, D, E) → F → G**。H（テスト）は各グループと並行して作成する。

### **3.1 グループA: 前提整備**

> *A-1〜A-3 は相互に並行可。*

#### **A-1. Dovecot コンテナの追加（*Phase 0 引き継ぎ事項の決着*）**

- [x] `tests/docker/compose.yaml` に `dovecot` サービスを追加する（IMAP: 3144 / IMAPS: 3994。GreenMail のポートと衝突させない）
- [x] `tests/docker/dovecot/` に設定を配置し、以下を再現できるようにする
  - [x] SPECIAL-USE 属性（`\Trash` / `\Sent` / `\Drafts`）を返すフォルダ
  - [x] modified UTF-7 でエンコードされる日本語フォルダ名（例: `受信トレイ/請求書`）
  - [x] 階層区切り文字が `.` のケース（お名前.com 想定。GreenMail の `/` と対比する）
- [x] テストから UIDVALIDITY を強制変更する手順を `tests/support/` のヘルパとして用意する（メールボックスの削除→再作成、または dovecot の uidvalidity ファイル操作）
- [x] `README.md` の WSL Dockerテスト手順に Dovecot を追記する
- [x] `docker compose -f tests/docker/compose.yaml up -d` で GreenMail と Dovecot が同時に healthy になることを確認する

#### **A-2. EMLコーパスとテスト支援（*Phase 0 引き継ぎ事項*）**

- [x] `tests/fixtures/eml/` に以下のケースを配置し、`README.md` に各ファイルの意図と期待値を記載する
  - [ ] 文字コード: ISO-2022-JP / CP932（機種依存文字を含む）/ EUC-JP / UTF-8
  - [ ] charset ラベル誤り（`shift_jis` 宣言だが実体は CP932、`x-sjis`、`iso-2022-jp-ms`）
  - [ ] charset 宣言なし（`charset-normalizer` 推定に落ちるケース）
  - [ ] 添付ファイル名: RFC 2231分割形式 / Outlook非標準（`filename=` にRFC2047）/ 日本語ファイル名 / パストラバーサル文字列 / Windows予約名 / 実行可能拡張子
  - [ ] 構造: `multipart/alternative`（plain+html）/ `multipart/related`（`cid:` インライン画像）/ ネストした multipart / 添付のみで本文なし
  - [ ] `Message-ID` 欠損 / 重複 `Message-ID`
  - [ ] `Date` 不正フォーマット / `Date` 欠損 / 未来日時（現在+10年）
  - [ ] 巨大添付（サイズ上限判定用。**リポジトリには入れず生成スクリプトで作る**）
  - [ ] 壊れたMIME（境界文字列不一致、途中で切れたBase64）
- [x] `tests/support/fake_fetcher.py` に `FakeFetcher(BaseMailFetcher)` を実装する（フォルダ・UID・INTERNALDATE・サイズ・生バイト列を任意に構成でき、指定UIDで `TransientError` / `PermanentError` を送出できる）
- [x] `tests/support/in_memory_repository.py` に `InMemoryMessageRepository(BaseMessageRepository)` を実装する
- [x] `tests/support/eml_builder.py` に、コーパス生成とアドホックなEML組み立てのヘルパを実装する

#### **A-3. 本計画書の運用（*A-1と並行可*）**

- [x] 本書のチェックボックスを、各タスクの完了確認後に埋める運用を守る
- [x] `002_sync_cursor.sql` で明示した変更以外に `001_init.sql` の過不足が判明した場合、既存マイグレーションは変更せず本書の「7. Phase 2 への引き継ぎ事項」へ記録する（D-12）

---

### **3.2 グループB: domain 層（*A に非依存で着手可*）**

> *B-1 → B-2 / B-3 → B-4 / B-5 の順。B-2 と B-3、B-4 と B-5 はそれぞれ並行可。*

#### **B-1. `domain/errors.py` — 例外階層の葉を追加**

- [x] `FetchError` 配下に以下を追加する（Phase 0 でコメントを残した箇所）

```Plaintext
FetchError
├─ AuthenticationError   # 再試行不可。資格情報の再入力を促す
├─ TransientError        # 再試行可（ネットワーク断・一時的なNO応答）
├─ PermanentError        # 再試行不可（フォルダ不存在・権限不足）
├─ UidValidityChanged    # 当該フォルダの再同期をトリガーする制御用例外
└─ OversizeError         # 1通あたりのサイズ上限超過
```

- [x] `StorageError` 配下に `ManifestCorruptError` を追加する（末尾torn行以外の破損＝切り離しで回復できない場合）
- [x] `MailDockError` 配下に `CredentialStoreError` を追加する（keyringバックエンド不在・保存失敗。認証拒否とは区別する）
- [x] 各例外に用途を説明する docstring を付ける
- [x] `UidValidityChanged` に「これは異常ではなく制御フローである」旨を docstring で明記する
- [x] 外部依存がゼロであることを維持する

#### **B-2. `domain/fetcher.py` — プロトコル抽象化**

- [x] `RemoteFolder`（frozen dataclass）を定義する: `raw_name` / `display_name` / `uidvalidity: int | None` / `special_use: frozenset[str]` / `delimiter: str | None`
- [x] `RemoteMessageRef`（frozen dataclass）を定義する: `uid: int` / `message_id: str | None` / `internal_date: datetime | None` / `size_bytes: int | None` / `flags: tuple[str, ...]`
- [x] `CancelToken` を定義する（`threading.Event` をラップし `raise_if_cancelled()` で `OperationCancelledError` を送出、`is_cancelled` プロパティを持つ）
- [x] `BaseMailFetcher(ABC)` を開発計画書4.1の署名どおりに定義する
  - [x] `connect()` / `disconnect()` / `__enter__` / `__exit__`
  - [x] `list_folders() -> list[RemoteFolder]`
  - [x] `select_folder(raw_name: str) -> int`（戻り値: UIDVALIDITY）
  - [x] `iter_message_refs(raw_name, *, min_uid=1, max_uid=None, descending=True, cancel) -> Iterator[RemoteMessageRef]`
  - [x] `get_max_uid(raw_name: str) -> int`（空フォルダは0。二カーソル初期化用）
  - [x] `list_existing_uids(raw_name: str) -> set[int]`
  - [x] `download_eml_bytes(raw_name: str, uid: int) -> bytes`
  - [x] `download_eml_headers(raw_name: str, uid: int) -> bytes`（oversizeのメタデータ登録用）
  - [x] `delete_remote_message(raw_name, uid, *, mode="trash") -> None`
- [x] クラス docstring に「**実装にリトライを書かない。リトライは usecases 層に集約する**」と明記する
- [x] `BaseArchiveImporter`（Phase 4.5）と統合しない理由をコメントで残す

#### **B-3. `domain/messages.py` — 解析結果のデータ構造**

- [x] `ParsedAttachment`（frozen dataclass）: `filename: str | None` / `content_type: str` / `size_bytes: int` / `is_inline: bool`
- [x] `ParsedMessage`（frozen dataclass）: `subject` / `sender` / `recipient` / `cc` / `date_sent: datetime | None` / `message_id: str | None` / `in_reply_to` / `references_ids` / `body_text` / `attachments: tuple[ParsedAttachment, ...]` / `has_attachment: bool` / `parse_error: str | None`
- [x] `StoredEml`（frozen dataclass）: `relative_path: str` / `file_hash: str` / `size_bytes: int` / `deduplicated: bool`
- [x] `content_key` / `thread_key` の算出結果を保持する箇所を明確にする（算出ロジックは `infrastructure/parsing` 側、値の保持は本モジュール）
- [x] 外部依存が標準ライブラリのみであることを維持する

#### **B-4. `domain/repository.py` — `BaseMessageRepository`（*B-3 に依存*）**

- [x] ABC として以下のみを定義する（**目的を超えたメソッドを足さない**）
  - [x] `upsert_account(...)` / `list_accounts()` / `upsert_folder(...)` / `list_folders(account_id)` / `list_sync_targets(account_id)` / `set_sync_target(...)`
  - [x] `initialize_sync_cursors(folder_id, uidvalidity, max_uid)` / `update_sync_cursors(folder_id, *, last_seen_uid=None, backfill_next_uid=None, initial_sync_completed=None)`
  - [x] `add_message(record, contents=None)` / `exists_source_item_key(account_id, folder_id, source_item_key)`。oversize時は `contents=None` とし `message_contents` 行を作らない
  - [x] `find_stored_eml(account_id, file_hash) -> StoredEml | None`
  - [x] `local_uids(account_id, folder_id, uidvalidity) -> set[int]`
  - [x] `find_move_candidates(account_id, content_key, file_hash, exclude_folder_id)`（`present` な候補をすべて返す）
  - [x] `update_remote_state(message_id, state, moved_to_folder_id=None)`
  - [x] `record_failure(account_id, folder_id, uidvalidity, uid, error_class, message)` / `pending_failures(account_id, folder_id, uidvalidity)` / `clear_failure(...)`
  - [x] `list_reparse_targets(account_id, only_failed)` / `update_message_contents(message_id, contents)`
  - [x] `begin_batch()` / `commit_batch()` / `checkpoint()`
- [x] モジュール docstring に「**この抽象はユースケースの単体テストをインメモリ実装へ差し替えるためだけに存在する**」と明記する

#### **B-5. `domain/ports.py` — 外部I/Oポート**

- [x] `BaseCredentialStore(ABC)` を定義する: `set_password(account_id, password)` / `get_password(account_id) -> str | None` / `delete_password(account_id)`
- [x] `BaseEmlStorage(ABC)` を定義する: `save(account_id, internal_date, raw) -> StoredEml` / `reuse(relative_path, expected_hash) -> StoredEml | None` / `read(relative_path) -> bytes`
- [x] `BaseManifestWriter(ABC)` を定義する: `append(event)` / `flush_and_sync()`
- [x] 型は標準ライブラリと `domain` のデータ構造だけで表現し、外部依存を持たせない
- [x] 削除APIはPhase 1のusecaseで不要なため `BaseEmlStorage` に追加しない

---

### **3.3 グループC: 解析（*B-3 完了後。C-1〜C-6 は相互に並行可*）**

#### **C-1. `infrastructure/parsing/charset.py`**

- [x] `normalize_charset_label(label: str) -> str` を実装する（`x-sjis` / `shift_jis` / `sjis` → `cp932`、`iso-2022-jp-ms` → `iso2022_jp_ext`、別名の小文字化・空白除去）
- [x] `decode_text(raw: bytes, declared: str | None) -> tuple[str, str]`（戻り値: テキストと採用エンコーディング）を実装する
- [x] フォールバック順序を厳守する: 宣言charset正規化 → `iso-2022-jp` → **`cp932`** → `euc_jp` → `utf-8` → `charset-normalizer` 推定 → `errors="replace"`
- [x] 最終手段に到達した場合は **例外を投げず** 警告ログを出す
- [x] `shift_jis` ではなく `cp932` を使う理由をコメントで1行残す

#### **C-2. `infrastructure/parsing/headers.py`**

- [x] `decode_header_value(value: str | None) -> str`（RFC 2047、`decode_header` + `make_header`、不正エンコード語は原文フォールバック）
- [x] `parse_content_disposition_filename(part) -> str | None` を実装する
  - [x] **RFC 2231 分割形式（`filename*0*=` / `filename*1*=` …）を自前でパースする**（標準ライブラリの `get_filename()` に依存しない）
  - [x] Outlook 非標準形式（`filename=` に RFC 2047 を直接埋める）に対応する
  - [x] `filename` が無ければ `name` パラメータへフォールバックする
- [x] `parse_date_header(value, internal_date) -> datetime | None` を実装する（try/except 必須、未来日時＝現在+1日超は `INTERNALDATE` へフォールバック、tz-aware UTC へ正規化）
- [x] `to_utc_iso8601(dt) -> str`（`YYYY-MM-DDTHH:MM:SSZ`）
- [x] `derive_thread_key(message_id, in_reply_to, references_ids) -> str | None`（`References` の先頭 → `In-Reply-To` → 自身の `Message-ID` の優先順）
- [x] `derive_content_key(message_id, eml_sha256) -> str`（`Message-ID` があればそれ、無ければ `sha256:{先頭32桁}`）

#### **C-3. `infrastructure/parsing/html_to_text.py`**

- [x] `html_to_text(html: str) -> str` を実装する（BeautifulSoup で `script` / `style` / コメントを除去 → `get_text("\n")` → 連続空白・空行を圧縮）
- [x] 巨大HTMLでも実用速度で動くことを確認する（数MBのHTMLで計測）
- [x] **表示用のHTMLサンドボックスは Phase 3。ここは検索・全文用のテキスト化のみ**である旨をコメントで残す

#### **C-4. `infrastructure/parsing/normalize.py`**

- [x] `normalize_for_search(text: str) -> str` を実装する（NFKC → `casefold()` → `\s+` を単一空白へ圧縮 → `strip()`）
- [x] ひらがな・カタカナの同一視を **行わない**
- [x] モジュール docstring に「**投入時と検索時で必ず本関数を使う。片方だけだと恒久的にヒットしなくなる**」と明記する（Phase 2 の検索実装もこれを使う）

#### **C-5. `infrastructure/parsing/eml_parser.py`**

- [x] `parse_eml(raw: bytes, internal_date: datetime | None) -> ParsedMessage` を実装する
- [x] ヘッダ（`Subject` / `From` / `To` / `Cc` / `Message-ID` / `In-Reply-To` / `References`）を C-2 経由で抽出する
- [x] 本文抽出: `text/plain` 優先 → 無ければ `text/html` を C-3 でテキスト化。`multipart/alternative` は plain 優先、`multipart/related` は本体を辿る
- [x] 添付一覧を抽出し、**`Content-ID` を持つインライン画像を添付名リストから除外**する（`is_inline=True` として保持はする）
- [x] `has_attachment` を判定する（インライン画像のみの場合は添付ありとしない）
- [x] **解析中の例外をすべて捕捉し、`parse_error` を設定した `ParsedMessage` を返す**（呼び出し側は例外を受け取らない）
- [x] コーパス全件が例外なく処理できることをテストで担保する

#### **C-6. `infrastructure/storage/filename.py`**

- [x] `sanitize_attachment_name(name: str) -> SanitizedName` を実装する（開発計画書 4.6-4 の8項目）
  - [x] パス成分（`../` / `\` / `/`）の除去と警告
  - [x] NTFS禁止文字 `< > : " / \ | ? *` を `_` へ置換（`:` を含めて代替データストリームを封じる）
  - [x] 末尾のドット・空白の除去
  - [x] Windows予約名（`CON` / `PRN` / `AUX` / `NUL` / `COM1-9` / `LPT1-9`）にサフィックスを付与
  - [x] NFC 正規化
  - [x] 長さ制限（UTF-8バイト長で判定し、拡張子を保ったまま切り詰め）
  - [x] 実行可能拡張子（`.exe .scr .js .vbs .lnk .bat .cmd .ps1`）に警告フラグを立てる
  - [x] `resolve_within(base: Path, name: str) -> Path`（`Path(dest).resolve()` が `base` 配下であることを確認する最終防御）
- [x] 空文字・全置換で空になる場合の代替名（`attachment`）を用意する
- [x] **実ファイル保存は Phase 3。ここは純粋関数のみ**である旨をコメントで残す

---

### **3.4 グループD: 保存基盤（*グループC完了後。D-1〜D-4 は相互に並行可*）**

#### **D-1. `infrastructure/storage/eml_storage.py` — 原子的保存（*設計不変条件2の中核*）**

- [x] `save_eml(root: Path, account_id: str, internal_date: datetime | None, raw: bytes) -> StoredEml` を実装する
  - [x] `root/tmp/{uuid4}.eml` へ書き込み → `flush()` → `os.fsync(fd)`
  - [x] `sha256(raw)` を計算し、先頭32桁をファイル名にする
  - [x] 保存先 `root/eml/{account_id}/{YYYY}/{MM}/{hash32}.eml`。`internal_date` が無ければ `{account_id}/unknown/`
  - [x] `file_hash` は完全なSHA-256を保持し、ファイル名だけを先頭32桁とする
  - [x] usecaseから同一アカウント内の既存候補が渡された場合、実体の完全なSHA-256を再検証し、一致すればtmpを書かず既存 `relative_path` を返す。不一致・実体欠損時は通常保存へ進む
  - [x] 計算した保存先が既に存在する場合も完全なSHA-256を検証し、一致時だけ `deduplicated=True` を返す
  - [x] `os.replace()` でアトミック配置し、POSIX では親ディレクトリを fsync する
  - [x] すべてのI/Oを `detach.storage_io()` で包み、`OSError` を `StorageDetachedError` 等へ分類する
- [x] `account_id` がWindowsで安全な文字列か登録時に検証する純粋関数を実装する。不正値を別文字列へ暗黙変換せず拒否し、ID衝突を防ぐ
- [x] `cleanup_tmp(root: Path) -> int` を実装する（起動時に `tmp/` の残骸を削除。`tmp/pstimp/` は対象外）
- [x] モジュール docstring に「**EMLの配置はこの関数以外から行わない。`tmp/` は必ずストレージルート配下（＝同一ボリューム）**」と明記する
- [x] `EmlStorage(BaseEmlStorage)` を実装し、`save()` は `save_eml()` へ委譲、`reuse()` は既存実体の完全ハッシュを再検証、`read()` は相対パスがルート配下であることを検証して読み込む

#### **D-2. `infrastructure/storage/manifest.py` — 永続マニフェスト**

- [x] `ManifestWriter` を実装する
  - [x] 出力先 `root/manifests/imap/{account_id}/events-{YYYYMM}.jsonl`（D-4 の月次ローテーション）
  - [x] 1行の形式は `{json}|CRC32:xxxxxxxx`（ペイロードのCRC32を16進8桁で行末に付与）
  - [x] `append(event: Mapping[str, JSONValue]) -> None` と `flush_and_sync() -> None` を分離し、**バッチ境界で fsync** する
  - [x] イベント種別: `fetch` / `fetch_skipped` / `parse_failed` / `delete_detected` / `moved` / `remote_state_unknown`（`purge_intent` / `purged` は Phase 4 で追加する旨をコメント）
  - [x] `fetch` イベントの必須項目: `event` / `account_id` / `folder_raw_name` / `uid` / `uidvalidity` / `source_item_key` / `message_id` / `relative_path` / `file_hash` / `size_bytes` / `timestamp`
  - [x] `fetch` イベントに `deduplicated` を記録し、物理共有時も取得元ごとに必ずイベントを追記する
  - [x] oversizeは `fetch_skipped` イベントとして、UID・UIDVALIDITY・サイズ・理由を記録する（EMLパスとハッシュは持たない）
- [x] `read_events(path: Path) -> Iterator[Mapping[...]]` を実装する
  - [x] CRC32 不一致・改行欠落・JSON不正の行を検出する
  - [x] **末尾の不正行のみ**を切り離す（`repair_tail(path) -> int` で truncate）
  - [x] 中間行が壊れている場合は `ManifestCorruptError` を送出する
- [x] append-only を守る（既存行の書き換え・削除を行うAPIを作らない）
- [x] モジュール docstring に「**EML＋本マニフェストが正本、`metadata.db` は派生キャッシュ**」と明記する
- [x] DB完全再構築（Phase 4）が本モジュールの読み取りAPIを使う前提であることをコメントで残す

#### **D-3. `infrastructure/database/message_repository.py`**

- [x] `SqliteMessageRepository(BaseMessageRepository)` を実装する
- [x] 書き込みトランザクションを `BEGIN IMMEDIATE` で開始する（`isolation_level=None` + 明示BEGIN）
- [x] `add_message()` が `messages` へINSERTし、contentsがある場合だけ `message_contents` へ `normalize_for_search()` 済みテキストをINSERTする。oversizeのヘッダ行ではcontentsを省略する
- [x] 同一 `(account_id, folder_id, uidvalidity, uid)` の再実行だけを `ON CONFLICT DO UPDATE` し、UIDVALIDITYが異なる旧世代の行は履歴として保持する
- [x] `local_uids()` はUIDVALIDITYを必須条件とし、`find_move_candidates()` は同一アカウントの別フォルダにある `remote_state='present'` の候補だけを返す
- [x] `find_stored_eml()` は `(account_id, file_hash)` で完全ハッシュ一致を検索する。`002_sync_cursor.sql` で索引 `idx_msg_file_hash` を追加する
- [x] `record_failure()` は `UNIQUE(account_id, folder_id, uidvalidity, uid)` に対して upsertし、`attempt_count` を加算、`last_failed_at` を更新する
- [x] `commit_batch()` はコミットだけを行う。`checkpoint()` はPhase 0の `checkpoint_truncate()` へ委譲し、同期usecaseから10バッチごとに呼ばれる
- [x] `sqlite3.Error` を `detach.classify_sqlite_error()` 経由でドメイン例外へラップする
- [x] 接続をスレッド間で共有しない（Phase 0 の `ConnectionManager` を使う）
- [x] **1通ごとのコミットを行わない**ことをテストで担保する

#### **D-4. `migrations/002_sync_cursor.sql`**

- [x] `folders` に `backfill_next_uid INTEGER` と `initial_sync_completed INTEGER NOT NULL DEFAULT 0` を追加する
- [x] `sync_failures` を安全に再作成し、`uidvalidity INTEGER NOT NULL DEFAULT 0` と `UNIQUE(account_id, folder_id, uidvalidity, uid)` を持たせる。既存行は `uidvalidity=0` の旧世代履歴として移行する
- [x] `messages(account_id, file_hash)` に部分索引 `idx_msg_file_hash WHERE file_hash IS NOT NULL` を追加する
- [x] oversizeは既存の `messages.relative_path IS NULL` / `file_hash IS NULL` と `sync_failures.error_class='oversize'` で表現し、専用列を追加しない
- [x] `001_init.sql` を変更しない

---

### **3.5 グループE: IMAP（*B-2 完了後。C / D と並行可*）**

#### **E-1. `infrastructure/fetchers/imap_common.py`**

- [x] modified UTF-7 のエンコード / デコードを実装する（`imap4-utf-7`。標準ライブラリに無いため自前）
- [x] `LIST` / `LSUB` 応答のパーサを実装する（属性リスト・階層区切り文字・引用符付きフォルダ名）
- [x] SPECIAL-USE（RFC 6154）属性の抽出（`\Trash` / `\Sent` / `\Drafts` / `\Junk`）
- [x] `FETCH` 応答のパーサを実装する（`UID` / `INTERNALDATE` / `RFC822.SIZE` / `FLAGS` / リテラル本体）
- [x] `INTERNALDATE` を tz-aware な UTC `datetime` へ変換する
- [x] `wrap_imap_errors()`（コンテキストマネージャ or デコレータ）を実装し、以下をラップする
  - [x] `imaplib.IMAP4.error` → 応答本文から `AUTHENTICATIONFAILED` を検出したら `AuthenticationError`、それ以外は `PermanentError`
  - [x] `imaplib.IMAP4.abort` / `socket.timeout` / `TimeoutError` / `ConnectionError` → `TransientError`
  - [x] `ssl.SSLError` → 証明書検証失敗は `PermanentError`、それ以外は `TransientError`
  - [x] `OSError` は `detach.classify_os_error()` へ委譲する
- [x] **上位層へ `imaplib` / `ssl` / `socket` の例外を一切漏らさない**ことをテストで担保する

#### **E-2. `infrastructure/fetchers/onamae_imap.py`**

- [x] `OnamaeImapFetcher(BaseMailFetcher)` を実装する（`IMAP4_SSL`、既定ポート993、接続タイムアウト・読み取りタイムアウトを設定）
- [x] `connect()` で `LOGIN` し、`CAPABILITY` を記録する（`MOVE` / `SPECIAL-USE` / `UIDPLUS` の有無を保持）
- [x] **1アカウント＝1接続**を守る（インスタンスが複数接続を張らない）
- [x] `list_folders()`: `LIST "" "*"` → modified UTF-7 デコード → `RemoteFolder` 化（区切り文字と SPECIAL-USE を含める）
- [x] `select_folder()`: `SELECT` の応答から `UIDVALIDITY` を取得して返す
- [x] `iter_message_refs()`
  - [x] `min_uid` / `max_uid` から `UID SEARCH UID {min_uid}:{max_uid or '*'}` を組み立て、`descending` の指定順に返す
  - [x] **500件ずつ**（D-10）`UID FETCH (UID INTERNALDATE RFC822.SIZE FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])` を発行する
  - [x] 各チャンクの前後で `cancel.raise_if_cancelled()` を呼ぶ
  - [x] 遅延生成（ジェネレータ）であり、全件をメモリに載せないこと
- [x] `list_existing_uids()`: `UID SEARCH ALL` の軽量実装
- [x] `get_max_uid()`: `UID SEARCH ALL` の応答末尾から最大UIDだけを返し、メッセージ参照や本文を取得しない
- [x] `download_eml_bytes()`: `UID FETCH {uid} BODY.PEEK[]`（**`\Seen` を立てない**）
- [x] `download_eml_headers()`: `UID FETCH {uid} BODY.PEEK[HEADER]`（本文を取得せず、**`\Seen` を立てない**）
- [x] `delete_remote_message()`（D-5）
  - [x] `mode="trash"`: `UID MOVE` があれば使用、無ければ `UID COPY` → `UID STORE +FLAGS (\Deleted)` → `UID EXPUNGE`（`UIDPLUS` 無ければ `EXPUNGE`）
  - [x] `mode="expunge"`: `UID STORE +FLAGS (\Deleted)` → `EXPUNGE`
  - [x] ゴミ箱の特定: SPECIAL-USE `\Trash` → 候補名（`Trash` / `ゴミ箱` / `Deleted Items` / `Deleted Messages` / `INBOX.Trash`）→ `AppConfig.remote_trash_folder`。特定できなければ `PermanentError`
  - [x] docstring に「**Phase 1 では usecase / CLI から呼ばない。安全装置は Phase 4**」と明記する
- [x] **リトライ・バックオフをこのクラスに書かない**
- [x] 実機で確認する項目をコメントで列挙する（区切り文字・modified UTF-7・同時接続数・タイムアウト）

---

### **3.6 グループF: usecases（*グループB〜E 完了後*）**

#### **F-1. `usecases/retry.py`**

- [x] `with_retry(fn, *, attempts=3, base_delay=1.0, cancel=None)` を実装する
- [x] **`TransientError` のみ**をリトライ対象とし、1s → 2s → 4s の指数バックオフに jitter を加える
- [x] `AuthenticationError` / `PermanentError` / `OversizeError` / `UidValidityChanged` は即座に再送出する
- [x] 待機中も `CancelToken` に反応する（`Event.wait(timeout)` を使う）
- [x] リトライ回数と待機時間をログに残す

#### **F-2. アカウント登録と資格情報ストア**

- [x] `usecases/register_account.py` に `register_account(repo, credential_store, *, account_id, host, port, username, password, display_name)` を実装する
- [x] `account_id` のファイルシステム安全性を検証し、不正なIDは登録前に拒否する
- [x] パスワードを注入された `BaseCredentialStore` へ保存する。usecaseから `keyring` を直接importしない
- [x] `accounts` へ upsert する（**パスワードはDBへ書かない**）
- [x] `load_credentials(credential_store, account_id) -> str` を実装し、未登録時は `AuthenticationError` を送出する
- [x] `list_accounts()` を実装する（パスワードを返さない）
- [x] `infrastructure/security/keyring_store.py` に `KeyringCredentialStore(BaseCredentialStore)` を実装し、keyringの生例外を `CredentialStoreError` へ変換する
- [x] keyringバックエンド不在時は平文へフォールバックせず、分かりやすいエラーで登録を中止する

#### **F-3. `usecases/sync_folders.py`**

- [x] `refresh_folders(fetcher, repo, account_id)` を実装する
- [x] 新規フォルダを **`is_sync_target=0`** で登録し、検出件数を返す（F-24）
- [x] 既存フォルダの `display_name` を更新する
- [x] サーバーから消えたフォルダは削除せず、ログと戻り値で通知する
- [x] `set_sync_target(repo, account_id, raw_name, enabled)` を実装する

#### **F-4. `usecases/sync_mail.py` — 同期フロー（*本フェーズの中核*）**

- [x] `SyncOptions`（frozen dataclass）を定義する: `max_message_bytes`。コンポジションルートが `AppConfig` から変換し、usecaseは `mail_dock.config` をimportしない
- [x] `SyncProgress`（frozen dataclass）を定義する: `transferred_bytes` / `total_bytes_estimate` / `message_count` / `current_folder` / `eta_seconds`
- [x] `sync_account(fetcher, repo, storage, manifest, *, account_id, options, cancel, on_progress)` を実装する。引数はdomainポートとusecase固有型だけにする
- [x] 同期対象フォルダごとに以下を実行する
  - [x] `select_folder()` で `UIDVALIDITY` を取得し、DBの値と比較する
  - [x] 初回／**UIDVALIDITY変化時**は `get_max_uid()` を呼び、`last_seen_uid=max_uid` / `backfill_next_uid=max_uid` / `initial_sync_completed=(max_uid == 0)` を同一トランザクションで初期化する。旧世代のメッセージと失敗履歴は保持する
  - [x] 現在のUIDVALIDITYに一致する `sync_failures` の未解決レコード（`attempt_count < 10`）だけを先に再試行する。`oversize` は現在の `options.max_message_bytes` 以下になった場合だけ再試行する（F-18）
  - [x] **新着パス**: 開始時に `new_max_uid=get_max_uid()` を固定し、`new_max_uid` から `last_seen_uid + 1` までUID**降順**で処理する。`last_seen_uid` は各バッチでは進めず、この固定範囲を全件処理した最後のバッチでだけ `new_max_uid` へ更新する
  - [x] 新着パス中断時は高水位が旧値のままなので、次回は同じ範囲を再走査する。確定済み行のupsert、完全ハッシュdedupe、マニフェストイベントの `source_item_key` による冪等適用を前提に、100件を超える新着でも欠損させない
  - [x] **履歴パス**: `backfill_next_uid` 以下をUID**降順**で処理し、バッチ確定時の最小UIDから1を引いた値へカーソルを進める。対象が無くなれば `backfill_next_uid=0` / `initial_sync_completed=1` とする
  - [x] `size_bytes > options.max_message_bytes` は本文をダウンロードせず `download_eml_headers()` だけを呼ぶ。解析したヘッダを `relative_path=NULL` / `file_hash=NULL` かつ `message_contents` なしの `messages` 行へ登録し、同じバッチで `oversize` failureと `fetch_skipped` イベントを記録する（F-19）
  - [x] `with_retry()` 経由で `download_eml_bytes()` を呼ぶ（F-17）
  - [x] EML取得後に完全なSHA-256で `repo.find_stored_eml()` を照会し、候補があればstorageで実体を再検証する。一致時は既存パスを共有し、不一致時は通常の `save()` を行う
  - [x] 解析失敗時もEMLを保存し、`message_contents` を空で登録し `parse` として記録する（F-9）
  - [x] **100通または50MB**でバッチを閉じ、次の順序を厳守する: 各EMLを原子的配置 → 各イベントを `manifest.append()` → バッチ境界で `manifest.flush_and_sync()` → `repo.begin_batch()` → 全メッセージ・failure・該当カーソルを更新 → `repo.commit_batch()`
  - [x] マニフェストfsync後・DBコミット前に停止した「マニフェストがDBより先行する状態」は許容する。逆順は許容しない
  - [x] 10バッチごとに `repo.checkpoint()` を1回呼ぶ（F-14）
  - [x] `on_progress` を**転送バイト数主体**で通知する（F-20）
  - [x] `cancel` は**バッチコミット境界**で成立させる（途中キャンセルで確定済み分のみ残る）
- [x] 全フォルダ完了後に削除・移動検知を行う（F-16）
  - [x] `list_existing_uids()` と `repo.local_uids(..., current_uidvalidity)` の差分を取る
  - [x] 消失UIDについて、他フォルダの `present` な候補を `content_key` と完全な `file_hash` の両方で照合する
  - [x] 一致候補が1件だけなら `remote_state='moved'` + `moved_to_folder_id`、候補なしなら `'deleted'`、複数候補またはハッシュ不明なら `'unknown'` + `moved_to_folder_id=NULL` とする
  - [x] マニフェストへ `moved` / `delete_detected` / `remote_state_unknown` を追記する
  - [x] **EMLファイルを絶対に削除しない**
- [x] `AuthenticationError` は同期全体を中止し、それ以外の単発失敗では**同期を止めない**（F-18）
- [x] `SyncResult`（取得件数・バイト数・スキップ件数・失敗件数・キャンセル有無）を返す

#### **F-5. `usecases/reparse.py`**

- [ ] `reparse_messages(repo, storage, *, account_id=None, only_failed=True, cancel)` を実装し、`BaseEmlStorage` からEMLを読む
- [ ] `relative_path` からEMLを読み直し、`file_hash` を再検証してから解析する
- [ ] `message_contents` を更新し（FTSトリガーが追随することを確認）、`sync_failures` の `parse` レコードを解消する
- [ ] 実体が無い・ハッシュ不一致のレコードはスキップして報告する（**この段階で修復は行わない。整合性チェックは Phase 4**）
- [ ] oversizeによるヘッダのみの行はEML実体がない正常状態として対象外にし、欠損として報告しない

---

### **3.7 グループG: CLI（*グループF 完了後*）**

#### **G-1. `__main__.py` — サブコマンド追加**

- [ ] 既存の起動シーケンス（設定 → ロギング → ルート解決 → ロック → マイグレーション）を再利用する形でサブコマンドを追加する
- [ ] `account add`: `--account-id` / `--host` / `--port` / `--username` を受け取り、**パスワードは `getpass` で対話入力**する（コマンドライン引数・環境変数から受け取らない）
- [ ] `account list`: 登録済みアカウントと接続情報を表示する（パスワードは表示しない）
- [ ] `folders`: 一覧表示（`--refresh` でサーバーから再取得）、`--enable RAW_NAME` / `--disable RAW_NAME` で `is_sync_target` を切り替える
- [ ] `sync`: `--account` で対象を絞る。進捗を標準出力へ（転送バイト数・通数・推定残り時間）
- [ ] `reparse`: `--all` / 既定は失敗レコードのみ
- [ ] `SIGINT` ハンドラで `CancelToken` をセットし、**バッチ境界で安全に停止**する（2回目の Ctrl+C で即時終了）
- [ ] 起動時に `cleanup_tmp()` を呼ぶ
- [ ] 新しい例外に対する終了コードを `_exit_code()` へ追加する（`AuthenticationError` → 5、`FetchError` → 6、`OperationCancelledError` → 130）
- [ ] `--help` の出力が日本語環境で崩れないことを確認する

---

### **3.8 グループH: テスト（*各グループと並行して作成*）**

#### **H-1. 単体テスト（Docker不要）**

- [x] `test_charset.py`: フォールバック順序、ラベル正規化、CP932機種依存文字、最終手段で例外を投げないこと
- [x] `test_headers.py`: RFC2047、**RFC2231分割**、Outlook非標準、`Date` 不正・欠損・未来日時のフォールバック、`thread_key` / `content_key` 算出
- [x] `test_eml_parser.py`: **コーパス全件が例外なく解析される**こと、本文優先順、`related` 追跡、インライン画像の除外、`has_attachment`、壊れたMIMEで `parse_error` が立つこと
- [ ] `test_html_to_text.py`: script/style除去、空白圧縮
- [x] `test_normalize.py`: 全角英数の半角化、大文字小文字、連続空白、かな・カナを同一視しないこと
- [x] `test_filename.py`: 8項目すべて（パストラバーサル、`:` 置換、予約名、長さ制限、実行可能拡張子、`resolve_within` の最終防御）
- [x] `test_eml_storage.py`: ファイル名＝sha256先頭32桁、`INTERNALDATE` による年月、`unknown/`、**dedupe時に書き込みが発生しないこと**、`tmp/` がルート配下であること、`os.replace` 前に中断しても本番ディレクトリが汚れないこと
- [x] `test_eml_storage.py`: 同一アカウントの別年月・別フォルダでも完全ハッシュ一致時は既存パスを再検証して共有し、アカウント間では実体を共有しないこと。先頭32桁だけが同じ候補を同一視しないこと
- [x] `test_manifest.py`: CRC32付与、追記のみ、月次ローテーション、**末尾torn行の切り離し**、中間破損で `ManifestCorruptError`
- [ ] `test_message_repository.py`: `BEGIN IMMEDIATE`、バッチ境界でのみコミットされること、同一UIDVALIDITY内だけの `ON CONFLICT` 更新、UIDVALIDITY別failureの `attempt_count` 加算、現在世代フィルタ
- [x] `test_retry.py`: `TransientError` のみリトライ、待機時間の系列、キャンセル即応
- [ ] `test_sync_mail.py`（`FakeFetcher` + `InMemoryMessageRepository`）: 最新優先の初回同期、新着範囲完了時だけ進む高水位、履歴カーソルの降順レジューム、初回同期中の新着、100件超の新着中断時の冪等再走査、UIDVALIDITY変化、oversizeのヘッダ行、解析失敗の継続、キャンセル境界、削除・移動・曖昧検知、`AuthenticationError` での中止
- [ ] `test_ports.py`: usecaseがdomainポートのFakeだけで動作し、`keyring` / SQLite / ファイルI/Oをimportしないこと
- [x] `test_keyring_store.py`: 保存・読込・削除、バックエンド不在時に平文へフォールバックせず `CredentialStoreError` になること
- [x] `test_002_sync_cursor.py`: v1 DBからカーソル列・UIDVALIDITY付きfailure一意制約・ハッシュ索引へデータを保持して移行できること
- [x] `test_imap_common.py`: modified UTF-7 往復、LIST/FETCH応答パース、**例外ラップの網羅**（`imaplib` / `ssl` / `socket` の例外がドメイン例外になること）
- [ ] `test_reparse.py`: ハッシュ不一致・実体欠損のスキップ、`message_contents` 更新

#### **H-2. 結合テスト（`docker` マーカー / WSL）**

- [ ] `test_imap_fetcher.py`（GreenMail + Dovecot の両方でパラメトライズ）: `connect` / `list_folders`（**日本語フォルダの modified UTF-7 と区切り文字**）/ `select_folder` / `iter_message_refs` / `list_existing_uids` / `download_eml_bytes`（`\Seen` が立たないこと）
- [ ] `test_imap_delete.py`（Dovecot）: `delete_remote_message(mode="trash")` でゴミ箱へMOVEされること、`mode="expunge"` で消えること、SPECIAL-USE からゴミ箱を特定できること
- [ ] `test_sync_flow.py`: 実ストレージルート + 実SQLite で end-to-end 同期し、EML・マニフェスト・DB の三者が一致すること
- [ ] `test_sync_resume.py`: 新着パスと履歴パスの各途中でキャンセル → 再実行で対応するカーソルから継続し、重複・欠損が出ないこと
- [ ] `test_uidvalidity_change.py`（Dovecot）: UIDVALIDITY を変更 → 二カーソルが再初期化され、旧世代の行とfailure履歴を保持し、現在世代だけを再試行し、EMLが再書き込みされないこと（dedupe）
- [ ] `test_delete_detection.py`: 一意な完全一致は `'moved'`、候補なしは `'deleted'`、同一Message-IDの複数候補・ハッシュ不明は `'unknown'` となり、**EMLが残っていること**
- [ ] `test_fetch_failure.py`: 接続断を注入し、`TransientError` リトライ後に `sync_failures` へ記録され、次回同期で自動再試行されること

#### **H-3. 実機検証（お名前.com）**

- [ ] 小規模フォルダ（数十〜数百通）で `account add` → `folders --refresh --enable` → `sync` を完走させる
- [ ] 以下を記録し、必要なら実装へ反映する
  - [ ] 階層区切り文字（`.` か `/` か）
  - [ ] modified UTF-7 の日本語フォルダ名が正しくデコードされるか
  - [ ] 同時接続数制限に抵触しないか（1接続で完走するか）
  - [ ] アイドル時の切断タイムアウトと、その際の例外分類が `TransientError` になるか
  - [ ] `UID MOVE` / `UIDPLUS` / `SPECIAL-USE` の対応状況
- [ ] 検証結果を本書「7. Phase 2 への引き継ぎ事項」へ追記する

#### **H-4. CI**

- [ ] `.github/workflows/ci.yml` の Linux ジョブに Dovecot コンテナを追加する
- [ ] 結合テストの所要時間が許容範囲に収まることを確認する（超える場合はマーカーでさらに分離）

---

## **4. 主要成果物**

| パス | 内容 | タスク |
| :---- | :---- | :---- |
| `src/mail_dock/domain/errors.py` | `FetchError` の葉 / `ManifestCorruptError` | B-1 |
| `src/mail_dock/domain/fetcher.py` | `BaseMailFetcher` / `RemoteFolder` / `RemoteMessageRef` / `CancelToken` | B-2 |
| `src/mail_dock/domain/messages.py` | `ParsedMessage` / `ParsedAttachment` / `StoredEml` | B-3 |
| `src/mail_dock/domain/repository.py` | `BaseMessageRepository` | B-4 |
| `src/mail_dock/domain/ports.py` | credential / EML storage / manifest の外部I/Oポート | B-5 |
| `src/mail_dock/infrastructure/parsing/charset.py` | 文字コードのフォールバック | C-1 |
| `src/mail_dock/infrastructure/parsing/headers.py` | RFC2047 / RFC2231 / Date / thread_key | C-2 |
| `src/mail_dock/infrastructure/parsing/html_to_text.py` | HTML→テキスト | C-3 |
| `src/mail_dock/infrastructure/parsing/normalize.py` | `normalize_for_search()` | C-4 |
| `src/mail_dock/infrastructure/parsing/eml_parser.py` | EML解析 | C-5 |
| `src/mail_dock/infrastructure/storage/filename.py` | 添付ファイル名サニタイズ | C-6 |
| `src/mail_dock/infrastructure/storage/eml_storage.py` | 原子的保存・ハッシュ・dedupe | D-1 |
| `src/mail_dock/infrastructure/storage/manifest.py` | 永続マニフェスト（JSONL+CRC32） | D-2 |
| `src/mail_dock/infrastructure/database/message_repository.py` | SQLite実装・バッチコミット | D-3 |
| `src/mail_dock/migrations/002_sync_cursor.sql` | 二カーソル・UIDVALIDITY別failure・ハッシュ索引 | D-4 |
| `src/mail_dock/infrastructure/security/keyring_store.py` | `BaseCredentialStore` のkeyring実装 | F-2 |
| `src/mail_dock/infrastructure/fetchers/imap_common.py` | modified UTF-7・応答パース・例外ラップ | E-1 |
| `src/mail_dock/infrastructure/fetchers/onamae_imap.py` | `OnamaeImapFetcher` | E-2 |
| `src/mail_dock/usecases/retry.py` | 指数バックオフ | F-1 |
| `src/mail_dock/usecases/register_account.py` | keyring連携 | F-2 |
| `src/mail_dock/usecases/sync_folders.py` | フォルダ一覧・同期対象 | F-3 |
| `src/mail_dock/usecases/sync_mail.py` | 同期フロー | F-4 |
| `src/mail_dock/usecases/reparse.py` | 再解析 | F-5 |
| `src/mail_dock/__main__.py` | CLIサブコマンド追加 | G-1 |
| `tests/docker/compose.yaml` / `tests/docker/dovecot/` | Dovecot 追加 | A-1 |
| `tests/fixtures/eml/` / `tests/support/` | EMLコーパス・FakeFetcher・InMemoryRepository | A-2 |
| `tests/unit/` / `tests/integration/` | 単体・結合テスト | H-1 / H-2 |

---

## **5. スコープ境界**

### **5.1 含むもの**

セクション3のグループA〜H。ヘッドレスで「アカウント登録 → フォルダ選択 → 初回同期 → 中断・再開 → 増分同期 → 削除・移動検知 → 再解析」が通る中核一式。

### **5.2 含まないもの（明示的に除外）**

| 除外項目 | 実施フェーズ |
| :---- | :---- |
| FTS5+trigram の性能PoC、検索クエリ構築（AND/OR、3文字未満のLIKEフォールバック）、構造化フィルタ | Phase 2 |
| PySide6 の画面 / ViewModel / QThreadワーカー / 遅延ロード一覧モデル / HTML表示5層サンドボックス / **添付ファイルの実ファイル保存** | Phase 3 |
| サーバー削除の**安全装置**（ドライラン・件数手入力確認・監査ログ・レート制限・ゴミ箱既定） | Phase 4 |
| ローカルゴミ箱・30日purge・墓標化・整合性チェック・再インデックス・mboxエクスポート | Phase 4 |
| 切断の状態機械 / `WM_DEVICECHANGE` 監視 / ハートビートのタイマー駆動 / 範囲限定検証 / フォールト注入試験 / VHDX detach試験 | Phase 4 |
| **EML＋マニフェストからの `metadata.db` 完全再構築** | Phase 4 |
| フルスケール同期（5万通/100GB）と `synchronous` の最終決定 | Phase 4 |
| PST関連一式 / `vendor/readpst` / `003_pst_import.sql` | Phase 4.5 |
| Gmail / OAuth2 / `message_folders` 中間テーブル | Phase 5 |
| IMAP IDLE（プッシュ受信）、フラグの双方向同期、ローカル既読管理 | 恒久的にスコープ外 |

---

## **6. 検証**

各項目の完了を確認したうえで、対応するタスクのチェックボックスを埋めること。

- [ ] V-1. `uv sync` → `uv run ruff format --check .` → `uv run ruff check .` → `uv run mypy` がすべて成功する
- [ ] V-2. `uv run pytest -m "not docker"` が全緑になり、`domain` + `usecases` のカバレッジが 80% 以上である
- [ ] V-3. `tests/fixtures/eml/` の全件が例外を投げずに解析され、期待テキスト（件名・本文・添付名）と一致する。文字化けがゼロである
- [ ] V-4. 中断注入試験: `os.replace` 直前 / マニフェスト追記途中 / マニフェストfsync後かつDBコミット前 / DBコミット前 の各点で中断させ、**「DBに行があるがEML実体またはfsync済みマニフェストが無い」状態が発生しない**こと。マニフェストがDBより先行する状態は許容し、再同期で回復すること
- [ ] V-5. dedupe 検証: 同一メールを同一アカウントの2フォルダ・異なる年月で取得し、EMLファイルが1個・`messages` が2行・両行の完全な `file_hash` と `relative_path` が一致する。別アカウントでは実体を共有しない
- [x] V-6. マニフェスト検証: 末尾行を意図的に途中で切って破損させると `read_events` が末尾行のみを切り離して継続し、中間行を破損させると `ManifestCorruptError` になる
- [ ] V-7. `docker compose -f tests/docker/compose.yaml up -d` → `MAILDOCK_DOCKER=1 uv run pytest -m docker` が GreenMail / Dovecot の両方で全緑になる
- [ ] V-8. UIDVALIDITY 変化検証（Dovecot）: 値を変更すると二カーソルが新世代の最大UIDで初期化され、旧世代の行とfailure履歴を保ったまま現在世代だけが再試行され、完全ハッシュ一致EMLの再書き込みが発生しない
- [ ] V-9. レジューム検証: 最新メールが最初に取得されること、新着の固定範囲完了までは `last_seen_uid` が進まず中断時に冪等再走査されること、履歴同期中は `backfill_next_uid` から再開すること、初回同期中に到着した新着と100件を超える新着も欠損しないことを確認する
- [ ] V-10. 削除・移動検知: サーバー側で削除・移動すると、一意な完全一致だけが `'moved'`、候補なしが `'deleted'`、曖昧候補が `'unknown'` となり、**EMLファイルがすべて残っている**
- [ ] V-11. 失敗記録: 接続断を注入すると `TransientError` が3回リトライされ `sync_failures` に記録され、次回同期で自動再試行されて `attempt_count` が加算される
- [ ] V-12. サイズ上限: `max_message_bytes` を小さくして同期すると、超過メールの本文がダウンロードされず、ヘッダ情報を持ち `relative_path` / `file_hash` がNULLの行と `oversize` failureが登録される
- [ ] V-13. 解析失敗の継続: 壊れたMIMEを含むメールボックスを同期すると、EMLは保存され `message_contents` は空、`sync_failures` に `parse` が記録され、`reparse` 実行後に内容が埋まる
- [ ] V-14. 例外の隔離: `usecases` / `presentation` 層のコードに `imaplib` / `ssl` / `socket` / `sqlite3` の import が無いことを静的に確認する
- [ ] V-15. 資格情報: 同期実行後に `metadata.db` / `config.json` / `logs/` をパスワード文字列で grep してヒットしないことを確認する
- [ ] V-16. 実機（お名前.com）: 小規模フォルダの初回同期が完走し、H-3 の確認項目がすべて記録される
- [ ] V-17. CI: プルリクエストで `lint` / `test-windows` / `test-linux`（Dovecot 追加後）の3ジョブがすべて成功する
- [ ] V-18. マイグレーション: v1の実データを保持したまま `002_sync_cursor.sql` が適用され、二カーソル、UIDVALIDITY別failure一意性、完全ハッシュ索引が利用できる
- [ ] V-19. バッチ耐久性: メッセージ・failure・対応カーソルが同じトランザクションで確定し、WALチェックポイントが1バッチごとではなく10バッチごとにだけ実行される

---

## **7. Phase 2 への引き継ぎ事項**

* `normalize.normalize_for_search()` は **検索側でも必ず同じ関数を使う**こと。Phase 2 で別実装を作らない。
* FTS5 + trigram の性能PoC（1万通で実測）を Phase 2 冒頭で行う。Phase 1 で蓄積した実データをそのまま計測に使う。
* `001_init.sql` は変更せず、Phase 1で必要な二カーソルとUIDVALIDITY別failure管理は `002_sync_cursor.sql` で追加する。これ以外の列の過不足は本節へ記録し、Phase 2以降のマイグレーションとして適用する。
* A-3のスキーマ確認（2026-07-30）では、`001_init.sql`との差分は`002_sync_cursor.sql`で明示済みの範囲（`folders.backfill_next_uid` / `folders.initial_sync_completed`、`sync_failures.uidvalidity`と世代単位の一意制約、`idx_msg_file_hash`）に限定された。その他の過不足は確認されていない。後から判明した差分は`001_init.sql`を変更せず、Phase 2以降のマイグレーションとして本節へ追記する。
* 実機検証（H-3）で判明したお名前.com のサーバー特性（区切り文字・`UID MOVE` / `UIDPLUS` / `SPECIAL-USE` 対応・タイムアウト値）を本節へ追記する。
* `delete_remote_message()` は実装済みだが未使用。Phase 4 で安全装置（ドライラン・件数手入力・監査ログ・レート制限・切断ガード）を前段に置いてから初めて呼び出す。
* `manifest.read_events()` / `repair_tail()` は Phase 4 の「EML＋マニフェストからのDB完全再構築」の入口として使う。
* マニフェストfsync後・DBコミット前の中断では、マニフェストがDBより先行し得る。これは正本優先の許容状態であり、Phase 4の再構築ではイベントの冪等適用で吸収する。
* oversizeのヘッダのみの行を後から全文取得する導線では、完全なEMLを保存し、完全ハッシュによる再dedupe後に `relative_path` / `file_hash` / `message_contents` を更新し、新しいマニフェストイベントを追記する。
* `SyncProgress` / `CancelToken` は Phase 3 の QThread ワーカーからそのまま駆動できる形にしてある。同期ロジックを `presentation` 層へ移さない。
* 添付ファイル名サニタイズ（`filename.py`）は純粋関数のみ。Phase 3 の「添付を保存」で `resolve_within()` を保存直前に必ず呼ぶ。
