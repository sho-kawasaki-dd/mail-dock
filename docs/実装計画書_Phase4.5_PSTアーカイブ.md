# **Phase 4.5: PSTアーカイブ 実装計画書**

対象: [ローカルメールバックアップ＆閲覧アプリ 開発計画書.md](./ローカルメールバックアップand閲覧アプリ開発計画書.md) の **4.10 PSTアーカイブ機能（ローカル .pst の .eml 変換）** および関連する 1.3 / 2.4 / 3.1 〜 3.6 / 5.9 / 5.10 / 5.11 の該当箇所。

前提: [Phase 4: 統合と例外処理 実装計画書](./実装計画書_Phase4_統合と例外処理.md) の成果物（切断状態機械、`ManifestWriter`/`ManifestReader`、`verify.py`/`reindex.py`、ゴミ箱・purge、監査ログ、`VerifyWorker`）が完成していること。[実装計画書_Phase5.1_汎用IMAPサーバー対応.md](./実装計画書_Phase5.1_汎用IMAPサーバー対応.md) は着手済みかどうかを問わないが、マイグレーション番号の割り当てには影響する（D-2参照）。

位置づけ: PSTアーカイブは **IMAPアーカイブとは独立した機能**であり、ストレージ・DB・検索基盤のみを共有し、データとしては一切接続しない（開発計画書 1.3）。本フェーズは開発計画書ロードマップの **Phase 4.5** にあたり、Phase 5（汎用IMAP / Gmail・OAuth2）とは無関係に着手できる。

本書と開発計画書に矛盾がある場合は、**開発計画書を正**とする。設計不変条件（真実の情報源はEML＋永続マニフェスト、書き込み順序の厳守、削除は常に多段防御）は本フェーズでも変更しない。

---

## **1. 目的**

- [ ] `readpst`（libpst）を外部プロセスとして起動し、`.pst` 内のメールを標準形式の `.eml` へ変換して永続保存できるようにする。
- [ ] readpst にレジューム・逐次進捗APIが無い制約を「Stage A: 抽出（レジューム不可）」「Stage B: 取込（レジューム可）」の2段構成に閉じ込め、Phase 4 までに確立した堅牢な保存パイプライン（原子的EML保存 → マニフェスト追記+fsync → DBコミット）をStage Bにそのまま適用する。
- [ ] 同一PSTの二重取込を検出し、未完了ジョブの再開・完成済みアーカイブの再変換（世代交代）を安全に行えるようにする。
- [ ] PST由来のメッセージ（`remote_state='no_remote'`）に対して、同期・サーバー削除・フォルダ選択（`is_sync_target`）をコードレベルで無効化する。
- [ ] 左ペインを「メールアカウント」と「PSTアーカイブ」の2ルートに分割し、PSTアーカイブ選択中は同期・サーバー削除のUIを隠す。
- [ ] readpst（GPL-2.0-or-later）を `subprocess` 経由の独立プロセスとして同梱し、リリース時にライセンス遵守（COPYING・対応ソース・DLL一覧）が欠けたらCIを失敗させる。
- [ ] 整合性チェック・再構築・ローカルゴミ箱・エクスポートがPST由来のメッセージにも同一に効くことを確認する。

**Phase 4.5 のゴール判定:** 6章の検証項目（とくに **V-1 のPoCブロッカー判定、V-2 のStage B冪等性、V-3 のマニフェストからのPSTアーカイブ完全復元、V-4 の世代交代失敗時の旧世代保護**）がすべて成功し、CIが緑になること。

---

## **2. 要件**

### **2.1 前提となる意思決定（確定済み）**

