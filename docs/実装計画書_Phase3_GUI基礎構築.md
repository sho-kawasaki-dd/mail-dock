# **Phase 3: GUI基礎構築 (PySide6) 実装計画書**

対象: [ローカルメールバックアップ＆閲覧アプリ 開発計画書.md](./ローカルメールバックアップand閲覧アプリ開発計画書.md) の「6. 開発ロードマップ」における **Phase 3: GUI基礎構築 (PySide6)**

前提: [Phase 2: DB & 検索エンジン 実装計画書](./実装計画書_Phase2_DBと検索エンジン.md) の成果物（`BaseSearchRepository`・`SqliteSearchRepository`・`parse_query()`・keyset ページング・`CancelToken` 対応の検索経路・CLI `search`）が完成していること。

本書と開発計画書に矛盾がある場合は、**開発計画書を正**とする。

---

## **1. 目的**

**Phase 1 / Phase 2 で完成したヘッドレス基盤の上に、GUI だけで「導入 → 同期 → 閲覧 → 検索 → 保存」が一周できるデスクトップアプリを構築する。**

具体的には、以下を満たす。

1. **UIスレッドを一切ブロックしないこと** — EML取得・解析・DB書き込み・検索はすべてワーカースレッドで実行する。長時間処理には必ず進捗表示とキャンセルを設ける（開発計画書 5.4）。
2. **HTMLメール本文を敵性入力として扱うこと** — JavaScript無効化だけで済ませず、5層防御（オフレコプロファイル・属性無効化・リクエストインターセプタ・`cid:` スキーム・CSP注入）とナビゲーション制御をすべて実装する（同 4.6-3）。
3. **一覧を SQL 側だけで駆動すること** — Phase 2 の keyset ページングをそのまま使い、200件単位の遅延ロードで 60fps を維持する。UI側でオフセット計算・ソート・フィルタを行わない（同 4.6-1）。
4. **presentation 層を最外殻に閉じ込めること** — `views` / `viewmodels` / `models` は `usecases` と `domain` のみに依存し、infrastructure の具象を知らない。具象の組み立てはコンポジションルート1箇所に集約する。
5. **ローカル保存を安全に行うこと** — 添付ファイル名を敵性入力として完全サニタイズし、保存直前に `resolve_within()` でパストラバーサルを再検証する（同 4.6-4）。

**Phase 3 のゴール判定:** 空のストレージルートから GUI だけでルート初期化・アカウント登録・フォルダ選択・同期・検索・本文表示・添付保存まで一周でき、6章の検証項目がすべて成功し、CIが緑になること。

---

## **2. 要件**

### **2.1 前提となる意思決定（確定済み）**

| # | 項目 | 決定内容 |
| :--- | :---- | :---- |
| D-1 | HTML表示エンジン | **QtWebEngine を採用する（確定）**。開発計画書が挙げる「Phase 3 冒頭の `QTextBrowser` 試作による比較」は行わず、5層防御の完全実装を優先する。`QTextBrowser` には `QWebEngineUrlRequestInterceptor` / CSP / カスタムスキームに相当する機構が無く、要件 4.6-3 を満たせないため。起動時間・パッケージサイズの実測値は本書「7.」へ記録し、Phase 4 のパッケージング判断へ渡す |
| D-2 | 依存関係 | **追加しない**。`PySide6>=6.8`（QtWebEngine を含む Addons 込み）・dev の `pytest-qt`・`gui` マーカー・mypy の `mail_dock.presentation.*` override は Phase 0 で宣言済み |
| D-3 | UIアーキテクチャ | **QObject + Signal/Slot ベースの MVVM**。View は状態を持たず ViewModel の Signal に接続するだけとし、ViewModel を `pytest-qt` の `qtbot` でテストする |
| D-4 | 層の隔離 | `presentation/views` / `viewmodels` / `models` は **`sqlite3` と `infrastructure` を import しない**。具象の組み立ては `presentation/app.py` と `presentation/context.py`（コンポジションルート）だけが行う。静的テストで固定する |
| D-5 | スレッド構成 | **固定2本**（読み取り用 `QueryWorker` 1本 ＋ 書き込み用 `SyncWorker` 1本）。`QThreadPool` は使わない。SQLite接続の生成・破棄が増えるうえ、「書き込みは同期ワーカー1本に集約」という Phase 1 の不変条件が崩れるため。UIスレッドから `sqlite3` に触らない |
| D-6 | 一覧のページ取得 | **常に非同期**。`fetchMore()` はリクエストを発行するだけで、結果 Signal の到着時に `beginInsertRows()` する。リクエストIDで世代管理し、古い結果は破棄する。LIKE 経路（Phase 2 の `has_slow_path`）が最大3秒かかり得るため同期実行にしない |
| D-7 | GUI起動導線 | **`mail-dock gui` サブコマンドを追加**し、**サブコマンド無しの起動もGUI**とする。既存の CLI サブコマンドは変更しない |
| D-8 | UI文言 | **`presentation/strings.py` の定数モジュールへ集約**する。`tr()` と翻訳ファイルは使わない（単一言語のため） |
| D-9 | 外部画像の解除 | **メール単位・一時的**（別のメールを開くとリセット）。永続ホワイトリストは作らない。設定 `block_remote_images` は既定値のみを制御する |
| D-10 | HTMLサニタイズの置き場所 | **Qt非依存の純粋関数**として `infrastructure/parsing/html_sanitizer.py` に置く（既存依存の BeautifulSoup を使う）。presentation はこれを呼ぶだけとし、サニタイズを Qt無しでテストできる状態にする |
| D-11 | 本文HTMLの配信 | `setHtml()` を使わず **`maildock:` カスタムスキーム経由**で配信する（`setHtml()` の約2MB制限を避けるため。開発計画書 4.6-3）。`cid:` と合わせて2スキームを登録する。`QWebEngineUrlScheme.registerScheme()` は **`QApplication` 生成前**に呼ぶ必要があるため、`presentation/app.py` の先頭で実行する |
| D-12 | 表示用パート抽出 | 新規 `infrastructure/parsing/eml_render.py` を作り、既存の `parse_eml()` と**統合しない**。前者は表示のためにバイト列と `Content-ID` を保持し、後者は検索用の正規化テキストだけを返す。目的とメモリコストが異なるため |
| D-13 | 保存機能 | 添付保存と **1通の `.eml` 保存**を含める。mbox エクスポートは Phase 4。保存は usecase 化し、**保存直前に必ず `resolve_within()`** を呼ぶ。GUI は `QFileDialog` で宛先を得るだけ |
| D-14 | 左ペインの構成 | **「メールアカウント」ルートのみ描画**する。PST アーカイブルートは Phase 4.5。ツリーモデルは**ルートノードのリスト**を受け取る構造にし、Phase 4.5 で足せるようにする |
| D-15 | 設定画面の範囲 | **Phase 3 で実際に効く項目だけ**を表示する。未実装機能（purge モード・ゴミ箱猶予日数・ハートビート間隔・サーバー削除モード）は表示しない |
| D-16 | ストレージ切断 | **Signal 配線の骨組みのみ**。ワーカーが `StorageDetachedError` を受けたら Signal で MainWindow へ通知し、閲覧不可を表示する。状態機械・ハートビート・`WM_DEVICECHANGE` 監視は Phase 4 |
| D-17 | フラグ表示 | `imap_flags` は**表示専用**。既読・スターを UI から変更する導線を作らない。同期時点のスナップショットである旨をツールチップで明示する（開発計画書 4.6-2） |
| D-18 | 多重起動防止 | GUI でも CLI と同じ `StorageLock` 規約を使う。取得失敗時は `QMessageBox` を出して終了コード **3** で終了する |
| D-19 | CLI との共有 | ルート解決・`StorageLock` 取得・`migrate` の手順は `__main__.py` から**共有関数へ抽出**して GUI と共用する。重複実装を作らない |
| D-20 | GUIテストの実行 | **`gui` マーカーで環境変数によるオプトイン**とし、CI では実行しない（`-m "not docker and not gui"`）。`QWebEngineUrlScheme` の登録順序を再現するため、`QApplication` は**セッションスコープの共通フィクスチャ**で1つだけ生成する |
| D-21 | 起動時の自動同期 | `sync_on_startup` を **Phase 3 で実際に動かす**（起動直後に `SyncWorker` を1回起動する）。`sync_interval_minutes` による `QTimer` 定期実行とシステムトレイ常駐は Phase 4 |
| D-22 | 検索の実行タイミング | **Enter キー確定のみ**。入力停止デバウンスによる自動実行は行わない（LIKE 経路が重く、打鍵ごとにキャンセルと再実行が発生するため） |

