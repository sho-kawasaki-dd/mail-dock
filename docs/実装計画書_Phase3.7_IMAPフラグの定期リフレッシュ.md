# **Phase 3.7: IMAPフラグの定期リフレッシュ 実装計画書**

対象: [ローカルメールバックアップ＆閲覧アプリ 開発計画書.md](./ローカルメールバックアップand閲覧アプリ開発計画書.md) の **4.6-2 IMAPフラグの扱い（スナップショット方式）** および **9章 スコープ外一覧**

前提: [Phase 1: 抽象化層とIMAPコア 実装計画書](./実装計画書_Phase1_抽象化層とIMAPコア.md) の `sync_account()` / `BaseMailFetcher` / `BaseMessageRepository`、[Phase 3: GUI基礎構築 実装計画書](./実装計画書_Phase3_GUI基礎構築.md) の未読・スター表示（D-17）が完成していること。

位置づけ: 「同期時点の一度きりのスナップショット」だったIMAPフラグ（`\Seen` 等）を、**サーバーからの一方向リフレッシュ**により定期的に取り直せるようにする小規模フェーズ。ローカル独自の既読管理・フラグの書き戻し（双方向同期）は引き続き行わない。

本書と開発計画書に矛盾がある場合は開発計画書を正とするが、**4.6-2・9章の該当記述自体は本書のGroup G完了をもって本書の内容に改訂する**（「一度きりのスナップショット」→「定期的に取り直すスナップショット」）。それ以外の不変条件（真実の情報源はEML＋マニフェスト、双方向同期をしない、ローカル既読管理をしない）は変更しない。

---

## **1. 目的**

- [ ] 既に取得済みのメッセージについて、サーバー側の `\Seen` / `\Flagged` 等の変化を**一方向（サーバー→ローカル）**で定期的に取り直せるようにする
- [ ] 上記を**新着同期・履歴backfillの負荷に影響を与えない形**（軽量IMAPコマンド・範囲限定・間引き・一括更新）で実現する
- [ ] `CONDSTORE`/`HIGHESTMODSEQ` に対応したサーバーでは差分のみを取得し、非対応サーバーでは範囲限定フォールバックで動作すること
- [ ] フラグ表示のツールチップを「同期時点の一度きりのスナップショット」から「最終確認日時つきのスナップショット」に更新する
- [ ] 個別メッセージの手動再同期ボタンのような新規UI操作を追加せず、既存の同期実行だけで自動的に恩恵を受けられるようにする

## **2. 要件**

### **2.1 前提となる意思決定（確定済み）**

