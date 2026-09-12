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
- [x] Windows禁止文字（`: \ / * ? " < > |`）を含むPST内フォルダ名、予約名（`CON`/`PRN`/`NUL`/`COM1`等）、末尾ドット・空白、同名フォルダ、NFC正規化後の衝突をそれぞれ作成し、readpst出力ディレクトリ名がどうなるかを確認する （libpst 0.6.76 の `check_filename()` / `mk_separate_dir()` を移植した `tools/pst_poc/simulate_readpst_dirnames.py` で実測。**`* ? " < > |` と制御文字は `EINVAL` で `DIE()` → 変換全体が異常終了**するのが最大のリスク。詳細は4.2節）
- [x] `..`・絶対パス・UNC・ドライブ指定・ADS・symlink/junction/reparse point相当の名前を含むPSTを試し、readpstの挙動とstaging外に出た出力を取り込まない検証を確認する（OSレベルのreadpst隔離は対象外） （**staging外へ出たケースはゼロ**。`/ \ :` が `_` へ置換されるためトラバーサル・UNC・ADSは無害化され、`.` / `..` はEEXISTループで `.1` / `..1` になる。詳細は4.2節）
- [x] MAX_PATH（260文字）を超えるパスが生成されるケースを作り、`tmp/pstimp/{import_uuid先頭8桁}/` の短いstagingパスで回避できることを確認する （`tools/pst_poc/run_longpath_probe.ps1`。238文字の `-o` で絶対パス310文字を生成し成功。ただし成立条件は `longPathAware=true` **かつ** OSの `LongPathsEnabled=1` の両方であり、後者はユーザー環境依存 → 短いstagingパスによる回避は必須のまま）
- [x] 破損PST・非対応形式PSTを用意し、readpstの終了コード・stderrの内容を確認する （`tools/pst_poc/make_corrupt_pst.py` + `run_corrupt_matrix.ps1`。**全ケースで終了コード1・stderrは空・メッセージはstdoutへ出力**）
- [x] `lspst` の出力形式を確認し、対応するバージョンでのフォーマット安定性・不明形式時のフォールバック方針を確定する （`run_lspst_matrix.ps1`。**有効なPSTでも `A second message_store has been found.` で途中終了し終了コード1**。フォールバック方針は下記A-3の注記参照）
- [x] 変換速度（PSTサイズあたりの所要時間）を実測し、進捗UIの見積もりに使う指標（出力ファイル数か経過時間か）を決定する  **進捗UIの見積もり指標は出力ファイル数を使う**

#### **A-3. PoC結果の記録**

- [x] 実測結果を本書に追記する表（`readpstバージョン` / `必要DLL一覧` / `-C既定値の妥当性` / `禁止文字・予約名・衝突・長パスの実際の挙動` / `破損PST時の終了コード` / `lspst出力の安定性` / `変換速度`）を埋める
- [x] 致命的な問題が見つかった場合はここで立ち止まり、方式の再検討（Outlook COM等）を行う。問題がなければ以降のグループへ進む

---

### **3.2 グループB: スキーマ・マニフェスト・リポジトリ（*Aに依存*）**

#### **B-1. `migrations/006_pst_import.sql`**

- [x] `pst_imports` テーブルを追加する（`import_uuid` UNIQUE、`account_id`、`source_filename`、`source_sha256`、`source_size_bytes`、`source_mtime`、`readpst_version`、`options_json`、`status`、`is_active`、`replaces_id`、`superseded_at`、`total_files`、`ingested_count`、`failed_count`、`staging_path`、`started_at`、`finished_at`、`error_message`）
- [x] `idx_pst_src`（`source_sha256`）と `uq_active_pst_source`（`source_sha256` WHERE `is_active=1`）を追加する
- [x] `pst_import_items` テーブルを追加する（PK `(import_id, source_item_key)`、`source_relative_path`、`folder_relative_path`、`source_size_bytes`、`source_sha256`、`final_relative_path`、`message_row_id`、`status`、`error_class`、`error_message`、`attempt_count`）
- [x] マイグレーション適用前の自動バックアップ（`metadata.db.bak.{version}`）が既存機構で働くことを確認する
- [x] [実装計画書_Phase5.1](./実装計画書_Phase5.1_汎用IMAPサーバー対応.md) と開発計画書・Phase 0/1の採番表記を `007_generic_imap_connection.sql` / `006_pst_import.sql` へ統一する（D-2）

