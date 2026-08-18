# **Phase 3.7: IMAPフラグの定期リフレッシュ 実装計画書**

対象: [ローカルメールバックアップ＆閲覧アプリ 開発計画書.md](./ローカルメールバックアップand閲覧アプリ開発計画書.md) の **4.6-2 IMAPフラグの扱い（スナップショット方式）** および **9章 スコープ外一覧**

前提: [Phase 1: 抽象化層とIMAPコア 実装計画書](./実装計画書_Phase1_抽象化層とIMAPコア.md) の `sync_account()` / `BaseMailFetcher` / `BaseMessageRepository`、[Phase 3: GUI基礎構築 実装計画書](./実装計画書_Phase3_GUI基礎構築.md) の未読・スター表示（D-17）が完成していること。

位置づけ: 「同期時点の一度きりのスナップショット」だったIMAPフラグ（`\Seen` 等）を、**サーバーからの一方向リフレッシュ**により定期的に取り直せるようにする小規模フェーズ。ローカル独自の既読管理・フラグの書き戻し（双方向同期）は引き続き行わない。

本書と開発計画書に矛盾がある場合は開発計画書を正とするが、**4.6-2・9章の該当記述自体は本書のGroup G完了をもって本書の内容に改訂する**（「一度きりのスナップショット」→「定期的に取り直すスナップショット」）。それ以外の不変条件（真実の情報源はEML＋マニフェスト、双方向同期をしない、ローカル既読管理をしない）は変更しない。

---

## **1. 目的**

- [ ] 既に取得済みのメッセージについて、サーバー側の `\Seen` / `\Flagged` 等の変化を**一方向（サーバー→ローカル）**で定期的に取り直せるようにする
- [ ] 上記を**新着同期・履歴backfillの負荷に影響を与えない形**（軽量IMAPコマンド・範囲限定・間引き）で実現する
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
| D-4 | 対象範囲の絞り込み | `internal_date` が直近 `flag_refresh_window_days`（既定30日）以内のメッセージに限定する。古いメッセージは対象外とし、必要であれば将来の個別再同期機能（未着手）に委ねる |
| D-5 | 間引き（TTL） | `flags_seen_at` が `flag_refresh_min_interval_seconds`（既定3600秒）より新しいメッセージは対象から除外する。同一セッション内で同期を連打しても無駄打ちしない |
| D-6 | `CONDSTORE` 対応 | `capabilities` に `CONDSTORE` が含まれる場合は `HIGHESTMODSEQ` を用いた差分取得（変化したメッセージのみ）を優先する。**実サーバー（`mail71.onamae.ne.jp`）で `CONDSTORE` 対応を確認済み（2026-08-18、`OnamaeImapFetcher.capabilities` に `CONDSTORE` を確認）。** CONDSTORE経路を通常経路として実装し、非対応サーバー・非対応契約に備えてフォールバック（D-7）も維持する。`capabilities` 判定により自動的に切り替わるため、経路選択のためのコード分岐以外に追加のフラグ管理は不要 |
| D-7 | フォールバック時の取得方法 | `UID FETCH <対象範囲> (FLAGS)` のみを発行する。本文・ヘッダは取得しない。1コマンドあたりの件数は既存の `_FETCH_CHUNK_SIZE`（500件）をそのまま再利用し、新しい定数は作らない |
| D-8 | 永続化方法 | `BaseMessageRepository` に `imap_flags` / `flags_seen_at` のみを更新する専用の軽量メソッドを追加する。全カラムを要求する既存の `add_message()`（fetch経路専用）は流用しない |
| D-9 | マニフェスト記録 | フラグリフレッシュは**マニフェストへイベントを追記しない**。フラグは表示専用の派生メタデータであり、EML＋マニフェストからDBを再構築した後も次回リフレッシュで自然に最新化されるため、真実の情報源としての記録は不要と判断する |
| D-10 | `HIGHESTMODSEQ` の保持 | `folders` テーブルに `highest_modseq INTEGER` 列を追加し、フォルダ単位で前回確認済みのMODSEQを保持する。`CONDSTORE` 非対応サーバーでは常に `NULL` のままとする |
| D-11 | バッチ制御 | 既存の `begin_batch` / `commit_batch` / `checkpoint` をそのまま再利用する。フラグリフレッシュ専用のコミット経路は作らない |
| D-12 | 失敗時の扱い | フラグリフレッシュ中に `FetchError` が発生してもフォルダ全体の同期を失敗させない。ログに残して次のフォルダ・次回同期に委ねる（新着・履歴backfillの成否とは独立に扱う） |