| # | 項目 | 決定内容 |
| :--- | :---- | :---- |
| D-1 | 同期方向 | **サーバー→ローカルの一方向リフレッシュのみ**。ローカルでの既読操作をサーバーへ書き戻す導線・ローカル独自の既読管理は追加しない |
| D-2 | 実行トリガー | 個別メッセージの手動ボタンは作らない。既存の `sync_account()` の一部として自動的に実行する |
| D-3 | 実行条件 | フォルダの `initial_sync_completed` が真になってから実行する。初回の大量バックログ取り込み中は実行せず、新着・履歴backfillの完了後に行う |
| D-4 | 対象範囲の絞り込み | `internal_date` が直近 `flag_refresh_window_days`（既定30日）以内のメッセージに限定する。古いメッセージの差分は意図的に反映せず、必要であれば将来の個別再同期機能（未着手）に委ねる |
| D-5 | 間引き（TTL） | `flags_seen_at` が `flag_refresh_min_interval_seconds`（既定3600秒）より古いメッセージが対象範囲内に1件以上あることを、リフレッシュ実行のトリガーとする。`CONDSTORE` 経路ではTTL未到来のメッセージも差分応答に含まれた場合は更新し、フォルダ単位MODSEQを進めることで変更を取り逃がさない。非 `CONDSTORE` 経路ではTTL切れUIDだけを取得する |
| D-6 | `CONDSTORE` 対応 | `capabilities` に `CONDSTORE` が含まれ、かつ `SELECT` で有効な `HIGHESTMODSEQ` を取得できる場合は差分取得を優先する。**実サーバー（`mail71.onamae.ne.jp`）で `CONDSTORE` 対応を確認済み（2026-08-18、`OnamaeImapFetcher.capabilities` に `CONDSTORE` を確認）。** CONDSTORE経路を通常経路として実装し、非対応サーバー・`NOMODSEQ`・不正または欠落した `HIGHESTMODSEQ` に備えてフォールバック（D-7）も維持する |
| D-7 | フォールバック時の取得方法 | `UID FETCH <対象範囲> (FLAGS)` のみを発行する。本文・ヘッダは取得しない。1コマンドあたりの件数は既存の `_FETCH_CHUNK_SIZE`（500件）をそのまま再利用し、新しい定数は作らない |
| D-8 | 永続化方法 | `BaseMessageRepository` に、対象範囲の `uid` / `imap_flags` / `flags_seen_at` を一括取得するメソッドと、`imap_flags` / `flags_seen_at` を更新する専用メソッドを追加する。フラグ変化があったメッセージは個別値更新、変化がなかったメッセージはN+1問題を回避するためUIDリストによる一括 `flags_seen_at` 更新（バッチ実行）を行う |
| D-9 | マニフェスト記録 | フラグリフレッシュは**マニフェストへイベントを追記しない**。フラグは表示専用の派生メタデータであり、EML＋マニフェストからDBを再構築した後も次回リフレッシュで自然に最新化されるため、真実の情報源としての記録は不要と判断する |
| D-10 | `HIGHESTMODSEQ` の保持 | `folders` テーブルに `highest_modseq INTEGER` 列を追加し、フォルダ単位で前回正常完了したMODSEQを保持する。初回（`NULL`）は直近ウィンドウ全体を通常FETCHして基準スナップショットを作成した後に現在値を保存する。`UIDVALIDITY` 変更時、`CONDSTORE` 非対応時、`NOMODSEQ` 時は `NULL` にリセットする |
| D-11 | バッチ制御 | IMAP通信中はDBトランザクションを保持しない。既存の `begin_batch` / `commit_batch` / `checkpoint` を再利用し、フラグ更新は500件単位でコミットできる。`highest_modseq` は全FETCHが正常完了した後にのみ別の最終バッチで保存する。途中失敗時はMODSEQを進めず、既にコミットした冪等なフラグ更新を次回再適用する |
| D-12 | 失敗時の扱い | フラグリフレッシュ中の `AuthenticationError` は再送出してアカウント同期を中断し、`OperationCancelledError` は既存のキャンセル処理へ渡す。それ以外の `FetchError` は警告ログに残して次のフォルダ・次回同期に委ねる（新着・履歴backfillの成否とは独立に扱う） |
| D-13 | 初回基準点 | `highest_modseq IS NULL` のときは `CHANGEDSINCE` を使わない。直近ウィンドウ内の全UIDを `iter_flags` で確認し、更新完了後に `SELECT` で得た現在の `HIGHESTMODSEQ` を保存する。現在値を先に保存して変更を取り逃がす実装は禁止する |
| D-14 | MODSEQの後退・異常 | 保存済みMODSEQがサーバーの現在値より大きい場合、またはMODSEQを正のSQLite整数として扱えない場合は、保存値を破棄してD-13の初回基準点処理へ戻る |