#### **B-2. リポジトリおよびストレージ拡張**

- [x] `domain/repository.py` に `BasePstImportRepository`（`create_import` / `update_import_status` / `find_active_by_source_sha256` / `find_incomplete_by_source_sha256` / `upsert_import_item` / `list_incomplete_items` / `list_items` / `activate_generation` / `restore_generation`）と、メッセージ・PST項目・集計を同一SQLite接続でcommit/rollbackする書込み単位を定義する
- [x] `infrastructure/database/pst_import_repository.py` に `SqlitePstImportRepository` を実装し、既存 `ConnectionManager` の接続を再利用する。`messages` / `message_contents` の登録は既存 `SqliteMessageRepository.add_message()` の正規化・FTS・競合解決ロジックを再利用/協調させ、同一トランザクション内で `pst_import_items` / 集計値とアトミックにコミット・明示的ロールバックできるようにする
- [x] `tests/support/in_memory_repository.py` 相当のインメモリ実装（`InMemoryPstImportRepository`）を追加する
- [x] `domain/ports.py` の `BaseEmlStorage` および `infrastructure/storage/eml_storage.py` に `save_from_file(account_id: str, internal_date: datetime | None, source_path: Path) -> StoredEml` を新設し、staging 上のファイルからチャンク読み取りでハッシュ計算と tmp への書き込み＋アトミック配置を行う（メモリ上限600MBを維持し、100MB超のファイルもストリーミング処理する）

#### **B-3. PST永続マニフェスト**

- [x] `infrastructure/storage/pst_manifest.py` を新設し、`manifests/pst/{import_uuid}/import.json` / `folders.json` / `items.jsonl` の読み書きを実装する
- [x] `import.json`（`account_id`、`display_name`、`import_uuid`、`created_at`、原本SHA-256・サイズ・mtime・ファイル同一性・readpstバージョン・オプション）および `folders.json`（staging相対パス `raw_name` と `display_name` のマッピング）をschema version・内容ハッシュ付きの不変スナップショットとして `tmp`→fsync→`os.replace`→親ディレクトリfsync の順で配置する
- [x] `items.jsonl` の行形式を既存 [manifest.py](../src/mail_dock/infrastructure/storage/manifest.py) と同じ `{JSON}|CRC32:{8hex}` にし、`flush_and_sync()` と末尾torn write切り離しのロジックを再利用する（共通化できる部分は抽出してヘルパー化してもよいが、IMAP側マニフェストのイベント種別・frozensetは変更しない）
- [x] 個別メッセージのイベント（`item_discovered` / `item_saved` / `item_parse_failed` / `item_oversize` / `item_reparsed` / `import_ready` / `purge_intent` / `purged`）および世代ライフサイクルイベント（`generation_switch_prepared` / `generation_switch_committed` / `generation_restored` / `generation_superseded` / `import_abandoned`）について、必須フィールド・遷移・冪等キーを検証するfrozensetとスキーマを実装する（個別メッセージの通常ゴミ箱移動・復元はマニフェストへ書かずDB `local_state` のみで管理しIMAPと対称にする）
- [x] `domain/ports.py` に `BasePstManifestWriter` / `BasePstManifestReader` を追加する

#### **B-4. アプリ側検証**

