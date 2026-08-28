# **Phase 4: 統合 & 例外処理 実装計画書**

対象: [ローカルメールバックアップ＆閲覧アプリ 開発計画書.md](./ローカルメールバックアップand閲覧アプリ開発計画書.md) の「6. 開発ロードマップ」における **Phase 4: 統合 & 例外処理**

前提: [Phase 3: GUI基礎構築 実装計画書](./実装計画書_Phase3_GUI基礎構築.md)、[Phase 3.5: ストレージ暗号化要件の緩和](./実装計画書_Phase3.5_ストレージ暗号化要件の緩和.md)、[Phase 3.6: ストレージルート切替とセットアップウィザード呼び出し](./実装計画書_Phase3.6_ストレージルート切替とセットアップウィザード呼び出し.md)、[Phase 3.7: IMAPフラグの定期リフレッシュ](./実装計画書_Phase3.7_IMAPフラグの定期リフレッシュ.md) の成果物が完成していること。

本書と開発計画書に矛盾がある場合は、**開発計画書を正**とする。

本書は [Phase4 レビュー修正案](./実装計画書_Phase4_レビュー修正案.md) の指摘をすべて反映済みである。両者に矛盾がある場合はレビュー修正案の判断を優先する。

---

## **1. 目的**

**「一度実行すると取り返しがつかない操作」と「予告なく発生する物理切断」を、設計不変条件を崩さずに扱えるようにし、mail-dock を実運用に耐えるバックアップアプリにする。**

Phase 3.x までで「導入 → 同期 → 閲覧 → 検索 → 保存」は GUI だけで一周できるようになった。しかし現状は次の状態にある。

* サーバー上のメールを削除する機能が**ユースケースとして存在しない**（`OnamaeImapFetcher.delete_remote_message()` は実装済みだが、安全装置が無いため意図的にどこからも呼んでいない）。
* ローカルのゴミ箱・purge が無く、`local_state` の `trashed` / `purged` は**スキーマ上の定義だけ**が存在する。
* 稼働中に外付けドライブが抜かれた場合、`StorageDetachedError` を Signal で通知してバナーを出すところまでしか実装されていない（Phase 3 D-16 の骨組み）。
* 「EMLと永続マニフェストが真実の情報源であり、`metadata.db` はいつでも再構築可能な派生キャッシュである」という**設計不変条件1が、まだコードで実証されていない**。

本フェーズは以下を満たす。

1. **不変条件2（書き込み順序）が、どの時点で電源やUSBが切れても破られないことを実証すること** — フォールト注入により、EML保存・マニフェスト追記・DBコミットの各段階で切断を再現し、発生し得る状態が「DB未登録のEML」だけであることをテストで固定する（開発計画書 1.3-2 / 5.10）。
2. **不変条件1を実証すること** — `metadata.db` を捨てて EML と永続マニフェストだけからDBを完全再構築できるようにする（同 1.3-1 / 4.8）。
3. **不変条件3（多段防御）をすべての破壊的操作へ適用すること** — サーバー削除とローカルpurgeの両方に、事前検証・確認・監査ログ・猶予期間を実装し、ワンクリックの取り消し不能操作を作らない（同 1.3-3 / 4.3 / 4.4）。
4. **切断を「異常系」ではなく「常態」として扱うこと** — 予防（安全な取り外し）・検知（3系統）・縮退（状態機械）・復帰（範囲限定検証）の4段構えを実装する（同 5.7.1）。
5. **常駐アプリとして運用できるようにすること** — 定期自動同期・システムトレイ・失敗メールの運用UI・DBバックアップ・ログ保持を実装する（同 5.5 / 5.7 / 5.8）。

**Phase 4 のゴール判定:** 6章の検証項目がすべて成功し、CIが緑になること。とくに **V-2（フォールト注入4点）と V-3（マニフェストからのDB再構築）** は本フェーズの中核であり、これが通らない限り Phase 4 は完了しない。

---

## **2. 要件**

### **2.1 前提となる意思決定（確定済み）**

| # | 項目 | 決定内容 |
| :--- | :---- | :---- |
| D-1 | 実装順序 | **切断対策（開発計画書 5.7.1）を最優先**とする。サーバー削除もローカルpurgeも「ストレージが `ATTACHED` であること」を入口条件に持つため、状態機械が無い状態でこれらを実装すると、切断中にハッシュ検証できないままサーバー削除が通る経路を作ってしまうため |
| D-2 | 計画書の構成 | **本フェーズは1冊の計画書にまとめ**、内部をグループA〜Hに分割する。切断・削除・purge・再構築は相互に依存条件を持ち、別冊に分けると依存関係の追跡コストが上回るため |
| D-3 | CLIへの公開範囲 | **検証（`verify`）と再インデックス（`reindex`）のみ CLI へ追加する。サーバー削除・ローカルpurge は GUI 限定**とする。Phase 3.5 D-10 と同じ思想であり、取り消しの効かない安全判断を、確認ダイアログの無い経路で通過させないため。CLIに削除系サブコマンドが存在しないことを静的テストで固定する |
| D-4 | 再インデックスの範囲 | **IMAP永続マニフェスト＋EMLからのDB完全再構築を実装する**。accounts / folders / messages / message_contents / FTS / purge墓標 / 監査イベントを復元する。PST永続マニフェストは Phase 4.5 のため、読み取り口だけ用意して未対応形式は明示的にスキップする |
| D-5 | 検証の実行スレッド | **3本目のワーカー `VerifyWorker`（読み取り中心）を追加する**。フル検証は100GB分のSHA-256再計算で数十分かかり、`SyncWorker` を占有すると同期・purge・削除がすべて止まるため。ただし**「書き込みは単一ライターに集約」という不変条件は崩さない**。検証結果を反映するDB書き込み（孤児の取り込み・未取得への差し戻し・再構築）は `SyncWorker` 側へ回すか、`SyncWorker` の停止を確認したうえで排他実行する |
| D-6 | 切断からの復帰 | **Phase 3.6 の切替基盤（旧セッション解放 → 新 `StorageSession` 開始 → `MainWindow` 差し替え）を再利用**し、同一プロセス内で `RECONNECTING` → `VERIFYING` → `ATTACHED` まで戻す。プロセス再起動を強制しない。数時間規模の初回同期中に瞬断が起きても、差し直しで復帰できることを優先する |
| D-7 | 範囲限定検証のチェックポイント | **永続マニフェストへ `checkpoint` イベントを追記し、最後の `checkpoint` 以降のEMLだけを再ハッシュする**。`app_state` テーブルに置く案は採らない。DBは「いつでも再構築可能な派生キャッシュ」であり、DBが壊れた・巻き戻った状況で検証範囲を決める根拠をDBに置くと循環するため。`checkpoint` は「このマニフェスト位置まで対応するDB変更がコミット済みである」ことを示す**完了マーカー**であり、DBコミットが**成功した後**に追記する。DBコミット前に書くと、コミットに失敗した項目まで検証済み範囲に含まれてしまうため（レビュー修正案 3.1） |
| D-8 | 読み取り専用の縮退モード | **提供しない**（開発計画書 5.7 の既定方針を維持）。`DETACHED` 中は「再接続を試す／終了」のモーダルのみを提示する。DBが読めない以上、中途半端に閲覧できる状態を作ると「保存されているつもりで保存されていない」誤解を生むため |
| D-9 | `FOREIGN` の扱い | `MISSING` より危険なものとして扱い、検出時は**即座に全書き込みを禁止**する。復帰判定は常に `.maildock_root` のUUID照合で行い、パスの存在を同定根拠にしない（開発計画書 2.4-10） |
| D-10 | 瞬断と抜去の区別 | I/Oエラー検知後、`config.reprobe_attempts`（既定3）回・**500ms間隔**でリプローブし、UUIDが一致して復帰した場合は「接続は生きているがハンドルだけが死んでいる」扱いとして、全接続を張り直しバッチ境界から再開する。3回失敗した場合のみ `DETACHED` へ遷移する |
| D-11 | サーバー削除の既定動作 | **ゴミ箱フォルダへ MOVE**（`config.remote_delete_mode = "trash"`）を既定とし、`STORE +FLAGS \Deleted` + `EXPUNGE`（`"expunge"`）は明示オプションとする。`config.remote_delete_mode` の設定値は `trash` / `expunge` の2値へ統一し、既存設定に残る `permanent` は読み込み時に `expunge` へ移行する（保存時は新しい値のみ書き出す）。UID EXPUNGE（対象UIDのみを永久削除できる操作。`UIDPLUS` 拡張）に対応しないサーバーでは `expunge` を拒否し、フォルダ全体 `EXPUNGE` へフォールバックしない。**既存の `OnamaeImapFetcher.delete_remote_message()` は `UIDPLUS` 非対応時にフォルダ全体 `expunge()` へフォールバックする実装になっており、この経路は Phase 4 で廃止する**（レビュー修正案 3.4 / 3.8）。1回の操作で削除できる上限は `config.delete_batch_limit`（既定1,000通） |
| D-12 | サーバー削除の事前条件 | **コードで強制し、満たさないメールは対象から自動除外する**（UIで警告するだけにしない）。①ローカルEMLが実在 ②その場で再計算した SHA-256 が `messages.file_hash` と一致 ③`message_contents` が存在（パース成功済み）。加えて `ATTACHED` 以外はユースケース入口で無条件に拒否する |
| D-13 | purge の共有EML対応 | 同一内容のEMLは物理ファイルを共有し得るため、**同じ `relative_path` を参照する非purgedレコードが残っていないことを確認し、最後の参照が消える場合だけ実ファイルを削除する**。`messages` 行は墓標として残す |
| D-14 | purge の実行契機 | `config.purge_mode` の3値（`manual` / `grace` / `immediate` ＝ 開発計画書4.4のA/B/C）をすべて実装する。**既定は `manual`**。`immediate` を選んだ場合は設定画面に「確認なしでファイルが削除されます」を常時表示する |
| D-15 | トレイ常駐時の閉じる挙動 | **閉じる → トレイへ最小化**とし、終了はトレイメニューまたはファイルメニューからのみとする。開発計画書 5.8 の「最小化中も同期を継続する」と整合させるため |
| D-16 | 実機テスト | **フルスケール実機同期（5万通/100GB）とVHDXの `detach vdisk` による実デバイス切断試験は本フェーズでは実施せず、手動検証手順として本書に記載するに留める**。代替として**フォールト注入による自動テスト**を必須とし、CIで常時実行する |
| D-17 | `synchronous` の決定 | D-16 により実測ができないため、**既定 `NORMAL` を維持し、最終決定は本フェーズのスコープ外**とする（開発計画書 3.6 / 5.11）。「コミット時のみ `FULL`」への切り替え口だけを設計上残し、実測後に設定1箇所の変更で切り替えられるようにする |
| D-18 | CSVエクスポート | **検索結果の汎用CSVエクスポートは実装しない**。開発計画書 4.9 でも優先度「任意」であり、mbox と添付一括抽出で運用上の要求は満たせるため。ただし**サーバー削除ドライランの監査用CSV（F-29）は対象内**であり、両者はスコープが異なるため混同しない（レビュー修正案 3.8） |
| D-19 | 依存関係 | **追加しない**。mbox は標準ライブラリ `mailbox`、`WM_DEVICECHANGE` 監視は `ctypes` と PySide6 の `QAbstractNativeEventFilter`、DBバックアップは `sqlite3.Connection.backup()` で実装する |
| D-20 | 状態機械の置き場所 | **遷移ロジックはQt非依存の純粋クラスとして `domain/storage_state.py` に置く**。QTimer・`WM_DEVICECHANGE`・Signal配線は `presentation` 側に置き、遷移表そのものを通常CIの単体テストで固定する（Phase 3 D-28 と同じ方針） |
| D-21 | 非Windows環境 | `WM_DEVICECHANGE` 監視は Windows 専用のため、**非Windowsでは no-op 実装**とし、ハートビートとI/O例外分類の2系統だけで動作させる。`import` は常に成功すること（CIのLinuxジョブが落ちないため） |
| D-22 | 監査ログの扱い | `audit_log` は**永久保存**とし、purge・ログローテーションの対象にしない。表示画面は**読み取り専用**とし、削除・編集の導線を作らない |
| D-23 | 削除確認の方式 | 削除実行前に「ドライラン一覧 → 件数と合計サイズを表示 → **件数を手入力させる** → 実行」の順を強制する。チェックボックス1つでの承認は認めない（開発計画書 4.3） |
| D-24 | ログ出力先の切替 | `DETACHED` 遷移時は**必ず内蔵ディスク側の `{config_dir}/logs/app.log` へ切り替える**。同期ログは `{storage_root}/logs/` にあり、切断時はまさにそこへ書けないため。切断イベントこそ内蔵ディスク側に残す必要がある（開発計画書 5.5 / 5.7.1-3） |
| D-25 | `tmp/` の掃除 | `tmp/*.eml` は無条件削除でよいが、**`tmp/pstimp/{job_id}/` は再開用であり削除してはならない**。既存 `cleanup_tmp()` はこの区別を実装済みのため、Phase 4 ではコメントとテストで固定するに留める |