### **2.2 機能要件**

| # | 要件 | 根拠（開発計画書） |
| :--- | :---- | :---- |
| F-1 | `mail-dock gui`、およびサブコマンド無しの `mail-dock` で GUI が起動し、既存の CLI サブコマンドの挙動が変わらないこと | D-7 |
| F-2 | ストレージルートが未設定・未接続の場合に初回セットアップウィザードが起動し、**ルート選択 → アカウント登録 → 同期対象フォルダ選択**を GUI だけで完了できること | 5.11 |
| F-3 | ウィザードのルート選択で、ドライブ種別・空き容量を確認し、暗号化状態が確認できない場合に警告を表示すること。`.maildock_root` の初期化を行うこと | 5.3 / 3.6 |
| F-4 | ウィザードのアカウント登録が `keyring` にのみ資格情報を保存し、DB・設定ファイルへ書き込まないこと。登録前に接続テストを実行できること | 5.3 |
| F-5 | メイン画面が**3ペイン**（左: アカウント／フォルダ、中央: 一覧、右: 本文プレビュー）で構成されること | 4.6-1 |
| F-6 | 左ペインが「メールアカウント」ルート配下に「すべてのアカウント」・各アカウント・各フォルダを表示し、選択が `MessageFilter` に反映されること | 4.6-1 / D-14 |
| F-7 | 一覧が `QAbstractTableModel` で **200件単位の遅延ロード**（`canFetchMore` / `fetchMore`）を行い、Phase 2 の `next_cursor` を**加工せずそのまま**次ページへ渡すこと | 4.6-1 / Phase 2 引き継ぎ |
| F-8 | 一覧の列に**アカウント列とフォルダ列**を含み、横断表示でも出所が分かること。ソートは `date_sent` 降順固定で SQL 側が担当すること | 4.6-1 / Phase 2 D-3 |
| F-9 | 一覧の視覚的ステータス表示が実装されていること: `remote_state='deleted'` グレーアウト、`'moved'` は移動先をツールチップ、`local_state='purged'` は「実体なし」、`\Seen` 無しは未読アイコン（同期時点のスナップショットである旨のツールチップ付き）、`\Flagged` はスターアイコン | 4.6-2 |
| F-10 | 検索ボックスの入力が **`parse_query()` 経由**でのみ正規化されること。UI 側で別の正規化を挟まないこと | 4.5 / Phase 2 引き継ぎ |
| F-11 | `SearchQueryError` が例外ダイアログではなく検索ボックス近傍のインラインメッセージとして提示されること | Phase 2 F-18 |
| F-12 | `has_slow_path` が立った場合に「短い語を含むため時間がかかる場合があります」を表示し、キャンセルボタンを有効にすること | 4.5-3 / 5.4 |
| F-13 | EML取得・解析・DB読み書き・検索・同期がすべてワーカースレッドで実行され、UIがフリーズしないこと。同期中も閲覧・検索が通常どおり動作すること | 5.4 |
| F-14 | 同期の進捗（転送バイト数・件数・現在フォルダ・ETA）が表示され、キャンセルボタンで `CancelToken` により中断できること | 5.4 |
| F-15 | 本文プレビューが HTML メールの5層防御をすべて実装していること: ①オフレコプロファイル ②危険な属性の無効化 ③リクエストインターセプタ ④`cid:` スキームハンドラ ⑤CSP注入 | 4.6-3 |
| F-16 | リンククリックがアプリ内で遷移せず、確認のうえ `QDesktopServices.openUrl()` で外部ブラウザへ転送されること。`<meta http-equiv="refresh">` が事前に除去されること | 4.6-3 |
| F-17 | 外部画像が既定でブロックされ、バナーの「画像を読み込む」で**そのメールだけ一時的に**解除できること。別のメールを開くと再びブロックされること | 4.6-3 / 5.11 / D-9 |
| F-18 | 本文プレビュー上部に「この会話のN件を表示」があり、`list_thread()` の結果を一覧に表示できること | 4.5 |
| F-19 | 添付ファイル一覧から任意の場所へ保存でき、`sanitize_attachment_name()` と `resolve_within()` を経由すること。実行可能拡張子の場合は保存前に警告を表示すること | 4.6-4 |
| F-20 | 1通を `.eml` としてエクスポートでき、書き出し前に `file_hash` を検証すること | 4.6-4 |
| F-21 | `local_state='purged'` のメッセージで本文プレビューが実体なし表示になり、EML 読み取りを試みないこと。`sync_failures.oversize` のメッセージにバッジが出ること | 4.6-2 / 4.4 |
| F-22 | 設定画面から 1通サイズ上限・外部画像ブロック・起動時同期・起動時整合性チェックを変更でき、`config.save()` で永続化されること。ログフォルダを開く導線があること | 5.11 / 5.5 |
| F-23 | `StorageLock` の取得に失敗した場合に「他のインスタンスが使用中です」を表示して起動を中止すること | 3.6 |
| F-24 | ワーカーが `StorageDetachedError` を送出した場合に Signal で MainWindow へ伝播し、閲覧不可を通知できること（骨組み） | 5.7.1 / D-16 |
| F-25 | `MailDockError` 階層が対応表を経由してユーザー向け文言と復旧導線に変換され、トレースバックが画面に出ないこと | 5.6 |
| F-26 | `views` / `viewmodels` / `models` が `sqlite3` と `infrastructure` を import しないこと | 2.2 / D-4 |