| # | 項目 | 決定内容 |
| :--- | :---- | :---- |
| D-1 | 実装順序 | **readpst の実PST PoC（グループA）を最優先の方式ブロッカー判定として先頭に置く**。日本語・文字コード・添付・階層・性能に加え、Windows固有の禁止文字・予約名・末尾ドット/空白・同名衝突・長パス・破損PST時の挙動・`lspst`出力の安定性を実測してから他グループへ進む。致命的な問題が見つかった場合は方式（Outlook COM等の代替）を再検討する |
| D-2 | マイグレーション番号 | 現時点の最新は `005_phase4.sql`。本フェーズは **`006_pst_import.sql`** を追加し、後続のPhase 5.1は **`007_generic_imap_connection.sql`** を使用する。開発計画書および関連する実装計画書の採番はこの割当へ統一済みとする |
| D-3 | 計画書の構成 | **本フェーズは1冊の計画書にまとめる**。PoC・スキーマ・変換エンジン・ユースケース・機能ガード・GUI・整合性対応・配布は相互依存が強く、別冊に分けると依存関係の追跡コストが上回るため |
| D-4 | CLIへの公開範囲 | Stage A/Bの実行（PSTインポート本体）は **GUI限定**とする。空き容量警告・オプション選択（文字セット・削除済み含む）・世代交代の確認は対話的な確認を要するため、Phase 4 D-3 と同じ思想でCLIには追加しない。既存の `verify` / `reindex` サブコマンドは **PSTマニフェストにも対応させ**、CLIから検証・再構築だけは行えるようにする |
| D-5 | readpst の入手経路 | **Windows版**は MSYS2 の `mingw-w64-ucrt-x86_64-libpst` から `readpst.exe` / `lspst.exe` と依存DLLを取得し `vendor/readpst/` へ同梱する（配布物はこれのみ）。**Linux版**（`pst-utils` パッケージ）はCIの結合テスト専用とし、配布物には含めない。取得手順は `tools/fetch_readpst.ps1`（Windows）としてスクリプト化し、CIでも同じスクリプトを使う。取得後、`vendor/readpst/readpst.exe.manifest` を `mt.exe` で `readpst.exe` のリソースへ適用するステップも同スクリプトに含める（D-19） |
| D-6 | PST取込ワーカーの置き場所 | 開発計画書 3.6「PST取込ワーカーも同期ワーカーと同じ単一ライター枠を使う。同期とPST取込の同時実行は許可しない」に従い、**既存 `SyncWorker` に PST取込操作（Stage A抽出・Stage B取込・世代交代）を追加する**（専用ワーカーは作らない）。単一ライター保証を追加の排他制御なしで満たせ、実装量も最小になるため。Stage A（readpst実行、数分〜数十分）の間も同期を止めてよいものとして扱う。ただし `probe()` はディスク書き込みを伴わないため、ウィザード側（またはバックグラウンドスレッド）で軽量に実行し、`SyncWorker` をブロックしない |
| D-7 | PSTのpurge/trashイベント記録先 | ローカルゴミ箱・30日purgeはIMAP側と完全に同一の振る舞い（開発計画書 4.10-6）とする。個別メッセージの通常ゴミ箱移動・復元はIMAP同様DB上の `local_state` のみで管理し、実削除（purge）イベントのみを **`manifests/pst/{import_uuid}/items.jsonl` に `purge_intent` / `purged` として追記する**。開発計画書 2.4-7「purge時も行を削除せずイベントを追記する」の対象をPST側マニフェストに閉じ込め、IMAP用マニフェスト（`manifests/imap/{account_id}/`）の構造・イベント種別（Phase 4 グループA）を変更しない。アーカイブ全体のゴミ箱化・復元は世代ライフサイクルイベント（`generation_superseded` / `generation_restored`）として記録する |
| D-8 | 再変換（世代交代） | 開発計画書 4.10-7 を**完全に実装する**。新世代の全EML・マニフェスト・DB登録を検証後、単一DBトランザクションで新世代 `is_active=1` / 旧世代 `is_active=0, status='superseded'` に切り替える。新世代が中断・失敗した場合は旧世代を一切変更しない |
| D-9 | probe() と lspst | `lspst` の出力は安定した機械可読APIではないため、**パースして「参考値」としてのみ表示する**。取得できたフォルダ名・推定件数・PST種別が不明な場合は `None` / `unknown` とし、確定値として扱わない。正確な階層・件数は Stage A 完了時に確定する |
| D-10 | PST種別の判定 | `lspst` に頼らず、PSTファイル先頭のマジックバイト（`!BDN` シグネチャ＋バージョンワード）を読み、Unicode PST / ANSI PST / 不明の3値を独自に判定する。`lspst` はあくまで補助情報として使う |
| D-11 | PST結合テストのCI方針 | pytest に **`pst` マーカーを追加**し、`ci.yml` の3ジョブすべてで `-m "not docker and not gui and not pst"` として**常に除外**する。ローカルでの手動実行のみとし、readpst未同梱環境ではフィクスチャ側でskipする |
| D-12 | 配布・GPL遵守 | Phase 4.5 に含める。`vendor/readpst/` の取得スクリプト・`THIRD-PARTY-LICENSES.md` の完成・リリースCI（`release.yml`）でのGPL成果物遵守チェックまでを本フェーズのスコープとする。**PyInstaller/Inno Setupによる実際のパッケージングは Phase 6（配布）へ送る**（開発計画書 5.9 は配布全体のフェーズであり、本フェーズはreadpst同梱の土台を作るところまで） |
| D-13 | 既存フックの扱い | 既存コードには PST 対応の**先回り実装**が3箇所ある: [infrastructure/database/reindex.py](../src/mail_dock/infrastructure/database/reindex.py) の `manifests/pst` 明示スキップ、[infrastructure/storage/eml_storage.py](../src/mail_dock/infrastructure/storage/eml_storage.py) の `tmp/pstimp` 保護、[presentation/models/folder_tree_model.py](../src/mail_dock/presentation/models/folder_tree_model.py) の `message_filter` コメント。本フェーズはこれらのスキップ・保護を「解除」または「実装で埋める」形で進め、既存の防御的挙動（未対応時は安全側にスキップする）を壊さない |
| D-14 | 重複排除・物理共有 | IMAPとPSTの重複排除・相互参照は行わない。物理EML共有は**同一アカウント（PSTでは同一取込世代）内だけ**とし、新旧PST世代間では共有しない。`content_key` は各系統内の非一意照合にのみ使い、系統や世代をまたいだ突合には使わない |
| D-15 | 対応形式 | `.pst` のみ。`.ost` / 単体 `.msg` / mbox はスコープ外（開発計画書 1.4）。`-t e`（メールのみ）は固定オプションとし、設定項目にしない |
| D-16 | 世代の可視性と切戻し | 通常のPST一覧・検索には `is_active=1` かつ `completed` / `completed_with_errors` の世代だけを表示する。取込途中は再開UIだけ、`superseded` 世代はPSTゴミ箱だけに表示する。30日猶予中はアーカイブ単位で切戻し（`restore_generation`）可能とし、現行世代との入替を単一トランザクションで行う。確定状態は復元旧世代が `is_active=1, status='completed'` かつ全メッセージ `local_state='active'`、退避現行世代が `is_active=0, status='superseded'` かつ全メッセージ `local_state='trashed'` とする |
| D-17 | 切断時の調停 | 切断中はDB・マニフェスト・stagingへ書き込まず、readpst停止と内蔵ディスクへのログ記録だけを行う。再接続後にマーカーとマニフェストを検証して状態を調停する。世代切替は `generation_switch_committed` が無い場合は旧世代を正として復旧する |
| D-18 | readpstの信頼境界 | 同梱readpstは信頼境界内の独立プロセスとして扱う。staging走査のパス検証はstaging外の出力を**取り込まない**ためのものであり、コンバーターによるstaging外書込みのOSレベル封じ込めは本フェーズの対象外とする |
| D-19 | readpst.exeのマニフェスト適用 | Windows実機PoCで、日本語等非ASCIIフォルダ名を含むPST変換時に `mk_separate_dir` が `Illegal byte sequence`（EILSEQ）で失敗することを確認した。原因はMinGW/UCRTビルドのreadpst.exeが `activeCodePage` 未指定の既定マニフェストを埋め込んでおり、narrow `_mkdir` がプロセスのANSIコードページ（日本語環境ではCP932）でUTF-8のフォルダ名バイト列を解釈しようとするため。**`vendor/readpst/readpst.exe.manifest`（`activeCodePage=UTF-8` / `longPathAware=true`）を `mt.exe -manifest ... -outputresource:readpst.exe;#1` でリソースへ適用**することで解消することを実機（Windows 11）で確認済み。readpstの**ソースコードは改変しない**が、配布バイナリの `RT_MANIFEST` リソースのみ上流から変更されるため、`tools/fetch_readpst.ps1` にこの適用ステップを組み込み、適用前後両方のバイナリのSHA-256を `THIRD-PARTY-LICENSES.md` に記録する |

### **2.2 機能要件**