### **2.2 機能要件**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-1 | `BaseMailFetcher` に、指定UID集合のFLAGSのみを取得する `iter_flags(raw_name, uids, *, cancel=None) -> Iterator[RemoteMessageRef]` を追加する。戻り値は既存の `RemoteMessageRef` を再利用し、`uid` / `flags` 以外のフィールドは未設定でよい | D-7 |
| F-2 | `BaseMailFetcher` に、`CONDSTORE` 専用の `iter_flags_since(raw_name, modseq, *, cancel=None) -> Iterator[RemoteMessageRef]` を追加する（`UID FETCH 1:* (UID FLAGS) (CHANGEDSINCE modseq)` 相当）。応答UIDを対象範囲のローカルUID集合と照合し、TTL未到来でも差分に含まれた対象範囲内メッセージは更新する。非対応サーバーでは呼び出し側がF-1にフォールバックする | D-5, D-6 |
| F-3 | `OnamaeImapFetcher.capabilities` が `CONDSTORE` の有無を反映すること。また `CONDSTORE` サポート時は `SELECT (CONDSTORE)` または `ENABLE CONDSTORE` のプロトコル仕様に沿って有効化すること | D-6 |
| F-4 | `select_folder()` 実行時に `HIGHESTMODSEQ` 応答を受信できた場合は保持し、専用の取得メソッドから参照できること。`select_folder()` 自体のシグネチャ（戻り値: `int`）は変更しない | D-10 |
| F-5 | `sync_account()` の `sync_folder()` に、新着同期・履歴backfillの**後**、`initial_sync_completed` なフォルダに対してのみ動くフラグリフレッシュ・ステップを追加する | D-2, D-3 |
| F-6 | `BaseMessageRepository` に、`internal_date >= since_internal_date` のメッセージについて `uid` / `imap_flags` / `flags_seen_at` を一括で返す `list_flag_refresh_items(...) -> Sequence[MessageRecord]` を追加する。ユースケースはこの結果からTTL切れUIDを判定し、N+1クエリを発生させない | D-4, D-5, D-8 |
| F-7 | フラグリフレッシュ時のDB更新: 変化があったメッセージは `update_flags` でフラグと `flags_seen_at` を更新する。`CONDSTORE` の差分照会が正常完了した場合、差分応答になかったTTL切れメッセージは「変更なし」として `touch_flags_seen_at` する。非 `CONDSTORE` のUID指定FETCHでは、応答があった未変更UIDだけをtouchし、応答欠落UIDはtouchしない | D-5, D-8 |
| F-8 | フラグリフレッシュはマニフェストへイベントを追記しないこと | D-9 |
| F-9 | `AppConfig` に `flag_refresh_enabled`（既定 `True`）・`flag_refresh_window_days`（既定30）・`flag_refresh_min_interval_seconds`（既定3600）を追加し、既存設定と同様にバリデーション・シリアライズ・既定値マージが行われること | D-4, D-5 |
| F-10 | `SyncOptions` に F-9 と同じ3項目を追加し、`__main__.py` / `sync_worker.py` / `main_window.py` の呼び出し箇所に反映すること | F-9 |
| F-11 | 一覧・詳細ビューの未読／フラグ表示ツールチップが、固定文言「同期時点のスナップショットです」から `flags_seen_at` を用いた具体的な確認日時（ローカル時刻）表示に変わること。`flags_seen_at` が `None` の場合は適切なフォールバック文言を表示すること | 目的 |
| F-12 | `MessageSummary` / `MessageDetail` に `flags_seen_at: datetime \| None` を追加し、一覧・詳細取得SQLと行マッピングでDB値を供給する | F-11 |
| F-13 | `highest_modseq` の更新は「値なし＝更新しない」と「`NULL`へリセット」を区別できる専用の `set_highest_modseq(folder_id, value: int \| None)` で行う | D-10, D-14 |

### **2.3 非機能要件・制約**

- フラグリフレッシュは新着同期・履歴backfillのクリティカルパスとは**独立したステップ**として実行し、失敗してもフォルダ全体・アカウント全体の同期を止めない（D-12）。
- レイヤー境界を守る: `CONDSTORE` 判定とIMAPコマンド発行は `infrastructure/fetchers`、対象範囲決定・間引きロジックは `usecases/sync_mail.py` に置く。`domain` 層は `RemoteMessageRef` の再利用のみで新規型を増やさない。
- `CancelToken` による中断に対応する（既存の `process_range` 等と同様）。キャンセル時は既にコミットしたフラグ更新を保持するが、`highest_modseq` は進めない。
- 既存の `BaseMessageRepository` に「目的を超えたメソッドを足さない」という制約（設計不変条件）を踏まえ、追加するメソッドは対象範囲の状態取得・フラグ更新・確認時刻一括更新・MODSEQ設定に限定する。
- 1回のフラグリフレッシュで発生するIMAP往復は、非 `CONDSTORE` 経路では「直近ウィンドウ内でTTLが切れた件数」にスケールさせる。`CONDSTORE` 経路はフォルダ全体の変更履歴を1コマンドで照会するが、本文・ヘッダを取得せず、DB更新は直近ウィンドウ内のローカルUIDに限定する。
- `CONDSTORE` の差分応答欠落は正常完了時に「変更なし」と解釈できるが、非 `CONDSTORE` のUID指定FETCHで応答が欠落したUIDは確認済みとして扱わない。リモート削除の確定は既存の削除検出ステップに委ねる。
- お名前.comの実サーバーは `CONDSTORE` 対応が確認済みだが、将来の契約・サーバー変更で非対応に戻る可能性もゼロではないため、フォールバック経路（D-7）を削除・簡略化せず維持する。