### **2.3 非機能要件・制約**

| # | 指標 | 目標値 | 備考 |
| :--- | :---- | :---- | :---- |
| N-1 | 一覧スクロール | **60fps** | 200件単位の遅延ロード必須（開発計画書 5.1） |
| N-2 | アプリ起動時間 | **3秒以内** | QtWebEngine 採用時は別途評価（同 5.1） |
| N-3 | メモリ使用量 | **同期中も600MB以下** | 1通2MB都度バッファ上限管理（同 5.1） |
| N-4 | UI応答 | 長時間処理中もUIが操作可能 | 進捗表示とキャンセルを必ず設ける（同 5.4） |

* `mypy` を通すこと。`presentation` 配下は Phase 0 の override（`disallow_untyped_defs = false`）の範囲内で、可能な限り型注釈を付ける。`# type: ignore` を使う場合は理由をコメントで併記する。
* レイヤーの依存方向を厳守する（`domain` ← `usecases` ← `presentation`）。`domain` / `usecases` に PySide6 を import しない。
* `BaseSearchRepository` は読み取り専用のまま維持する。ViewModel から書き込みが必要になっても、このポートにメソッドを足さない（Phase 2 引き継ぎ）。
* 本文テキストをログへ出力しない。件名・メールアドレスは Phase 0 の `MaskingFilter` を通す。
* Qt のウィジェット生成は必ずUIスレッドで行う。ワーカースレッドから View を直接操作しない（Signal/Slot のみ）。

---

## **3. タスク**

> 依存関係: **A → (B・C・D を並行) → E → F**。G（テスト）は各グループと並行して作成する。

### **3.1 グループA: アプリ基盤とシェル（*本フェーズの前提。最優先*）**

#### **A-1. `__main__.py` — コンポジションルートの共有化と `gui` 起動導線**

- [ ] ルート解決（`_select_root`）・`StorageLock` 取得・`ConnectionManager` 生成・`migrate` 実行・設定書き戻し・後始末を、CLI と GUI が共用できる関数（またはコンテキストマネージャ）へ抽出する
- [ ] 抽出により既存 CLI サブコマンドの挙動が変わらないことを既存テストで確認する（重複実装を作らない。D-19）
- [ ] `gui` サブコマンドを追加する
- [ ] サブコマンド無しで起動した場合に GUI を起動する（従来のヘルプ表示から変更する）
- [ ] `_exit_code()` の既存マッピングを GUI 経路でも使う（`StorageLockedError` → 3 等）
- [ ] GUI 起動時に `presentation` を import する（CLI 経路で PySide6 を import しない**遅延 import** とする）

#### **A-2. `presentation/app.py` — GUI エントリポイント**

- [ ] `run_gui(...) -> int` を実装する
- [ ] **`QApplication` 生成の前に** `presentation/web/schemes.py` の `register_schemes()` を呼ぶ（`cid` / `maildock` の `QWebEngineUrlScheme` 登録。D-11）
- [ ] ストレージルートが未設定・未解決の場合に初回セットアップウィザードを起動する
- [ ] `StorageLock` の取得に失敗した場合に `QMessageBox` を表示して終了コード 3 を返す（F-23）
- [ ] `AppContext` を構築して `MainWindow` を表示し、`app.exec()` の戻り値を返す
- [ ] 終了時にワーカースレッドを停止し、`ConnectionManager` を閉じ、`StorageLock` を解放する
- [ ] `sync_on_startup` が真なら起動直後に同期を1回起動する（D-21）

#### **A-3. `presentation/context.py` — GUI用コンポジションルート**

- [ ] `AppContext`（storage_root / `StorageLock` / `ConnectionManager` / `AppConfig` / `KeyringCredentialStore` / `EmlStorage` / `ManifestWriter` / repo・fetcher のファクトリ）を実装する
- [ ] repo とストレージの生成を**呼び出しスレッド側で行うファクトリ**として公開する（接続をスレッド間で共有しない）
- [ ] 設定変更を `config.save()` で永続化するメソッドを持たせる
- [ ] `views` / `viewmodels` / `models` は `AppContext` を通してのみ具象へ到達することを docstring に明記する（D-4）

#### **A-4. `presentation/strings.py` — UI文言定数**

- [ ] 画面タイトル・ボタン・メニュー・列名・バナー・エラーメッセージの文言を定数として定義する
- [ ] 文言のハードコードをこのモジュール以外に置かない（D-8）

#### **A-5. `presentation/threads/worker.py` — ワーカー基盤**

- [ ] `QObject` ワーカー ＋ `QThread` の `moveToThread` パターンで基底クラスを実装する
- [ ] タスクを Signal で受け、結果／エラーを Signal で返す（例外はスレッドを越えて raise しない）
- [ ] 例外は `MailDockError` に正規化して `failed` Signal で返す
- [ ] スレッド終了時に、そのスレッドが開いた SQLite 接続を必ず閉じる
- [ ] `stop()` で `CancelToken` を立ててからスレッドを終了する（`quit()` + `wait()`）

#### **A-6. `presentation/threads/query_worker.py` — 読み取り専用ワーカー**

- [ ] `list_messages` / `search_messages` / `count_messages` / `list_thread` / `get_message` / `open_message` の要求を受ける
- [ ] 要求ごとに **リクエストID** を採番し、結果 Signal に含める（D-6 の世代管理用）
- [ ] 新しい要求を受けたら実行中の要求の `CancelToken` を立てる
- [ ] `OperationCancelledError` は失敗ではなく「キャンセル済み」として扱い、UIにエラーを出さない
- [ ] `sqlite3` を直接使わず、`usecases/search_messages.py` 経由でのみ呼ぶ

#### **A-7. `presentation/threads/sync_worker.py` — 書き込みワーカー**

- [ ] `sync_account()` と `refresh_folders()` を実行する（書き込みはこの1本に集約。D-5）
- [ ] `on_progress` の `SyncProgress` を **100ms間引き**して Signal へ中継する（毎通 Signal を出さない）
- [ ] `CancelToken` によるキャンセルを受け付け、`SyncResult.cancelled` を UI へ返す
- [ ] `StorageDetachedError` を専用 Signal で通知する（F-24）
- [ ] `AuthenticationError` / `FetchError` を対応表経由の文言で通知する

