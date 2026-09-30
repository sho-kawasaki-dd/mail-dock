# **Phase 7: 条件指定によるサーバー削除（期間・スター付き除外） 実装計画書**

対象: [ローカルメールバックアップ＆閲覧アプリ 開発計画書.md](./ローカルメールバックアップand閲覧アプリ開発計画書.md) の **4.3 サーバーメール手動削除機能**

前提: [Phase 4: 統合と例外処理](./実装計画書_Phase4_統合と例外処理.md) のサーバー削除（`usecases/delete_remote.py`、ドライラン・件数手入力確認・監査ログ）、[Phase 3.7: IMAPフラグの定期リフレッシュ](./実装計画書_Phase3.7_IMAPフラグの定期リフレッシュ.md) の `imap_flags` / `iter_flags`、[Phase 3: GUI基礎構築](./実装計画書_Phase3_GUI基礎構築.md) の一覧の期間・添付フィルタ、およびエクスポートの「現在の一覧すべて」導線（`list_all_messages`）が完成していること。

位置づけ: 現行のサーバー削除は「一覧で手動選択したメール」だけが対象で、期間で一括指定したり、スター付き（`\Flagged`）を除外したりできない。本フェーズは**既存の一覧フィルタ（期間・検索語・添付）の絞り込み結果をそのまま削除対象にできる導線**と、**スター付きを除外する安全オプション**を追加する小規模フェーズである。削除の安全装置（ローカルEML検証・ドライラン・件数手入力・マニフェスト意図記録・監査ログ・件数上限）は**一切変更せず、すべて既存経路を通す**。

本書と開発計画書に矛盾がある場合は開発計画書を正とする。4.3の追記は本書のGroup E完了をもって行う。

---

## **1. 目的**

- 一覧の絞り込み結果（期間・検索語・添付有無・選択フォルダ）を、**手動で範囲選択せずに**サーバー削除の対象にできるようにする
- スター付き（`\Flagged`）のメールを削除対象から**除外するオプション**（既定ON）を提供する
- スター付き判定を、ローカルのスナップショットだけに依存させず、**削除実行の直前にサーバーの最新フラグで再確認**する
- 新機能でも設計不変条件3（多段防御）を崩さない。ワンクリックで大量削除に到達する導線を作らない

## **2. 要件**

### **2.1 前提となる意思決定（要合意）**