- [x] `remote_state='no_remote'` のメッセージについて、`uid`/`uidvalidity`/`imap_flags`/`flags_seen_at`/`last_seen_at`/`internal_date` が常にNULLであることをリポジトリ層で検証するヘルパーを追加する（CHECK制約は置かない。F-16）

---

### **3.3 グループC: 抽象化層・readpstランナー（*Bと並行可、Aに依存*）**

#### **C-1. `domain/importer.py`**

- [x] `ArchiveFolder`（`relative_path` / `display_name` / `estimated_count`）、`ArchiveInfo`（`format` / `folders` / `estimated_total` / `source_sha256` / `source_size_bytes`）、`ExtractResult`（`staging_root` / `file_count` / `stdout_tail` / `stderr_tail`）、`ImportOptions` を定義する
- [x] `BaseArchiveImporter`（`probe()` / `extract()`）を定義する。`BaseMailFetcher` とは統合しない

#### **C-2. `domain/errors.py` の拡張**

- [x] `ArchiveImportError` を `MailDockError` 配下に追加し、`ConverterNotFound` / `ConverterFailed` / `UnreadableArchive` を配下に定義する

#### **C-3. `infrastructure/importers/readpst_locator.py`**

- [x] 同梱パス（`vendor/readpst/readpst.exe` 等）の絶対解決を実装する
- [x] `readpst -V` によるバージョン取得を実装する
- [x] 実行ファイル・依存DLLの欠落を `ConverterNotFound` へラップする

#### **C-4. `infrastructure/importers/readpst_runner.py`**

- [x] `shell=False`・引数リストで F-1 のオプション構成を起動する
- [x] 出力ファイル数のポーリングと経過時間による粗い進捗を実装する
- [x] `CancelToken` 連携（`terminate()` → タイムアウト後 `kill()`）を実装する
- [x] 非ゼロ終了・クラッシュを `ConverterFailed` へラップし、**stdoutとstderr両方**の末尾を保持する（readpstは致命的エラーをstdoutへ出力する。P-1）
- [x] `mk_separate_dir: Cannot create directory` を検出した場合は、フォルダ名がWindowsで作成できないことを示す専用メッセージを `ConverterFailed` に付与する（P-7）

#### **C-5. `infrastructure/importers/lspst_parser.py`**

- [x] `lspst` 出力をパースし、不明な形式は値を推測せず `unknown` / `None` へフォールバックする。**終了コードを成否判定に使わず**、取得できた `Folder` 行だけをフラットな参考値として採用する（件数は `None`。P-6）
- [x] PSTファイル先頭のマジックバイトによる種別判定（Unicode/ANSI/不明）を実装する（D-10）

---

### **3.4 グループD: 取込ユースケース（*B・Cに依存*）**

#### **D-1. `usecases/import_pst.py` — 事前検証とジョブ解決**

- [x] 原本PSTのSHA-256をチャンク計算する（キャンセル可、読み取り専用オープンのみ）
- [x] probe時に原本のSHA-256・サイズ・`mtime_ns`・取得可能ならファイル同一性を記録し、Stage A完了後に再検証する。不一致時は抽出物を採用せず `source_changed` として中止する
- [x] `check_free_space()` を使い PSTサイズ×2.5 の空き容量を確認する（再変換時は旧世代保持分を加算）
- [x] 完全SHA-256で既存 `pst_imports` を照合し、未完了ジョブがあれば「再開」/「破棄」を、`is_active=1` の完成世代があれば「中止」/「再変換」を返す

#### **D-2. Stage A（抽出）**

- [x] `tmp/pstimp/{import_uuid先頭8桁}/` へ `readpst_runner` を実行する
- [x] readpst終了後に全項目インベントリを永続化・fsyncし、項目数と各マニフェストの内容ハッシュを含む `stageA_done.json` を原子的に作成する
- [x] 通常のキャンセル・失敗時はstagingを破棄し `status='abandoned'` とする。切断中は書込みや削除をせず、再接続後の調停でマーカーなしを `suspect` として破棄＋再抽出を提示する