#### **A-8. `presentation/errors.py` — 例外→UI文言の対応表**

- [ ] `MailDockError` 階層（`StorageDetachedError` / `StorageLockedError` / `InsufficientSpaceError` / `DatabaseError` / `AuthenticationError` / `TransientError` / `PermanentError` / `OversizeError` / `CredentialStoreError` / `SearchQueryError` / `OperationCancelledError` 等）に対する文言と復旧導線の対応表を定義する
- [ ] 未知の例外は汎用文言＋「ログを開く」導線にフォールバックする
- [ ] トレースバックを画面へ出さず、ログにのみ記録する（F-25）

---

### **3.2 グループB: 一覧（左ペイン・中央ペイン）**

#### **B-1. `presentation/models/message_table_model.py`**

- [ ] `MessageTableModel(QAbstractTableModel)` を実装する
- [ ] 列: 日付 / アカウント / フォルダ / 差出人 / 件名 / サイズ（F-8）
- [ ] `canFetchMore()` は「`exhausted` が偽」かつ「取得要求が飛んでいない」ときに真を返す
- [ ] `fetchMore()` は `QueryWorker` へ要求を出すだけで、DB に触らない（D-6）
- [ ] 結果 Signal の到着時に `beginInsertRows()` / `endInsertRows()` で行を追加する
- [ ] **リクエストIDが最新でない結果は破棄する**（フィルタ変更や検索実行と競合したときの混線防止）
- [ ] `next_cursor` を加工せず保持し、そのまま次の要求へ渡す（Phase 2 引き継ぎ）
- [ ] フィルタ・検索条件が変わったら `beginResetModel()` でモデルを初期化し、カーソルを捨てる
- [ ] `limit=200` を既定とする

#### **B-2. 視覚的ステータス表示（F-9）**

- [ ] `remote_state='deleted'`: `ForegroundRole` でグレーアウト ＋ 削除済みアイコン
- [ ] `remote_state='moved'`: `ToolTipRole` に移動先フォルダ名
- [ ] `local_state='purged'`: 「実体なし」表示
- [ ] `imap_flags` に `\Seen` が無い: 未読アイコン ＋ 「同期時点のスナップショット」ツールチップ
- [ ] `imap_flags` に `\Flagged`: スターアイコン
- [ ] `sync_failures` の `oversize`: 「未取得（サイズ上限超過）」バッジ
- [ ] フラグを変更する導線を作らない（D-17）

#### **B-3. `presentation/models/folder_tree_model.py`**

- [ ] 「メールアカウント」ルート → 「すべてのアカウント」／各アカウント → 各フォルダのツリーを構築する
- [ ] **ルートノードのリスト**を受け取る構造にする（Phase 4.5 の PST ルート追加に備える。D-14）
- [ ] 表示名は `folders.display_name` を使い、非同期対象フォルダも表示する（同期対象かどうかを区別できるようにする）
- [ ] 選択が `MessageFilter`（`account_ids` / `folder_ids`）へ変換されること

#### **B-4. `presentation/viewmodels/message_list_viewmodel.py`**

- [ ] 現在の `MessageFilter`・検索クエリ・検索モード・選択メッセージIDを保持する
- [ ] フィルタ変更・検索実行・行選択を Signal として公開する
- [ ] `QueryWorker` への要求発行と結果の受け取りを担当する
- [ ] `sqlite3` / infrastructure を import しない（F-26）

#### **B-5. 検索ボックスの配線**

- [ ] **Enter キー確定でのみ検索を実行する**（D-22）
- [ ] `parse_query()` 経由でのみ正規化する。UI 側で別の正規化を行わない（F-10）
- [ ] `SearchQueryError` を検索ボックス近傍のインラインメッセージとして表示する（F-11）
- [ ] `has_slow_path` が立ったら警告バナーを表示し、キャンセルボタンを有効化する（F-12）
- [ ] 検索モード（AND / OR）の切り替えを提供する
- [ ] 構造化フィルタ（日付範囲・添付有無）をツールバーから指定できるようにする
- [ ] 検索をクリアすると `list_messages` の一覧へ戻ること

#### **B-6. `presentation/views/message_list.py`**

- [ ] `QTableView` の設定（行高固定・列幅・選択単位＝行・ヘッダによるソートを無効化）
- [ ] スクロールで `fetchMore()` が発火することを確認する
- [ ] スレッド表示（`list_thread` の結果）を同じビューで表示できるようにする

---

### **3.3 グループC: 詳細ペインと HTML 5層サンドボックス**

#### **C-1. `domain/messages.py` — 表示用データ構造の追加**

- [ ] `MessagePart`（frozen dataclass）を追加する: `content_id: str | None` / `content_type: str` / `filename: str | None` / `payload: bytes` / `is_inline: bool`
- [ ] `RenderedMessage`（frozen dataclass）を追加する: `html_body: str | None` / `text_body: str` / `parts: tuple[MessagePart, ...]`
- [ ] 外部依存がゼロであることを維持する

#### **C-2. `infrastructure/parsing/eml_render.py` — 表示用パート抽出**

- [ ] `extract_render_parts(raw: bytes) -> RenderedMessage` を実装する
- [ ] `text/html` パートを（デコードして）`html_body` に、`text/plain` を `text_body` に格納する
- [ ] `Content-ID` の山括弧を除去して `MessagePart.content_id` に格納する
- [ ] 添付・インラインの判定は `eml_parser.py` と同じ規則（`Content-Disposition` と `Content-ID`）に揃える
- [ ] 文字コードのデコードは既存の `charset.decode_text()` を使う（フォールバック順序を崩さない）
- [ ] `parse_eml()` と統合しない理由（表示用はバイト列を保持し、検索用は正規化テキストのみ）を docstring に明記する（D-12）

#### **C-3. `infrastructure/parsing/html_sanitizer.py` — CSP注入とタグ除去（*Qt非依存*）**

- [ ] `sanitize_mail_html(html: str, *, allow_remote_images: bool) -> str` を実装する
- [ ] `<head>` へ CSP の `<meta http-equiv="Content-Security-Policy">` を**強制挿入**する
  - [ ] 既定: `default-src 'none'; img-src cid:; style-src 'unsafe-inline'; form-action 'none'; frame-src 'none'`
  - [ ] `allow_remote_images=True` のとき `img-src` に `https:` / `http:` を加える
  - [ ] 本文中に既存の CSP `<meta>` があれば除去してから挿入する（上書きされないため）