### **2.2 機能要件**

#### **切断対策（開発計画書 5.7.1）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-1 | ストレージ状態が `ATTACHED` / `DEGRADED` / `DETACHED` / `DETACHED_BY_USER` / `RECONNECTING` / `VERIFYING` の6状態で管理され、遷移がQt非依存のクラスとして単体テストできること | 5.7.1-3 / D-20 |
| F-2 | I/O例外の分類（既存 `detach.py`）・`.maildock_root` のハートビート・`WM_DEVICECHANGE` の3系統で切断を検知すること。ハートビートは存在確認ではなく**UUID照合**を行い `OK` / `MISSING` / `FOREIGN` を返すこと | 5.7.1-2 / D-9 |
| F-3 | I/Oエラー検知後、500ms間隔で `config.reprobe_attempts` 回リプローブし、復帰した場合は接続を張り直してバッチ境界から再開すること。3回失敗時のみ `DETACHED` へ遷移すること | 5.7.1-2(d) / D-10 |
| F-4 | `DETACHED` 遷移時に、①書き込みを一切試みない ②ワーカーをI/Oを伴わない経路で停止する ③全SQLite接続を close する ④ハートビート更新を停止する ⑤ログ出力先を `{config_dir}/logs/app.log` へ切り替える ⑥「サーバーから削除」を無効化する — をすべて実行すること | 5.7.1-3 / D-24 |
| F-5 | 「ストレージを安全に取り外す」メニューがあり、ワーカー停止（バッチ境界待ち）→ `PRAGMA wal_checkpoint(TRUNCATE)` → 全接続 close → QWebEngineプロファイル破棄 → ログハンドル close → `.lock` 解放・削除 → `DETACHED_BY_USER` 表示、の順で実行すること | 5.7.1-1 |
| F-6 | `app_state` に `clean_shutdown` フラグを持ち、正常終了時に `1`、起動時に `0` へ戻すこと。起動時に `0` のままなら前回異常終了と判定し、マニフェスト末尾修復と範囲限定検証を自動実行すること | 5.7.1-4 |
| F-7 | `DETACHED` から `DBT_DEVICEARRIVAL` または手動再試行で `RECONNECTING` へ入り、UUID照合・`.lock` 再取得・`PRAGMA user_version` 確認・`PRAGMA quick_check` を経て `VERIFYING` へ進み、範囲限定検証が通れば `ATTACHED` へ戻ること。この復帰が同一プロセス内で完結すること | 5.7.1-4 / D-6 |
| F-8 | 読み取り専用の縮退モードを提供しないこと。`DETACHED` 中は「再接続を試す／終了」のみを提示すること | 5.7 / D-8 |
| F-9 | スタール `.lock`（ロック実体は取得できるが `heartbeat_at` が古い）を「前回の異常終了」と判定し、起動できること | 3.6 |
| F-10 | `WM_DEVICECHANGE` 監視モジュールが非Windowsでも import でき、no-op として動作すること | D-21 |

#### **整合性チェック・再構築（開発計画書 4.8）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-11 | 永続マニフェストに `checkpoint` イベントを追記でき、DBコミット成功後に同期のバッチコミット境界で記録されること（コミット完了マーカー） | D-7 |
| F-12 | クイック検証（`relative_path` の存在確認とサイズ照合）が起動時に自動実行されること | 4.8 |
| F-13 | 範囲限定検証が、最後の `checkpoint` 以降のEMLのみSHA-256を再計算し、不一致レコードを未取得へ戻して当該EMLを `tmp/` へ隔離すること | 4.8 / 5.7.1-4 |
| F-14 | フル検証が全EMLのSHA-256を再計算し、進捗表示とキャンセルに対応すること。UIをブロックしないこと | 4.8 / 5.4 |
| F-15 | 孤児スキャンが `eml/` 配下のDB未登録ファイルを検出し、マニフェストの `fetch` イベントで出所（`source_item_key` / パス / ハッシュ）を確定できる孤児だけを再登録し、対応イベントの無い孤児は隔離すること（UID・フォルダを推測して自動登録しない） | 4.8 / レビュー修正案 3.6 |
| F-16 | マニフェスト検証が、全マニフェストのCRC32・スキーマを検証し、**末尾の不完全レコードのみ**を安全に切り離すこと。末尾以外の破損は `ManifestCorruptError` として報告すること | 4.8 / 2.4-7 |
| F-17 | 再インデックスが `metadata.db` を破棄し、EML群と永続マニフェストから accounts / folders / messages / message_contents / FTS / purge墓標 / 監査イベントを再構築できること。`messages.id` / `folders.id` などのDB固有サロゲートIDに依存せず、自然キーとスナップショットイベントから再構築できること | 4.8 / 1.3-1 / D-4 / レビュー修正案 3.2 |
| F-18 | 再解析（パース失敗メールのみ本文・添付名を抽出し直す）がGUIから実行できること | 4.7 / 5.6 |
| F-19 | `verify` サブコマンドが `--mode quick\|range\|full\|orphans\|manifest` を受け付け、`reindex` サブコマンドが追加されること。削除系サブコマンドが存在しないこと | D-3 |

#### **ローカルゴミ箱・purge（開発計画書 4.4）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-20 | 「ローカルから削除」で `local_state='trashed'` と `trashed_at` が記録され、**この時点ではEMLが残る**こと | 4.4-1 |
| F-21 | ゴミ箱ビューから「元に戻す」で `active` へ復帰できること。残り日数が表示されること | 4.4-2 / 4.6-2 |
| F-22 | purge が「マニフェストへ `purge_intent` 追記+fsync → 共有参照ゼロの確認 → EML削除 → マニフェストへ `purged` 追記+fsync → `message_contents` 削除 → `local_state='purged'`, `relative_path=NULL`」の順で実行されること | 4.4-4 |
| F-23 | 同じ `relative_path` を参照する非purgedレコードが残っている場合、実ファイルを削除しないこと | 2.4-8 / D-13 |
| F-24 | `messages` の行が墓標レコードとして残り、`message_contents` の削除によりFTSからも除去されること | 3.4 / 4.4-4 |
| F-25 | purge 実行モードが `manual` / `grace` / `immediate` から選択でき、既定が `manual` であること。`immediate` 選択時は設定画面に警告を常時表示すること | 4.4 / D-14 |
| F-26 | purge 実行前に対象件数と合計サイズがログへ記録され、`audit_log` に `operation='local_purge'` が残ること | 4.4-5 |

#### **サーバーメール手動削除（開発計画書 4.3）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-27 | 削除対象が3つの事前条件（EML実在・SHA-256一致・`message_contents` 存在）をコードレベルで検証され、満たさないメールが自動除外されること | 4.3 / D-12 |
| F-28 | ストレージ状態が `ATTACHED` 以外のとき、削除ユースケースの入口で無条件に拒否されること | 4.3 / 5.7.1-3 |
| F-29 | ドライランで対象一覧（件名・日付・サイズ・合計容量）を表示でき、CSVへ保存できること。このCSVは削除ドライラン専用の監査用CSVであり、検索結果の汎用CSVエクスポート（D-18で対象外）とはスコープが異なる | 4.3 / D-18 |
| F-30 | 確認ダイアログが件数と合計サイズを表示し、**件数の手入力**を要求すること | 4.3 / D-23 |
| F-31 | 既定動作が `trash`（ゴミ箱フォルダへの MOVE）であり、`expunge` が明示オプションであること。UID EXPUNGEに対応しないサーバーでは `expunge` が拒否されること。1回の操作の上限が `config.delete_batch_limit` であること | 4.3 / D-11 |
| F-32 | ゴミ箱フォルダが「SPECIAL-USE の `\Trash` → 一般的な候補名の自動探索 → 設定による手動指定」の順で決定され、特定できない間は削除機能が無効化されること | 4.3 |
| F-33 | 削除が永続マニフェストへ `remote_delete_intent` / `remote_delete_completed` / `remote_delete_uncertain` として記録され、`audit_log` にも日時・アカウント・Message-ID・件名・サイズ・モードが残ること。状態確定後に `remote_state='deleted'` へ更新され一覧でグレーアウトすること | 4.3 / レビュー修正案 3.4 |

