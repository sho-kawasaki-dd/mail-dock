# **Phase 7: 条件指定によるサーバー削除（期間・スター付き除外） 実装計画書**

対象: [ローカルメールバックアップ＆閲覧アプリ 開発計画書.md](./ローカルメールバックアップand閲覧アプリ開発計画書.md) の **4.3 サーバーメール手動削除機能**

前提: [Phase 4: 統合と例外処理](./実装計画書_Phase4_統合と例外処理.md) のサーバー削除（`usecases/delete_remote.py`、ドライラン・件数手入力確認・監査ログ）、[Phase 3.7: IMAPフラグの定期リフレッシュ](./実装計画書_Phase3.7_IMAPフラグの定期リフレッシュ.md) の `imap_flags` / `iter_flags`、[Phase 3: GUI基礎構築](./実装計画書_Phase3_GUI基礎構築.md) の一覧の期間・添付フィルタ、およびエクスポートの「現在の一覧すべて」導線（`list_all_messages`）が完成していること。

位置づけ: 現行のサーバー削除は「一覧で手動選択したメール」だけが対象で、期間で一括指定したり、スター付き（`\Flagged`）を除外したりできない。本フェーズは**既存の一覧フィルタ（期間・検索語・添付）の絞り込み結果をそのまま削除対象にできる導線**と、**スター付きを除外する安全オプション**を追加する。フェッチャーのUIDVALIDITY照合契約と大量除外行のモデルベース表示も実装範囲に含み、GUI導線だけの小規模変更とは見積もらない。削除の安全装置（ローカルEML検証・ドライラン・件数手入力・マニフェスト意図記録・監査ログ・件数上限）は**維持し、すべて既存経路を通す**。新しい保護付き経路では、FETCHと削除に使うSELECTの世代照合を追加する。

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
| D-6 | フラグ確認の多段化 | ① `select_delete_scope` は D-8 の状態による除外後、ローカルの `imap_flags` でスター付きを `flagged_message_ids` に振り分ける。② `dry_run` には候補・スター付き・状態による対象外の3集合を渡し、後者2集合は理由 `flagged` / `remote_state_not_deletable` の**除外確定行**とする。DB状態が変わっても候補へ戻さず、EML読出し・ハッシュ計算を行わない。候補側で新たなスター付与や削除不能状態が見つかった場合はさらに除外し、空き枠は補充しない。③ `execute` ではフォルダ単位の `UID FETCH (FLAGS)`（既存の `iter_flags`）で最新フラグを再確認し、スター付きをスキップする。**全対象フォルダの再確認を完了してから削除ループへ進む**。再確認後に付与されたスターを捕捉できないTOCTOUは残り、本フェーズでは許容する |
| D-7 | フラグ再確認の失敗時 | 再確認と削除は二段階に分け、フォルダごとに交互に行わない。再確認中の（a）`TransientError` / `StorageDetachedError` はそのまま再送出して当該 `execute` を中止する。その呼出し内では削除コマンド・`remote_delete_intent`・`remote_delete_uncertain` はすべて0件とする。（b）その他の `FetchError`（`PermanentError` 等）は当該フォルダの全候補を `flag_unverified` とし、他フォルダの再確認を続行する。（c）応答欠落UIDも `flag_unverified` とする。`TransientError` は `FetchError` のサブクラスなので `except` の順序に注意する。「削除0件」の保証範囲は単一の `execute` 呼出しであり、新GUI導線は単一フォルダ・単一アカウントなのでこの保証で足りる。複数アカウントをまたぐロールバックは追加しない。取得できたフラグは F-4 に従いDBへ反映する（派生メタデータなのでマニフェストへは追記しない。Phase 3.7 D-9 と同じ） |
| D-7b | UIDVALIDITY照合 | `exclude_flagged=True` 経路では、先行する `select_folder(raw_name)` の照合だけでなく、**`iter_flags` 内部のSELECTと削除用のSELECTでも期待UIDVALIDITYを照合する**。`BaseMailFetcher.iter_flags` / `move_remote_message_to_trash` / `expunge_remote_message` / `remove_remote_membership` と実装に、キーワード専用の `expected_uidvalidity: int \| None = None` を追加する。期待値ありの実際のSELECTで不一致なら、同じ選択状態を使うFETCH・MOVE・COPY・STORE・EXPUNGEより前に既存の `UidValidityChanged` を送出する。事前再確認中の不一致は当該フォルダの全候補を `uidvalidity_mismatch` とし、`intent` は記録しない。削除用SELECTで初めて不一致が判明した場合も削除コマンドを送らず、その候補を同理由でスキップするが、**既に永続化した `intent` は残る**（`completed` / `uncertain` / 成功監査ログは追加しない）。後続の同フォルダ候補も除外し、新たな `intent` を記録しない。`exclude_flagged=False` 経路は追加引数を渡さず従来どおりとする |
| D-7c | FLAGS応答の完全性 | FLAGS-only取得経路では、要求UIDの応答に**構文的に正しいFLAGS項目が存在することを必須**とする。要求外UIDはFLAGS検証より先に無視する。正常な `FLAGS ()` は「フラグなし」、要求UIDのUIDのみの応答・FLAGS欠落・不正形式は「確認不能」として区別する。`iter_flags` で不正応答を `PermanentError` に変換し、D-7に従い当該フォルダの全候補を `flag_unverified` とする。汎用の `parse_fetch_response` がFLAGS欠落を空タプルへ変換する挙動をそのまま削除の確認済み判定に利用しない。本文・ヘッダ取得など、FLAGS-only以外の解析挙動は変更しない |
| D-8 | 件数上限 | 既存の `delete_batch_limit`（既定1,000）を使う。選択順序は①選択フォルダの `remote_state != "present"`（`deleted` / `uncertain` / `moved` 等）を `non_deletable_message_ids` へ振り分け、②残りから設定ON時のスター付きを振り分け、③残った候補を古い順に上限まで選択する。状態・スターによる除外は上限を消費しない。「古い順」は `COALESCE(date_sent, internal_date)` 昇順、両方 `None` は最古扱い、同値は `id` 昇順。超過分はドライランで明示する。削除済み・移動済みが一覧に残っても次回の枠を塞がない。ただしEML破損・サーバー確認失敗等が続けば再実行で進まない場合があるため、原因解消や条件変更が必要と案内する。新しい設定項目は追加しない |
| D-9 | 対象外の画面 | PSTアーカイブ、ローカルゴミ箱（`local_states` が `active` 以外）、一覧0件、フォルダ以外のノード選択中は無効化する。フォルダ未選択時のツールチップは「フォルダを選択してください」とする（一覧の代表所属を削除対象フォルダと誤認しないため）。ストレージ接続・ゴミ箱フォルダ特定・削除モードの条件は既存経路に合わせ、他操作との排他は F-12 に従う。ただし新アクションは**行選択を要求しない**。Gmailラベル解除の可否は選択フォルダのアカウントで判定し、選択行を参照する既存の `_gmail_label_removal_is_available` をそのまま流用しない |
| D-10 | 既存安全装置 | ローカルEML実在・SHA-256再検証・`message_contents` 存在確認・`DeleteDryRunDialog`（CSV保存可）・`DeleteConfirmationDialog`（件数手入力）・`remote_delete_intent`/`completed` マニフェスト・`audit_log`・多アカウント複数所属の `folder_selection_required` 除外は、新アクションでも**そのまま通す**（迂回経路を作らない） |
| D-11 | スキーマ・設定 | DBマイグレーション・マニフェストイベント・`AppConfig` の追加は行わない |
| D-12 | 操作条件の固定と寿命 | オプション画面に示したフォルダID・検索語・検索モード・フィルタ・**`delete_batch_limit`**と、承認したスター除外設定を、presentation層のリクエスト単位の不変データ（`frozen=True` のデータクラス）として確定する。一覧取得・対象選定・ドライランはこの値だけを使い、結果受信時のGUI選択・現在設定を参照しない。上限は `DeleteScope.delete_batch_limit` に引き継ぎ、表示した上限・選定上限・実行上限を一致させる。処理途中の上限設定変更は次の操作から適用する。ドライランへの引渡し後は一覧取得用データを破棄し、**ドライラン結果生成後の除外設定・集計情報・固定上限は `DeleteDryRunResult` とその `scope` のみから取得する**。成功・失敗・キャンセル・0件終了で一時状態を解放し、手動選択削除とは共有しない。次の操作へ設定が残留して D-5 を破ることを防ぐ |
| D-13 | スコープ外 | 検索バーへのフラグ絞り込みの追加、サーバー側 `UID SEARCH` による対象解決、保持期間ポリシーによる自動削除・定期削除、手動選択経路（`exclude_flagged=False`）へのUIDVALIDITY照合追加 |
| D-14 | 大量件数の表示 | `delete_batch_limit` は削除候補の上限であり、状態・スターによる除外行や一致総数の上限ではない。新導線のドライラン表は `QTableView` と `QAbstractTableModel` で候補・除外の既存タプルを参照し、必要なセルだけ表示する。`QTableWidget` の全行セル生成や全行を走査する列幅自動調整は使わない。件数・全除外行・CSV全件保存は維持し、表示負荷対策のためにデータを切り捨てない。手動選択経路の従来表示・操作は維持する |