- [ ] `<meta http-equiv="refresh">` を除去する（F-16）
- [ ] `<script>` / `<iframe>` / `<frame>` / `<object>` / `<embed>` / `<form>` / `<link>` / `<base>` を除去する
- [ ] `on*` イベント属性と `javascript:` / `data:` の `href` / `src` を除去する
- [ ] `<head>` が無いHTML断片でも正しく `<html><head>` を補って処理すること
- [ ] Qt を import せず、純粋関数として単体テストできること（D-10）

#### **C-4. `usecases/open_message.py` — 閲覧ユースケース**

- [ ] `OpenedMessage`（frozen dataclass）を定義する: `detail: MessageDetail` / `rendered: RenderedMessage`
- [ ] `open_message(search_repo, storage, *, message_id) -> OpenedMessage` を実装する
- [ ] `local_state='purged'` または `relative_path` が無い場合は明示的なドメイン例外で拒否し、EML の読み取りを試みない（F-21）
- [ ] `file_hash` を検証し、不一致は `StorageError` 系で拒否する
- [ ] Phase 1 と同じ呼び出し規約（ポートを位置引数、以降 keyword-only）
- [ ] `sqlite3` / PySide6 / infrastructure の具象を import しない

#### **C-5. `presentation/web/profile.py` — 層1・層2**

- [ ] 名前無しの `QWebEngineProfile()` を生成し、**オフレコ**であることを確認する
- [ ] `setHttpCacheType(NoCache)` / `setPersistentCookiesPolicy(NoPersistentCookies)` を設定する
- [ ] `QWebEngineSettings` で以下をすべて `False` にする: `JavascriptEnabled` / `LocalStorageEnabled` / `PluginsEnabled` / `LocalContentCanAccessRemoteUrls` / `LocalContentCanAccessFileUrls` / `AllowRunningInsecureContent` / `ScreenCaptureEnabled` / `FullScreenSupportEnabled` / `AutoLoadIconsForPage`
- [ ] プロファイルの寿命をアプリと同じにし、`QWebEnginePage` より長く保持する（先に破棄するとクラッシュするため）

#### **C-6. `presentation/web/interceptor.py` — 層3**

- [ ] `MailUrlRequestInterceptor(QWebEngineUrlRequestInterceptor)` を実装する
- [ ] **既定で全リクエストをブロック**し、`cid` / `maildock` スキームのみ許可する
- [ ] 外部画像の一時解除中は、`resourceType()` が画像のときに限り `http` / `https` を許可する（D-9）
- [ ] 解除状態はメールを切り替えるとリセットされること
- [ ] プロファイルへ `setUrlRequestInterceptor()` で設定する

#### **C-7. `presentation/web/schemes.py` — 層4**

- [ ] `register_schemes()` を実装し、`cid` と `maildock` を `QWebEngineUrlScheme` として登録する（**`QApplication` 生成前に呼ぶ**。D-11）
- [ ] `CidSchemeHandler(QWebEngineUrlSchemeHandler)`: 現在表示中メッセージの `MessagePart` から `Content-ID` 一致のパートを `QBuffer` で返す
- [ ] `MailBodySchemeHandler`: 本文HTMLを配信する（`setHtml()` の約2MB制限回避）
- [ ] 未知の `cid` は 404 相当で失敗させる（外部へ問い合わせない）
- [ ] メッセージ切り替え時に前のメッセージのパートを破棄する（メモリ滞留の防止。N-3）
- [ ] `installUrlSchemeHandler()` をプロファイルへ設定する

#### **C-8. `presentation/web/page.py` — 層6（ナビゲーション制御）**

- [ ] `MailPage(QWebEnginePage)` で `acceptNavigationRequest()` をオーバーライドする
- [ ] 初回の本文表示（`maildock:` へのナビゲーション）のみ許可する
- [ ] リンククリックは URL を提示する確認ダイアログを経て `QDesktopServices.openUrl()` へ渡し、**アプリ内では遷移させない**（F-16）
- [ ] `javaScriptAlert` 等のダイアログ系を無効化する
- [ ] 証明書エラー・認証要求を拒否する

#### **C-9. `presentation/views/detail_view.py`**

- [ ] ヘッダ領域（件名・差出人・宛先・Cc・日付・アカウント・フォルダ）を表示する
- [ ] 外部画像がブロックされている場合にバナーと「画像を読み込む」ボタンを表示する（F-17）
- [ ] 添付ファイル一覧（ファイル名・サイズ・種別）を表示し、インライン画像を添付一覧に混ぜない
- [ ] 「この会話のN件を表示」を表示し、`list_thread()` の結果を一覧へ反映する（F-18）
- [ ] `local_state='purged'` は代替表示にし、EML を読まない（F-21）
- [ ] `sync_failures.oversize` のメッセージは「未取得（サイズ上限超過）」を表示する
- [ ] 本文の読み込みは `QueryWorker` 経由の非同期とし、UIをブロックしない

---

### **3.4 グループD: 同期とメイン画面**

#### **D-1. `presentation/views/main_window.py`**

- [ ] `QSplitter` による3ペインレイアウトを実装する（F-5）
- [ ] ツールバー（同期・フォルダ再取得・検索ボックス・検索モード・フィルタ・設定）
- [ ] ステータスバー（進捗・件数・キャンセルボタン・ストレージ状態）
- [ ] メニュー（ファイル: `.eml` として保存 / 終了、表示: スレッド表示、ヘルプ: ログフォルダを開く）
- [ ] ペイン幅と列幅を `QSettings` に保存・復元する
- [ ] ウィンドウを閉じるときにワーカーの停止を待つ

#### **D-2. 同期の実行と進捗（F-14）**

- [ ] ツールバーの「同期」で `SyncWorker` を起動し、多重起動を防ぐ
- [ ] 進捗（転送バイト数・件数・現在フォルダ・ETA）をステータスバーに表示する
- [ ] キャンセルボタンで `CancelToken` を立て、`SyncResult.cancelled` を結果表示に反映する
- [ ] 完了時に取得件数・スキップ件数・失敗件数をサマリ表示し、一覧を更新する
- [ ] 起動時同期（`sync_on_startup`）を実行する（D-21）

#### **D-3. フォルダ再取得**

- [ ] 「フォルダを再取得」で `refresh_folders()` を実行し、新規フォルダ数と消失フォルダを表示する
- [ ] 新規フォルダが既定で同期対象にならないことを UI 上で明示する

#### **D-4. 同期中の読み取り（F-13）**

- [ ] 同期中も一覧のスクロール・検索・本文表示が動作することを確認する
- [ ] 読み取りワーカーと書き込みワーカーがそれぞれ独立した接続を使うこと（`ConnectionManager` の `threading.local`）

#### **D-5. ストレージ切断の Signal 配線（骨組み。F-24 / D-16）**

- [ ] ワーカーの `StorageDetachedError` を `storage_detached` Signal として MainWindow へ伝播する
- [ ] 受信時に閲覧不可のバナーを出し、同期ボタンを無効化する
- [ ] 状態機械・ハートビート・`WM_DEVICECHANGE` は Phase 4 である旨をコメントに残す