#### **変換エンジンとStage A/B（開発計画書 4.10-1・4.10-2）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-1 | `readpst -e -t e -8 -j 0 -q -C {charset} [-D] -d {logs/pstimp-{job_id}.log} -o {staging} {pst}` を `shell=False`・引数リストで起動すること。`-w` は使用しないこと | 4.10-1 |
| F-2 | 起動前に `readpst -V` でバージョンを取得し `pst_imports.readpst_version` へ記録すること | 4.10-1 |
| F-3 | Stage A はレジューム不可とし、中断時は `tmp/pstimp/{job_id}/` を破棄してやり直すこと。`CancelToken` 経由で `Popen.terminate()` → 応答が無ければ `kill()` すること。切断中は削除・状態更新を試みず、再接続後の調停で処理する | 4.10-2 / D-17 |
| F-4 | Stage A のreadpst終了後、staging全走査・全項目マニフェストの永続化・fsyncを完了してから、項目数・インベントリSHA-256・静的マニフェストSHA-256を含む `stageA_done.json` を `tmp`→fsync→`os.replace`→親ディレクトリfsync の順で作成すること。マーカーまたは検証が欠けるstagingは `suspect` とし、再開ではなく破棄＋再抽出を促すこと | 5.7.1-5 / D-17 |
| F-5 | Stage A 完了後、staging 配下を全走査して各項目の `source_item_key`・相対パス・フォルダ対応・サイズ・ハッシュを確定し、`pst_import_items` と `items.jsonl` へ固定してfsyncしてから `stageA_done.json` を作成すること。走査時に通常ファイル以外・symlink・junction/reparse point・`Path(p).resolve()` がstagingルート外となる項目をスキップし、警告ログを残すこと | 4.10-5-2 / D-18 |
| F-6 | Stage B は未完了の `pst_import_items` のみを対象とし、1通ごとのコミットを禁止してバッチ単位（100〜500件ごと、または一定時間・一定サイズごと）で処理すること。各項目を `EmlStorage.save_from_file()` で保存 → 対応する `items.jsonl` イベント追記 → バッチ単位で `flush_and_sync()` → `BEGIN IMMEDIATE` で `messages` / `message_contents` / 項目状態・パス・集計値を同一DBトランザクションでコミットすること。失敗時はロールバックし、マニフェストを正として未完了バッチから再適用できること | 4.7 / 4.10-2 |
| F-7 | Stage B の途中でキャンセルまたはアプリ終了した場合は `status='cancelled_resumable'` とし、同じ `import_uuid`・staging・項目マニフェストを保持すること。次回は新規取込ではなく同一ジョブの再開として扱うこと | 4.10-2 |
| F-8 | 全EMLが最終保存済みで解析失敗だけが残る場合は `completed_with_errors` としstagingを削除すること。未保存項目が残る場合は `failed_resumable` としstagingを保持すること。全項目成功時は `completed` としstagingを削除すること | 4.10-2 |
| F-9 | staging からの取り込みは `EmlStorage.save_from_file()` によるストリーミング保存でハッシュをチャンク計算し、メモリ上限600MBを維持すること。1ファイルが100MBを超える場合は本文解析をスキップして `pst_import_items.error_class='oversize'` として記録すること。Dateは上限付きヘッダー読み取りで判定し、不正・未来日時・取得不能時は `unknown/` に保存する（後から「再解析」で復旧可能） | 4.10-5-6 |
| F-10 | `Date` が解釈できないメールは `internal_date=NULL`、`date_sent=NULL` とし、保存先を `unknown/` にすること | 4.7 / 4.10-4 |

#### **抽象化層・変換ランナー（開発計画書 4.10-3）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-11 | `domain/importer.py` に `BaseArchiveImporter`（`probe()` / `extract()`）、`ArchiveFolder`、`ArchiveInfo`、`ExtractResult`、`ImportOptions` を定義すること。`BaseMailFetcher` とは統合しないこと（開発計画書 2.3） | 2.3 / 4.10-3 |
| F-12 | readpst 固有の終了コード・メッセージを上位層へ漏らさず、`domain/errors.py` に `ArchiveImportError`（→ `ConverterNotFound` / `ConverterFailed` / `UnreadableArchive`）として `MailDockError` 配下にラップすること | 4.10-3 |
| F-13 | `probe()` はキャンセル可能・長時間処理として実装し、取得できた範囲のフラットなフォルダ名と推定件数だけを任意情報として返すこと。不明な値は `None` とすること | 4.10-1 / D-9 |
| F-14 | `readpst_locator` が同梱パスの絶対解決・依存DLL欠落の検出を行い、失敗時は `ConverterNotFound` を送出すること | 4.10-3 |

#### **スキーマ・マニフェスト・リポジトリ（開発計画書 3.3・3.5.1・2.4-7）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-15 | `migrations/006_pst_import.sql` に `pst_imports`（`import_uuid` UNIQUE、`source_sha256`、`status`、`is_active`、`replaces_id`、`superseded_at`、`staging_path` 等）と `pst_import_items`（PK `(import_id, source_item_key)`）を追加すること。`uq_active_pst_source`（`source_sha256` に対する `is_active=1` の一意インデックス）を含むこと | 3.5.1 |
| F-16 | `remote_state='no_remote'` はCHECK制約を置かずアプリ側で検証すること。`uid`/`uidvalidity`/`imap_flags`/`flags_seen_at`/`last_seen_at`/`internal_date` は常にNULLとすること | 3.3 |
| F-17 | `folders.raw_name` に staging ルートからの相対ディレクトリパスを登録すること。元PST名を一意に復元できない場合はreadpst出力名を表示し、`original_name_unresolved=true` をマニフェストへ記録すること。`uidvalidity=NULL` / `last_seen_uid=0` / `is_sync_target=0` で固定すること | 3.2 |
| F-18 | `manifests/pst/{import_uuid}/` に、schema versionを持つ不変の `import.json`（`account_id`、`display_name`、`import_uuid`、`created_at`、原本SHA-256・サイズ・mtime・ファイル同一性・readpstバージョン・オプション）と `folders.json`（staging相対パス `raw_name` と `display_name` のマッピング）、append-onlyの `items.jsonl` を生成すること。静的JSONは `tmp`→fsync→`os.replace`→親ディレクトリfsyncで配置し、`items.jsonl` は各行末CRC32・torn write末尾修復・イベントスキーマ検証を既存 [manifest.py](../src/mail_dock/infrastructure/storage/manifest.py) と同等に実装すること。DBを除去しても全状態（アカウント・フォルダ・メッセージ・purge墓標・監査イベント）を再構築できるイベントを保存すること | 2.4-7 / D-17 |
| F-19 | アカウントIDを `pst_{原本SHA-256の先頭12桁}_{import_uuidの先頭8桁}` 形式で生成すること。完全な原本SHA-256はDBとマニフェストで保持し、短縮値を同一性判定に使わないこと | 2.4-6 |

#### **取込フロー・世代交代（開発計画書 4.10-4・4.10-7）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-20 | 取込開始時に完全な `source_sha256` で照合し、未完了ジョブがあれば「再開」または「未完了ジョブを破棄」を提示すること。再開では新しい `pst_imports` 行を作らないこと | 4.10-7 |
| F-21 | `is_active=1` の完成済みアーカイブがある場合、通常の再取り込みを禁止すること。ユーザーが明示的に「再変換」を選んだ場合のみ新しい `import_uuid` と `replaces_id` を持つ世代を作ること | 4.10-7 |
| F-22 | 世代交代は新世代の全EML・永続マニフェスト・DB登録を検証後、`generation_switch_prepared` をfsyncし、単一DBトランザクションで新世代の有効化・旧世代の `superseded` 化・旧世代全メッセージのゴミ箱化を行う。commit前に `generation_switch_committed` をfsyncし、これが無い停止時は旧世代を正として復旧すること | 4.10-7 / D-17 |
| F-23 | 切替完了後の旧世代はアーカイブ単位でローカルゴミ箱に置き、通常の30日猶予（`config.purge_mode` に従う）を経てpurgeすること。猶予中はアーカイブ単位の逆切替（`restore_generation`）で復元可能にし、確定状態（復元旧世代: `is_active=1, status='completed'` かつ全メッセージ `local_state='active'`、退避現行世代: `is_active=0, status='superseded'` かつ全メッセージ `local_state='trashed'`）を単一DBトランザクションで切り替え、旧世代purgeは同一世代内の共有EMLだけを参照確認すること | 4.10-7 / D-14 / D-16 |
| F-24 | 開始前に **PSTサイズ×2.5** の空き容量を確認し、不足時は開始させないこと。再変換では旧世代保持分も加算すること | 4.10-5-7 |
| F-25 | 原本 `.pst` は読み取りのみで開き、コピー・移動・変更を一切行わないこと | 4.10-5-5 |
| F-26 | `audit_log` とPSTマニフェストへ `pst_import` / `pst_reimport` / `pst_supersede` / `pst_restore_generation` / `pst_import_abandon` を記録すること | 3.5 / D-16 |