### **2.2 機能要件**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-1 | `BaseMailFetcher` に、指定UID集合のFLAGSのみを取得する `iter_flags(raw_name, uids, *, cancel=None) -> Iterator[RemoteMessageRef]` を追加する。戻り値は既存の `RemoteMessageRef` を再利用し、`uid` / `flags` 以外のフィールドは未設定でよい | D-7 |
| F-2 | `BaseMailFetcher` に、`CONDSTORE` 専用の `iter_flags_since(raw_name, modseq, *, cancel=None) -> Iterator[RemoteMessageRef]` を追加する（`UID FETCH 1:* (FLAGS) (CHANGEDSINCE modseq)` 相当）。非対応サーバーでは呼び出し側がF-1にフォールバックする | D-6 |
| F-3 | `OnamaeImapFetcher.capabilities` が `CONDSTORE` の有無を反映すること（既存の `_parse_capabilities` を使う） | D-6 |
| F-4 | `select_folder()` 実行時に `HIGHESTMODSEQ` 応答を受信できた場合は保持し、専用の取得メソッドから参照できること。`select_folder()` 自体のシグネチャ（戻り値: `int`）は変更しない | D-10 |
| F-5 | `sync_account()` の `sync_folder()` に、新着同期・履歴backfillの**後**、`initial_sync_completed` なフォルダに対してのみ動くフラグリフレッシュ・ステップを追加する | D-2, D-3 |
| F-6 | フラグリフレッシュの対象は、`internal_date` が `flag_refresh_window_days` 以内、かつ `flags_seen_at` が `flag_refresh_min_interval_seconds` より古いメッセージに限定すること | D-4, D-5 |
| F-7 | フラグに変化があったメッセージだけ `imap_flags` / `flags_seen_at` をDB更新すること（変化がないメッセージへの書き込みを避ける） | D-8 |
| F-8 | フラグリフレッシュはマニフェストへイベントを追記しないこと | D-9 |
| F-9 | `AppConfig` に `flag_refresh_enabled`（既定 `True`）・`flag_refresh_window_days`（既定30）・`flag_refresh_min_interval_seconds`（既定3600）を追加し、既存設定と同様にバリデーション・シリアライズ・既定値マージが行われること | D-4, D-5 |
| F-10 | `SyncOptions` に F-9 と同じ3項目を追加し、`__main__.py` / `sync_worker.py` / `main_window.py` の呼び出し箇所に反映すること | F-9 |
| F-11 | 一覧・詳細ビューの未読／フラグ表示ツールチップが、固定文言「同期時点のスナップショットです」から `flags_seen_at` を用いた具体的な確認日時表示に変わること | 目的 |

### **2.3 非機能要件・制約**

* フラグリフレッシュは新着同期・履歴backfillのクリティカルパスとは**独立したステップ**として実行し、失敗してもフォルダ全体・アカウント全体の同期を止めない（D-12）。
* レイヤー境界を守る: `CONDSTORE` 判定とIMAPコマンド発行は `infrastructure/fetchers`、対象範囲決定・間引きロジックは `usecases/sync_mail.py` に置く。`domain` 層は `RemoteMessageRef` の再利用のみで新規型を増やさない。
* `CancelToken` による中断に対応する（既存の `process_range` 等と同様）。
* 既存の `BaseMessageRepository` に「目的を超えたメソッドを足さない」という制約（設計不変条件）を踏まえ、追加するメソッドはフラグ2カラムの更新に限定する。
* 1回のフラグリフレッシュで発生するIMAP往復は、対象範囲を絞ることでフォルダの全メッセージ数ではなく「直近ウィンドウ内でTTLが切れた件数」にスケールさせる。
* お名前.comの実サーバーは `CONDSTORE` 対応が確認済みだが、将来の契約・サーバー変更で非対応に戻る可能性もゼロではないため、フォールバック経路（D-7）を削除・簡略化せず維持する。

---

## **3. タスク**

### **Group A: ドメイン層とIMAPフェッチャー**