---

### **3.5 グループE: ウィザードと設定**

#### **E-1. `presentation/views/setup_wizard.py`（F-2 / F-3 / F-4）**

- [ ] ページ1: ストレージルート選択
  - [ ] `QFileDialog` でディレクトリを選択する
  - [ ] `drive_kind()` でドライブ種別を表示し、`check_free_space()` で空き容量を確認する
  - [ ] 暗号化状態を確認できない場合に警告を表示する（BitLocker To Go 前提。開発計画書 5.3）
  - [ ] 既存の `.maildock_root` があれば UUID を照合して再利用し、無ければ `initialize_root()` で初期化する
  - [ ] 他インスタンスがロック中なら明示的に伝える
- [ ] ページ2: アカウント登録
  - [ ] `account_id` / ホスト / ポート / ユーザー名 / パスワード / 表示名を入力する
  - [ ] 「接続テスト」で `BaseMailFetcher.connect()` をワーカー経由で実行し、結果を表示する
  - [ ] `register_account()` を呼び、パスワードは `keyring` にのみ保存する
  - [ ] `validate_account_id()` の失敗をインライン表示する
- [ ] ページ3: 同期対象フォルダ選択
  - [ ] `refresh_folders()` を実行し、フォルダ一覧をチェックボックスで提示する
  - [ ] `set_sync_target()` で選択を反映する
  - [ ] 新規フォルダが既定で非対象であることを説明文で明示する
- [ ] ウィザードのキャンセルで不完全な状態が残らないこと（ルート初期化済みなら設定へ書き戻す）
- [ ] 完了後に設定を `config.save()` で永続化する

#### **E-2. `presentation/views/dialogs/settings_dialog.py`（F-22 / D-15）**

- [ ] 1通あたりサイズ上限（`max_message_bytes`、既定 50MB）
- [ ] 外部画像の読み込み（`block_remote_images`、既定ブロック）
- [ ] 起動時に同期する（`sync_on_startup`）
- [ ] 起動時の整合性チェック（`startup_verification`: quick / full）
- [ ] ログフォルダを開く（`QDesktopServices.openUrl()`）
- [ ] アカウント一覧の表示・追加、同期対象フォルダの再編集
- [ ] Phase 4 / 4.5 の設定項目（purge モード・ゴミ箱猶予日数・ハートビート間隔・サーバー削除モード・PST取込設定）は**表示しない**

#### **E-3. `presentation/views/dialogs/` — 共通ダイアログ**

- [ ] エラーダイアログ（`presentation/errors.py` の対応表を使用。詳細はログへ）
- [ ] 確認ダイアログ（外部リンクを開く・実行可能ファイルを保存する・上書きする）
- [ ] 進捗ダイアログ（キャンセル付き。接続テスト・フォルダ再取得で使用）

---

### **3.6 グループF: 保存機能**

#### **F-1. `usecases/save_attachment.py`**

- [ ] `SavedFile`（frozen dataclass）を定義する: `path: Path` / `warnings: tuple[str, ...]` / `is_executable: bool`
- [ ] `save_attachment(storage, *, relative_path, part_index, dest_dir, filename=None) -> SavedFile` を実装する
- [ ] `sanitize_attachment_name()` を必ず経由する
- [ ] **保存直前に `resolve_within(dest_dir, name)` を呼び**、宛先が指定ディレクトリ配下であることを再検証する（F-19）
- [ ] 同名ファイルが存在する場合は連番を付ける（呼び出し側で上書き確認済みの場合を除く）
- [ ] 一時ファイルへ書き込んでから `os.replace` で配置する
- [ ] `sqlite3` / PySide6 を import しない

#### **F-2. `usecases/export_message.py`**

- [ ] `export_eml(storage, *, relative_path, expected_hash, dest_path) -> Path` を実装する
- [ ] 書き出し前に `file_hash` を検証する（F-20）
- [ ] 一時ファイル ＋ `os.replace` で配置する

#### **F-3. UI 配線**

- [ ] 添付一覧のコンテキストメニュー／ボタンから `QFileDialog.getExistingDirectory()` で保存先を選ぶ
- [ ] `SanitizedName.is_executable` が真なら**保存前に**警告ダイアログを出す（`.exe .scr .js .vbs .lnk .bat .cmd .ps1`）
- [ ] サニタイズで名前が変わった場合にその旨を通知する
- [ ] 「.eml として保存」を `QFileDialog.getSaveFileName()` から実行する
- [ ] 保存処理をワーカーで実行し、UIをブロックしない

---

### **3.7 グループG: テスト・CI・ドキュメント（*各グループと並行して作成*）**

#### **G-1. 単体テスト（Qt不要・CIで実行）**

- [ ] `tests/unit/test_html_sanitizer.py`
  - [ ] CSP `<meta>` が `<head>` へ挿入されること、既存の CSP `<meta>` が除去されること
  - [ ] `allow_remote_images` で `img-src` が切り替わること
  - [ ] `<meta http-equiv="refresh">` が除去されること
  - [ ] `<script>` / `<iframe>` / `<object>` / `<embed>` / `<form>` / `<link>` / `<base>` が除去されること
  - [ ] `on*` 属性と `javascript:` / `data:` の `href` / `src` が除去されること
  - [ ] `<head>` の無い断片・空文字・不正なHTMLでも例外にならないこと
- [ ] `tests/unit/test_eml_render.py`
  - [ ] `text/html` と `text/plain` の両方を持つメールで両方が取得できること
  - [ ] `Content-ID` の山括弧が除去されること
  - [ ] インライン画像と通常添付が区別されること（`eml_parser` と同じ規則）
  - [ ] ISO-2022-JP / CP932 のHTML本文が正しくデコードされること
- [ ] `tests/unit/test_open_message.py`（Fake ポートのみ）
  - [ ] `purged` と `relative_path` 欠如で拒否され、EML を読まないこと
  - [ ] `file_hash` 不一致で拒否されること
- [ ] `tests/unit/test_save_attachment.py`
  - [ ] パストラバーサル名・NTFS禁止文字・予約名・末尾ドットがサニタイズされること
  - [ ] 実行可能拡張子で `is_executable` が真になること
  - [ ] `resolve_within` により宛先ディレクトリ外へ書けないこと
  - [ ] 同名ファイルの連番付与