#### **D-3. 項目確定とStage B（取込）**

- [x] staging全走査で各項目の `source_item_key`・相対パス・フォルダ対応・サイズ・ハッシュを固定し、`pst_import_items` と `item_discovered` イベントへ書き込んでfsyncする。`folder_relative_path` は**実際にディスク上に存在する名前**を記録し、正規化や元名の推測を行わない（P-10～P-12）
- [x] 通常ファイル以外、symlink、junction/reparse point、`Path(p).resolve()` がstagingルート外の項目はスキップし、警告ログを残す
- [x] `total_files` とインベントリハッシュを確定し、`import_ready` をfsyncした後に `stageA_done.json` を原子的に配置して `status='ready_to_ingest'` とする
- [x] Stage Bで未完了項目のみを対象とし、1通ごとのコミットを禁止してバッチ単位（100〜500件ごと、または一定時間・一定サイズごと）で処理する。各項目を `EmlStorage.save_from_file()` によるストリーミングで保存 → `items.jsonl` イベント追記 → バッチ単位で `flush_and_sync()` → 同一トランザクションで `messages` / `message_contents` / 項目状態・集計値をcommitする。失敗時はrollbackし、マニフェストから未完了バッチを再適用する
- [x] `Date` 未解釈は `internal_date=NULL` / `unknown/` へ、100MB超は `oversize` 記録＋本文解析スキップとする
- [x] キャンセル・アプリ終了時は `cancelled_resumable` とし、staging・項目マニフェストを保持する
- [x] 全項目成功で `completed`（staging削除）、解析失敗のみ残存で `completed_with_errors`（staging削除）、未保存項目残存で `failed_resumable`（staging保持）とする

#### **D-4. 世代交代**

- [x] 新世代の全EML・マニフェスト・DB登録の検証を実装する
- [x] `generation_switch_prepared` をfsync後、`activate_generation()` により単一DBトランザクションで新世代の有効化・旧世代の `superseded` 化・旧世代全件のゴミ箱化を行い、commit前に `generation_switch_committed` をfsyncする
- [x] committedイベントが無い停止時は旧世代を正として復旧し、失敗時に旧世代が一切変更されないことを保証する
- [x] 切替完了後の旧世代はPSTゴミ箱にだけ表示し、猶予中は `restore_generation()` によりアーカイブ単位で逆切替できるようにする。確定状態（復元旧世代: `is_active=1, status='completed'` かつ全メッセージ `local_state='active'`、退避現行世代: `is_active=0, status='superseded'` かつ全メッセージ `local_state='trashed'`）を単一DBトランザクションで切り替え、マニフェストに `generation_restored` を記録する

#### **D-5. 監査・切断対応**

- [x] `audit_log` へ `pst_import` / `pst_reimport` / `pst_supersede` / `pst_import_abandon` を記録する
- [x] `StorageDetachedError` 検知時はreadpstプロセスを `terminate()`→`kill()` し、切断中にDB・マニフェスト・stagingへ書込まない。再接続後にマーカー・インベントリ・イベントを調停し、マーカーなし/不正は `suspect`（再抽出）、正常なマーカーはマニフェストをDBへ再適用して通常再開とする

---

### **3.5 グループE: 機能ガード（*Dと並行可、Bに依存*）**

- [x] `usecases/sync_mail.py::sync_account()` の入口で `provider_type=='pst_import'` を拒否する
- [x] `usecases/sync_folders.py::refresh_folders()` / `set_sync_target()` の入口で同様に拒否する
- [x] `usecases/delete_remote.py::dry_run()` / `execute()` の入口で同様に拒否する
- [x] 定期同期・起動時同期のアカウント列挙からPSTアカウントを除外する
- [x] CLI の `sync` / `folders` / `delete-remote` からPSTアカウントを対象外にする
- [x] 上記すべてが「UIで隠すだけでなく拒否される」ことを単体テストで固定する