#### **エクスポート（開発計画書 4.9）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-34 | フォルダ単位・検索結果単位で mbox エクスポートができること（標準ライブラリ `mailbox` を使用） | 4.9 |
| F-35 | 添付ファイルの一括抽出ができ、既存の `sanitize_attachment_name()` と保存直前の `resolve_within()` を必ず経由すること | 4.9 / 4.6-4 |

#### **運用UI・自動化（開発計画書 5.5 / 5.7 / 5.8）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-36 | `config.sync_interval_minutes`（既定60、0で無効）の QTimer による定期同期が動作し、前回の同期が実行中ならスキップすること。ルート未接続時はダイアログを出さず静かにスキップすること | 5.8 / 5.7 |
| F-37 | システムトレイに常駐して同期状態を表示し、**ウィンドウを閉じるとトレイへ最小化**され、終了はトレイメニューまたはファイルメニューからのみ可能であること | 5.8 / D-15 |
| F-38 | `sync_failures` の「要確認」一覧（`attempt_count >= 10`）が表示できること | 5.6 |
| F-39 | `oversize` でスキップされたメールをUIから個別に「それでも取得する」で再取得できること | 4.2-3 |
| F-40 | 監査ログ表示画面が読み取り専用で `audit_log` を新しい順に表示し、削除・編集の導線を持たないこと | 5.5 / D-22 |
| F-41 | `sqlite3.Connection.backup()` により週1回および終了時に `metadata.db.bak` が作成されること。`db_backup_to_local_disk` は既定OFFで、有効化時は複製先の暗号化状態が保管元より弱い場合に警告すること | 5.7 / Phase 3.5 D-16 |
| F-42 | `{storage_root}/logs/sync-*.log` が `config.sync_log_retention_days`（既定90日）を過ぎたら削除されること。`audit_log` は削除対象にしないこと | 5.5 / D-22 |
| F-43 | 設定画面に purge モード・ゴミ箱猶予日数・サーバー削除モード・ハートビート間隔・定期同期間隔が追加されること（Phase 3 D-15 で非表示にしていた項目の解禁） | 5.11 |

### **2.3 非機能要件・制約**

| # | 指標 | 目標値 | 備考 |
| :--- | :---- | :---- | :---- |
| N-1 | ハートビート間隔 | **5秒**（`config.heartbeat_interval_sec`） | 0による無効化を許可しない（安全上の必須機能。開発計画書 5.11） |
| N-2 | 瞬断リプローブ | **500ms間隔・3回** | 超過で `DETACHED` へ遷移（同 5.11） |
| N-3 | 範囲限定検証の所要時間 | **起動パスに置いても実用的な範囲** | 対象は最後の checkpoint 以降のみ。数百件・数百MB規模を想定（同 4.8） |
| N-4 | UI応答 | フル検証・再インデックス中もUIが操作可能 | 進捗表示とキャンセルを必ず設ける（同 5.4） |
| N-5 | メモリ使用量 | フル検証中も **600MB 以下** | EMLはチャンク読みでハッシュ計算し、全体をメモリへ載せない（同 5.1） |
| N-6 | WALサイズ | 約1,000通ごとに `wal_checkpoint(TRUNCATE)` | 切断時に失われ得る範囲＝再検証すべき範囲を一定以下に保つ（同 3.6） |

* `ruff format --check` / `ruff check` / `mypy` / `pytest` を通すこと。
* レイヤーの依存方向を厳守する（`domain` ← `usecases` ← `presentation`）。`domain` / `usecases` に PySide6 を import しない。`presentation/views` / `viewmodels` / `models` に `sqlite3` と `mail_dock.infrastructure` を import しない（Phase 3 F-26 を維持）。
* **書き込みは同期ワーカー1本に集約する不変条件を崩さない**。`VerifyWorker` は読み取り中心とし、DB書き込みを伴う後処理は `SyncWorker` へ回すか排他実行する（D-5）。
* `winerror` / `sqlite_errorname` を `infrastructure/storage/detach.py` の外へ漏らさない（既存の境界を維持する）。
* 本文テキストをログへ出力しない。件名は先頭20文字、メールアドレスはマスクする（Phase 0 の `MaskingFilter`）。
* 破壊的操作のマニフェスト追記は**必ず fsync してからDBを更新する**。逆順にしない。

---

## **3. タスク**

> 依存関係: **A → B → (C・D を並行) → E → (F・G を並行)**。H（テスト・ドキュメント）は全グループと並行して作成する。

### **3.1 グループA: マニフェスト・DB基盤の拡張（*全グループの前提。最初に着手*）**

#### **A-1. `infrastructure/storage/manifest.py` — イベント種別の拡張**

- [x] `_MANIFEST_EVENTS` へ `checkpoint` / `account_snapshot` / `folder_snapshot` / `purge_intent` / `purged` / `remote_delete_intent` / `remote_delete_completed` / `remote_delete_uncertain` を追加する（`remote_delete` は単独では追加せず、意図・成功確認・不確定状態の3イベントへ分割する。レビュー修正案 3.1 / 3.2 / 3.4 / 3.8）
- [x] `_validate_event()` に各イベントの必須フィールド検証を追加する
    - [x] `checkpoint`: `account_id` / `timestamp` / 単調増加する `sequence` / 対象バッチを識別する `batch_id`。`sequence` の重複・逆行を検出し、マニフェストのファイルローテーション後も `batch_id` で対象バッチを追跡できること（レビュー修正案 3.1）
    - [x] `account_snapshot`: `account_id` / `provider_type` / `display_name` / 接続先の非秘密情報 / `timestamp`。**資格情報・パスワード・アクセストークンは記録しない**（レビュー修正案 3.2）
    - [x] `folder_snapshot`: `account_id` / `folder_raw_name` / `display_name` / `uidvalidity` / `delimiter` 等のフォルダ属性 / `timestamp`（レビュー修正案 3.2）
    - [x] `purge_intent` / `purged`: `account_id` / `source_item_key` / `relative_path` / `file_hash` / `timestamp` に加え、共有参照確認の結果・物理削除を実施するかどうかを記録する（レビュー修正案 3.3）
    - [x] `remote_delete_intent` / `remote_delete_completed` / `remote_delete_uncertain`: `account_id` / `folder_raw_name` / `uid` / `uidvalidity` / `mode`（`trash` または `expunge`）/ `timestamp`。`uncertain` は再接続後の照合で `completed` または取り消しへ確定させる（レビュー修正案 3.4 / 3.8）
- [x] `fetch` イベントへ `internal_date` を追加する（追加が必要なのはこの1点のみ。既存 `_FETCH_FIELDS` は `message_id` / `size_bytes` をすでに保持しており、`content_key` は `derive_content_key(message_id, eml_sha256)` で導出され、`message_contents`（件名・送信者・本文・添付名）は `parse_eml()` の出力から導出されるため、解析済みコンテンツはマニフェストへ複製しない。再構築（C-2）は `fetch` イベントの `relative_path` からEMLを読み直し、既存 `reparse.py` の解析経路で `message_contents` を作る。レビュー修正案 9.2）
- [x] `moved` イベントへ `moved_to_folder_raw_name`（移動先フォルダの自然キー）を追加する。**既存の `usecases/sync_mail.py` は移動検出イベントへ `"folder_id": folder_id` と `"moved_to_folder_id": moved_to` というDB固有サロゲートIDを直接書き込んでおり（`moved_to_folder_id` はマニフェストの正本にしないと決めたものそのもの）、Phase 4 で `targets` 一覧から `folder_id → raw_name` を解決して `moved_to_folder_raw_name` を追加し、`moved_to_folder_id` の直接記録を廃止する必要がある**（レビュー修正案 3.2 / 9.4）
- [x] `ManifestWriter.checkpoint(sequence, batch_id)` を実装する（append → `flush_and_sync()` まで1操作で行う）
- [x] 最後の `checkpoint` 以降のイベントだけを列挙する読み取りAPIを追加する（範囲限定検証の入力。既存 `read_events()` を再利用し、末尾修復の挙動を変えないこと）
- [x] 対応する完了イベントの無い `purge_intent` / `remote_delete_intent` を列挙する読み取りAPIを追加する（未完了intentの起動時回復用。レビュー修正案 3.3 / 3.4）
- [x] `purge_intent` / `purged` を「Phase 4 で実装する予約」と記した既存 docstring を、実装済みの記述へ更新する

#### **A-2. `domain/ports.py` — マニフェストポートの拡張**

- [x] `BaseManifestWriter` に `checkpoint(sequence: int, batch_id: str) -> None` を追加する
- [x] `BaseManifestReader` を新設し、全イベント列挙・最後の `checkpoint` 取得・`checkpoint` 以降のイベント列挙・未完了 `purge_intent` / `remote_delete_intent` の列挙を提供する（レビュー修正案 3.5）
- [x] 既存のテストダブル（`tests/support/` 配下）をすべて更新する

#### **A-3. `usecases/sync_mail.py` — checkpoint と WAL チェックポイントの記録**

- [x] バッチコミット境界（既存 `_BATCH_MESSAGE_LIMIT` / `_BATCH_BYTES_LIMIT`）でDBコミットを行う
- [x] **バッチコミット10回ごと（＝約1,000通ごと）に `PRAGMA wal_checkpoint(TRUNCATE)` を実行**する（N-6）
- [x] checkpoint は「このマニフェスト位置まで対応するDB変更がコミット済みである」ことを表す**完了マーカー**とし、`ManifestWriter.checkpoint()` の追記は**DBコミットが成功した後**に fsync まで完了させる（レビュー修正案 3.1。DBコミット前に checkpoint を書かない）
- [x] DBコミットに失敗した場合は checkpoint を追記しないこと、失敗した直前のバッチが次回の範囲限定検証で再検証対象になることをコードで保証する

#### **A-4. `migrations/005_phase4.sql` — 運用クエリ用インデックス**