- [ ] `tests/unit/test_export_message.py`: ハッシュ不一致で拒否、正常時に原本と同一バイト列
- [ ] `tests/unit/test_presentation_errors.py`: 例外階層の全クラスが対応表に存在し、未知例外がフォールバックすること
- [ ] `tests/unit/test_ports.py`（既存を拡張）: `presentation/views` / `viewmodels` / `models` に `sqlite3` と `mail_dock.infrastructure` の import が無いことを静的に確認する（F-26）
- [ ] `tests/unit/test_main.py`（既存を拡張）: `gui` サブコマンドとサブコマンド無しが GUI 起動を呼ぶこと（`run_gui` をモックし、PySide6 を import せずに検証する）。既存サブコマンドの挙動が変わらないこと

#### **G-2. GUIテスト（`gui` マーカー／ローカル手動）**

- [ ] `tests/conftest.py` に `gui` マーカーのスキップ条件を追加する（`docker` と同じ形。環境変数でオプトイン。D-20）
- [ ] **セッションスコープの `QApplication` フィクスチャ**を用意し、その前に `register_schemes()` を呼ぶ（本番と同じ初期化順を再現する。D-20）
- [ ] `tests/gui/test_message_table_model.py`
  - [ ] `fetchMore` が非同期で行を追加すること
  - [ ] 全ページを連結した結果が `list_messages` の一括取得と**完全一致**すること（重複・欠損ゼロ）
  - [ ] `date_sent` NULL 混在・同一 `date_sent` でも成立すること
  - [ ] 古いリクエストIDの結果が破棄されること
  - [ ] フィルタ変更でモデルがリセットされ、カーソルが捨てられること
- [ ] `tests/gui/test_folder_tree_model.py`: ツリー構造と選択→`MessageFilter` 変換
- [ ] `tests/gui/test_query_worker.py`: 新規要求で前要求がキャンセルされること、`OperationCancelledError` がエラー通知されないこと
- [ ] `tests/gui/test_sync_worker.py`: 進捗の間引き、キャンセル、`StorageDetachedError` の Signal 伝播
- [ ] `tests/gui/test_web_sandbox.py`
  - [ ] プロファイルがオフレコで、キャッシュ・永続クッキーが無効なこと
  - [ ] JavaScript ほか危険な属性がすべて無効なこと
  - [ ] インターセプタが `http` / `https` / `file` をブロックし、`cid` / `maildock` を通すこと
  - [ ] 外部画像を解除すると画像リクエストのみ通り、メールを切り替えると再びブロックされること
  - [ ] `cid:` ハンドラがインライン画像を返し、未知の `cid` を失敗させること
  - [ ] 2MB を超える本文HTMLが `maildock:` 経由で表示できること
  - [ ] リンククリックがアプリ内遷移せず外部ブラウザ導線へ渡ること（`QDesktopServices` をモック）
- [ ] `tests/gui/test_detail_view.py`: `purged` の代替表示、添付一覧、「この会話のN件を表示」
- [ ] `tests/gui/test_main_window.py`: 3ペイン構成、同期ボタンの多重起動防止、終了時のワーカー停止
- [ ] `tests/gui/test_setup_wizard.py`: 3ページの遷移とバリデーション（`FakeFetcher` と一時ルートを使用）
- [ ] `tests/gui/test_settings_dialog.py`: 変更が `config.save()` へ反映されること、Phase 4 項目が表示されないこと

#### **G-3. CI**

- [ ] `lint` / `test-windows` / `test-linux` の3ジョブを `-m "not docker and not gui"` で実行する
- [ ] GUIテストが CI で実行されないことを確認する
- [ ] 新規モジュールが `ruff` / `mypy` を通ることを確認する

#### **G-4. ドキュメント**

- [ ] `README.md` に GUI の起動方法（`mail-dock` / `mail-dock gui`）を追記する
- [ ] `README.md` に GUIテストのローカル実行方法（環境変数・オフスクリーン実行）を追記する
- [ ] `THIRD-PARTY-LICENSES.md` に QtWebEngine（Chromium）関連の記載を追加する

---

## **4. 主要成果物**

| パス | 内容 | タスク |
| :---- | :---- | :---- |
| `src/mail_dock/__main__.py` | コンポジションルートの共有化・`gui` サブコマンド | A-1 |
| `src/mail_dock/presentation/app.py` | GUIエントリポイント・スキーム登録・起動シーケンス | A-2 |
| `src/mail_dock/presentation/context.py` | GUI用コンポジションルート（`AppContext`） | A-3 |
| `src/mail_dock/presentation/strings.py` | UI文言定数 | A-4 |
| `src/mail_dock/presentation/errors.py` | 例外→UI文言の対応表 | A-8 |
| `src/mail_dock/presentation/threads/` | ワーカー基盤・読み取りワーカー・同期ワーカー | A-5 〜 A-7 |
| `src/mail_dock/presentation/models/` | 一覧テーブルモデル・フォルダツリーモデル | B-1 / B-3 |
| `src/mail_dock/presentation/viewmodels/` | 一覧・詳細・同期・セットアップの ViewModel | B-4 |
| `src/mail_dock/presentation/views/` | メイン画面・一覧・詳細・ウィザード・ダイアログ | B-6 / C-9 / D-1 / E-1 〜 E-3 |
| `src/mail_dock/presentation/web/` | プロファイル・インターセプタ・スキームハンドラ・ページ | C-5 〜 C-8 |
| `src/mail_dock/domain/messages.py` | `MessagePart` / `RenderedMessage` の追加 | C-1 |
| `src/mail_dock/infrastructure/parsing/eml_render.py` | 表示用パート抽出 | C-2 |
| `src/mail_dock/infrastructure/parsing/html_sanitizer.py` | CSP注入・危険タグ除去（Qt非依存） | C-3 |
| `src/mail_dock/usecases/open_message.py` | 閲覧ユースケース | C-4 |
| `src/mail_dock/usecases/save_attachment.py` | 添付保存ユースケース | F-1 |
| `src/mail_dock/usecases/export_message.py` | `.eml` エクスポートユースケース | F-2 |
| `tests/unit/test_html_sanitizer.py` 他 | 単体テスト（CI実行） | G-1 |
| `tests/gui/` | GUIテスト（`gui` マーカー・ローカル手動） | G-2 |

---

## **5. スコープ境界**

### **5.1 含むもの**

セクション3のグループA〜G。「初回セットアップ（ルート・アカウント・フォルダ）→ 同期（進捗・キャンセル）→ 一覧（遅延ロード・状態バッジ）→ 検索（Phase 2 基盤の UI 化）→ 本文表示（5層サンドボックス）→ スレッド表示 → 添付・`.eml` 保存 → 設定」が GUI だけで一周できる基礎インフラ一式。

### **5.2 含まないもの（明示的に除外）**