### **2.2 機能要件**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-1 | `domain` に、`imap_flags` 文字列から指定フラグの有無を判定する純粋関数 `has_imap_flag(flags: str \| None, flag: str) -> bool` を追加する（空白区切りトークン単位・大文字小文字無視）。既存の `presentation/models/message_table_model.py` と `presentation/views/detail_view.py` にある `_has_imap_flag` の2実装は、この関数の呼び出しに差し替える（判定ロジックの3重化を防ぐ。挙動は同一） | D-4 |
| F-2 | `dry_run()` に `exclude_flagged: bool = False` と `scope: DeleteScope \| None = None` を追加する。`scope` の除外確定IDはDBの後続変更にかかわらず理由 `flagged` / `remote_state_not_deletable` の除外行にし、EML検証を行わない。候補IDには既存の安全検証を適用し、`_candidate_from_record` のメンバーシップマージ直後・`storage.read_verified()` より前に、`scope` ありなら `remote_state != "present"`、除外設定ONなら `\Flagged` を追加除外する。候補は常に `scope.message_ids` の部分集合とし、除外分の昇格・空き枠補充はしない。`scope is None` の手動選択の状態判定は変更しない | D-5, D-6, D-8 |
| F-3 | `execute()` に `exclude_flagged: bool = False` を追加する。ONの場合、①候補をフォルダ単位にまとめ、候補のUIDVALIDITYが単一であることと `select_folder(raw_name)` の戻り値を照合する。不一致フォルダは全候補を `uidvalidity_mismatch` とし、`iter_flags` を呼ばない。一致したフォルダは `iter_flags(raw_name, uids, expected_uidvalidity=期待値)` で再確認し、内部SELECTでも照合する。`UidValidityChanged` は一般の `FetchError` より先に扱い当該フォルダを `uidvalidity_mismatch`、`\Flagged` は `flagged_on_server`、応答欠落は `flag_unverified` として `skipped_ids` / `errors` に入れる。FLAGS欠落・不正形式はD-7cに従う。要求外UIDは `sync_mail.refresh_flags` と同様に無視する。②**全対象フォルダの再確認完了後**、除外されなかった候補だけを既存の再検証・`intent`・削除ループへ渡す。ONの場合のみ各削除APIへ `expected_uidvalidity=candidate.uidvalidity` を渡し、削除用SELECTでの不一致も同理由でスキップする（永続化済み `intent` の扱いはD-7b）。その他の例外はD-7に従い、再確認中の接続断・ストレージ切断では当該呼出しの削除・`intent`・`uncertain` を0件とする | D-6, D-7, D-7b, D-7c |
| F-4 | F-3の再確認で得た、世代照合済み・構文検証済みのフラグだけを `repo.update_flags(account_id, folder_id, uidvalidity, uid, imap_flags, flags_seen_at)` でDBへ反映する。FLAGS欠落を空文字として保存しない。`begin_batch` / `commit_batch` で囲み、IMAP通信中はトランザクションを保持しない。`DeleteCandidate.folder_id` が `None` の候補はDB反映をスキップする（`update_flags` は `folder_id` 必須）。Gmailでは `X-GM-LABELS` も応答に含まれるが、本フェーズでは **flags のみ反映し `gmail_labels` は更新しない**（`refresh_flags` との差分として意識する）。マニフェストへは追記しない | D-7, D-7c |
| F-5 | `usecases` に `select_delete_scope(summaries, *, exclude_flagged, limit) -> DeleteScope` を追加する。`DeleteScope` は不変値とし、`message_ids: tuple[int, ...]`（状態・スター除外後の候補を古い順に上限まで）、`flagged_message_ids: tuple[int, ...]`、`non_deletable_message_ids: tuple[int, ...]`、`matched_count`（元の一覧総数）、`flagged_excluded_count`（一覧取得時のスター除外数）、`truncated`、`delete_batch_limit: int`（引数 `limit` の固定値）を持つ。`limit <= 0` は `ValueError` とする。3集合は重複させず、状態除外をスター判定より優先する。除外設定OFFでも状態による除外を行い、`flagged_message_ids` は空にする。件数の定義は2.3節に従う | D-6, D-8, D-12 |
| F-6 | `DeleteDryRunResult` に `exclude_flagged: bool = False` と `scope: DeleteScope \| None = None` を追加し、`dry_run()` が引数の値を結果へ焼き込む。`SyncWorker.dry_run_remote_delete()` は `exclude_flagged` / `scope` を受け取って `dry_run` へ渡し、`SyncWorker.execute_remote_delete()` は **`plan.exclude_flagged` を読んで** アカウント別の `execute` へ渡す（GUIから別引数で渡さない）。`plan.scope` ありの場合の実行上限は **`plan.scope.delete_batch_limit`** を使い、既存の `delete_batch_limit` 引数・現在設定で上書きしない。`scope is None` の手動選択は既存引数による上限判定を維持する。ドライランは従来どおりIMAPサーバーへ接続しない | D-6, D-12 |
| F-7 | 新アクション「一覧の全件をサーバーから削除…」で、一覧条件（フォルダ・期間・検索語・検索モード・添付）の要約、設定上限とスター除外チェック（既定ON）を表示する。承認した条件と表示した上限を D-12 の不変データに固定し、`list_all_messages(channel="delete/list", ...)` → `select_delete_scope(..., limit=固定上限)` → `dry_run(message_ids=scope.message_ids + scope.flagged_message_ids + scope.non_deletable_message_ids, folder_id=開始時のフォルダID, exclude_flagged=確定設定, scope=scope)` → 既存のドライラン・件数手入力確認 → 実行の順に進む。結果受信時にフォルダ・検索条件・設定上限をGUIから取り直さない | D-1, D-2, D-5, D-12 |
| F-8 | オプション画面では条件・除外設定・設定上限のみ表示し、取得前の件数は表示しない。ドライラン画面では一致総数・状態除外数・スター除外数・上限による対象外数・その他の検証除外数・検証後の対象数を2.3節に従って表示する。**実対象数は `result.candidate_count`** を使い、手入力確認件数と一致させる。`scope is None` の手動選択経路は従来表示を維持する | D-8, D-12 |
| F-9 | 新アクションの有効化条件を D-9 のとおり実装する。Gmailラベル解除の可否は選択フォルダのアカウントで判定し、行を選択していなくても利用可能にする。既存の手動選択削除の行選択要件は変えない | D-5, D-9 |
| F-10 | `execute` の結果表示（成功／不確定／スキップ件数）は既存のまま利用する。スター付きスキップ・`flag_unverified`・`uidvalidity_mismatch` は `skipped` に計上され、理由は `errors` に保持される | D-10 |
| F-11 | 一覧取得は**専用 `"delete/list"` チャネルを採用**する。`RequestChannel` と `RequestState.CHANNELS` の両方へ追加し、`list_all_messages(channel=...)` の既定値はエクスポート互換の `"export/list"` とする。`result` / `request_failed` / `request_cancelled` はチャネルと発行時の `RequestHandle.request_id` の両方を照合し、古い要求で現在の状態を解除しない。**一致した成功結果も、保存したトークンの `is_cancelled` が真ならドライランへ渡さず、キャンセル終了としてトークン・不変データを解放する**。取消要求はトークンをキャンセルするだけで要求番号を無効化しないため、送信済み結果の遅延受信もこの判定で拒否する。同じ要求の失敗も取消済みならキャンセル終了を優先する。成功時は確定条件をドライランへ引き渡してから、失敗・キャンセル・0件終了時は直ちに、トークン・不変データを破棄し、ステータス・キャンセルボタンを次の状態に更新する。遅延通知が重複しても終了処理は一度だけ行う | D-2, D-12 |
| F-12 | 削除一覧取得中は手動選択削除・エクスポート・別の一覧削除を開始させず、**エクスポート一覧取得中も新削除を開始させない**。`_update_remote_delete_action`・新アクションの起動ガード・`_begin_export` で相互排他にし、`_cancel_current_operation` で新トークンを取り消す。ストレージ解放前判定の `has_active_operations()` にも新処理を含める。状態遷移ごとにアクション・ステータス・キャンセルボタンを更新する | D-9, D-12 |
| F-13 | `scope` ありの `DeleteDryRunDialog` はD-14のモデルベース表を使う。セル値は要求された行・列で生成し、候補・除外の全行について `QTableWidgetItem` や表示文字列の別配列を事前生成しない。列幅は固定幅・伸長または限定したサンプルで決め、全行に対する `resizeColumnsToContents()` 相当の走査を避ける。対象選定だけでなく、ドライラン結果の受信・件数集計・表の初期表示までを性能確認対象とする | D-14 |