- [x] `CREATE INDEX idx_audit_recent ON audit_log(occurred_at DESC)` を追加する（監査ログ表示画面用）
- [x] `CREATE INDEX idx_msg_purge ON messages(account_id, local_state, trashed_at)` を追加する（purge候補抽出用。既存の `idx_msg_trash` はアカウント横断のため、アカウント単位の抽出を補う）
- [x] `CREATE INDEX idx_msg_path ON messages(account_id, relative_path)` を追加する（共有EML参照チェック用。F-23 の判定が全表走査にならないようにする）
- [x] マイグレーション適用前の自動バックアップ（`metadata.db.bak.{version}`）が働くことを既存機構で確認する

#### **A-5. リポジトリの監査・状態更新メソッド追加**

- [x] `BaseMessageRepository` に以下を追加する（**目的を超えたメソッドを足さない**という制約を守り、Phase 4 のユースケースが必要とする最小限に限定する）
    - [x] `record_audit(entry) -> None`
    - [x] `list_audit_log(limit, offset) -> Sequence[MessageRecord]`
    - [x] `set_local_state(message_id, state, trashed_at=None) -> None`
    - [x] `list_trashed(account_id=None, older_than=None) -> Sequence[MessageRecord]`
    - [x] `count_path_references(account_id, relative_path, exclude_message_id) -> int`（purged を除外して数える）
    - [x] `delete_message_contents(message_id) -> None`
    - [x] `get_app_state(key) -> str | None` / `set_app_state(key, value) -> None`
    - [x] 再構築（C-2）が必要とする最小限のメソッド: ID指定のメッセージ取得・保存パスを持つメッセージの列挙・`message_contents` 存在確認・検証結果を単一ライターへ渡すための状態更新・再構築用のアカウント/フォルダ/メッセージ投入（レビュー修正案 3.5。再構築処理そのものはinfrastructure側の再構築コーディネータが担当し、usecaseから直接SQLiteファイルを操作しない）
- [x] `SqliteMessageRepository` に実装する
- [x] `tests/support/in_memory_repository.py` に同メソッド群を追加する

#### **A-6. アカウント/フォルダ snapshot の書き込みとバックフィル（レビュー修正案 9.1）**

- [x] アカウント登録・設定変更の既存ユースケース入口へ、`account_snapshot` の追記を追加する
- [x] フォルダ属性が変化した際（`is_sync_target` 切替え等）に `folder_snapshot` を追記する
- [x] 直前の snapshot と非秘密フィールドが完全一致する場合は追記を省略し、マニフェストの肥大化を防ぐ
- [x] Phase 4 導入時の起動パスへ、既存アカウント・既存フォルダについて `account_snapshot` / `folder_snapshot` が一度も存在しない場合に現在の状態で遡及して一回だけ書く**バックフィル**を実装する（B-5の起動パスから呼ぶ。これが無いとPhase 3以前作成の既存アカウントは `reindex` で復元不能になる）
- [x] `sqlite3` / PySide6 / infrastructure の具象を import しないこと

---

### **3.2 グループB: 稼働中のストレージ切断対策（*最優先。D-1*）**

#### **B-1. `domain/storage_state.py` — 状態機械（*Qt非依存*）**

- [x] `StorageState(StrEnum)` を定義する: `ATTACHED` / `DEGRADED` / `DETACHED` / `DETACHED_BY_USER` / `RECONNECTING` / `VERIFYING`
- [x] `StorageEvent(StrEnum)` を定義する: `PROBE_OK` / `PROBE_MISSING` / `PROBE_FOREIGN` / `IO_ERROR` / `REPROBE_OK` / `REPROBE_FAILED` / `DEVICE_REMOVED` / `DEVICE_ARRIVED` / `USER_DETACH` / `RECONNECT_REQUESTED` / `IDENTITY_OK` / `IDENTITY_FOREIGN` / `VERIFY_OK` / `VERIFY_FAILED`（`PROBE_*` は定期ハートビート監視の結果、`REPROBE_*` は瞬断からの再試行結果として明確に区別する。レビュー修正案 3.7）
- [x] `StorageStateMachine` を実装し、開発計画書 5.7.1-3 の遷移図に加え、次の遷移を明示する: `ATTACHED + PROBE_MISSING -> DEGRADED` / `ATTACHED + PROBE_FOREIGN -> DETACHED`（`FOREIGN` はリプローブを待たず即座に遷移する。レビュー修正案 3.7）
- [x] `is_write_allowed()` / `is_remote_delete_allowed()` を公開し、**`ATTACHED` 以外では両方とも偽**を返すこと（`DEGRADED` は瞬断判定中であり、書き込みを許可しない）
- [x] 外部依存ゼロを維持し、PySide6 / `sqlite3` / ファイルI/O を持ち込まない

#### **B-2. `presentation/native/device_watcher.py` — `WM_DEVICECHANGE` 監視**

- [x] `presentation/native/__init__.py` を新規作成する
- [x] `DeviceWatcher(QAbstractNativeEventFilter)` を実装する
    - [x] `DBT_DEVICEQUERYREMOVE (0x8001)`: **本命の分岐**。ハンドルを閉じて取り外しを許可する（拒否するとユーザーが引き抜く）
    - [x] `DBT_DEVICEREMOVECOMPLETE (0x8004)`: I/Oエラーを待たず `DETACHED` へ即遷移
    - [x] `DBT_DEVICEARRIVAL (0x8000)`: 再検出をトリガー
 [x] `DBT_DEVTYP_VOLUME` のブロードキャストから `dbcv_unitmask` を読み、ドライブレターを復元する
- [x] **非Windowsでは no-op 実装**とし、`install()` / `uninstall()` が安全に呼べること（D-21）
 - [x] ドライブレターの復元ロジックをQt非依存の純粋関数へ分離し、通常CIでテストする

#### B-3. `presentation/storage_monitor.py` — ハートビートと縮退制御

- [x] `StorageMonitor(QObject)` を実装する
- [x] `config.heartbeat_interval_sec`（既定5秒）の `QTimer` で `storage_root.probe(root, root_uuid)` を実行し、`PROBE_OK` / `PROBE_MISSING` / `PROBE_FOREIGN` を状態機械へ渡す（レビュー修正案 3.7。定期ハートビートの結果であり、瞬断リプローブの `REPROBE_*` とは別イベントとして扱う）
- [x] `PROBE_OK` のときだけ `StorageLock.touch_heartbeat()` を呼ぶ
- [x] `PROBE_FOREIGN` 検出時は即座に全書き込みを禁止し、警告を表示する（D-9）
- [x] `QueryWorker` / `SyncWorker` / `VerifyWorker` の `storage_detached` Signal を集約し、`IO_ERROR` として状態機械へ渡す
- [x] 瞬断判定: `IO_ERROR` 受信後、500ms 間隔で `config.reprobe_attempts` 回リプローブする。UUID一致で復帰したら全接続を張り直して `ATTACHED` へ戻す（F-3）
- [x] `storage_state_changed` Signal で `MainWindow` へ通知する
- [x] `DETACHED` 遷移時の処理を実装する（F-4）
    - [x] 書き込みを一切試みない（死んだハンドルへの再書き込みを行わない）
    - [x] ワーカーを `CancelToken` で停止する。**停止処理自体がI/Oを伴わない**ことをコードで保証する
    - [x] 全SQLite接続を close し、プールに残さない
    - [x] `.lock` のハートビート更新を停止する
    - [x] ログ出力先を `{config_dir}/logs/app.log` へ切り替える（D-24）

#### **B-4. 「ストレージを安全に取り外す」メニュー（F-5）**

- [x] `main_window.py` の「ストレージ」メニュー（Phase 3.6 で追加済み）へアクションを追加する
- [x] 以下の順序で実行する
    - [x] 1. 同期／検証ワーカーへ `CancelToken` を送り、**バッチ境界で停止するまで待つ**
    - [x] 2. `PRAGMA wal_checkpoint(TRUNCATE)` で `-wal` を空にする
    - [x] 3. 全SQLite接続を `close()` する（スレッドローカル接続を漏れなく破棄する）
    - [x] 4. QWebEngineプロファイルを破棄する（本文・添付のファイルハンドル解放）
    - [x] 5. `logs/` のファイルハンドルを閉じる
    - [x] 6. `.lock` を解放して削除する
    - [x] 7. 状態を `DETACHED_BY_USER` にし「取り外して構いません」を表示する
- [x] セットアップウィザードに、OS側の推奨設定（ドライブポリシーを「クイック取り外し」にする、USBハブ経由・バスパワー運用を避ける、Vault/VeraCryptの自動ロック・自動アンマウントを無効化する）の案内を追加する

#### **B-5. クリーンシャットダウンフラグと復帰（F-6 / F-7）**

- [x] `StorageSession` 開始時に `app_state.clean_shutdown` を読み、`0` なら「前回異常終了」と判定して結果を保持する
- [x] 起動直後に `clean_shutdown = 0` を書き、正常終了時に `1` を書く
- [ ] 前回異常終了時は、起動パスで **①マニフェスト末尾修復 → ②範囲限定検証 → ③未完了 `purge_intent` / `remote_delete_intent` の回復** を自動実行する（グループC・D・E に依存。レビュー修正案 3.3 / 3.4）
- [x] 起動パスの最初に、A-6のアカウント/フォルダ snapshot バックフィルを実行する（レビュー修正案 9.1）
- [ ] 復帰フローを実装する（D-6）
    - [ ] `DBT_DEVICEARRIVAL` または「再接続を試す」で `RECONNECTING` へ入る
    - [ ] `.maildock_root` のUUID照合 → `.lock` 再取得 → `PRAGMA user_version` 確認 → `PRAGMA quick_check`
    - [ ] `VERIFYING` で範囲限定検証を実行し、成功したら `ATTACHED` へ戻す
    - [ ] Phase 3.6 の共通ブートストラップ（旧セッション解放 → 新 `StorageSession` → `MainWindow` 差し替え）を再利用し、二重セッションを作らない
    - [ ] 検証失敗時は `DETACHED` へ戻し、ユーザー判断を求める
- [ ] `DETACHED` 中は「再接続を試す／終了」のモーダルのみを表示し、読み取り専用モードを提供しない（F-8）
- [x] `cleanup_tmp()` が `tmp/pstimp/` を保護している区別を、コメントとテストで固定する（D-25）

---

### **3.3 グループC: 整合性チェック・再構築（*B と並行着手可。B-5 が依存*）**

#### **C-1. `usecases/verify.py` — 検証ユースケース**