#### **機能ガード（開発計画書 4.10-6）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-27 | `provider_type='pst_import'` のアカウントに対し、同期・サーバー削除・フォルダ選択・定期同期を**ユースケース入口で無条件に拒否**すること。UIで隠すだけにしないこと | 4.10-6 |
| F-28 | CLIの `sync` / `folders` / `delete-remote` サブコマンドがPSTアカウントを対象外にすること | 4.10-6 / D-4 |

#### **GUI（開発計画書 4.6-1・4.10-8）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-29 | 左ペインを「メールアカウント」「PSTアーカイブ」の2ルートに分けること。「すべてのアカウント」はIMAPのみ、「すべてのPSTアーカイブ」はアクティブで完成済みのPST世代のみを横断表示・検索すること。取込途中は再開UIだけ、旧世代はPSTゴミ箱だけに表示すること | 4.6-1 / D-16 |
| F-30 | PSTアーカイブ選択中はツールバーの「同期」「サーバーから削除」を非表示にすること | 4.6-1 |
| F-31 | インポートウィザードが「ファイル選択 → probe（書き込みを伴わないバックグラウンド処理） → 再開/破棄または中止/再変換の提示 → オプション指定 → 空き容量チェック → Stage A進捗（SyncWorker） → Stage B進捗（SyncWorker、バッチ件数ベース） → （再変換時）世代切替 → サマリ（原本PST保管の注意書き付き）」の順で進むこと | 4.10-8 |
| F-32 | Stage Aのキャンセルは不完全stagingを削除して `abandoned` とし、Stage Bのキャンセルはstagingと項目マニフェストを保持して `cancelled_resumable` とすること | 4.10-8 |

#### **整合性チェック・ゴミ箱・エクスポート（開発計画書 4.8・4.4・4.9）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-33 | 再インデックス（`reindex`）が `manifests/pst/` からPST擬似アカウント・フォルダ・メッセージ・`pst_imports`/`pst_import_items`・purge墓標・監査イベントを再構築できること | 4.8 / D-4 |
| F-34 | 孤児スキャン・マニフェスト検証がPSTマニフェストにも対応し、対応イベントの無い孤児は推測登録せず隔離すること | 4.8 |
| F-35 | ローカルゴミ箱・30日purgeがPST由来メッセージにもIMAP側と完全に同一に効くこと。個別メッセージの通常ゴミ箱移動・復元はマニフェストへ書かずDB `local_state` で管理し、実削除（purge）時に出自に応じてIMAPまたはPSTのマニフェストwriterへ `purge_intent` / `purged` を記録すること。共有EML参照カウントは同一世代内で正しく機能し、新旧世代のpurgeが互いのEMLへ影響しないこと | 4.10-6 / D-14 |
| F-36 | エクスポート（eml / mbox / CSV）がPST由来メッセージにも共通で動作すること | 4.10-6 |

#### **配布・ライセンス（開発計画書 5.9）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-37 | `vendor/readpst/` のバイナリをGit管理外とし、`tools/fetch_readpst.ps1` が MSYS2 から取得したうえで `mt.exe` により `activeCodePage=UTF-8` マニフェストを `readpst.exe` へ適用すること | 5.9 / D-19 |
| F-38 | `THIRD-PARTY-LICENSES.md` に readpst・libpst・同梱DLLごとの名称・バージョン・ライセンス・対応ソース・取得元・SHA-256（マニフェスト適用前後の両方）を記載すること | 5.9 / D-19 |
| F-39 | リリースワークフローに、GPL成果物（バイナリ＋COPYING＋対応ソース＋SHA-256）が揃っていなければリリースを失敗させるチェックを追加すること | 5.9 |

### **2.3 非機能要件・制約**

| # | 指標 | 目標値 | 備考 |
| :--- | :---- | :---- | :---- |
| N-1 | 一時的な必要空き容量 | PSTサイズ × 約2 （抽出先 + 最終EML） | 開始判定は F-24 の×2.5（余裕を含む） |
| N-2 | Stage A/B の同時実行 | 常に1PSTファイルずつ順次処理 | 一時領域のピークが「最大のPST 1個分」に収まること（1.4） |
| N-3 | メモリ使用量 | 通常解析時も600MB以下を維持 | 100MB超はストリーミング保存し、本文解析をスキップ（F-9） |
| N-4 | Stage Bの再開性 | ディレクトリ走査順・連番に依存しない | `pst_import_items` に固定した項目のみで判定する |
| N-5 | レイヤー依存方向 | `domain` ← `usecases` ← `infrastructure`/`presentation` を維持 | `domain/importer.py` は外部依存ゼロ |
| N-6 | 単一ライター | 同期・PST取込・検証書き込みを`SyncWorker`へ集約 | Phase 4 の不変条件を維持（D-6） |

* `ruff format --check` / `ruff check` / `mypy` / `pytest`（`pst` マーカーを除く）を通すこと。
* `subprocess` は常に `shell=False`・引数リスト・実行ファイルは同梱パスの絶対解決とし、ユーザー入力をコマンド文字列へ連結しないこと。`CREATE_NO_WINDOW` でコンソールを表示しないこと。
* readpst の**ソースコード**は一切改変しないこと。回避策はすべてアプリ側で行うこと。ただし、日本語等非ASCIIフォルダ名の変換に必要な `activeCodePage=UTF-8` マニフェスト適用に限り、配布バイナリの `RT_MANIFEST` リソースを `mt.exe` でパッチすることを許可する（D-19）。適用前後のバイナリ両方のSHA-256を記録すること。

---

## **3. タスク**

> 依存関係: **A（PoC）→ (B（スキーマ・マニフェスト）・C（抽象化層・ランナー）) → D（取込）→ (F（GUI）・G（整合性）) → H**。E（機能ガード）はBの後に実施でき、I（テスト）は各グループと並行して作成する。

### **3.1 グループA: readpst PoC（*最優先。方式のブロッカー判定*）**

#### **A-1. Windows/Linux 双方でのバイナリ確保**