### **2.3 件数と除外集合の定義**

- 一覧取得時の `MessageSummary.remote_state` / `imap_flags` は、開始時に固定した単一フォルダの所属情報を使う。他フォルダの状態を合成しない。
- `matched_count` は状態・スター・上限で除外する前の一覧総数。`flagged_excluded_count = len(flagged_message_ids)` は一覧取得時点の集計として固定する。
- 上限による対象外数は `matched_count - len(non_deletable_message_ids) - len(flagged_message_ids) - len(message_ids)`。`truncated` はこの値が正かどうかで決める。上限超過分は今回のドライランへ渡さず、集計と注意文で表示する。
- ドライランの状態除外数・スター除外数は、それぞれ `result.exclusions` の理由 `remote_state_not_deletable` / `flagged` を数える。取得後の状態変化による追加除外も含め、一覧取得時の集計をそのまま最終除外数として表示しない。その他の検証除外は別に数える。
- 各IDを候補・除外のいずれか1行だけにし、`matched_count = 上限による対象外数 + result.excluded_count + result.candidate_count` を満たす。件数手入力は `result.candidate_count`、容量は検証済み候補の合計とする。
- `scope` ありの候補は常に `scope.message_ids` の部分集合とし、後続検証で減ることはあっても増やさない。状態除外は新導線では除外設定OFFでも適用するが、`scope is None` の手動選択へは拡張しない。