| # | 項目 | 決定内容 |
| :--- | :---- | :---- |
| D-1 | UI方針 | 削除専用の条件入力画面は作らない。既存の一覧フィルタ（開始日・終了日・検索語・添付有無）と左ペインのフォルダ選択で対象を絞り込み、**「現在の一覧すべて」を対象にする**（エクスポートの `list_all_messages` 導線と同じ考え方）。対象が画面上で確認でき、条件の二重管理を避けられる |
| D-2 | 起動口 | 既存の「サーバーから削除」（手動選択）は**挙動を変えない**。別アクション「一覧の全件をサーバーから削除…」を追加し、**ファイルメニューのみ**に置く（ツールバーには置かない。一覧の右クリックメニューにも置かない。行を右クリックして出るメニューに「全件」削除があると、選択行への操作と誤認しやすいため） |
| D-3 | 期間の基準 | 一覧フィルタと同じ `COALESCE(date_sent, internal_date)` を用いる（`SqliteSearchRepository._filter_clause`）。保存パスの年月（`INTERNALDATE`）とは異なり得るため、ドライラン画面の日付列で確認させる |
| D-4 | スター付きの定義 | `imap_flags` に `\Flagged` トークンが含まれること（大文字小文字を区別しない、空白区切りのトークン単位で照合）。Gmailの「スター」も `\Flagged` に対応する |
| D-5 | 除外オプションの適用範囲 | 「スター付きを除外」（既定ON）は**新アクションのみ**に適用する。手動選択による削除は、利用者が対象を個別に選んだ明示的な意思とみなし従来どおり変更しない |
| D-6 | フラグ確認の多段化 | ①一覧取得後に `select_delete_scope` がローカルの `imap_flags` でスター付きを**別集合（`flagged_message_ids`）に振り分ける**（件数上限は非スター分だけで数える）。②`dry_run` には非スター分とスター付き分を**両方**渡し、`dry_run` 側でスター付きを理由 `flagged` の除外行として表示する（除外の事実をドライラン画面とCSVに残すため。①で落としてしまうと除外行が空になる）。③**`execute` の実行直前にサーバーへ `UID FETCH (FLAGS)`（既存の `iter_flags`）を発行して再確認**し、スター付きをスキップする。ダイアログ確認から実行までの間に付けたスターを取り逃がさない。③はフォルダ単位で一括再確認してから各件の `intent` を記録するため、再確認〜`intent` の間の短い窓は残る。これは既存の手動削除と同程度のTOCTOUであり許容する |
| D-7 | フラグ再確認の失敗時 | 除外オプションON時、再確認段階はまだ削除コマンドを1件も発行しておらずサーバー状態は不変なので、**既存の「不確定（`remote_delete_uncertain`）」扱いとは切り離す**。（a）`TransientError` / `StorageDetachedError`（接続断・ストレージ切断）は**そのまま再送出して実行全体を中止**する（続行すると各件で `intent` → 削除失敗 → `uncertain` が量産され、起動時の reconcile 負荷になる。`TransientError` は `FetchError` のサブクラスなので、`except FetchError` で先に捕まえないこと）。（b）それ以外の `FetchError`（`PermanentError` 等）は、そのフォルダの全候補を `flag_unverified` として `skipped_ids` に入れ、他フォルダの処理は続行する。（c）応答が欠落したUIDは `flag_unverified` としてスキップする。確認できないものは削除しない側に倒す。再確認で得たフラグはDBの `imap_flags` / `flags_seen_at` へ反映する（派生メタデータのためマニフェストへは追記しない。Phase 3.7 D-9 と同じ） |
| D-7b | UIDVALIDITY照合 | `iter_flags` は内部で `select_folder` を呼ぶが UIDVALIDITY を返さない。再確認時は `execute` 側で先に `fetcher.select_folder(raw_name)` を呼び、戻り値が候補の `uidvalidity` と一致しないフォルダは**全候補を `uidvalidity_mismatch` としてスキップ**し `intent` を記録しない（UIDが別メッセージを指している可能性があるため、フラグ判定以前に除外する）。既存の `execute` はサーバー側 UIDVALIDITY を照合していないので、新設する往復が唯一の確認機会になる。`exclude_flagged=False` 経路には追加しない |
| D-8 | 件数上限 | 既存の `delete_batch_limit`（既定1,000）をそのまま使う。一致件数が上限を超える場合は**古い順に上限件数まで**を対象とし、超過分は対象外であることをダイアログに明示する（超過時は再度実行してもらう）。「古い順」は一覧の並び順と同じ `COALESCE(date_sent, internal_date)` を昇順にしたもので、両方 `None` のものはSQLの `''` と同じく**最古扱い**、同値は `id` 昇順とする。新しい設定項目は追加しない |
| D-9 | 対象外の画面 | PSTアーカイブ選択中、ローカルゴミ箱ノード（`local_states` が `active` 以外）、一覧が0件のときは無効化する。**フォルダ以外のノード（アカウント・全アカウント）選択中も無効化**する（`folder_id=None` で `dry_run` に渡ると複数所属メッセージが `folder_selection_required` で一律除外され、Gmailのようにラベル多重所属が普通なアカウントでは「一覧に見えているのにほぼ全部除外」になる。一覧は `MIN(folder_id)` の所属を表示しているだけで、削除対象フォルダを表していない）。無効化理由のツールチップに「フォルダを選択してください」を出す。ストレージ接続・他操作の実行中・ゴミ箱フォルダ未特定・Gmailラベル解除モードの可否など、その他の有効化条件は既存の `_update_remote_delete_action` と同一にする |
| D-10 | 既存安全装置 | ローカルEML実在・SHA-256再検証・`message_contents` 存在確認・`DeleteDryRunDialog`（CSV保存可）・`DeleteConfirmationDialog`（件数手入力）・`remote_delete_intent`/`completed` マニフェスト・`audit_log`・多アカウント複数所属の `folder_selection_required` 除外は、新アクションでも**そのまま通す**（迂回経路を作らない） |
| D-11 | スキーマ・設定 | DBマイグレーション・マニフェストイベント・`AppConfig` の追加は行わない |
| D-12 | 除外条件の持ち回り | `exclude_flagged` と件数情報（`DeleteScope`）は**GUI側の一時状態（`self._pending_...`）で持ち回らず、`DeleteDryRunResult` に焼き込む**。`_show_delete_dry_run_result` は `DeleteDryRunResult` だけを受け取るスロットであり、キャンセル・失敗・0件終了のいずれかでリセット漏れが起きると、次の手動選択削除に `exclude_flagged=True` が残留して D-5 を破る。結果に焼き込めばドライラン結果と実行条件が常に一致する |
| D-13 | スコープ外 | 検索バーへのフラグ絞り込みの追加、サーバー側 `UID SEARCH` による対象解決、保持期間ポリシーによる自動削除・定期削除、手動選択経路（`exclude_flagged=False`）へのUIDVALIDITY照合追加 |