- [x] MSYS2 に `mingw-w64-ucrt-x86_64-libpst` を導入し、`readpst.exe` / `lspst.exe` を取得する
- [x] `ldd` 相当（`objdump -p` 等）で依存DLL（iconv / zlib 等）を列挙し、`vendor/readpst/` へ収集する
- [ ] `tools/fetch_readpst.ps1` として取得手順をスクリプト化する。MSYS2からの取得に加え、`mt.exe` による `activeCodePage=UTF-8` マニフェスト適用（D-19）と、適用前後のバイナリのSHA-256記録を含めること（**Group H で実施**）
- [ ] WSL または Linux CI コンテナに `pst-utils`（apt）を導入し、Windows版との出力差分（改行・ファイル名・文字コード）を確認する (**WSLに`pst-utils`を導入。出力差分確認は保留**)

#### **A-2. 実PSTでの実測（手元の実PSTを使用）**

- [x] 日本語フォルダ名・日本語本文（`cp932` / `iso-2022-jp`）・添付ファイル・深い階層を含むPSTで変換し、文字化け・添付欠損の有無を確認する **問題なし**
- [x] 日本語フォルダ名を含むPSTの変換で `mk_separate_dir` が `Illegal byte sequence` で失敗する事象を確認した。原因はreadpst.exeの既定マニフェストに `activeCodePage` 指定が無く、プロセスのANSIコードページ（CP932）でUTF-8フォルダ名をnarrow `_mkdir` に渡していたため。`vendor/readpst/readpst.exe.manifest`（`activeCodePage=UTF-8`）を `mt.exe` でreadpst.exeのリソースへ適用し、実機（Windows 11）で解消を確認した（D-19）
- [x] `-C cp932` と `-8` の組み合わせで文字化けが解消するか実測する
- [ ] Windows禁止文字（`: \ / * ? " < > |`）を含むPST内フォルダ名、予約名（`CON`/`PRN`/`NUL`/`COM1`等）、末尾ドット・空白、同名フォルダ、NFC正規化後の衝突をそれぞれ作成し、readpst出力ディレクトリ名がどうなるかを確認する
- [ ] `..`・絶対パス・UNC・ドライブ指定・ADS・symlink/junction/reparse point相当の名前を含むPSTを試し、readpstの挙動とstaging外に出た出力を取り込まない検証を確認する（OSレベルのreadpst隔離は対象外）
- [ ] MAX_PATH（260文字）を超えるパスが生成されるケースを作り、`tmp/pstimp/{import_uuid先頭8桁}/` の短いstagingパスで回避できることを確認する
- [ ] 破損PST・非対応形式PSTを用意し、readpstの終了コード・stderrの内容を確認する
- [ ] `lspst` の出力形式を確認し、対応するバージョンでのフォーマット安定性・不明形式時のフォールバック方針を確定する
- [ ] 変換速度（PSTサイズあたりの所要時間）を実測し、進捗UIの見積もりに使う指標（出力ファイル数か経過時間か）を決定する

#### **A-3. PoC結果の記録**

- [ ] 実測結果を本書に追記する表（`readpstバージョン` / `必要DLL一覧` / `-C既定値の妥当性` / `禁止文字・予約名・衝突・長パスの実際の挙動` / `破損PST時の終了コード` / `lspst出力の安定性` / `変換速度`）を埋める
- [ ] 致命的な問題が見つかった場合はここで立ち止まり、方式の再検討（Outlook COM等）を行う。問題がなければ以降のグループへ進む

---

### **3.2 グループB: スキーマ・マニフェスト・リポジトリ（*Aに依存*）**

#### **B-1. `migrations/006_pst_import.sql`**

- [ ] `pst_imports` テーブルを追加する（`import_uuid` UNIQUE、`account_id`、`source_filename`、`source_sha256`、`source_size_bytes`、`source_mtime`、`readpst_version`、`options_json`、`status`、`is_active`、`replaces_id`、`superseded_at`、`total_files`、`ingested_count`、`failed_count`、`staging_path`、`started_at`、`finished_at`、`error_message`）
- [ ] `idx_pst_src`（`source_sha256`）と `uq_active_pst_source`（`source_sha256` WHERE `is_active=1`）を追加する
- [ ] `pst_import_items` テーブルを追加する（PK `(import_id, source_item_key)`、`source_relative_path`、`folder_relative_path`、`source_size_bytes`、`source_sha256`、`final_relative_path`、`message_row_id`、`status`、`error_class`、`error_message`、`attempt_count`）
- [ ] マイグレーション適用前の自動バックアップ（`metadata.db.bak.{version}`）が既存機構で働くことを確認する
- [x] [実装計画書_Phase5.1](./実装計画書_Phase5.1_汎用IMAPサーバー対応.md) と開発計画書・Phase 0/1の採番表記を `007_generic_imap_connection.sql` / `006_pst_import.sql` へ統一する（D-2）

#### **B-2. リポジトリおよびストレージ拡張**

- [ ] `domain/repository.py` に `BasePstImportRepository`（`create_import` / `update_import_status` / `find_active_by_source_sha256` / `find_incomplete_by_source_sha256` / `upsert_import_item` / `list_incomplete_items` / `list_items` / `activate_generation` / `restore_generation`）と、メッセージ・PST項目・集計を同一SQLite接続でcommit/rollbackする書込み単位を定義する
- [ ] `infrastructure/database/pst_import_repository.py` に `SqlitePstImportRepository` を実装し、既存 `ConnectionManager` の接続を再利用する。`messages` / `message_contents` の登録は既存 `SqliteMessageRepository.add_message()` の正規化・FTS・競合解決ロジックを再利用/協調させ、同一トランザクション内で `pst_import_items` / 集計値とアトミックにコミット・明示的ロールバックできるようにする
- [ ] `tests/support/in_memory_repository.py` 相当のインメモリ実装（`InMemoryPstImportRepository`）を追加する
- [ ] `domain/ports.py` の `BaseEmlStorage` および `infrastructure/storage/eml_storage.py` に `save_from_file(account_id: str, internal_date: datetime | None, source_path: Path) -> StoredEml` を新設し、staging 上のファイルからチャンク読み取りでハッシュ計算と tmp への書き込み＋アトミック配置を行う（メモリ上限600MBを維持し、100MB超のファイルもストリーミング処理する）

#### **B-3. PST永続マニフェスト**