### **2.4 非機能要件・制約**

- レイヤー境界を守る。`has_imap_flag` と期待UIDVALIDITYを受け取るフェッチャー契約は `domain`、`dry_run`・`execute`・`select_delete_scope` は `usecases`（`domain` のみに依存）、SELECTの世代照合・FLAGS応答検証は `infrastructure`、ダイアログ・アクション・表モデルは `presentation` に置く。IMAP通信は既存の `BaseMailFetcher` を拡張して使い、`imaplib` の生の例外を `usecases` へ持ち込まない。
- リトライは `usecases` 層に集約する既存方針に従い、フラグ再確認にフェッチャー独自のリトライを追加しない。
- 一覧全件取得（`list_all_messages`）はキャンセル可能な既存実装を使い、キャンセル時は何も削除しない。
- スター付き再確認のためのIMAP往復は、フォルダ単位のUID一括指定（`iter_flags` のチャンク500件）に限定し、本文・ヘッダは取得しない。
- 手動選択の削除経路（`scope is None` かつ `exclude_flagged=False`）の挙動・既存テストを変えない。新導線の状態除外は `scope` の有無で区別し、スター除外設定と混同しない。`DeleteDryRunResult` へ追加するフィールドはすべて既定値付きにし、既存の呼び出し・テストを壊さない。
- フェッチャーAPIの `expected_uidvalidity` は既定値 `None` とし、指定された場合のみ実際のSELECT応答を照合する。内部の再SELECTを未照合のまま残さず、照合後は同じ選択状態でコマンドを発行する。削除用SELECTの不一致で残る `intent` は取り消したり完了扱いにせず、既存の回復処理の世代不一致時の保留を維持する。SELECT後、コマンド発行までのサーバー側の変化を完全に原子的に防ぐ契約ではなく、この残余リスクはスター付与のTOCTOUとは区別する。
- 削除候補1,000件・除外100,000件の合成データで、対象選定・ドライラン結果受信・集計・初期表示の時間とメモリを計測して記録する。表は全セルの事前生成・全行列幅走査を行わず、スクロール・閉じる操作へのGUI応答を維持する。CIでは環境依存の厳しい時間閾値だけに頼らず、遅延セル生成と有界な列幅計算を構造的にも検証する。