- [x] `domain/ports.py` へ `BaseIntegrityStorage`（`stat` / `iter_chunks` / `iter_eml_paths` / `quarantine`）と `BasePurgeStorage`（`delete` / 存在確認）を新設し、既存 `BaseEmlStorage` へ検証・削除責務を無制限に追加しない（レビュー修正案 3.5）
- [x] `quick_verify(repo, storage, *, cancel) -> QuickVerifyResult` を実装する（`relative_path` の存在確認とサイズ照合。F-12）
- [x] `range_verify(repo, storage, manifest_reader, *, cancel) -> RangeVerifyResult` を実装する（F-13）
    - [x] 最後の `checkpoint` 以降のイベントに含まれるEMLのみSHA-256を再計算する
    - [x] 不一致レコードは未取得へ戻し（`sync_failures` へ記録）、当該EMLを `tmp/` へ隔離する
- [x] `full_verify(repo, storage, *, cancel, on_progress) -> FullVerifyResult` を実装する（F-14）
    - [x] `BaseIntegrityStorage.iter_chunks()` によるチャンク読みでハッシュ計算し、EML全体をメモリへ載せない（N-5）
- [x] `orphan_scan(repo, storage, *, cancel, on_progress) -> OrphanScanResult` を実装する（F-15）
    - [x] マニフェストの `fetch` イベントと `source_item_key` / パス / ハッシュが一致する孤児だけを再登録対象とする
    - [x] マニフェストに対応イベントが無い孤児は、UID・UIDVALIDITY・フォルダを推測せず `tmp/orphans/` などへ隔離し、次回同期の重複候補として監査ログへ記録する（レビュー修正案 3.6）
- [x] `verify_manifest(root, *, cancel) -> ManifestVerifyResult` を実装する（F-16）
    - [x] 既存 `manifest.repair_tail()` を利用し、**末尾の不完全レコードのみ**切り離す
    - [x] 末尾以外の破損は `ManifestCorruptError` として報告し、自動修復しない
- [x] すべてのユースケースが `CancelToken` に対応すること
- [x] `sqlite3` / PySide6 / infrastructure の具象を import しないこと

#### **C-2. `usecases/reindex.py` — マニフェストからのDB完全再構築（*本フェーズの中核の1つ*）**

- [x] `reindex(repo, storage, manifest_reader, *, cancel, on_progress) -> ReindexResult` を実装する（F-17 / D-4）
- [x] 再構築対象: accounts / folders / messages / message_contents / FTS / purge墓標 / 監査イベント
- [x] `account_snapshot` / `folder_snapshot` イベントから accounts / folders を復元する（資格情報は含まれないため、再接続にはユーザーの再設定を要求する。レビュー修正案 3.2）
- [x] `fetch` イベントから messages を復元し、EMLを再解析して `message_contents` を作る（既存 `reparse.py` の解析経路を再利用する）
- [x] `purge_intent` / `purged` イベントから墓標レコード（`local_state='purged'`, `relative_path=NULL`）を復元する
- [x] `remote_delete_completed` / `delete_detected` / `moved` イベントから `remote_state` を復元する。`remote_delete_uncertain` のまま確定していない項目は再構築時も `uncertain` として扱う
- [x] `folders.raw_name` / `display_name` / `uidvalidity` をマニフェストから復元し、`is_sync_target` は**既定で0**（勝手に同期対象にしない）
- [x] `messages.id` / `folders.id` などのDB固有サロゲートIDをマニフェストの正本にせず、`account_id` / `folder_raw_name` / `source_item_key` などの自然キーから新しいIDを解決する（レビュー修正案 3.2）
- [x] PST永続マニフェスト（`manifests/pst/`）は Phase 4.5 のため、読み取り口だけ用意し未対応形式は明示的にスキップして警告ログを残す
- [x] 新しいDBファイルの作成・マイグレーション適用・整合性検証・既存 `metadata.db` との原子的入れ替えは、usecaseから直接SQLiteファイルを操作せず、infrastructure側の**再構築コーディネータ**が担当する（途中失敗で既存DBを壊さない。レビュー修正案 3.5）

#### **C-3. `presentation/threads/verify_worker.py` — 3本目のワーカー（D-5）**

- [x] `VerifyWorker(Worker)` を実装する（読み取り中心）
- [x] `quick_verify` / `range_verify` / `full_verify` / `orphan_scan` / `verify_manifest` / `reindex` / `reparse` を受け付ける
- [x] 進捗を100ms間引きして Signal へ中継する（`SyncWorker` と同じ規約）
- [x] **DB書き込みを伴う後処理は `SyncWorker` へ委譲するか、`SyncWorker` の停止を確認したうえで排他実行する**。単一ライター規約を崩さないことをコメントとテストで固定する
- [x] `VerifyWorker` は `BasePurgeStorage.delete()` など物理削除を伴うポートを直接呼び出さず、検証結果を返すだけにする（レビュー修正案 3.5）
- [x] `StorageDetachedError` を `storage_detached` Signal で `StorageMonitor` へ通知する
- [x] スレッド終了時に、そのスレッドが開いたSQLite接続を必ず閉じる

#### **C-4. CLI 拡張（F-19 / D-3）**

- [x] `verify` サブコマンドへ `--mode quick|range|full|orphans|manifest`（既定 `quick`）を追加する
- [x] `reindex` サブコマンドを新設し、実行前に確認プロンプトを出す
- [x] `--account` による対象絞り込みを `full` / `orphans` / `reindex` で受け付ける
- [x] **削除系（サーバー削除・purge）のサブコマンドを追加しない**。追加されていないことを静的テストで固定する

#### **C-5. GUI 配線**

- [x] 「ツール」メニューを新設し、整合性チェックダイアログを追加する
- [x] ダイアログから各検証モードを選択・実行でき、進捗表示とキャンセルが効くこと
- [x] 結果（破損ファイル一覧・孤児一覧・修復件数）を一覧表示する
- [x] 再インデックスは実行前に「DBを再構築します」という確認を出す
- [x] 再解析をGUIから実行できるようにする（F-18）

---

### **3.4 グループD: ローカルゴミ箱・30日purge（*B に依存。C と並行可*）**

#### **D-1. `usecases/trash.py` — ゴミ箱と purge**

- [x] `move_to_trash(repo, *, message_ids, now) -> TrashResult` を実装する（F-20）
    - [x] `local_state='trashed'`、`trashed_at` を記録する。**EMLは削除しない**
- [x] `restore_from_trash(repo, *, message_ids) -> TrashResult` を実装する（F-21）
- [x] `list_purge_candidates(repo, *, now, grace_days) -> Sequence[MessageRecord]` を実装する
- [x] `purge(repo, storage, manifest, *, message_ids, storage_state) -> PurgeResult` を実装する（F-22）。各段階を**再実行しても結果が変わらない冪等な状態遷移**として実装する（レビュー修正案 3.3）
    - [x] 1. `storage_state.is_write_allowed()` が偽なら入口で拒否する
    - [x] 2. 対象件数と合計サイズをログへ記録する（F-26）
    - [x] 3. マニフェストへ `purge_intent` を追記し **fsync** する
    - [x] 4. `count_path_references()` で同じ `relative_path` を参照する非purgedレコードが無いことを確認し、**最後の参照が消える場合だけ**EMLを削除する（F-23 / D-13）。EMLが既に存在しない場合は、ハッシュとintentが一致する限り削除済みとして扱う（レビュー修正案 3.3）
    - [x] 5. マニフェストへ `purged` を追記し **fsync** する
    - [x] 6. `message_contents` を削除する（トリガーでFTSからも除去される。F-24）
    - [x] 7. `local_state='purged'`, `relative_path=NULL` に更新する。**`messages` の行は残す**
    - [x] 8. `audit_log` へ `operation='local_purge'` を記録する
- [x] `recover_incomplete_purges(repo, storage, manifest_reader, *, storage_state) -> None` を実装する。起動時または範囲限定検証前に、対応する `purged` が存在しない `purge_intent` を列挙し、共有参照を再確認したうえで未完了部分（4〜8）を再開する（レビュー修正案 3.3）
- [x] `sqlite3` / PySide6 / infrastructure の具象を import しないこと

#### **D-2. purge 実行モード（F-25 / D-14）**

- [x] `manual`（既定）: ゴミ箱画面の「今すぐ完全削除」でのみ purge する
- [x] `grace`: 起動時に猶予経過分を検出し、対象一覧と確認ダイアログを表示してから実行する
- [x] `immediate`: 猶予経過分を確認なしで purge する
- [x] `immediate` 選択時、設定画面に「確認なしでファイルが削除されます」を常時表示する
- [x] 猶予日数は `config.trash_grace_days`（既定30）で変更できること

#### **D-3. ゴミ箱ビュー（GUI）**

- [x] 左ペインに「ゴミ箱」ノードを追加し、`MessageFilter.local_states` を `frozenset({"trashed"})` へ切り替える（既存フィールドをそのまま使い、モデルを再設計しない）
- [x] 一覧に残り日数を表示する（`trashed_at + trash_grace_days`）
- [x] 「元に戻す」「今すぐ完全削除」のアクションを配置する
- [x] `local_state='purged'` の墓標レコードが「実体なし」として表示され、本文プレビューがEMLを読まないこと（Phase 3 F-21 の挙動を維持）

---

### **3.5 グループE: サーバーメール手動削除（*B・D に依存*）**

#### **E-1. ゴミ箱フォルダの特定（F-32）**

- [x] `OnamaeImapFetcher` の `LIST` 応答から **SPECIAL-USE（RFC 6154）の `\Trash` 属性**を解釈する
- [x] 見つからない場合、一般的な候補名（`Trash` / `ゴミ箱` / `Deleted Items` / `INBOX.Trash` 等）を自動探索する
- [x] それでも特定できない場合は `config.remote_trash_folder` による手動指定を要求し、**未指定の間は削除機能を無効化**する
- [x] 自動検出結果を設定画面に表示し、ユーザーが上書きできるようにする
- [x] 既存の `_find_trash_folder()` を上記の3段階へ拡張し、`BaseMailFetcher` の契約として整理する
- [x] フォルダのUID EXPUNGE対応可否（`UIDPLUS` 拡張等）を判定し、`BaseMailFetcher` の契約として公開する（レビュー修正案 3.4）
- [x] 既存の `OnamaeImapFetcher.delete_remote_message()` が `UIDPLUS` 非対応時にフォルダ全体 `expunge()` へフォールバックしている現行実装を廃止し、非対応サーバーでは `expunge` を拒否するよう修正する（レビュー修正案 3.4）
#### **E-2. `usecases/delete_remote.py` — 削除ユースケース**