- [ ] `infrastructure/storage/pst_manifest.py` を新設し、`manifests/pst/{import_uuid}/import.json` / `folders.json` / `items.jsonl` の読み書きを実装する
- [ ] `import.json`（`account_id`、`display_name`、`import_uuid`、`created_at`、原本SHA-256・サイズ・mtime・ファイル同一性・readpstバージョン・オプション）および `folders.json`（staging相対パス `raw_name` と `display_name` のマッピング）をschema version・内容ハッシュ付きの不変スナップショットとして `tmp`→fsync→`os.replace`→親ディレクトリfsync の順で配置する
- [ ] `items.jsonl` の行形式を既存 [manifest.py](../src/mail_dock/infrastructure/storage/manifest.py) と同じ `{JSON}|CRC32:{8hex}` にし、`flush_and_sync()` と末尾torn write切り離しのロジックを再利用する（共通化できる部分は抽出してヘルパー化してもよいが、IMAP側マニフェストのイベント種別・frozensetは変更しない）
- [ ] 個別メッセージのイベント（`item_discovered` / `item_saved` / `item_parse_failed` / `item_oversize` / `item_reparsed` / `import_ready` / `purge_intent` / `purged`）および世代ライフサイクルイベント（`generation_switch_prepared` / `generation_switch_committed` / `generation_restored` / `generation_superseded` / `import_abandoned`）について、必須フィールド・遷移・冪等キーを検証するfrozensetとスキーマを実装する（個別メッセージの通常ゴミ箱移動・復元はマニフェストへ書かずDB `local_state` のみで管理しIMAPと対称にする）
- [ ] `domain/ports.py` に `BasePstManifestWriter` / `BasePstManifestReader` を追加する

#### **B-4. アプリ側検証**

- [ ] `remote_state='no_remote'` のメッセージについて、`uid`/`uidvalidity`/`imap_flags`/`flags_seen_at`/`last_seen_at`/`internal_date` が常にNULLであることをリポジトリ層で検証するヘルパーを追加する（CHECK制約は置かない。F-16）

---

### **3.3 グループC: 抽象化層・readpstランナー（*Bと並行可、Aに依存*）**

#### **C-1. `domain/importer.py`**

- [ ] `ArchiveFolder`（`relative_path` / `display_name` / `estimated_count`）、`ArchiveInfo`（`format` / `folders` / `estimated_total` / `source_sha256` / `source_size_bytes`）、`ExtractResult`（`staging_root` / `file_count` / `stderr_tail`）、`ImportOptions` を定義する
- [ ] `BaseArchiveImporter`（`probe()` / `extract()`）を定義する。`BaseMailFetcher` とは統合しない

#### **C-2. `domain/errors.py` の拡張**

- [ ] `ArchiveImportError` を `MailDockError` 配下に追加し、`ConverterNotFound` / `ConverterFailed` / `UnreadableArchive` を配下に定義する

#### **C-3. `infrastructure/importers/readpst_locator.py`**

- [ ] 同梱パス（`vendor/readpst/readpst.exe` 等）の絶対解決を実装する
- [ ] `readpst -V` によるバージョン取得を実装する
- [ ] 実行ファイル・依存DLLの欠落を `ConverterNotFound` へラップする

#### **C-4. `infrastructure/importers/readpst_runner.py`**

- [ ] `shell=False`・引数リストで F-1 のオプション構成を起動する
- [ ] 出力ファイル数のポーリングと経過時間による粗い進捗を実装する
- [ ] `CancelToken` 連携（`terminate()` → タイムアウト後 `kill()`）を実装する
- [ ] 非ゼロ終了・クラッシュを `ConverterFailed` へラップし、stderr末尾を保持する

#### **C-5. `infrastructure/importers/lspst_parser.py`**

- [ ] `lspst` 出力をパースし、不明な形式は値を推測せず `unknown` / `None` へフォールバックする
- [ ] PSTファイル先頭のマジックバイトによる種別判定（Unicode/ANSI/不明）を実装する（D-10）

---

### **3.4 グループD: 取込ユースケース（*B・Cに依存*）**

#### **D-1. `usecases/import_pst.py` — 事前検証とジョブ解決**

- [ ] 原本PSTのSHA-256をチャンク計算する（キャンセル可、読み取り専用オープンのみ）
- [ ] probe時に原本のSHA-256・サイズ・`mtime_ns`・取得可能ならファイル同一性を記録し、Stage A完了後に再検証する。不一致時は抽出物を採用せず `source_changed` として中止する
- [ ] `check_free_space()` を使い PSTサイズ×2.5 の空き容量を確認する（再変換時は旧世代保持分を加算）
- [ ] 完全SHA-256で既存 `pst_imports` を照合し、未完了ジョブがあれば「再開」/「破棄」を、`is_active=1` の完成世代があれば「中止」/「再変換」を返す

#### **D-2. Stage A（抽出）**

- [ ] `tmp/pstimp/{import_uuid先頭8桁}/` へ `readpst_runner` を実行する
- [ ] readpst終了後に全項目インベントリを永続化・fsyncし、項目数と各マニフェストの内容ハッシュを含む `stageA_done.json` を原子的に作成する
- [ ] 通常のキャンセル・失敗時はstagingを破棄し `status='abandoned'` とする。切断中は書込みや削除をせず、再接続後の調停でマーカーなしを `suspect` として破棄＋再抽出を提示する

#### **D-3. 項目確定とStage B（取込）**

- [ ] staging全走査で各項目の `source_item_key`・相対パス・フォルダ対応・サイズ・ハッシュを固定し、`pst_import_items` と `item_discovered` イベントへ書き込んでfsyncする
- [ ] 通常ファイル以外、symlink、junction/reparse point、`Path(p).resolve()` がstagingルート外の項目はスキップし、警告ログを残す
- [ ] `total_files` とインベントリハッシュを確定し、`import_ready` をfsyncした後に `stageA_done.json` を原子的に配置して `status='ready_to_ingest'` とする
- [ ] Stage Bで未完了項目のみを対象とし、1通ごとのコミットを禁止してバッチ単位（100〜500件ごと、または一定時間・一定サイズごと）で処理する。各項目を `EmlStorage.save_from_file()` によるストリーミングで保存 → `items.jsonl` イベント追記 → バッチ単位で `flush_and_sync()` → 同一トランザクションで `messages` / `message_contents` / 項目状態・集計値をcommitする。失敗時はrollbackし、マニフェストから未完了バッチを再適用する
- [ ] `Date` 未解釈は `internal_date=NULL` / `unknown/` へ、100MB超は `oversize` 記録＋本文解析スキップとする
- [ ] キャンセル・アプリ終了時は `cancelled_resumable` とし、staging・項目マニフェストを保持する
- [ ] 全項目成功で `completed`（staging削除）、解析失敗のみ残存で `completed_with_errors`（staging削除）、未保存項目残存で `failed_resumable`（staging保持）とする

#### **D-4. 世代交代**

- [ ] 新世代の全EML・マニフェスト・DB登録の検証を実装する
- [ ] `generation_switch_prepared` をfsync後、`activate_generation()` により単一DBトランザクションで新世代の有効化・旧世代の `superseded` 化・旧世代全件のゴミ箱化を行い、commit前に `generation_switch_committed` をfsyncする
- [ ] committedイベントが無い停止時は旧世代を正として復旧し、失敗時に旧世代が一切変更されないことを保証する
- [ ] 切替完了後の旧世代はPSTゴミ箱にだけ表示し、猶予中は `restore_generation()` によりアーカイブ単位で逆切替できるようにする。確定状態（復元旧世代: `is_active=1, status='completed'` かつ全メッセージ `local_state='active'`、退避現行世代: `is_active=0, status='superseded'` かつ全メッセージ `local_state='trashed'`）を単一DBトランザクションで切り替え、マニフェストに `generation_restored` を記録する