---

## **3. タスク**

### **Group A: ドメイン・フェッチャー・ユースケース**

- [x] `domain` に `has_imap_flag(flags, flag)` を追加する（`imap_flags` は同期時に `" ".join(ref.flags)` で保存される空白区切り文字列。大文字小文字無視）
- [x] `message_table_model.py` / `detail_view.py` の `_has_imap_flag` を `domain` の `has_imap_flag` 呼び出しに差し替える（F-1。既存GUIテストが変更なく通ることを確認）
- [x] `BaseMailFetcher.iter_flags` / `move_remote_message_to_trash` / `expunge_remote_message` / `remove_remote_membership` と `GenericImapFetcher` に `expected_uidvalidity: int | None = None` を追加する（D-7b）
  - [x] `iter_flags` 内部のSELECTと削除用SELECTのUIDVALIDITYを照合し、不一致なら同じ選択状態でのFETCH・MOVE・COPY・STORE・EXPUNGEより前に `UidValidityChanged` を送出する
  - [x] `expected_uidvalidity=None` の呼出し・互換ラッパーは従来の経路を維持する。既存フェイク・API利用箇所とのシグネチャ互換性を確認する
- [x] `GenericImapFetcher.iter_flags` のFLAGS-only応答でFLAGS項目の存在・構文を検証する（D-7c）
  - [x] 要求外UIDを先に無視し、要求UIDについて `FLAGS ()` とFLAGS欠落・不正形式を区別する。後者は `PermanentError` に変換し、汎用 `parse_fetch_response` の他用途の解析挙動は変えない
- [x] `select_delete_scope()` と不変の `DeleteScope` を追加する（F-5）
  - [x] 選択フォルダの `remote_state != "present"` を `non_deletable_message_ids` へ先に振り分け、残りから設定ON時のスターを `flagged_message_ids` へ振り分ける（3集合は重複させない）
  - [x] 残りを `COALESCE(date_sent, internal_date)` 昇順・両方 `None` は最古・同値は `id` 昇順で上限まで選ぶ。除外集合は上限を消費しない
  - [x] `matched_count`・`flagged_excluded_count`・`truncated` と上限対象外数を2.3節の定義で集計する
  - [x] 表示・承認した正の `limit` を `DeleteScope.delete_batch_limit` へ保存し、`limit <= 0` を拒否する
- [x] `DeleteDryRunResult` に `exclude_flagged: bool = False` と `scope: DeleteScope | None = None` を既定値付きで追加する
- [x] `delete_remote.dry_run()` に `exclude_flagged` / `scope` を追加し、除外確定と追加除外を実装する
  - [x] `scope` のスター・状態除外集合をそれぞれ `flagged` / `remote_state_not_deletable` の除外行にする。DBの後続変更で候補へ戻さず、EML検証を呼ばない
  - [x] 候補側はメンバーシップマージ直後・`storage.read_verified()` の前で、`scope` ありなら状態、除外設定ONならスターを判定する。手動選択の状態判定は変更しない
  - [x] 候補を `scope.message_ids` の部分集合に限定し、追加除外による空き枠を補充しない
  - [x] 引数の `exclude_flagged` / `scope` を結果へ焼き込む