| 除外項目 | 実施フェーズ |
| :---- | :---- |
| `QTextBrowser` による軽量HTML表示版の試作・比較 | 実施しない（D-1） |
| ローカルゴミ箱・30日purge・墓標化・「実体なし」への遷移処理 | Phase 4 |
| サーバーからの削除（ドライラン・件数手入力確認・監査ログ・レート制限） | Phase 4 |
| 整合性チェック全般（クイック・フル・孤児スキャン・マニフェスト検証・再インデックス）のUI | Phase 4 |
| 切断の状態機械・`WM_DEVICECHANGE` 監視（`native/device_watcher.py`）・ハートビート・「安全な取り外し」 | Phase 4 |
| `QTimer` による定期自動同期・システムトレイ常駐・トレイステータス表示 | Phase 4 |
| `sync_failures` の「要確認」一覧・「それでも取得」の個別再取得UI・再解析UI | Phase 4 |
| mbox エクスポート | Phase 4 |
| 監査ログ表示画面・DBバックアップ | Phase 4 |
| フルスケール実機テスト（5万通 / 100GB） | Phase 4 |
| PSTインポートウィザード（`import_wizard.py`）・左ペインのPSTアーカイブルート | Phase 4.5 |
| PyInstaller / Inno Setup によるパッケージング | Phase 4 以降 |
| Gmail / OAuth2 のアカウント種別 | Phase 5 |
| 検索結果のスニペット・ハイライト表示 | 将来拡張（Phase 2 D-4） |
| 一覧のスレッドグルーピング表示（Gmail風） | 将来拡張 |
| フラグの変更・ローカル既読管理・IMAP IDLE | 恒久的にスコープ外 |
| 多言語化（`tr()` / 翻訳ファイル）・DPI個別対応 | 恒久的にスコープ外（D-8） |

---

## **6. 検証**

各項目の完了を確認したうえで、対応するタスクのチェックボックスを埋めること。

- [ ] V-1. `uv sync` → `uv run ruff format --check .` → `uv run ruff check .` → `uv run mypy` がすべて成功する
- [ ] V-2. `uv run pytest -m "not docker and not gui"` が全緑になり、`domain` + `usecases` のカバレッジが 80% 以上である
- [ ] V-3. GUIテストがローカルで全緑になる（`gui` マーカーのオプトイン実行）
- [ ] V-4. 起動導線: `mail-dock gui` とサブコマンド無しの `mail-dock` で GUI が起動し、既存の CLI サブコマンド（`migrate` / `verify` / `account` / `folders` / `sync` / `reparse` / `search`）の挙動が変わらない
- [ ] V-5. ウィザード一周: 空のディレクトリから GUI だけでルート初期化 → アカウント登録 → フォルダ選択 → 同期 → 検索 → 本文表示 → 添付保存 まで完了できる
- [ ] V-6. HTML 5層防御: ①プロファイルがオフレコでキャッシュ・永続クッキーが無効 ②JavaScript ほか危険な属性がすべて無効 ③`http` / `https` / `file` がブロックされ `cid` / `maildock` のみ通る ④`cid:` ハンドラがインライン画像を返し未知の `cid` は失敗する ⑤CSP `<meta>` が本文へ注入されている — の5点が個別テストで固定されている
- [ ] V-7. ナビゲーション: リンククリックがアプリ内で遷移せず、確認のうえ外部ブラウザへ転送される。`<meta http-equiv="refresh">` が除去されている
- [ ] V-8. 外部画像: 既定でブロックされ、「画像を読み込む」で画像リクエストのみ許可され、別のメールを開くと再びブロックされる
- [ ] V-9. 遅延ロード: `fetchMore` で全ページを連結した結果が `list_messages` の一括取得と**完全に一致**し、重複・欠損がゼロである。同一 `date_sent` と `date_sent` NULL の混在でも成立する。`next_cursor` を加工していない
- [ ] V-10. UI応答: 同期中も一覧のスクロール・検索・本文表示が可能である。2文字語を含む検索で警告バナーが表示され、キャンセルが実際に効く
- [ ] V-11. 層の隔離: `presentation/views` / `viewmodels` / `models` に `sqlite3` と `mail_dock.infrastructure` の import が無いことを静的に確認する。`domain` / `usecases` に PySide6 の import が無い
- [ ] V-12. 保存の安全性: パストラバーサル・NTFS禁止文字・予約名・実行可能拡張子が期待どおり処理され、保存先が指定ディレクトリ外へ出ない。実行可能拡張子で保存前に警告が出る
- [ ] V-13. 状態表示: `deleted` グレーアウト・`moved` ツールチップ・`purged` の実体なし表示・未読／スターアイコン・`oversize` バッジが正しく出る。フラグを変更する導線が存在しない
- [ ] V-14. エラー表示: `StorageLockedError`（他インスタンス使用中）・`AuthenticationError`・`StorageDetachedError` がそれぞれ専用の文言で表示され、トレースバックが画面に出ない
- [ ] V-15. 性能: 起動時間が 3秒以内（N-2）、1万件規模の一覧スクロールが 60fps（N-1）、同期中のメモリが 600MB 以下（N-3）であることを実測し、本書「7.」へ記録する
- [ ] V-16. CI: プルリクエストで `lint` / `test-windows` / `test-linux` の3ジョブがすべて成功し、GUIテストが実行されていない

---

## **7. Phase 4 への引き継ぎ事項**

> *実装中に判明した事項・実測値をここへ追記する。*

* QtWebEngine の起動時間・メモリ・配布サイズの実測値（V-15）と、パッケージング時の注意（`QtWebEngineProcess` とリソース類の同梱確認。PyInstaller は onedir、onefile は不採用）を記録すること。
* `presentation/native/device_watcher.py`（`WM_DEVICECHANGE` 監視）、ストレージ状態機械（`ATTACHED` / `DEGRADED` / `DETACHED` / `RECONNECTING` / `VERIFYING`）、ハートビート `QTimer`、「安全な取り外し」メニューは Phase 4。Phase 3 は `storage_detached` Signal の受け口までを用意する。
* `QTimer` による定期自動同期・`QSystemTrayIcon` 常駐は Phase 4。Phase 3 は `sync_on_startup` の1回起動のみを実装する。
* 設定画面は Phase 4 / 4.5 の項目（purge モード・ゴミ箱猶予日数・ハートビート間隔・サーバー削除モード・PST取込設定）を追加できる構造にしておくこと。
* 左ペインのツリーモデルは**ルートノードのリスト**を受け取る。Phase 4.5 で「PSTアーカイブ」ルートを足す際にモデルの再設計を不要にすること。PSTアーカイブ選択中は「同期」「サーバーから削除」を非表示にする。
* ゴミ箱ビュー（`local_state='trashed'` と残り日数の表示）は Phase 4。`MessageFilter.local_states` を切り替えるだけで実現できる形にしておくこと。
* `sync_failures` の「要確認」一覧・「それでも取得」・再解析の導線は Phase 4。Phase 3 はバッジ表示までとする。