#### **D-5. 監査・切断対応**

- [ ] `audit_log` へ `pst_import` / `pst_reimport` / `pst_supersede` / `pst_import_abandon` を記録する
- [ ] `StorageDetachedError` 検知時はreadpstプロセスを `terminate()`→`kill()` し、切断中にDB・マニフェスト・stagingへ書込まない。再接続後にマーカー・インベントリ・イベントを調停し、マーカーなし/不正は `suspect`（再抽出）、正常なマーカーはマニフェストをDBへ再適用して通常再開とする

---

### **3.5 グループE: 機能ガード（*Dと並行可、Bに依存*）**

- [ ] `usecases/sync_mail.py::sync_account()` の入口で `provider_type=='pst_import'` を拒否する
- [ ] `usecases/sync_folders.py::refresh_folders()` / `set_sync_target()` の入口で同様に拒否する
- [ ] `usecases/delete_remote.py::dry_run()` / `execute()` の入口で同様に拒否する
- [ ] 定期同期・起動時同期のアカウント列挙からPSTアカウントを除外する
- [ ] CLI の `sync` / `folders` / `delete-remote` からPSTアカウントを対象外にする
- [ ] 上記すべてが「UIで隠すだけでなく拒否される」ことを単体テストで固定する

---

### **3.6 グループF: GUI（*Dに依存、E・Gと並行可*）**

- [ ] `presentation/models/folder_tree_model.py` に `build_pst_archive_root()` を追加する
- [ ] `MainWindow` を「メールアカウント」「PSTアーカイブ」の2ルート表示に対応させる。横断ビューは各ルート内に限定する
- [ ] PSTルート選択中はツールバーの「同期」「サーバーから削除」を非表示にする
- [ ] `presentation/views/import_wizard.py`（`QWizard`）を新設する: ファイル選択 → `probe()`（ディスク書き込みを伴わないためウィザード側バックグラウンドスレッドで実行し、`SyncWorker` をブロックしない）→ 再開/破棄または中止/再変換の提示 → オプション（表示名・`-C`文字セット・`-D`削除済み含む）→ 空き容量チェック → Stage A進捗（`SyncWorker` へ投入、キャンセル可）→ Stage B進捗（`SyncWorker` へ投入、バッチ件数ベース、キャンセル可）→ （再変換時）世代切替 → サマリ（成功/失敗/スキップ件数、ログ導線、**原本PST保管の注意書き**）
- [ ] ファイルメニューへ「PSTをインポート」アクションを追加する
- [ ] 同期とPST取込の同時実行を防ぐため、先行処理完了までウィザードのStage A/B実行操作をブロックする（D-6）
- [ ] `strings.py` に関連文言を追加する

---

### **3.7 グループG: 整合性・再構築・ゴミ箱の対応（*Dに依存*）**

- [ ] [reindex.py](../src/mail_dock/infrastructure/database/reindex.py) の `manifests/pst` スキップを解除し、`import.json`（アカウントID・表示名・作成日時等）/ `folders.json`（フォルダ名対応）/ `items.jsonl` からPST擬似アカウント・フォルダ・メッセージ・`pst_imports`/`pst_import_items`・purge墓標・監査イベントの完全再構築を実装する
- [ ] `verify.py` の `orphan_scan()` / `verify_manifest()` をPSTマニフェストに対応させる。対応イベントの無い孤児は隔離し、推測登録しない
- [ ] 出自によりIMAPまたはPSTのマニフェストwriterを選ぶルーターを追加し、実削除（purge）時に `purge_intent` / `purged` イベントを正しく永続化する（個別メッセージの通常ゴミ箱移動・復元はマニフェストへ記録せずDB `local_state` のみで管理）。PSTの参照カウントは同一世代内だけを対象とし、新旧世代のpurgeが互いのEMLへ影響しないことを確認する
- [ ] エクスポート（`export_message.py` / `export_mbox.py` / `export_attachments.py`）がPST由来メッセージでも動作することを確認する

---

### **3.8 グループH: 配布・GPL遵守（*A・Gに依存*）**

- [ ] `tools/fetch_readpst.ps1` を確定版にし、取得物のSHA-256を記録する
- [ ] `vendor/readpst/COPYING`（GPL-2.0全文）を配置する
- [ ] `THIRD-PARTY-LICENSES.md` の「Phase 4.5で追記予定」コメントを埋め、readpst / libpst / 同梱DLLごとに名称・バージョン・ライセンス・対応ソース・取得元・SHA-256を列記する
- [ ] `README.md` に readpst入手・検証手順と「原本PSTを保管すること」の注意を追記する
- [ ] `.github/workflows/release.yml` を新設し、MSYS2から取得 → クリーンなWindowsで `readpst -V` のスモークテスト → GPL成果物（バイナリ＋COPYING＋対応ソース＋SHA-256）が揃わない場合にリリースを失敗させるチェックを実装する（PyInstaller/Inno Setup本体は含めない。D-12）

---

### **3.9 グループI: テスト（*各グループと並行して作成*）**

#### **I-1. CI設定**

- [ ] `pyproject.toml` の `markers` に `pst` を追加する
- [ ] `.github/workflows/ci.yml` の3ジョブすべてを `-m "not docker and not gui and not pst"` に更新する

#### **I-2. 単体テスト**

- [ ] `tests/unit/test_readpst_locator.py`：パス解決・バージョン取得・DLL欠落時の`ConverterNotFound`
- [ ] `tests/unit/test_readpst_runner.py`：fake subprocessでのクラッシュ・無応答・キャンセルの注入
- [ ] `tests/unit/test_lspst_parser.py`：不明形式のフォールバック、PST種別のマジックバイト判定
- [ ] `tests/unit/test_eml_storage.py` の拡張：`save_from_file()` によるストリーミング保存・ハッシュ計算・重複検出・アトミック配置
- [ ] `tests/unit/test_import_pst.py`：状態遷移表、Stage Aインベントリ確定前の停止、Stage Bのバッチ処理・冪等性・項目順変更耐性・再開・cancel、DB文単位の失敗とrollback、`completed_with_errors`、未完了破棄、切替committed前の停止時に旧世代が維持されること、世代切戻し（確定状態の検証）、原本変更、パストラバーサル防御、oversize処理
- [ ] `tests/unit/test_pst_manifest.py`：静的JSONの原子的配置・内容ハッシュ、`items.jsonl` のCRC32検証・末尾torn write切り離し、全イベントからの状態再構築
- [ ] 既存ガードテスト（`test_sync_mail.py` 等）にPSTアカウント拒否のケースを追加する

#### **I-3. 結合テスト（`pst` マーカー、readpst未同梱環境ではskip）**

- [ ] `tests/integration/test_pst_import.py`：実readpstで小規模PSTを取り込み、日本語・文字コード・添付・禁止文字・予約名・末尾ドット/空白・衝突・深い階層・破損PST・staging外を指す名称を検証する
- [ ] `tests/integration/test_pst_reindex.py`：`metadata.db`を破棄し、EML＋PSTマニフェストのみからPSTアーカイブが完全復元されることを検証する