- [x] `delete_remote.execute()` に `exclude_flagged` を追加し、全フォルダ再確認と既存削除ループの二段階に分ける
  - [x] 当該呼出しの全対象フォルダの再確認が完了するまで削除コマンド・`intent` を発行しない。フォルダごとに確認・削除を交互に行わない
  - [x] 候補をフォルダ（`folder_raw_name`）でグループ化し、候補の世代が単一であることと `fetcher.select_folder(raw_name)` の戻り値を照合する。不一致フォルダは全候補を `uidvalidity_mismatch` でスキップし `iter_flags` を呼ばない
  - [x] `fetcher.iter_flags(raw_name, uids, expected_uidvalidity=期待値)` の検証済み応答からUIDごとの最新フラグを得る（要求集合にないUIDは無視）。内部SELECTの `UidValidityChanged` は一般の `FetchError` より先に扱い、全候補を `uidvalidity_mismatch` にする
  - [x] `\Flagged` は `flagged_on_server`、応答欠落は `flag_unverified` としてスキップする
  - [x] FLAGS欠落・不正形式による `PermanentError` は当該フォルダの全候補を `flag_unverified` にし、欠落応答を空フラグとしてDB保存しない
  - [x] `TransientError` / `StorageDetachedError` は捕まえずに再送出する（削除コマンド発行前なのでサーバー状態は不変。`remote_delete_uncertain` は書かない）。それ以外の `FetchError` はそのフォルダの全候補を `flag_unverified` にして続行する。`except` の順序に注意（`TransientError` は `FetchError` のサブクラス）
  - [x] 再確認したフラグを `update_flags` でDBへ反映する（`begin_batch` / `commit_batch` を使い、IMAP通信中はトランザクションを保持しない。`folder_id is None` の候補はスキップ。`gmail_labels` は更新しない）
  - [x] 保護ON時の削除APIへ `expected_uidvalidity=candidate.uidvalidity` を渡す。削除用SELECTでの `UidValidityChanged` を `uidvalidity_mismatch` としてスキップし、後続の同フォルダ候補も除外する。永続化済み `intent` は残すが、削除コマンド・`completed`・`uncertain`・成功監査ログは追加しない
- [x] `execute` の既存上限チェックを維持する。ドライランに渡す `message_ids + flagged_message_ids + non_deletable_message_ids` は上限を超え得るが、候補は `scope.message_ids` の部分集合なので選定時の上限内に収まることを確認する

### **Group B: ワーカー**

- [x] `SyncWorker.dry_run_remote_delete()` に `exclude_flagged` / `scope` を追加し、開始時に固定した `folder_id` とともに `dry_run` へ渡す
- [x] `SyncWorker.execute_remote_delete()` が `plan.exclude_flagged` と、`scope` ありなら `plan.scope.delete_batch_limit` を読んでアカウント別の `execute` へ渡す（新しいGUI引数は追加しない。手動選択は既存の上限引数を使う）
- [x] `RequestChannel` と `RequestState.CHANNELS` に `"delete/list"` を追加し、`QueryWorker.list_all_messages(channel=...)` で指定可能にする。既定値は `"export/list"` を維持する（F-11）
- [x] `select_delete_scope` は一覧取得結果を受けた後にGUIスレッドから呼べる軽量処理か、候補1,000件・除外100,000件を含む合成データで確認する。重い場合は既存ワーカー経由にする。表の表示性能はGroup Cでも別途確認する（101,000件で約0.031秒）

### **Group C: GUI**

- [x] `strings.py` に新アクション名・条件と設定上限・2.3節の件数表示・無効化理由（「フォルダを選択してください」を含む）・上限超過と継続的な失敗への注意・除外理由ラベル（`remote_state_not_deletable` / `flagged` / `flagged_on_server` / `flag_unverified` / `uidvalidity_mismatch`）を追加・整備する
- [x] `delete_remote_dialog.py` に `DeleteByListOptionsDialog` を追加する
  - [x] 現在の一覧条件の要約（フォルダ名・期間・検索語・検索モード・添付有無）と設定上限を読み取り専用で表示する。取得前の件数は表示しない
  - [x] 「スター付きを除外」チェックボックス（既定ON）。OFFにした場合は警告文を表示する
  - [x] 承認時に `exclude_flagged` を返す
- [x] `main_window.py` に `delete_remote_by_list_action` を追加し、ファイルメニューへ登録する（右クリックメニュー・ツールバーには置かない。D-2）
- [x] 有効化条件（D-9・F-9）を `_update_remote_delete_action` と同じ更新タイミングで反映し、無効時の理由をツールチップに出す
  - [x] Gmailラベル解除の可否は選択フォルダのアカウントから判定し、行選択を要求しない。既存の手動選択経路は変更しない
- [x] 押下ハンドラを実装する
  - [x] オプション承認時に、表示したフォルダID・検索語・検索モード・フィルタ・`delete_batch_limit` と承認した除外設定をpresentation層の `frozen=True` のデータクラスへ固定する
  - [x] 確定条件で `query_worker.list_all_messages(channel="delete/list", query=..., mode=..., filters=...)` を発行し、不変データと `RequestHandle` を対応付ける
  - [x] 結果・失敗・キャンセルを専用トークン（例: `_delete_list_token`）とチャネル・`request_id` の一致で受信し、エクスポートや新しい要求へ干渉させない（F-11）
  - [x] 一致した成功結果でもトークンの `is_cancelled` を確認し、取消済みならドライランへ渡さずキャンセル終了処理する。取消済みの同要求の失敗もキャンセル終了を優先し、遅延通知・重複通知で現在の状態を再変更しない
  - [x] 確定条件で `select_delete_scope(..., limit=固定上限)` → `sync_worker.dry_run_remote_delete(scope.message_ids + scope.flagged_message_ids + scope.non_deletable_message_ids, ..., folder_id=開始時のフォルダID, exclude_flagged=確定設定, scope=scope)` へ渡す。結果受信時のGUI選択・現在設定は参照しない
  - [x] ドライランへの引渡し・失敗・キャンセル・0件終了の各経路で一覧取得用トークンと不変データを解放し、ステータスとキャンセルボタンを更新する。手動選択削除と一時状態を共有しない
  - [x] 既存の `_show_delete_dry_run_result` へ合流させる。結果生成後の除外設定・集計・固定上限は `result` とその `scope` だけから取得し、そのまま `execute_remote_delete` へ渡す。`scope` ありなら現在設定の上限を読み直さず、手動選択の上限処理は変更しない（D-12）