---

### **3.6 グループF: GUI（*Dに依存、E・Gと並行可*）**

- [x] `presentation/models/folder_tree_model.py` に `build_pst_archive_root()` を追加する
- [x] `MainWindow` を「メールアカウント」「PSTアーカイブ」の2ルート表示に対応させる。横断ビューは各ルート内に限定する
- [x] PSTルート選択中はツールバーの「同期」「サーバーから削除」を非表示にする
- [x] `presentation/views/import_wizard.py`（`QWizard`）を新設する: ファイル選択 → `probe()`（ディスク書き込みを伴わないためウィザード側バックグラウンドスレッドで実行し、`SyncWorker` をブロックしない）→ 再開/破棄または中止/再変換の提示 → オプション（表示名・`-C`文字セット・`-D`削除済み含む）→ 空き容量チェック → Stage A進捗（`SyncWorker` へ投入、キャンセル可）→ Stage B進捗（`SyncWorker` へ投入、バッチ件数ベース、キャンセル可）→ （再変換時）世代切替 → サマリ（成功/失敗/スキップ件数、ログ導線、**原本PST保管の注意書き**）
- [x] ファイルメニューへ「PSTをインポート」アクションを追加する
- [x] 同期とPST取込の同時実行を防ぐため、先行処理完了までウィザードのStage A/B実行操作をブロックする（D-6）
- [x] `strings.py` に関連文言を追加する

---

### **3.7 グループG: 整合性・再構築・ゴミ箱の対応（*Dに依存*）**

- [x] [reindex.py](../src/mail_dock/infrastructure/database/reindex.py) の `manifests/pst` スキップを解除し、`import.json`（アカウントID・表示名・作成日時等）/ `folders.json`（フォルダ名対応）/ `items.jsonl` からPST擬似アカウント・フォルダ・メッセージ・`pst_imports`/`pst_import_items`・purge墓標・監査イベントの完全再構築を実装する
- [x] `verify.py` の `orphan_scan()` / `verify_manifest()` をPSTマニフェストに対応させる。対応イベントの無い孤児は隔離し、推測登録しない
- [x] 出自によりIMAPまたはPSTのマニフェストwriterを選ぶルーターを追加し、実削除（purge）時に `purge_intent` / `purged` イベントを正しく永続化する（個別メッセージの通常ゴミ箱移動・復元はマニフェストへ記録せずDB `local_state` のみで管理）。PSTの参照カウントは同一世代内だけを対象とし、新旧世代のpurgeが互いのEMLへ影響しないことを確認する
- [x] エクスポート（`export_message.py` / `export_mbox.py` / `export_attachments.py`）がPST由来メッセージでも動作することを確認する

---

### **3.8 グループH: 配布・GPL遵守（*A・Gに依存*）**

- [x] `tools/fetch_readpst.ps1` を確定版にし、取得物のSHA-256を記録する
- [x] `vendor/readpst/COPYING`（GPL-2.0全文）を配置する
- [x] `THIRD-PARTY-LICENSES.md` の「Phase 4.5で追記予定」コメントを埋め、readpst / libpst / 同梱DLLごとに名称・バージョン・ライセンス・対応ソース・取得元・SHA-256を列記する
- [x] `README.md` に readpst入手・検証手順と「原本PSTを保管すること」の注意を追記する
- [x] `.github/workflows/release.yml` を新設し、MSYS2から取得 → クリーンなWindowsで `readpst -V` のスモークテスト → GPL成果物（バイナリ＋COPYING＋対応ソース＋SHA-256）が揃わない場合にリリースを失敗させるチェックを実装する（PyInstaller/Inno Setup本体は含めない。D-12）

---

### **3.9 グループI: テスト（*各グループと並行して作成*）**

#### **I-1. CI設定**

- [x] `pyproject.toml` の `markers` に `pst` を追加する
- [x] `.github/workflows/ci.yml` の3つの pytest 実行に `not pst` を追加する（Docker実行の `docker` 条件は維持）