#### **I-4. GUIテスト（`gui` マーカー）**

- [ ] `tests/gui/test_import_wizard.py`：ウィザード各ページの遷移・キャンセル・進捗表示
- [ ] `tests/gui/test_folder_tree_model.py` の拡張：2ルート構成、PSTルート選択時のツールバー抑止

#### **I-5. 静的テスト・ドキュメント**

- [ ] `tests/unit/test_main.py` の拡張：CLIにPSTインポート系サブコマンドが存在しないこと
- [ ] `ruff check .` / `mypy .` / `pytest -m "not docker and not gui and not pst"` を実行し、全テスト通過を確認する
- [ ] 開発計画書6章「Phase 4.5」の行、および本書「4. PoC結果」節を実測値で更新する
- [ ] `.github/copilot-instructions.md` の該当記述を更新する

---

## **4. PoC実測記録（グループA完了後に記入）**

| 項目 | 実測値 |
| :---- | :---- |
| readpstバージョン | v0.6.76 |
| 必要DLL一覧 | `libpst-4.dll`, `libgcc_s_seh-1.dll`, `libgsf-1-114.dll`, `libgobject-2.0-0.dll`, `libsystre-0.dll`, `libwinpthread-1.dll`, `zlib1.dll`, `libiconv-2.dll`, `libbz2-1.dll`, `libintl-8.dll`, `libglib-2.0-0.dll`, `libstdc++-6.dll`, `libffi-8.dll`, `libtre-5.dll`, `libgio-2.0-0.dll`, `libxml2-16.dll`, `libgmodule-2.0-0.dll`, `libpcre2-8-0.dll`（MSYS2 UCRT64由来。Windows標準DLLは同梱対象外） |
| `-C cp932` + `-8` の日本語再現性 | （記入） |
| Windows上の日本語フォルダ名変換時の`Illegal byte sequence`と対処 | readpst.exe既定マニフェストに`activeCodePage`指定が無くANSIコードページ（CP932）想定のためEILSEQで失敗。`mt.exe`で`vendor/readpst/readpst.exe.manifest`（`activeCodePage=UTF-8`）をreadpst.exeのリソースへ適用し解消（実機Windows 11で確認。D-19） |
| Windows禁止文字・予約名・末尾ドット/空白・衝突時の挙動 | （記入） |
| MAX_PATH超過時の回避可否 | （記入） |
| 破損PST時の終了コード・stderr | （記入） |
| `lspst`出力の安定性 | （記入） |
| 変換速度（分/GB目安） | 10.3分/GB |
| 致命的問題の有無・方式継続の可否 | （記入） |

---

## **5. スコープ境界**

### **5.1 含むもの**

セクション3のグループA〜I。「readpst PoC → スキーマ・マニフェスト → 抽象化層・変換ランナー → 取込ユースケース（Stage A/B・再開・世代交代）→ 機能ガード → GUI → 整合性・再構築・ゴミ箱対応 → 配布・GPL遵守」の一式。

### **5.2 含まないもの（明示的に除外）**

| 除外項目 | 実施フェーズ・理由 |
| :---- | :---- |
| `.ost` / 単体 `.msg` / mbox の取込 | **恒久的にスコープ外**（開発計画書 1.4） |
| 予定表・連絡先・仕事（PST内の非メールアイテム） | **恒久的にスコープ外**。`-t e` 固定 |
| IMAP側とPST側の重複排除・相互参照・統合ビュー | **恒久的にスコープ外**（開発計画書 1.3） |
| PyInstaller / Inno Setup による実際のパッケージング | **Phase 6（配布）** |
| Gmail / OAuth2 / 汎用IMAP対応 | **Phase 5** |
| クライアント証明書・mTLS | 恒久的にスコープ外（Phase 5.1 と同様） |
| コード署名・自動更新機構 | 恒久的にスコープ外 |

---

## **6. 検証**

各項目の完了を確認したうえで、対応するタスクのチェックボックスを埋めること。

- [ ] V-1（ブロッカー）. 実PSTのPoCで、日本語・添付・Windows禁止文字・予約名・末尾ドット/空白・衝突・長パスに致命的な問題が無いこと。問題があれば方式を再検討し、本書4章に結論を記載すること
- [ ] V-2（中核）. Stage Bを任意の件数で中断→再開して、二重登録・欠落なく完了すること。項目順（ディレクトリ走査順）を入れ替えても同一結果になること
- [ ] V-3（中核）. `metadata.db` を削除し、EML＋PST永続マニフェストだけからPSTアーカイブ（擬似アカウント・フォルダ・メッセージ・項目状態・ゴミ箱/ purge墓標・監査イベント・世代の可視性）が完全復元されること
- [ ] V-4. 世代交代を新世代検証直後に強制失敗させ、旧世代が閲覧可能なまま無傷であること。また世代切戻し（`restore_generation`）により確定状態（新世代trashed / 旧世代active）が正しく入れ替わること
- [ ] V-5. Stage A中／Stage B中に切断を注入し、切断中にストレージへ書込みを試みないこと。再接続後の調停で、Stage Aはマーカーなし/不正なら `suspect`（破棄＋再抽出）、Stage Bは正常なマーカーとインベントリがあれば `cancelled_resumable`（再開可能）になること
- [ ] V-6. PSTアカウントに対する同期・フォルダ選択・サーバー削除が、GUI・CLI・ユースケースのすべてで拒否されること
- [ ] V-7. 同一取込世代内の共有EMLで最後の非purged参照が消える場合だけ実ファイルが削除されること。新旧世代のEMLパスは共有されず、旧世代のpurgeが新世代のEMLへ影響しないこと
- [ ] V-8. リリースCIで、GPL成果物（バイナリ＋COPYING＋対応ソース＋SHA-256）が欠けた場合にジョブが失敗すること
- [ ] V-9. `uv run ruff format --check .` / `uv run ruff check .` / `uv run mypy` が成功すること
- [ ] V-10. `uv run pytest -m "not docker and not gui and not pst"` がCIで緑になること（`pst` マーカーはローカル手動実行のみ）
- [ ] V-11. `domain` / `usecases` の層依存方向が維持されていること（`domain/importer.py` に外部依存が無いこと）

---

## **7. 引き継ぎ事項**

- [ ] Phase 4.5 完了後、開発計画書6章の該当行と `docs/実装計画書_Phase5.1_汎用IMAPサーバー対応.md` のマイグレーション番号表記の整合を最終確認する
- [ ] Phase 6（配布）へ、`vendor/readpst/` 同梱・GPL遵守チェックを含めたPyInstaller/Inno Setupパッケージングの実施を引き継ぐ
- [ ] 実機での大規模PST（数GB規模）検証は本フェーズでは小〜中規模PSTでのPoC・結合テストに留めており、フルスケール検証が必要であれば別途手動検証として実施すること