- [x] `dry_run(repo, storage, *, message_ids, storage_state) -> DeleteDryRunResult` を実装する（F-29）
    - [x] 3つの事前条件を検証し、満たさないメールを**自動除外**して除外理由を返す（F-27 / D-12）
    - [x] 対象一覧（件名・日付・サイズ）と合計容量を返す
- [x] `execute(fetcher, repo, storage, manifest, *, plan, mode, storage_state) -> DeleteResult` を実装する。exactly-once を前提とせず、意図・成功確認・不確定状態を別イベントとして記録する（レビュー修正案 3.4）
    - [x] `storage_state.is_remote_delete_allowed()` が偽なら**入口で無条件拒否**する（F-28）
    - [x] `dry_run` の結果を再検証してから実行する（TOCTOU対策。実行直前にもハッシュを再計算する）
    - [x] `config.delete_batch_limit`（既定1,000）を超える要求を拒否する（F-31）
    - [x] 既定はゴミ箱へ MOVE（`mode="trash"`）、`mode="expunge"` は明示指定時のみ（D-11）
    - [x] `expunge` 指定時、サーバーが UID EXPUNGE（対象UIDのみを永久削除できる操作）に対応しない場合は実行を拒否し、通常のフォルダ全体 EXPUNGE へフォールバックしない（レビュー修正案 3.4）
    - [x] 1. マニフェストへ `remote_delete_intent` を追記し fsync してから IMAP MOVE/EXPUNGE を実行する
    - [x] 2. サーバー応答を確認し、成功が確認できた場合のみ `remote_delete_completed` を追記し fsync する
    - [x] 3. 応答不明・通信断の場合は `remote_delete_uncertain` を記録し、その場では `deleted` と表示しない
    - [x] 4. 状態確定後（再接続後の照合を含む）に `audit_log` へ記録し、`remote_state='deleted'` を更新する（F-33）
- [x] `reconcile_uncertain_deletes(fetcher, repo, manifest, *, storage_state) -> None` を実装する。再接続後に元フォルダ・UID・UIDVALIDITY・移動先を照合し、`remote_delete_uncertain` を `remote_delete_completed` または取り消しへ確定させる（レビュー修正案 3.4）
- [x] `sqlite3` / PySide6 / infrastructure の具象を import しないこと

#### **E-3. GUI 配線**

- [x] メール一覧のコンテキストメニュー／ツールバーに「サーバーから削除」を追加する
- [x] ドライラン結果ダイアログ（対象一覧・合計サイズ・除外されたメールとその理由）を表示し、CSVへ保存できるようにする
- [x] 確認ダイアログで件数と合計サイズを表示し、**件数の手入力**を要求する（F-30 / D-23）
- [x] ストレージが `ATTACHED` 以外のとき、削除アクションを無効化する
- [x] ゴミ箱フォルダが未特定のとき、削除アクションを無効化して理由を表示する
- [x] 削除後に `remote_state='deleted'` のグレーアウト表示へ反映する

---

### **3.6 グループF: エクスポート（*E と並行可*）**

#### **F-1. `usecases/export_mbox.py`**

- [x] `export_mbox(repo, storage, *, message_ids, dest_path, cancel, on_progress) -> Path` を実装する（F-34）
- [x] 標準ライブラリ `mailbox.mbox` を使用し、EMLを1通ずつ追記する（全体をメモリへ載せない）
- [x] 書き出し前に各EMLの `file_hash` を検証する（`read_verified()` を使う）
- [x] 一時ファイルは `dest_path.parent` に作成し、`flush()` + `fsync()` 後に同一ボリューム上で `os.replace` する（既存 `export_message.py` の作法を踏襲）
- [x] `purged` のメッセージはスキップし、スキップ件数を結果に含める

#### **F-2. `usecases/export_attachments.py`**

- [ ] `export_attachments(storage, renderer, *, messages, dest_dir, cancel, on_progress) -> ExportAttachmentsResult` を実装する（F-35）
- [ ] 既存 `save_attachment.py` の `sanitize_attachment_name()` と `resolve_within()` を必ず経由する
- [ ] インラインパート（`Content-ID` 付き）を抽出対象から除外する
- [ ] 同名衝突は連番で回避し、既存ファイルを無確認で上書きしない
- [ ] 実行可能拡張子は結果に警告として含め、UIで提示する

#### **F-3. GUI 配線**

- [ ] 「ファイル」メニューに「mbox としてエクスポート」「添付ファイルを一括抽出」を追加する
- [ ] 対象は「選択したメール」または「現在の一覧（フィルタ・検索結果）全体」から選べるようにする
- [ ] エクスポートを `SyncWorker`（ファイル書き込み系の既存経路）で実行し、進捗表示とキャンセルを設ける

---

### **3.7 グループG: 運用UI・自動化（*F と並行可*）**

#### **G-1. 定期自動同期とシステムトレイ（F-36 / F-37）**

- [ ] `config.sync_interval_minutes`（既定60、0で無効）の `QTimer` を `MainWindow` に実装する
- [ ] 前回の同期が実行中ならスキップする
- [ ] ストレージが `ATTACHED` 以外のとき、**ダイアログを出さず静かにスキップ**する
- [ ] `QSystemTrayIcon` を実装し、同期状態（待機中／同期中／エラー／切断）をアイコンとツールチップで表示する
- [ ] トレイメニュー: 「開く」「今すぐ同期」「終了」
- [ ] **ウィンドウを閉じるとトレイへ最小化**し、終了はトレイメニューまたはファイルメニューからのみ可能にする（D-15）
- [ ] 最小化中も同期・定期同期が継続すること
- [ ] トレイが利用できない環境（`QSystemTrayIcon.isSystemTrayAvailable()` が偽）では、閉じる＝終了へフォールバックする

#### **G-2. `sync_failures` の運用UI（F-38 / F-39）**

- [ ] 「要確認」一覧（`attempt_count >= 10`）を表示するダイアログを追加する
- [ ] `oversize` のメッセージに対する「それでも取得する」を実装し、サイズ上限を無視して単一メールを取得する経路を `sync_mail.py` へ追加する
- [ ] `parse` 失敗のメッセージに対する再解析をGUIから実行できるようにする（既存 `reparse.py` を利用）
- [ ] 一覧に失敗種別（`transient` / `permanent` / `parse` / `oversize`）と試行回数・最終失敗日時を表示する

#### **G-3. 監査ログ表示画面（F-40）**

- [ ] 「ツール」メニューに「監査ログ」を追加する
- [ ] `audit_log` を `occurred_at DESC` でページング表示する（読み取り専用）
- [ ] 表示項目: 日時・操作・アカウント・Message-ID・件名・サイズ・詳細
- [ ] **削除・編集の導線を作らない**（D-22）
- [ ] 件名・メールアドレスは既存のマスキング規約に従って表示する

#### **G-4. DBバックアップとログ保持（F-41 / F-42）**

- [ ] `sqlite3.Connection.backup()` により **週1回および終了時**に `metadata.db.bak` を作成する
- [ ] 最終バックアップ日時を `app_state` に保持し、週1回の判定に使う
- [ ] `config.db_backup_to_local_disk` を実処理へ接続する（既定OFF）
- [ ] 有効化時、複製先の暗号化状態が保管元より弱い場合は警告し、既定では実行しない（Phase 3.5 D-16）
- [ ] `{storage_root}/logs/sync-*.log` を `config.sync_log_retention_days`（既定90日）を過ぎたら削除する
- [ ] **`audit_log` テーブルは削除対象にしない**ことをコメントとテストで固定する

#### **G-5. 設定画面の拡張（F-43）**

- [ ] purge 実行モード（`manual` / `grace` / `immediate`）と警告表示
- [ ] ゴミ箱の猶予日数（`trash_grace_days`）
- [ ] サーバー削除モード（`remote_delete_mode`）とゴミ箱フォルダ（`remote_trash_folder`、自動検出結果の表示と上書き）
- [ ] 1回の削除上限（`delete_batch_limit`）
- [ ] ハートビート間隔（`heartbeat_interval_sec`）— **0による無効化を許可しない**（N-1）
- [ ] 定期同期間隔（`sync_interval_minutes`）
- [ ] 同期ログ保持日数（`sync_log_retention_days`）
- [ ] `db_backup_to_local_disk`（既定OFF、警告付き）
- [ ] Phase 4.5 の設定項目（PST取込設定）は**表示しない**

---

### **3.8 グループH: テスト・ドキュメント（*各グループと並行して作成*）**

#### **H-1. 切断フォールト注入テスト（*本フェーズ最重要。D-16 の代替*）**

- [ ] `tests/support/fault_injection.py` を作成し、`BaseEmlStorage` とDB接続をラップして N回目の操作で `OSError(winerror=1167)` / `sqlite3.OperationalError(SQLITE_IOERR)` を注入できるようにする
- [ ] `tests/unit/test_detach_faults.py` に、以下4点それぞれで切断を注入するテストを書く
    - [ ] ① EML の fsync 前
    - [ ] ② `os.replace` の直前
    - [ ] ③ マニフェスト追記の**行途中**（部分書き込みを再現する）
    - [ ] ④ DBコミット中
- [ ] 各注入点で、**不変条件2で許容される状態（DB未登録のEML、またはマニフェスト済みでDB未登録の項目）にしかならない**ことを検証する
- [ ] 「DBにあるが実体が無い」状態が**一度も発生しない**ことを検証する
- [ ] DBコミット失敗時に checkpoint が追記されないこと、およびそのバッチが次回の範囲限定検証対象になることを検証する（レビュー修正案 3.1）
- [ ] ③ の後に `verify_manifest()` を実行すると末尾の不完全レコードだけが切り離され、それ以前のレコードが失われないことを検証する
- [ ] 各注入点からの復帰後、次回同期で欠落分が再取得されること（冪等性）を検証する

#### **H-2. 状態機械・切断検知のテスト（*Qt非依存・通常CI*）**

- [x] `tests/unit/test_storage_state.py`: 開発計画書 5.7.1-3 の状態機械遷移を検証する
    - [x] `ATTACHED` → I/Oエラー → `DEGRADED` → リプローブ成功 → `ATTACHED`
    - [ ] `DEGRADED` → リプローブ3回失敗 → `DETACHED`
    - [x] `DEGRADED` → `DEVICE_REMOVED` → `DETACHED`（リプローブを待たない）
    - [x] `ATTACHED` → `USER_DETACH` → `DETACHED_BY_USER`
    - [x] `DETACHED` → `DEVICE_ARRIVED` → `RECONNECTING` → `IDENTITY_OK` → `VERIFYING` → `VERIFY_OK` → `ATTACHED`
    - [x] `VERIFYING` → `VERIFY_FAILED` → `DETACHED`
    - [x] `RECONNECTING` → `IDENTITY_FOREIGN` → `DETACHED`（`FOREIGN` は書き込み禁止のまま）
    - [x] `ATTACHED` 以外で `is_write_allowed()` と `is_remote_delete_allowed()` が偽であること