#### **I-2. 単体テスト**

- [x] `tests/unit/test_readpst_locator.py`：パス解決・バージョン取得・DLL欠落時の`ConverterNotFound`
- [x] `tests/unit/test_readpst_runner.py`：fake subprocessでのクラッシュ・無応答・キャンセルの注入
- [x] `tests/unit/test_lspst_parser.py`：不明形式のフォールバック、PST種別のマジックバイト判定
- [x] `tests/unit/test_eml_storage.py` の拡張：`save_from_file()` によるストリーミング保存・ハッシュ計算・重複検出・アトミック配置
- [x] `tests/unit/test_import_pst.py`：状態遷移表、Stage Aインベントリ確定前の停止、Stage Bのバッチ処理・冪等性・項目順変更耐性・再開・cancel、DB文単位の失敗とrollback、`completed_with_errors`、未完了破棄、切替committed前の停止時に旧世代が維持されること、世代切戻し（確定状態の検証）、原本変更、パストラバーサル防御、oversize処理
- [x] `tests/unit/test_pst_manifest.py`：静的JSONの原子的配置・内容ハッシュ、`items.jsonl` のCRC32検証・末尾torn write切り離し、全イベントからの状態再構築
- [x] 既存ガードテスト（`test_sync_mail.py` 等）にPSTアカウント拒否のケースを追加する

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
| `-C cp932` + `-8` の日本語再現性 | 良好。`-e -t e -8 -j 0 -q -C cp932` で日本語フォルダ名（`削除済みアイテム` / `受信トレイ` / `送信済みアイテム` / `千總` 等）がUTF-8で正しく生成され、文字化け・添付欠損なし |
| Windows上の日本語フォルダ名変換時の`Illegal byte sequence`と対処 | readpst.exe既定マニフェストに`activeCodePage`指定が無くANSIコードページ（CP932）想定のためEILSEQで失敗。`mt.exe`で`vendor/readpst/readpst.exe.manifest`（`activeCodePage=UTF-8`）をreadpst.exeのリソースへ適用し解消（実機Windows 11で確認。D-19） |
| Windows禁止文字・予約名・末尾ドット/空白・衝突時の挙動 | 4.2節に詳細。要点は **`* ? " < > \|` と制御文字を含むフォルダ名で readpst が変換全体を異常終了する**こと、`/ \ :` のみ `_` へ置換されること、予約名（`CON`/`PRN`/`NUL`/`AUX`/`COM1`/`LPT1`）はWindows 11ではディレクトリとして作成できること、末尾ドット/空白はOSが除去し衝突すると連番が付くこと |
| MAX_PATH超過時の回避可否 | **回避可**。`-o` 238文字 → 絶対パス310文字の出力を生成して成功（`tools/pst_poc/run_longpath_probe.ps1`）。ただし成立条件は `readpst.exe` の `longPathAware=true`（D-19）**かつ** OSの `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled=1` の両方。後者はユーザー環境依存のため、**短い staging パス（`tmp/pstimp/{uuid8}/`）による回避は必須**。実PSTの最大相対パス長は71文字（`{store名}/連絡先/{GUID}`）で、短い staging 前提なら十分な余裕がある |
| 破損PST時の終了コード・stderr | **全10ケースで終了コード1・生成ファイル0件・stderrは空**。メッセージは**stdoutへ出力**される。2種類のみ: シグネチャ／`wVer`／非PSTは `Error opening File`、ヘッダは有効だが構造が壊れている場合（切り詰め・`wMagicClient`破壊・BREF 0埋め・Unicode を ANSI と偽装）は `Could not get root record`。→ `readpst_runner` は **stderr だけでなく stdout も捕捉**し `ConverterFailed` に載せること（F-1/C-4の修正が必要） |
| `lspst`出力の安定性 | **機械可読APIとして信頼できない**（D-9を裏付け）。実PST（Unicode PST・7,715通）に対し `A second message_store has been found. Sorry, this must be an error.` で**途中終了し終了コード1**。列挙できたのは6フォルダ・1,847通のみで、実際の階層（11フォルダ）・件数と乖離。出力はUTF-8/CRLF、`Folder "名前"` / `Email\tFrom: x\tSubject: y` / `Contact` / `Appointment` のTAB区切りだが、**階層情報を一切含まず**（インデントなし）、Subject内の改行がそのまま継続行になるため行単位パースも安全でない。破損PSTに対する挙動は readpst と完全に同一（終了コード1・stdoutへ2種のメッセージ）。→ **方針: `lspst` の終了コードは無視し、パースできた `Folder` 行のみをフラットな参考値として採用、件数は `None`。PST種別判定はマジックバイト（D-10）に一本化** |
| 変換速度（分/GB目安） | 10.3分/GB（初回計測）。今回の再計測では3.32GB・7,715通の抽出が90秒未満で完了（キャッシュ温状態）。進捗UIの指標は引き続き出力ファイル数を使う |
| 致命的問題の有無・方式継続の可否 | 致命的問題は無し。方式の継続は可能。 |