---

## **3. タスク**

### **Group A: ドメイン層とIMAPフェッチャー**

- [x] `BaseMailFetcher` に `iter_flags(raw_name, uids, *, cancel=None) -> Iterator[RemoteMessageRef]` を追加する
- [x] `BaseMailFetcher` に `iter_flags_since(raw_name, modseq, *, cancel=None) -> Iterator[RemoteMessageRef]` を追加する
- [x] `BaseMailFetcher` に `get_highest_modseq() -> int | None` を追加する（非対応時は `None`）
- [x] `BaseMailFetcher` の追加抽象メソッドに合わせ、`tests/support/fake_fetcher.py` の `FakeFetcher` と `tests/unit/test_fetcher.py` の `MinimalFetcher` を含む全テストダブルを更新する
- [x] `OnamaeImapFetcher` で `CONDSTORE` を `capabilities` に反映する
- [x] `select_folder()` で `CONDSTORE` サポート時に `(CONDSTORE)` パラメータまたは有効化を行い、`HIGHESTMODSEQ` / `NOMODSEQ` 応答を解釈して現在値を保持するアクセサを実装する
- [x] `imap_common.py` の `parse_fetch_response` を、literal のない単一 `bytes` レスポンス（`FLAGS` / `CHANGEDSINCE` の FETCH 結果）もパースできるように拡張する
- [x] `iter_flags` / `iter_flags_since` を `onamae_imap.py` に実装し、`_FETCH_CHUNK_SIZE` を再利用してバッチ取得を行う

### **Group B: DBスキーマとリポジトリ**

- [x] `migrations/004_flag_refresh.sql` を追加し、`folders.highest_modseq INTEGER` と候補検索用インデックス `messages(folder_id, uidvalidity, internal_date)` を追加する
- [x] `message_repository.py` の `_FOLDER_COLUMNS` に `highest_modseq` を追加し、`list_folders` / `list_sync_targets` からユースケースへ保存値を供給する
- [x] `BaseMessageRepository` に、`list_flag_refresh_items(account_id, folder_id, uidvalidity, since_internal_date) -> Sequence[MessageRecord]` を追加し、`uid` / `imap_flags` / `flags_seen_at` を返す
- [x] `BaseMessageRepository` に `update_flags(account_id, folder_id, uidvalidity, uid, imap_flags, flags_seen_at) -> None` を追加する
- [x] `BaseMessageRepository` に `touch_flags_seen_at(account_id, folder_id, uidvalidity, uids: Sequence[int], flags_seen_at: str) -> None` を追加し、N+1クエリを防ぐ一括更新を定義する
- [x] `BaseMessageRepository` に `set_highest_modseq(folder_id, value: int | None) -> None` を追加し、値の保存と明示的な `NULL` リセットを可能にする
- [x] `SqliteMessageRepository` に上記メソッド群を実装する（`touch_flags_seen_at` は SQLiteのパラメータ上限を考慮し500件単位でチャンク分割実行）
- [x] `initialize_sync_cursors` では `highest_modseq = NULL` にリセットし、`set_highest_modseq` は既存のバッチ内で利用できるようにする
- [x] `tests/support/in_memory_repository.py` に同メソッド群を追加する

### **Group C: 設定**

- [x] `AppConfig` に `flag_refresh_enabled` / `flag_refresh_window_days` / `flag_refresh_min_interval_seconds` を追加し、バリデーション・シリアライズ・既定値マージに組み込む
- [x] `SyncOptions` に同3項目を追加する
- [x] `__main__.py` / `presentation/threads/sync_worker.py` / `presentation/views/main_window.py` の `SyncOptions(...)` 呼び出しに新設定を反映する

### **Group D: 同期ユースケース**