- [x] `tests/unit/test_device_watcher.py`: `dbcv_unitmask` からのドライブレター復元（純粋関数）
- [x] 同テストへWindows専用ケースを追加し、`ctypes` 構造体のABIレイアウト、ネイティブメッセージ解析、Qtへのイベントフィルタ登録・解除を `windows-latest` 上で検証する（非Windowsではskip）
- [ ] `tests/unit/test_detach.py`（既存を拡張）: リプローブ回数と間隔の制御

#### **H-3. マニフェストと再構築のテスト**

- [ ] `tests/unit/test_manifest.py`（既存を拡張）
    - [ ] `checkpoint` / `account_snapshot` / `folder_snapshot` / `purge_intent` / `purged` / `remote_delete_intent` / `remote_delete_completed` / `remote_delete_uncertain` のスキーマ検証
    - [ ] 最後の `checkpoint` 以降のイベント列挙が正しいこと
    - [ ] 末尾以外のCRC32不一致が `ManifestCorruptError` になり、自動修復されないこと
- [ ] `tests/integration/test_reindex.py`
    - [ ] 同期後に `metadata.db` を削除し、EML＋マニフェストだけから再構築した結果が**再構築前と一致**すること（messages / message_contents / FTS / folders）
    - [ ] purge済みメッセージの墓標レコードと `local_state='purged'` が復元されること
    - [ ] `remote_delete_completed` / `delete_detected` / `moved` から `remote_state` が復元されること。未確定の `remote_delete_uncertain` は `uncertain` のまま復元されること
    - [ ] 再構築された `folders.is_sync_target` がすべて 0 であること
    - [ ] 再構築が途中で失敗しても既存 `metadata.db` が壊れないこと
    - [ ] `manifests/pst/` が存在しても警告ログを残してスキップされること

#### **H-4. 検証ユースケースのテスト**

- [ ] `tests/unit/test_verify.py`（Fake ポートのみ）
    - [ ] クイック検証がサイズ不一致を検出すること
    - [ ] 範囲限定検証が checkpoint 以降のみを対象にすること
    - [ ] ハッシュ不一致のEMLが `tmp/` へ隔離され、`sync_failures` へ記録されること
    - [ ] フル検証と孤児スキャンが `CancelToken` で中断できること
- [ ] `tests/gui/test_verify_worker.py`: 進捗の間引き、キャンセル、`storage_detached` の Signal 伝播、DB書き込みが `SyncWorker` 側で行われること

#### **H-5. purge・削除のテスト**

- [ ] `tests/unit/test_trash.py`
    - [ ] ゴミ箱移動でEMLが削除されないこと
    - [ ] 復元で `active` に戻ること
    - [ ] purge の順序（`purge_intent` fsync → EML削除 → `purged` fsync → DB更新）が守られること
    - [ ] **同じ `relative_path` を参照する非purgedレコードがある場合、実ファイルが削除されないこと**
    - [ ] 最後の参照が消える場合だけ実ファイルが削除されること
    - [ ] 墓標レコードが残り、`message_contents` の削除でFTSからも消えること
    - [ ] `purge_mode` の3値それぞれの挙動
    - [ ] 書き込み不可状態（`ATTACHED` 以外）で purge が拒否されること
    - [ ] `purge_intent` 追記後・EML削除後・`purged` 追記後・DB更新中のそれぞれで中断し、再起動後に `recover_incomplete_purges()` で墓標化が完了すること（レビュー修正案 3.3）
    - [ ] purge を同じ対象へ複数回実行しても、EMLや監査ログが不正に二重処理されないこと（冪等性）
- [ ] `tests/unit/test_delete_remote.py`
    - [ ] 3つの事前条件のいずれかが欠けた対象が**自動除外**され、除外理由が返ること
    - [ ] `ATTACHED` 以外で入口拒否されること
    - [ ] `delete_batch_limit` 超過が拒否されること
    - [ ] 既定が MOVE であり、`expunge` が明示指定時のみ実行されること
    - [ ] ドライラン後にEMLが差し替えられた場合、実行直前の再検証で拒否されること
    - [ ] マニフェスト追記 → `audit_log` → `remote_state` 更新の順序が守られること
    - [ ] ゴミ箱フォルダ未特定時に削除が実行できないこと
    - [ ] 応答前の通信断が `remote_delete_uncertain` として記録され、その場では `deleted` と表示されないこと
    - [ ] 再接続後の照合で `remote_delete_uncertain` が `remote_delete_completed` または取り消しへ確定すること
    - [ ] UID EXPUNGE非対応サーバーで `expunge` が拒否されること（レビュー修正案 3.4）
    - [ ] 上記の不確定状態・UID EXPUNGE拒否の分岐はFakeフェッチャー（応答読み取り時の例外注入・`list_existing_uids()`のスクリプト）による決定的な単体テストとして検証し、ソケット切断の実際の再現は狙わない（レビュー修正案 9.3）
- [ ] `tests/integration/test_remote_delete.py`（Docker/Dovecot）: SPECIAL-USE の `\Trash` 検出、MOVE、`EXPUNGE`
    - [ ] `remote_delete_uncertain` をテストが直接投入し、別の生 IMAP 接続でサーバー側を先に削除済み/未削除の両方に作り分けてから `reconcile_uncertain_deletes()` を実行し、実サーバーの状態と正しく照合できることを検証する（既存 `test_delete_detection.py` と同じ手法。レビュー修正案 9.3）

#### **H-6. エクスポート・運用機能のテスト**

- [ ] `tests/unit/test_export_mbox.py`: ハッシュ検証、`purged` のスキップ、一時ファイルが残らないこと、`mailbox` で読み戻せること
- [ ] `tests/unit/test_export_attachments.py`: サニタイズ、インライン除外、連番、ディレクトリ外へ書けないこと
- [ ] `tests/unit/test_backup.py`: 週1回判定、終了時バックアップ、`db_backup_to_local_disk` の既定OFF
- [ ] `tests/unit/test_log_retention.py`: 90日超の同期ログのみ削除され、`audit_log` が対象外であること

#### **H-7. GUIテスト（`gui` マーカー／ローカル手動）**

- [ ] `tests/gui/test_storage_monitor.py`: ハートビート、`FOREIGN` 検出時の書き込み禁止、瞬断復帰、`DETACHED` バナー
- [ ] `tests/gui/test_safe_eject.py`: 「安全な取り外し」の7手順が順序どおり実行されること
- [ ] `tests/gui/test_trash_view.py`: ゴミ箱ビュー、残り日数、復元、完全削除
- [ ] `tests/gui/test_delete_remote_dialog.py`: ドライラン表示、**件数手入力**を通らないと実行できないこと、切断中は無効化されること
- [ ] `tests/gui/test_tray.py`: 閉じる→最小化、トレイメニューからの終了、トレイ非対応環境のフォールバック
- [ ] `tests/gui/test_audit_log_view.py`: 読み取り専用であること、削除導線が無いこと
- [ ] `tests/gui/test_verify_dialog.py`: 各モードの起動、進捗、キャンセル

#### **H-8. 静的テスト・CI**

- [ ] `tests/unit/test_main.py`（既存を拡張）: **CLIに削除系サブコマンドが存在しない**ことを固定する（D-3）
- [ ] `tests/unit/test_ports.py`（既存を拡張）: `presentation/views` / `viewmodels` / `models` に `sqlite3` と `mail_dock.infrastructure` の import が無いこと、`domain` / `usecases` に PySide6 の import が無いこと
- [ ] `tests/unit/test_presentation_errors.py`（既存を拡張）: 新規例外が対応表に存在すること
- [ ] `lint` / `test-windows` / `test-linux` の3ジョブが `-m "not docker and not gui"` で緑になること
- [ ] 非Windows環境で `device_watcher` が import でき、no-op として動作すること（Linuxジョブで確認）

#### **H-9. ドキュメント**

- [ ] 開発計画書 6章「Phase 4」の行に、実機テストを Phase 4 では実施せず延期した旨と、その代替（フォールト注入）を追記する
- [ ] 開発計画書 5.11 の「リムーバブル時の `synchronous`」に、Phase 4 では実測できず `NORMAL` を維持した旨を追記する
- [ ] `README.md` に「ストレージを安全に取り外す」手順と、ドライブポリシー「クイック取り外し」の推奨を追記する
- [ ] `README.md` に `verify --mode` と `reindex` の使い方を追記する
- [ ] 本書「7.」へ、延期した手動検証手順（VHDX切断シナリオ、フルスケール同期、`synchronous` 実測）を記載する

---

## **4. 主要成果物**

| パス | 内容 | タスク |
| :---- | :---- | :---- |
| `src/mail_dock/domain/storage_state.py` | ストレージ状態機械（Qt非依存） | B-1 |
| `src/mail_dock/domain/ports.py` | `BaseManifestWriter.checkpoint()` / `BaseManifestReader` | A-2 |
| `src/mail_dock/domain/repository.py` | 監査・ゴミ箱・`app_state` 用の最小メソッド追加 | A-5 |
| `src/mail_dock/infrastructure/storage/manifest.py` | `checkpoint` / `account_snapshot` / `folder_snapshot` / `purge_intent` / `purged` / `remote_delete_intent` / `remote_delete_completed` / `remote_delete_uncertain` イベント | A-1 |
| `src/mail_dock/migrations/005_phase4.sql` | 監査・purge・共有EML参照用インデックス | A-4 |
| `src/mail_dock/usecases/verify.py` | クイック／範囲限定／フル検証・孤児スキャン・マニフェスト検証 | C-1 |
| `src/mail_dock/usecases/reindex.py` | EML＋マニフェストからのDB完全再構築 | C-2 |
| `src/mail_dock/usecases/trash.py` | ゴミ箱・猶予期間・purge | D-1 |
| `src/mail_dock/usecases/delete_remote.py` | サーバー削除（事前検証・ドライラン・監査） | E-2 |
| `src/mail_dock/usecases/export_mbox.py` | mbox エクスポート | F-1 |
| `src/mail_dock/usecases/export_attachments.py` | 添付一括抽出 | F-2 |
| `src/mail_dock/presentation/native/device_watcher.py` | `WM_DEVICECHANGE` 監視（非Windowsは no-op） | B-2 |
| `src/mail_dock/presentation/storage_monitor.py` | ハートビート・縮退制御・復帰 | B-3 / B-5 |
| `src/mail_dock/presentation/threads/verify_worker.py` | 検証用ワーカー（読み取り中心） | C-3 |
| `src/mail_dock/presentation/views/dialogs/` | 整合性チェック・削除確認・監査ログ・要確認一覧 | C-5 / E-3 / G-2 / G-3 |
| `src/mail_dock/__main__.py` | `verify --mode` / `reindex` / `clean_shutdown` | B-5 / C-4 |
| `tests/support/fault_injection.py` | I/O例外の注入ラッパー | H-1 |
| `tests/unit/test_detach_faults.py` | フォールト注入4点の検証 | H-1 |
| `tests/integration/test_reindex.py` | マニフェストからの再構築の同一性検証 | H-3 |