### **4.1 追加で判明した実装上の注意（グループA実測）**

| # | 実測事実 | 実装への反映先 |
| :--- | :---- | :---- |
| P-1 | readpst / lspst は致命的エラーを **stdout** に出力し stderr は空 | C-4: `ConverterFailed` へ stdout 末尾も含める。F-1の「stderr末尾を保持」を「stdout/stderr両方の末尾を保持」に読み替える |
| P-2 | 出力ツリーの最上位に **PSTのメッセージストア表示名のディレクトリ**（今回は `setsubi-mghk03-dkk@dkg.co.jp`）が作られる。これはPST内部の文字列でありサニタイズ対象 | D-3: staging走査は staging ルート直下の1段目も untrusted 名として扱う |
| P-3 | `-t e` 指定でも `予定表` / `連絡先` / `送信トレイ` など**メールを含まない空ディレクトリが作られる**。`連絡先/{GUID}/` のようなGUID名サブディレクトリも生成される | D-3: ファイルを1件も含まないディレクトリは `folders` へ登録しない |
| P-4 | EMLファイル名は各フォルダ内で **1始まりの連番 `{N}.eml`**。フォルダ間で重複する | N-4のとおり連番に依存しない。`source_item_key` は staging 相対パス基準にする |
| P-5 | 同一の葉フォルダ名が異なる親配下に併存する（`京都DKBS` / `千總`） | F-17の `folders.raw_name` = staging相対パス を厳守 |
| P-6 | `lspst` は有効なPSTでも終了コード1で途中終了しうる | C-5: 終了コードを成否判定に使わない。取得できた行だけを参考値に採用 |

### **4.2 敵性フォルダ名の実測（libpst 0.6.76 のソース準拠）**

libpst 0.6.76 の `src/readpst.c` において、`-e`（MODE_SEPARATE）経路のディレクトリ名生成は次の2関数だけで決まる。

```c
void check_filename(char *fname) {
    while ((t = strpbrk(t, "/\\:"))) *t = '_';   // 置換対象は / \ : の3文字のみ
}

void mk_separate_dir(char *dir) {
    do {
        snprintf(dir_name, dirsize, (y == 0) ? "%s" : "%s%i", dir, y);
        check_filename(dir_name);
        if (D_MKDIR(dir_name)) {
            if (errno != EEXIST) DIE(...);   // EEXIST 以外は変換全体が異常終了
        } else break;
        y++;
    } while (overwrite == 0);
    if (chdir(dir_name)) DIE(...);
}
```

この2関数をWindows上へ移植した `tools/pst_poc/simulate_readpst_dirnames.py` による実測結果。