- [ ] `BaseMailFetcher` に `iter_flags(raw_name, uids, *, cancel=None) -> Iterator[RemoteMessageRef]` を追加する
- [ ] `BaseMailFetcher` に `iter_flags_since(raw_name, modseq, *, cancel=None) -> Iterator[RemoteMessageRef]` を追加する
- [ ] `OnamaeImapFetcher` で `CONDSTORE` を `capabilities` に反映する
- [ ] `select_folder()` が `HIGHESTMODSEQ` 応答を保持し、取得用のアクセサ（例: `get_highest_modseq()`）を追加する。非対応時は `None` を返す
- [ ] `iter_flags` / `iter_flags_since` を `onamae_imap.py` に実装し、`_FETCH_CHUNK_SIZE` を再利用する

### **Group B: DBスキーマとリポジトリ**

- [ ] `migrations/004_flag_refresh.sql` を追加し、`folders.highest_modseq INTEGER` を追加する
- [ ] `BaseMessageRepository` に `update_flags(account_id, folder_id, uidvalidity, uid, imap_flags, flags_seen_at) -> None` を追加する
- [ ] `SqliteMessageRepository.update_flags()` を実装する（`imap_flags` / `flags_seen_at` のみの `UPDATE`。`begin_batch` / `commit_batch` 配下で呼べること）
- [ ] `SqliteMessageRepository` に `folders.highest_modseq` の読み書きを追加する（`update_sync_cursors` の拡張、または専用メソッド）
- [ ] `tests/support/in_memory_repository.py` に同メソッドを追加する

### **Group C: 設定**

- [ ] `AppConfig` に `flag_refresh_enabled` / `flag_refresh_window_days` / `flag_refresh_min_interval_seconds` を追加し、バリデーション・シリアライズ・既定値マージに組み込む
- [ ] `SyncOptions` に同3項目を追加する
- [ ] `__main__.py` / `presentation/threads/sync_worker.py` / `presentation/views/main_window.py` の `SyncOptions(...)` 呼び出しに新設定を反映する

### **Group D: 同期ユースケース**

- [ ] `sync_mail.py` の `sync_folder()` に、新着・履歴backfill完了後、`initial_sync_completed` なフォルダに対してのみ動くフラグリフレッシュ・ステップを追加する
- [ ] 対象UID決定ロジック（`internal_date` が `flag_refresh_window_days` 以内、かつ `flags_seen_at` が `flag_refresh_min_interval_seconds` より古い）を実装する
- [ ] `CONDSTORE` 対応時は `iter_flags_since()` ＋ `highest_modseq` 更新、非対応時は `iter_flags()` ＋対象範囲フィルタのフォールバックを実装する
- [ ] 変化があったメッセージのみ `update_flags()` を呼び、既存のバッチ制御（`begin_batch` / `commit_batch`）に統合する
- [ ] フラグリフレッシュ中の `FetchError` はログに残し、フォルダ・アカウント全体の同期を失敗させないことを実装する
- [ ] `CancelToken` によるキャンセルに対応する

### **Group E: GUI表示**

- [ ] `strings.py` の `TOOLTIP_UNREAD` / `TOOLTIP_IMAP_FLAGS` を `flags_seen_at` を使った確認日時表示に変更する
- [ ] `message_table_model.py` および詳細ビューのツールチップ生成箇所を更新する

### **Group F: テスト**

- [ ] `tests/unit/test_sync_mail.py` にフラグリフレッシュの単体テストを追加する（対象範囲・TTL間引き・変化なし時に書き込みしないことを検証）
- [ ] `tests/integration/` にGreenMail/Dovecot結合テストを追加し、サーバー側フラグ変更が次回同期で反映されることを検証する
- [ ] Dovecotで `CONDSTORE` 対応を確認し、対応していれば `iter_flags_since` の結合テストを追加する（お名前.com実サーバーでは対応確認済み。Dovecot/GreenMailが非対応の場合はテスト環境側の制約として記録し、フォールバック経路のテストで代替する）
- [ ] `tests/unit/test_config.py` に新設定項目のテストを追加する
- [ ] `tests/gui/` のツールチップ表示テストを更新する

### **Group G: ドキュメント整合**

- [ ] 開発計画書 4.6-2・9章の該当記述を「同期時点の一度きりのスナップショット」から「定期的に取り直すスナップショット（双方向同期・ローカル既読管理は引き続き行わない）」に改訂する
- [ ] `ruff check .` / `mypy .` / `pytest` を実行し、既存テストが壊れていないことを確認する