### **2.2 機能要件**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-1 | `domain` に、`imap_flags` 文字列から指定フラグの有無を判定する純粋関数 `has_imap_flag(flags: str \| None, flag: str) -> bool` を追加する（空白区切りトークン単位・大文字小文字無視） | D-4 |
| F-2 | `delete_remote.dry_run()` に `exclude_flagged: bool = False` を追加する。ONの場合、ローカルレコード（フォルダ所属レコードをマージ済みの値）の `imap_flags` に `\Flagged` があるメッセージを理由 `flagged` の除外行にする | D-6 |
| F-3 | `delete_remote.execute()` に `exclude_flagged: bool = False` を追加する。ONの場合、`intent` 記録の前に、対象をフォルダ単位でまとめて `fetcher.iter_flags()` によりサーバーの最新フラグを確認し、`\Flagged` は理由 `flagged_on_server`、確認できないものは `flag_unverified` として `skipped_ids` / `errors` へ入れる | D-6, D-7 |
| F-4 | F-3の再確認結果は `repo.update_flags()` でDBへ反映する。マニフェストへは追記しない | D-7 |
| F-5 | `usecases` に、一覧の `MessageSummary` 群から削除対象IDを決める `select_delete_scope(summaries, *, exclude_flagged, limit) -> DeleteScope` を追加する。`DeleteScope` は `message_ids`（古い順・上限まで）、`matched_count`（一致総数）、`flagged_excluded_count`、`truncated`（上限超過の有無）を持つ | D-6, D-8 |
| F-6 | `SyncWorker.dry_run_remote_delete()` / `execute_remote_delete()` が `exclude_flagged` を受け取り、それぞれ `dry_run` / `execute` へ渡す。ドライランは従来どおりIMAPサーバーへ接続しない | D-6 |
| F-7 | 新アクション「一覧の全件をサーバーから削除…」を追加し、押下時に現在の一覧条件（フォルダ・期間・検索語・添付）の要約と「スター付きを除外（既定ON）」チェックを表示するオプションダイアログを出す。承認後、`list_all_messages` → `select_delete_scope` → 既存のドライラン → 件数手入力確認 → 実行の順で進む | D-1, D-2, D-5 |
| F-8 | オプションダイアログとドライランの上部に、一致総数・スター付き除外数・実際の対象数・上限超過時の注意を表示する | D-8 |
| F-9 | 新アクションの有効化条件を D-9 のとおり実装する。一覧が0件のときも無効化する | D-9 |
| F-10 | `execute` の結果表示（成功／不確定／スキップ件数）は既存のまま利用する。スター付きスキップは `skipped` に計上され、理由は `errors` に保持される | D-10 |

### **2.3 非機能要件・制約**

- レイヤー境界を守る。`has_imap_flag` は `domain`、`dry_run`・`execute`・`select_delete_scope` は `usecases`（`domain` のみに依存）、ダイアログ・アクションは `presentation` に置く。IMAP通信は既存の `BaseMailFetcher.iter_flags` を使い、`imaplib` の生の例外を `usecases` へ持ち込まない。
- リトライは `usecases` 層に集約する既存方針に従い、フラグ再確認にフェッチャー独自のリトライを追加しない。
- 一覧全件取得（`list_all_messages`）はキャンセル可能な既存実装を使い、キャンセル時は何も削除しない。
- スター付き再確認のためのIMAP往復は、フォルダ単位のUID一括指定（`iter_flags` のチャンク500件）に限定し、本文・ヘッダは取得しない。
- 手動選択の削除経路（`exclude_flagged=False`）の挙動・既存テストを変えない。

---

## **3. タスク**

### **Group A: ドメイン・ユースケース**

- [ ] `domain` に `has_imap_flag(flags, flag)` を追加する（`imap_flags` は同期時に `" ".join(ref.flags)` で保存される空白区切り文字列。大文字小文字無視）
- [ ] `delete_remote.dry_run()` に `exclude_flagged` を追加し、`flagged` 除外を実装する（`_candidate_from_record` のメンバーシップマージ後のレコードで判定）
- [ ] `delete_remote.execute()` に `exclude_flagged` を追加し、`intent` 記録前のフォルダ単位サーバー再確認を実装する
  - [ ] `fetcher.iter_flags(raw_name, uids)` の応答からUIDごとの最新フラグを得る
  - [ ] `\Flagged` は `flagged_on_server`、応答欠落・`FetchError` は `flag_unverified` としてスキップし、`TransientError` 系の扱いは既存の不確定処理と混同しない（削除コマンド発行前なのでサーバー状態は不変。不確定扱いにはしない）
  - [ ] 再確認したフラグを `update_flags` でDBへ反映する（`begin_batch` / `commit_batch` を使い、IMAP通信中はトランザクションを保持しない）