| 入力フォルダ名 | サニタイズ後 | 結果 | 最終ディレクトリ名 | staging外へ脱出 |
| :---- | :---- | :---- | :---- | :---- |
| `a:b` / `a\b` / `a/b` | `a_b` | ok | `a_b` / `a_b1` / `a_b2` | しない |
| `a*b` `a?b` `a"b` `a<b` `a>b` `a\|b` | 変化なし | **EINVAL → `DIE()`** | 作られない | — |
| タブ等の制御文字 | 変化なし | **EINVAL → `DIE()`** | 作られない | — |
| `CON` `PRN` `NUL` `AUX` `COM1` `LPT1` `CON.txt` | 変化なし | ok | そのまま | しない |
| `trail.` | 変化なし | ok | `trail`（OSが末尾ドットを除去） | しない |
| `trail ` | 変化なし | ok | `trail 1`（末尾空白除去で `trail` と衝突し連番付与） | しない |
| ` lead` | 変化なし | ok | ` lead` | しない |
| `.` / `..` | 変化なし | ok | `.1` / `..1`（EEXISTループで退避） | しない |
| `..\..\evil` | `.._.._evil` | ok | `.._.._evil` | しない |
| `C:\Windows\Temp\evil` | `C__Windows_Temp_evil` | ok | 同左 | しない |
| `C:evil` | `C_evil` | ok | `C_evil` | しない |
| `\\server\share\evil` | `__server_share_evil` | ok | 同左 | しない |
| `note.txt:hidden`（ADS） | `note.txt_hidden` | ok | 同左 | しない |
| 250文字の名前 | 変化なし | ok | そのまま | しない |
| NFC `がtest` / NFD `かﾞtest` | 変化なし | ok | **別々のディレクトリとして共存**（Windowsは正規化しない） | しない |
| 同名の兄弟フォルダ ×2 | 変化なし | ok | `SameName` / `SameName1` | しない |

**結論と実装への反映**

| # | 実測事実 | 実装への反映先 |
| :--- | :---- | :---- |
| P-7 | **`* ? " < > \|` と制御文字を含むフォルダ名が1つでもあると readpst が `DIE()` し、変換全体が終了コード1で失敗する**（部分的なstagingだけが残る） | Stage A失敗時はstaging破棄＋再抽出（F-3/D-2）で機能的には安全側に倒れるが、**そのPSTは永久に取り込めない**。stdout末尾の `mk_separate_dir: Cannot create directory ...` を検出し、ユーザーへ「PST内のフォルダ名がWindowsで作成できない」旨を提示する専用メッセージを用意する（C-4・GUIサマリ） |
| P-8 | パストラバーサル・UNC・ドライブ指定・ADSは `check_filename()` によって無害化され、**staging外へ出るケースは観測されなかった** | D-3の `Path.resolve()` によるstaging外判定は多層防御として維持する（readpstの将来変更・別コンバーター対応のため）。D-18の判断は妥当 |
| P-9 | 予約名（`CON` 等）はWindows 11でディレクトリとして作成可能。ただし `folders.raw_name` としてDBへ入る | 表示・エクスポート時のパス組み立てでは予約名を再サニタイズする（4.6-4のファイル名サニタイズを流用） |
| P-10 | `a:b` / `a\b` / `a/b` はいずれも `a_b` へ写像され、連番で区別される。**元のPSTフォルダ名は復元不能** | F-17の `original_name_unresolved=true` はこのケースで実際に発生する。連番サフィックスの有無だけでは元名を判定できないため推測しない |
| P-11 | NFC と NFD は別ディレクトリとして共存する | フォルダツリー表示で見た目が同一の兄弟が並びうる。`raw_name`（staging相対パス）で一意性を担保し、正規化して突合しない |
| P-12 | 末尾ドット/空白はOSが除去し、既存名と衝突すると readpst が連番を付ける | 同上。`raw_name` は**実際にディスク上に存在する名前**を記録する（要求した名前ではない） |

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