- [x] `_update_remote_delete_action`・新アクションの起動ガード・`_begin_export` に相互排他を追加する。削除一覧取得中の手動削除・エクスポート・別の一覧削除と、エクスポート一覧取得中の新削除を拒否する（F-12）
- [x] `_cancel_current_operation` と `has_active_operations()` に新トークンを追加し、キャンセル・ストレージ解放前判定へ含める。状態遷移時にアクションを再評価する
- [x] 一覧取得中のキャンセル（既存のキャンセルボタン）で処理が止まり、何も削除されないことを確認する
- [x] `DeleteDryRunDialog` 上部に2.3節の件数と上限超過注意を表示する。取得時集計は `result.scope`、最終除外数は `result.exclusions`、検証後の対象数は `result.candidate_count` を使う（`scope is None` なら従来表示）
- [x] `scope` ありのドライラン表を `QTableView` と `QAbstractTableModel` にし、候補・除外のタプルを参照して要求されたセルだけ値を生成する（F-13）。既存ダイアログ内または適切な既存モデル配置を使い、不必要にファイルを増やさない
  - [x] 全行セルの事前生成・全行列幅走査を避ける。固定幅・伸長または限定サンプルで列幅を決め、CSV保存は表示範囲にかかわらず全候補・全除外を対象とする
  - [x] 候補1,000件・除外100,000件で結果受信・集計・初期表示時間とメモリを計測し、スクロール・閉じる操作への応答とともに記録する。手動選択の従来表示・操作は維持する。表示初期化・集計は0.132秒、Pythonヒープピーク0.01 MiB（tracemalloc、Qtネイティブメモリ除外）。スクロールと閉じる操作を確認

### **Group D: テスト**

- [x] `tests/unit/test_delete_remote.py` に追加する
  - [x] `has_imap_flag` の判定（トークン境界、大文字小文字、`None`）
  - [x] `dry_run(exclude_flagged=True)` でスター付きが `flagged` 除外になる／`False` では従来どおり対象になる／除外分では `storage.read_verified` が呼ばれない
  - [x] `dry_run` が `exclude_flagged` / `scope` を結果に焼き込む
  - [x] 一覧取得後にスターを解除しても、`scope.flagged_message_ids` は `flagged` のままで候補へ戻らず、EML検証も呼ばれない（上限件数の候補＋スター1件で上限超過が起きない）
  - [x] `scope.non_deletable_message_ids` は状態が `present` に戻っても除外確定を維持し、候補側の後続状態変化は追加除外する。除外設定OFFの新導線でも状態除外を適用し、手動選択の状態判定は変えない
  - [x] 追加除外で空き枠ができても補充せず、候補IDは `scope.message_ids` の部分集合になる
  - [x] `execute(exclude_flagged=True)` でサーバー側でスターが付いたメールがスキップされ、`remote_delete_intent` が記録されない
  - [x] 再確認で応答欠落したUIDが `flag_unverified` でスキップされる
  - [x] `select_folder` の戻り値が候補の `uidvalidity` と異なるフォルダは全候補が `uidvalidity_mismatch` でスキップされ、`iter_flags` も `intent` も発生しない
  - [x] 先行SELECTは一致しても `iter_flags` 内部SELECTで世代が変われば当該フォルダの全候補が `uidvalidity_mismatch` となり、FETCH・削除コマンド・`intent` が発生しない
  - [x] フラグ確認後、削除用SELECTで世代が変わった場合は `uidvalidity_mismatch` となり、MOVE・COPY・STORE・EXPUNGEを発行しない。永続化済み `intent` は残るが `completed` / `uncertain` / 成功監査ログはなく、後続の同フォルダ候補の `intent` も増えない
  - [x] 複数フォルダに分かれた候補で `iter_flags` がフォルダごとに1回ずつ呼ばれ、最後のフォルダの確認完了より前に削除コマンド・`intent` が発生しない
  - [x] 再確認中の `TransientError` は再送出され、`intent` / `uncertain` が1件も書かれない
  - [x] 1番目のフォルダの再確認成功後、2番目で `TransientError` / `StorageDetachedError` が発生しても、当該 `execute` の削除コマンド・`intent`・`uncertain` はすべて0件になる
  - [x] 再確認中の `PermanentError` はそのフォルダの全候補を `flag_unverified` にし、他フォルダは処理が続く
  - [x] UIDのみ・FLAGS欠落・不正FLAGS形式による `PermanentError` でも当該フォルダの全候補を `flag_unverified` にし、欠落応答を空フラグとして保存せず `intent` も記録しない
  - [x] 再確認したフラグがDBへ反映される（`folder_id is None` の候補は反映されない）
  - [x] `exclude_flagged=False` ではフラグ再確認用の `select_folder` / `iter_flags` の呼出しを追加しない（既存の削除内部のフォルダ選択は対象外。手動選択経路の非回帰）
  - [x] `select_delete_scope` の状態除外優先・3集合の非重複・古い順（`None` 日付が最古扱い・同値は `id` 昇順）・上限切り詰め・各件数・`truncated`
  - [x] 選定時の `limit` が `scope.delete_batch_limit` に保持され、非正の上限を拒否する
  - [x] `deleted` / `uncertain` / `moved` が上限を消費せず、削除済み・移動済みが一覧に残っていても、連続2回の対象選定・実行で残りの候補へ進む
  - [x] EML欠落等の検証除外があっても、2.3節の件数合計が一致する