---

## **5. スコープ境界**

### **5.1 含むもの**

セクション3のグループA〜H。「切断対策（予防・検知・縮退・復帰）→ 整合性チェックと再構築 → ローカルゴミ箱とpurge → サーバー削除の安全装置 → エクスポート → 常駐運用UI」の一式。

### **5.2 含まないもの（明示的に除外）**

| 除外項目 | 実施フェーズ・理由 |
| :---- | :---- |
| フルスケール実機同期テスト（5万通 / 100GB） | **延期**（D-16）。手順のみ本書「7.」に記載 |
| VHDX `detach vdisk` による実デバイス切断試験 | **延期**（D-16）。フォールト注入テストで代替 |
| `synchronous` の最終決定（`NORMAL` → コミット時 `FULL`） | **本フェーズ外**（D-17）。既定 `NORMAL` を維持 |
| 検索結果のCSVエクスポート | **実装しない**（D-18） |
| PSTアーカイブ一式（`import_pst.py` / `readpst` / 左ペインのPSTルート / `pst_imports` テーブル） | Phase 4.5 |
| Gmail / OAuth2 / `message_folders` 中間テーブル | Phase 5 |
| PyInstaller / Inno Setup によるパッケージング・GPL遵守チェックのリリースCI | Phase 4.5 以降 |
| 一覧のスレッドグルーピング表示（Gmail風） | 将来拡張 |
| 検索結果のスニペット・ハイライト | 将来拡張 |
| IMAPフラグの双方向同期・ローカル既読管理・IMAP IDLE | 恒久的にスコープ外 |
| 読み取り専用の縮退モード | 恒久的にスコープ外（D-8） |
| 多言語化・自動更新機構・コード署名 | 恒久的にスコープ外 |

---

## **6. 検証**

各項目の完了を確認したうえで、対応するタスクのチェックボックスを埋めること。

- [ ] V-1. `uv sync` → `uv run ruff format --check .` → `uv run ruff check .` → `uv run mypy` がすべて成功する
- [ ] V-2. **フォールト注入4点**（fsync前 / `os.replace` 直前 / マニフェスト追記の行途中 / DBコミット中）すべてで、発生する状態が「DB未登録のEML」または「マニフェスト済みでDB未登録の項目」だけであり、「DBにあるが実体が無い」状態が一度も発生しない。DBコミット失敗時に checkpoint が残らないことも合わせて検証する
- [ ] V-3. **`metadata.db` を削除し、EML＋永続マニフェストだけからDBを再構築**した結果が、再構築前と**意味的に一致**する（accounts/foldersの自然キー・表示名・UIDVALIDITY、messagesの自然キー・EMLパス・ハッシュ・状態・日時・サイズ、message_contentsとFTSの検索結果、purge墓標とremote_state、audit_logが一致する。`is_sync_target` は再構築後すべて0とする運用設定として比較対象から除外する。レビュー修正案 3.2）。再構築が途中失敗しても既存DBが壊れない
- [ ] V-4. purge が「`purge_intent` fsync → 共有参照ゼロ確認 → EML削除 → `purged` fsync → FTS除去 → 墓標化 → 監査記録」の順序で実行され、他レコードが参照するEMLを削除しない。各段階での途中停止後も、再起動・再実行で墓標化が完了する（冪等性。レビュー修正案 3.3）
- [ ] V-5. サーバー削除が3つの事前条件を満たさない対象を自動除外し、`ATTACHED` 以外では入口で拒否され、件数手入力を経ないと実行できない。応答前の通信断は `uncertain` として保持され、再接続後の照合を経てから `deleted` へ確定する。UID EXPUNGEが保証できないサーバーでは `expunge` を実行しない（レビュー修正案 3.4）
- [x] V-6. 状態機械が開発計画書 5.7.1-3 の遷移表どおりに動き、`ATTACHED` 以外で `is_write_allowed()` と `is_remote_delete_allowed()` が偽になる（Qt非依存の単体テスト）
- [ ] V-7. `FOREIGN`（別デバイスが同じドライブレターを取得）検出時に即座に全書き込みが禁止される
- [ ] V-8. スタール `.lock`（ロック実体は取得できるが `heartbeat_at` が古い）で起動でき、範囲限定検証が自動実行される
- [ ] V-9. 「ストレージを安全に取り外す」がワーカー停止からロック解放までを規定の順序で実行し、実行後にWindowsがドライブの取り外しを拒否しない（手動確認）
- [ ] V-10. 切断→再接続で、プロセスを終了せずに `RECONNECTING` → `VERIFYING` → `ATTACHED` まで戻り、同期がバッチ境界から再開される
- [ ] V-11. CLI に削除系サブコマンドが存在せず、`verify --mode` と `reindex` が動作する（静的テストで固定）
- [ ] V-12. `uv run pytest -m "not docker and not gui"` が全緑になり、`domain` + `usecases` のカバレッジが 80% 以上である
- [ ] V-13. GUIテストがローカルで全緑になる（`gui` マーカーのオプトイン実行）
- [ ] V-14. 層の隔離が維持されている（`presentation/views` / `viewmodels` / `models` に `sqlite3` と `mail_dock.infrastructure` が無く、`domain` / `usecases` に PySide6 が無い）
- [ ] V-15. 非Windows環境で `device_watcher` が import でき no-op として動作し、Linux CIジョブが緑になる
- [ ] V-16. トレイ常駐で、閉じる→最小化・定期同期の継続・トレイからの終了が動作する。トレイ非対応環境では閉じる＝終了へフォールバックする
- [ ] V-17. CI（`lint` / `test-windows` / `test-linux`）が3ジョブとも成功し、GUIテストが実行されていない

---

## **7. 延期した検証と、その手動実施手順**

> D-16 により本フェーズでは実施しない。将来実施する際の手順をここに残す。実施したら結果を本節へ追記すること。

### **7.1 VHDX による実デバイス切断シナリオ**

1. `diskpart` で VHDX を作成・アタッチし、ドライブレターを割り当てる。
2. mail-dock のストレージルートをそのドライブに初期化し、テスト用アカウントで同期を開始する。
3. 同期中に `diskpart` の `detach vdisk` を実行して強制切離しする。
4. 確認項目
   * `DETACHED` へ遷移し、書き込みが即座に停止すること
   * ログが `{config_dir}/logs/app.log` へ切り替わること
   * 「サーバーから削除」が無効化されること
5. 再アタッチし、`DBT_DEVICEARRIVAL` で自動復帰すること、範囲限定検証が走ること、同期がバッチ境界から再開されることを確認する。
6. `.lock` を残したままプロセスを強制終了し、再起動時にスタールロックとして回収されることを確認する。
7. **`FOREIGN` の再現**: 別のVHDX（別の `.maildock_root` を持つ）を同じドライブレターへアタッチし、書き込みが即座に禁止されることを確認する。

### **7.2 フルスケール実機同期テスト（5万通 / 100GB）**

1. お名前.com の実アカウントで全フォルダを同期対象にし、初回同期を実行する。
2. 測定項目: 実効スループット（目標 8MB/s 以上）、メモリ使用量（目標 600MB 以下）、`-wal` の最大サイズ、FTSインデックスサイズ、検索応答時間（3文字以上で 300ms 以内）。
3. 途中で複数回中断し、レジュームが正しく働くことを確認する。
4. 完了後にフル検証を実行し、全EMLのSHA-256が一致することを確認する。

### **7.3 `synchronous` の実測と決定**

1. 7.2 と同じ条件で `synchronous=NORMAL` と「バッチコミット時のみ `FULL`」の2パターンを計測する。
2. 外付けSSDでの同期所要時間の差が実用上許容範囲であれば `FULL` へ変更する。差が大きければ `NORMAL` を維持する。
3. 決定結果を開発計画書 3.6 / 5.11 / 8章へ反映する。

---

## **8. Phase 4.5 への引き継ぎ事項**

> *実装中に判明した事項をここへ追記する。*

* 永続マニフェストの読み取り口は `manifests/imap/` と `manifests/pst/` の両方を走査する構造にしてある。Phase 4.5 では `manifests/pst/`（`import.json` / `folders.json` / `items.jsonl`）のパーサを足すだけで、再インデックスがPST由来のデータも復元できるようにすること。
* `cleanup_tmp()` は `tmp/pstimp/` を保護している。Stage A の再開に必要な領域であるため、Phase 4.5 でこの保護を外さないこと。
* 左ペインのツリーモデルは**ルートノードのリスト**を受け取る構造を維持している（Phase 3 D-14）。Phase 4.5 では「PSTアーカイブ」ルートを追加するだけでよい。
* ゴミ箱・purge（グループD）と整合性チェック（グループC）は `provider_type` に依存しない実装にしてあるため、PST由来のメッセージにもそのまま効く。逆に**同期とサーバー削除は `provider_type='pst_import'` をユースケース入口でガードする**必要がある。Phase 4 で作った `delete_remote.py` / `sync_mail.py` の入口へ、Phase 4.5 でこのガードを追加すること。
* 書き込みワーカーは `SyncWorker` 1本の規約を維持している。PST取込ワーカーも同じ枠を使い、同期とPST取込の同時実行を許可しないこと（開発計画書 3.6）。
* 監査ログの `operation` は現在 `remote_delete` / `remote_trash` / `local_purge` を使用している。Phase 4.5 では `pst_import` / `pst_reimport` / `pst_supersede` / `pst_import_abandon` を追加すること。
* PSTインポート開始前の空き容量チェック（PSTサイズ × 2.5）は、既存の `check_free_space()` とは別の判定として実装すること。