- [x] `sync_mail.py` の `sync_folder()` に、新着・履歴backfill完了後、`initial_sync_completed` かつ `options.flag_refresh_enabled` が有効なフォルダに対してのみ動くフラグリフレッシュ・ステップを追加する
- [x] `list_flag_refresh_items` の結果から直近ウィンドウのUID→ローカルフラグの対応とTTL切れUIDを構築し、TTL切れUIDが0件ならIMAPコマンドを発行しない
- [x] `CONDSTORE` 対応時の経路を実装:
  - `folders.highest_modseq` が `NULL` の初回は、直近ウィンドウ内の全UIDを `iter_flags` で取得して基準スナップショットを作る
  - 保存済みMODSEQが有効な通常時は `iter_flags_since` でフォルダ全体の差分を取得し、直近ウィンドウ内のローカルUIDに該当する応答をTTLに関係なく `update_flags` する
  - 差分照会が正常完了したら、変化がなかったTTL切れUIDを `touch_flags_seen_at` する
  - 全FETCH正常完了後にのみ、サーバーの最新 `highest_modseq` を `set_highest_modseq` で最終バッチコミットする
  - `NOMODSEQ`、現在値欠落、保存値の後退・範囲異常時は `highest_modseq` を `NULL` にして初回基準点処理または非 `CONDSTORE` フォールバックへ移る
- [x] 非 `CONDSTORE` 時のフォールバック経路を実装:
  - TTL切れUIDのみを対象として `iter_flags` を実行する
  - `list_flag_refresh_items` で得たローカルフラグとリモートフラグを比較する
  - 変化があったメッセージは `update_flags`、応答があって変化がなかったメッセージは `touch_flags_seen_at`（N+1防止）で一括更新し、応答欠落UIDは更新しない
- [x] IMAP通信中は `BEGIN IMMEDIATE` を開始せず、DB更新のみ500件単位の短いバッチで実行する
- [x] フラグリフレッシュ中の `AuthenticationError` は再送出し、それ以外の `FetchError` は警告ログに残してフォルダ・アカウント全体の同期を失敗させない（`highest_modseq` は進めない）
- [x] `CancelToken` によるキャンセルは `OperationCancelledError` として既存のキャンセル処理へ渡し、その時点までにコミット済みの更新だけを保持して `highest_modseq` は進めない

### **Group E: GUI表示**

- [ ] `strings.py` の `TOOLTIP_UNREAD` / `TOOLTIP_IMAP_FLAGS` をフォーマット文字列に変更（例: `TOOLTIP_UNREAD = "未読 (確認日時: {seen_at})"`、`TOOLTIP_UNREAD_UNKNOWN = "未読 (同期時点のスナップショット)"`）
- [ ] `MessageSummary` / `MessageDetail` に `flags_seen_at: datetime | None` を追加し、`SqliteSearchRepository` の一覧SQL・詳細SQL・行マッピングから供給する
- [ ] `message_table_model.py` で `flags_seen_at` をローカル時刻表記に変換して未読・スターのツールチップを構築する
- [ ] 詳細ビューのヘッダ領域に読み取り専用の未読・スターステータス表示を追加し、同じ確認日時ツールチップを設定する（操作ボタンは追加しない）

### **Group F: テスト**

- [ ] `tests/unit/test_sync_mail.py` にフラグリフレッシュの単体テストを追加する（対象範囲、TTLによる実行抑止、初回基準点、CONDSTORE差分、TTL未到来差分、CONDSTORE応答なし時のtouch、非対応・NOMODSEQ・MODSEQ後退時のフォールバック、フォールバック応答欠落UID、認証失敗、一般FetchError、キャンセルを検証）
- [ ] `tests/unit/test_message_repository.py` に `list_flag_refresh_items`, `update_flags`, `touch_flags_seen_at`, `set_highest_modseq`、UIDVALIDITY変更時のリセット、候補検索インデックスの単体テストを追加する
- [ ] `tests/unit/test_fetcher.py` / `test_imap_common.py` に単一 bytes の FLAGS レスポンスパース、`HIGHESTMODSEQ` / `NOMODSEQ`、`iter_flags` / `iter_flags_since`、キャンセルのテストを追加する
- [ ] `tests/integration/` に結合テストを追加し、初回基準点の確立後、サーバー側のフラグ変更が次回同期で反映され、MODSEQが正常完了時だけ進むことを検証する
- [ ] `tests/unit/test_config.py` に新設定項目のテストを追加する
- [ ] `tests/gui/` の一覧・詳細ツールチップ表示テストを更新し、確認日時あり・なしの両方を検証する

### **Group G: ドキュメント整合**

- [ ] 開発計画書 4.6-2・9章の該当記述を「同期時点の一度きりのスナップショット」から「定期的に取り直すスナップショット（双方向同期・ローカル既読管理は引き続き行わない）」に改訂する
- [ ] `ruff check .` / `mypy .` / `pytest` を実行し、全テスト通過を確認する