- [x] `tests/support/fake_fetcher.py` / `in_memory_repository.py` が `expected_uidvalidity` / `select_folder` / `iter_flags` / `update_flags` を必要な範囲で模擬できることを確認・補う。事前SELECT・FETCH用SELECT・削除用SELECTそれぞれの世代変更を再現できるようにする
- [x] 既存の `GenericImapFetcher` / `imap_common` 単体テストへ、`FLAGS ()` とFLAGS欠落・不正形式の区別、実際のSELECTでの世代不一致時に後続コマンドを送らないケースを追加する。usecaseのフェイクだけでなくフェッチャー実装も検証し、FLAGS-only以外の解析の非回帰を確認する
  - [x] 要求外UIDの応答にFLAGSがなくても無視され、要求UIDの検証結果・DB更新・削除可否へ影響しないことを確認する
- [x] 既存の `SyncWorker` テストで `scope` ありの固定上限・除外設定が `execute` へ渡り、現在設定・既存上限引数で上書きされないことを確認する。手動選択は既存引数を使う
- [x] `tests/gui/test_query_worker.py` と既存の要求状態テストに、`"delete/list"` の登録・独立した要求管理・エクスポートの既定チャネル維持を追加する
- [x] `tests/gui/test_delete_remote_dialog.py` にオプションダイアログ（既定ON、OFF時警告、条件と設定上限のみ表示）と、ドライランの scope 表示（あり／なし）を追加する
  - [x] EML検証・追加スター除外で候補が減った場合も、最終除外数・`result.candidate_count`・手入力確認件数が一致する
  - [x] 候補1,000件・除外100,000件で表モデルが全行を保持しつつ全セルを事前生成せず、列幅計算も有界であることを確認する。先頭・末尾行の表示とCSV全件保存を検証する
- [x] `tests/gui/test_main_window.py` に新アクションの導線と状態管理を追加する
  - [x] 有効・無効条件（PST、ローカルゴミ箱、0件、アカウントノード、ストレージ切断、実行中）と、承認後に既存ドライランへ進む流れ
  - [x] Gmailラベル解除で行選択なしでも利用でき、選択フォルダのアカウントで判定される
  - [x] 一覧取得中にフォルダAからBへ切り替え、検索条件を変更しても、開始時のフォルダ・条件・除外設定が使われる。Gmailの複数所属メールでもBの所属を対象にしない
  - [x] 一覧取得中およびドライラン後に `delete_batch_limit` を増減しても、オプションに表示・承認した上限が選定・実行へ引き継がれる。次の新操作は変更後の上限を使い、手動選択は従来の上限処理を維持する
  - [x] 削除一覧取得中は手動削除・エクスポート・別の一覧削除を開始できず、エクスポート一覧取得中も新削除を開始できない
  - [x] キャンセル・失敗・0件・正常引渡しでトークンと不変データが解放され、ステータス・キャンセルボタンが更新される。次の手動選択削除へ除外設定が残らない
  - [x] 古い `request_id` の結果・失敗・キャンセルが現在の要求に干渉せず、エクスポートのハンドラとも競合しない
  - [x] 「ワーカーが成功結果を送信済み → GUIでキャンセル → 同じ `request_id` の成功結果を受信」の順を再現し、ドライラン・削除を開始せずトークン・不変データを解放する。後続の重複成功・失敗・キャンセル通知が新要求へ干渉しない
  - [x] 一覧取得中は `has_active_operations()` が真になり、キャンセル後は処理が停止して削除されない
- [x] `tests/integration/test_remote_delete.py` に、スター付きメールが実サーバー相当（Dovecot、WSL上で実行）で削除されないことを検証するケースを追加する
- [x] `pytest -m "not docker and not gui and not pst"` と GUIテストを実行し、既存の手動削除テストが変更なく通ることを確認する

### **Group E: ドキュメント整合**

- [ ] 開発計画書 4.3 に、条件指定削除（一覧全件・スター付き除外・実行直前のサーバー再確認・件数上限の扱い）を追記する
- [ ] 開発計画書のフェーズ一覧表に Phase 7 の行を追加し、本書へのリンクを記載する
- [ ] `ruff check .` / `mypy .` / `pytest` を実行し、全テスト通過を確認する