- [ ] `select_delete_scope()` と `DeleteScope` を追加する（古い順ソート・ローカルフラグでの事前除外・上限切り詰め・各件数の集計）
- [ ] `execute` の上限チェック（`delete_batch_limit` 超過は `ValueError`）と `select_delete_scope` の上限が矛盾しないことを確認する

### **Group B: ワーカー**

- [ ] `SyncWorker.dry_run_remote_delete()` に `exclude_flagged` を追加して `dry_run` へ渡す
- [ ] `SyncWorker.execute_remote_delete()` に `exclude_flagged` を追加してアカウント別の `execute` へ渡す
- [ ] `select_delete_scope` は一覧取得結果を受けた後にGUIスレッドから呼べる軽量処理か確認し、重い場合は既存ワーカー経由にする

### **Group C: GUI**

- [ ] `strings.py` に新アクション名・オプションダイアログの文言・件数表示・無効化理由・上限超過注意・除外理由ラベルを追加する
- [ ] `delete_remote_dialog.py` に `DeleteByListOptionsDialog` を追加する
  - [ ] 現在の一覧条件の要約（フォルダ名・期間・検索語・添付有無）を読み取り専用で表示する
  - [ ] 「スター付きを除外」チェックボックス（既定ON）。OFFにした場合は警告文を表示する
  - [ ] 承認時に `exclude_flagged` を返す
- [ ] `main_window.py` に `delete_remote_by_list_action` を追加し、ファイルメニューと一覧の右クリックメニューへ登録する
- [ ] 有効化条件（D-9・F-9）を `_update_remote_delete_action` と同じ更新タイミングで反映し、無効時の理由をツールチップに出す
- [ ] 押下ハンドラを実装する
  - [ ] オプションダイアログ → `query_worker.list_all_messages(query, mode, filters)`
  - [ ] 結果受信時、エクスポート用トークンと区別する専用トークン（例: `_delete_list_token`）で受け取り、`_show_export_list_result` と競合させない
  - [ ] `select_delete_scope` → `sync_worker.dry_run_remote_delete(..., exclude_flagged=...)`
  - [ ] 既存の `_show_delete_dry_run_result` へ合流させ、実行時に `exclude_flagged` を保持して `execute_remote_delete` へ渡す
- [ ] 一覧取得中のキャンセル（既存のキャンセルボタン）で処理が止まり、何も削除されないことを確認する
- [ ] ドライランダイアログ上部に、一致総数・スター付き除外数・対象数・上限超過注意を表示する

### **Group D: テスト**

- [ ] `tests/unit/test_delete_remote.py` に追加する
  - [ ] `has_imap_flag` の判定（トークン境界、大文字小文字、`None`）
  - [ ] `dry_run(exclude_flagged=True)` でスター付きが `flagged` 除外になる／`False` では従来どおり対象になる
  - [ ] `execute(exclude_flagged=True)` でサーバー側でスターが付いたメールがスキップされ、`remote_delete_intent` が記録されない
  - [ ] 再確認で応答欠落したUIDが `flag_unverified` でスキップされる
  - [ ] 再確認したフラグがDBへ反映される
  - [ ] `exclude_flagged=False` では `iter_flags` が呼ばれない（既存経路の非回帰）
  - [ ] `select_delete_scope` の古い順・上限切り詰め・除外数・`truncated`
- [ ] `tests/support/fake_fetcher.py` / `in_memory_repository.py` が `iter_flags` / `update_flags` を必要な範囲で模擬できることを確認・補う
- [ ] `tests/gui/test_delete_remote_dialog.py` にオプションダイアログ（既定ON、OFF時警告、要約表示）を追加する
- [ ] `tests/gui/test_main_window.py` に新アクションの有効・無効条件（PST、ローカルゴミ箱、0件、ストレージ切断、実行中）と、承認後にドライランへ進む流れを追加する
- [ ] `tests/integration/test_remote_delete.py` に、スター付きメールが実サーバー相当（Dovecot、WSL上で実行）で削除されないことを検証するケースを追加する
- [ ] `pytest -m "not docker and not gui and not pst"` と GUIテストを実行し、既存の手動削除テストが変更なく通ることを確認する

### **Group E: ドキュメント整合**

- [ ] 開発計画書 4.3 に、条件指定削除（一覧全件・スター付き除外・実行直前のサーバー再確認・件数上限の扱い）を追記する
- [ ] 開発計画書のフェーズ一覧表に Phase 7 の行を追加し、本書へのリンクを記載する
- [ ] `ruff check .` / `mypy .` / `pytest` を実行し、全テスト通過を確認する
