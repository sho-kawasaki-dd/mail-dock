# Phase 4 レビュー修正案

<!-- markdownlint-disable MD024 -->

対象: [実装計画書_Phase4_統合と例外処理.md](./実装計画書_Phase4_統合と例外処理.md)

作成日: 2026-08-19

## 1. 目的

Phase 4 の実装計画書に対するレビュー指摘を、実装前に確定すべき設計判断として整理する。
本書は元の実装計画書を置き換えず、修正方針・採用理由・受入条件を定義する補足文書である。

本書で優先する判断基準は次のとおり。

1. EMLと永続マニフェストだけから、DBを保守的に再構築できること
2. 電源断・USB切断・通信断の後に、破壊済みの状態を誤って未破壊として扱わないこと
3. 不明な状態を隠さず、再照合またはユーザー判断へ送ること
4. 既存のレイヤー境界、単一ライター規約、keyringによる資格情報管理を維持すること
5. 既存実装済みの機能は書き直さず、契約整理と回帰テストで固定すること

## 2. 結論一覧

| # | 論点 | 推奨する決定 |
| :--- | :--- | :--- |
| 1 | checkpointの順序 | DBコミット成功後にcheckpointを追記する「コミット完了マーカー」とする |
| 2 | DB完全再構築 | 自然キー・スナップショットをマニフェストへ記録し、DB固有IDを正本にしない |
| 3 | purge途中停止 | `purge_intent`を起点に、起動時に冪等な回復処理を行う |
| 4 | リモート削除途中停止 | intent / completed / uncertainを分け、不確定状態を再照合する |
| 5 | 検証用ポート | `BaseEmlStorage`を肥大化させず、検証・purge・再構築用ポートを分離する |
| 6 | orphan処理 | manifestで出所を確定できる孤児だけ再登録し、未確定ファイルは隔離する |
| 7 | ストレージ状態機械 | probe結果、再probe結果、再接続時のidentity確認を別イベントとして定義する |
| 8 | 用語とスコープ | `trash / expunge`へ統一し、汎用CSVと削除ドライランCSVを明確に分ける |

実装順序は、次の順を推奨する。

### 推奨する実装順序

checkpoint設計 → マニフェストスキーマ → purge / remote deleteの復旧プロトコル → ポート設計 → 状態機械 → UI

## 3. 修正案

### 3.1 checkpointの書き込み順序

#### 推奨する決定

checkpointを「このマニフェスト位置まで対応するDB変更がコミット済みである」ことを表す完了マーカーとして定義する。

処理順序は次のとおりとする。

1. EMLを`tmp/`へ書き込み、`flush()`と`fsync()`を行う
2. `os.replace()`で最終配置する
3. fetchイベントをマニフェストへ追記し、`flush_and_sync()`を行う
4. `BEGIN IMMEDIATE`でDBを更新し、コミットする
5. DBコミット成功後、checkpointをマニフェストへ追記し、`flush_and_sync()`を行う

checkpointには、少なくとも次の情報を持たせる。

- `event: "checkpoint"`
- `account_id`
- `timestamp`
- 単調増加する`sequence`
- 対象バッチを識別する`batch_id`

可能なら、checkpoint対象を明確にするため、マニフェストファイル名とコミット済みオフセットも保持する。
ただし、オフセットを仕様上の唯一の識別子にはせず、ファイルローテーション後も追跡できる`batch_id`を併用する。

#### この案を選ぶ理由

DBコミット前にcheckpointを書き込むと、DBコミットに失敗した項目まで「検証済み範囲」に含まれる。
その場合、最後のcheckpoint以降だけを検証する範囲限定検証が、DB未登録のEMLを見逃す可能性がある。

DBコミット後にcheckpointを書けば、checkpoint書き込み前に切断されても、次回起動時に余分な範囲を再検証するだけで済む。
不確実な状態を安全側へ倒せるため、この順序を採用する。

#### 計画書への反映

- A-3の「checkpoint追記はDBコミットの前」を「DBコミット成功後」へ変更する
- checkpoint欠落時は、直前のバッチを範囲限定検証の対象に含める
- V-2へ、DBコミット失敗時にcheckpointが存在しないことの検証を追加する
- checkpointの単調性、重複、ファイルローテーション時の扱いをA-1へ追加する

#### 受入条件

- DBコミット失敗後にcheckpointが残らない
- checkpointが無いバッチは再検証対象になる
- EML、manifest、DBの各フォールト注入で「DBにあるがEMLがない」状態が発生しない
- 既存の10バッチごとのWAL checkpointは回帰テストで維持される

### 3.2 DB完全再構築に必要なマニフェスト情報

#### 推奨する決定

マニフェストをDBの行コピーではなく、DBを再生成できる業務イベントの正本とする。
次の情報をイベントへ追加する。

#### 追加するイベント・フィールド

`account_snapshot`イベント:

- `account_id`
- `provider_type`
- `display_name`
- 接続先の非秘密情報
- `timestamp`

資格情報、パスワード、アクセストークンは記録しない。

`folder_snapshot`イベント:

- `account_id`
- `folder_raw_name`
- `display_name`
- `uidvalidity`
- `delimiter`または必要なフォルダ属性
- `timestamp`

`fetch`イベント:

- `internal_date`
- 再構築に必要なメッセージヘッダー情報
- `folder_raw_name`を自然キーとして維持

`moved`イベント:

- 移動元の`folder_raw_name`
- 移動先の`moved_to_folder_raw_name`
- UID、UIDVALIDITY、`source_item_key`

`delete_detected`、`remote_delete`、監査関連イベント:

- Message-ID
- 件名のマスキング前の正本値、または監査再生成に必要な値
- サイズ
- アカウントID
- 操作詳細
- timestamp

監査ログ表示時にマスキングを適用する。ログやマニフェストへ本文全体を記録しない。

#### DB固有値の扱い

次の値はマニフェストの正本にしない。

- SQLiteの`messages.id`、`folders.id`などのサロゲートID
- DB作成時刻など、再構築時に自然に変わる値
- 旧DBの`moved_to_folder_id`

再構築時は、`account_id`、`folder_raw_name`、`source_item_key`などから新しいIDを解決する。

#### V-3の比較基準

V-3の「再構築前と一致」は、DBの物理的一致ではなく、次の意味的一致として定義する。

- accountsの自然キーと非秘密属性が一致する
- foldersの自然キー、表示名、UIDVALIDITYが一致する
- messagesの自然キー、EMLパス、ハッシュ、状態、日時、サイズが一致する
- message_contentsとFTSの検索結果が一致する
- purge墓標とremote_stateが一致する
- audit_logの操作、対象、時刻、詳細が一致する
- `is_sync_target`は安全上、再構築後はすべて`0`とする

`is_sync_target`を強制的に`0`へ初期化する場合、V-3ではこの列を「意図的に再構築しない運用設定」として比較対象から除外する。

#### この案を選ぶ理由

DB固有IDを永続マニフェストへ保存すると、DB再構築時に別のID体系へ変換できず、フォルダ参照や移動履歴が壊れる。
自然キーとスナップショットを記録すれば、DBスキーマやSQLiteの採番から独立して復元できる。
また、資格情報をマニフェストへ書かないことで、keyringのみへ保管する既存方針を維持できる。

#### 受入条件

- 同期後にDBを削除しても、マニフェストとEMLから同じ業務状態を復元できる
- UIDVALIDITYが異なる世代のメッセージを混同しない
- 移動先フォルダを新DBのフォルダIDへ解決できる
- `manifests/pst/`は未対応形式として明示的にスキップできる
- 再構築途中に失敗しても既存の`metadata.db`は変更されない

### 3.3 purge途中停止からの復旧

#### 推奨する決定

purgeを、再実行しても結果が変わらない冪等な状態遷移として実装する。

処理順序は次のとおりとする。

1. `purge_intent`をマニフェストへ追記し、fsyncする
2. 同一`relative_path`の非purged参照を再確認する
3. 最後の参照である場合だけEMLを削除する
4. EMLが既に存在しない場合は、ハッシュとintentが一致する限り削除済みとして扱う
5. `purged`をマニフェストへ追記し、fsyncする
6. DBトランザクションで`message_contents`を削除する
7. `local_state='purged'`、`relative_path=NULL`へ更新する
8. `audit_log`へ記録する

`purge_intent`には、少なくとも次を記録する。

- `account_id`
- `source_item_key`
- `relative_path`
- `file_hash`
- `timestamp`
- 共有参照確認の結果
- 物理削除を実施するかどうか

起動時または範囲限定検証前に、`purge_intent`に対応する`purged`が存在しないイベントを列挙する。
未完了intentは共有参照を再確認し、上記手順の未完了部分を再開する。

#### この案を選ぶ理由

EML削除は通常のDBロールバックでは戻せない。
そのため、削除済みファイルを復活させる補償処理よりも、各段階を再実行して前へ進める設計の方が単純で安全である。

`purge_intent`だけが残る状態を「破損」として扱うと、次回起動時に既に削除されたEMLを再取得する危険がある。
intentを回復情報として使い、墓標化まで完了させる必要がある。

#### 計画書への反映

- D-1へ未完了`purge_intent`の回復手順を追加する
- H-1からpurgeのフォールト注入を独立させるか、H-5に追加する
- H-5へ「intent後」「EML削除後」「purged後」「DB更新中」の注入点を追加する
- V-4へ途中停止後の再起動・再実行で墓標化が完了することを追加する

#### 受入条件

- purgeを同じ対象へ複数回実行しても、EMLや監査ログが不正に二重処理されない
- 共有参照が残る場合は物理EMLを削除しない
- EML削除後に切断しても、次回起動で`purged`と墓標が完成する
- `messages`行は残り、`message_contents`とFTSだけが除去される

### 3.4 リモート削除途中停止と不確定状態

#### 推奨する決定

ネットワーク上の削除操作について、exactly-onceを前提にしない。
操作の意図、成功確認、不確定状態を別イベントとして記録する。

推奨する状態は次のとおり。

- `remote_delete_intent`
- `remote_delete_completed`
- `remote_delete_uncertain`

処理順序は次のとおりとする。

1. `remote_delete_intent`をマニフェストへ追記し、fsyncする
2. IMAP MOVEまたはEXPUNGEを実行する
3. サーバー応答を確認する
4. 成功確認後に`remote_delete_completed`を追記し、fsyncする
5. 応答不明・通信断の場合は`remote_delete_uncertain`を記録する
6. 再接続後に元フォルダ、UID、UIDVALIDITY、移動先を照合する
7. 状態確定後に監査ログとDBのremote_stateを更新する

永久削除は、対象UIDだけを削除できるUID EXPUNGEが利用可能な場合に限定する。
UID EXPUNGEが利用できない場合、通常のフォルダ全体EXPUNGEへフォールバックしない。

#### この案を選ぶ理由

IMAP操作とローカルマニフェストのfsyncは同一トランザクションにできない。
通信断が操作の前後どちらで発生したか判定できない場合、成功・失敗のどちらかに決め打ちすると二重操作または誤表示につながる。
不確定状態を明示し、再接続後にサーバー状態を照合する方が誤削除を防げる。

通常のEXPUNGEは、他クライアントが同じフォルダで削除予約したメールまで削除する可能性があるため、永久削除の安全条件を厳しくする。

#### 計画書への反映

- A-1のイベント一覧へintent / completed / uncertainを追加する
- E-2へTOCTOUだけでなく、通信断後の状態照合を追加する
- E-1またはfetcher契約へ「UID EXPUNGE非対応時はexpungeを拒否する」条件を追加する
- H-5へIMAP操作直後の通信断テストを追加する

#### 受入条件

- 操作前の通信断ではサーバー削除を実行済みと表示しない
- 操作後・応答前の通信断では`uncertain`として停止する
- 再接続後にサーバーを照合してからdeletedへ確定する
- UID単位の永久削除が保証できないサーバーではexpungeを実行しない

### 3.5 検証・再構築用ポート

#### 推奨する決定

既存の`BaseEmlStorage`へ検証・物理削除・全走査の責務を無制限に追加しない。
用途別の最小ポートを追加する。

`BaseIntegrityStorage`:

- `stat(relative_path)`
- `iter_chunks(relative_path)`
- `iter_eml_paths(account_id=None)`
- `quarantine(relative_path)`

`BasePurgeStorage`:

- `delete(relative_path)`
- 必要なら、削除済みを再実行可能にする存在確認

`BaseManifestReader`:

- 全イベント列挙
- 最後のcheckpoint取得
- checkpoint以降のイベント列挙
- 未完了intentの列挙

再構築は、ユースケースから直接SQLiteファイルを操作しない。
新しいDBの作成、マイグレーション、整合性検証、既存DBとの入れ替えは、infrastructure側の再構築コーディネータが担当する。

repositoryへ追加するAPIは、実際のユースケースが必要とするものに限定する。
候補は次のとおり。

- ID指定のメッセージ取得
- 保存パスを持つメッセージの列挙
- `message_contents`存在確認
- 検証結果を単一ライターへ渡すための状態更新
- 再構築用のアカウント・フォルダ・メッセージ投入

#### この案を選ぶ理由

`read()`でEML全体をbytesへ読み込むだけでは、100GB規模のフル検証でメモリ上限を保証できない。
また、検証ワーカーへ物理削除権限を渡すと、単一ライター規約や切断時の書き込み禁止と衝突する。
読み取り、破壊、DB再構築をポート単位で分離することで、依存方向と権限境界を維持できる。

#### 計画書への反映

- C-1へチャンク読み、サイズ取得、EML列挙、隔離のポート要件を追加する
- C-2へ新DB作成・検証・入れ替えをinfrastructure側で行う責務を追加する
- C-3へVerifyWorkerが物理削除を直接実行しないことを明記する
- A-5のrepository追加メソッドを、必要な読み取り・状態更新・再構築投入に分ける

#### 受入条件

- フル検証がチャンク読みで動作し、EML全体を保持しない
- VerifyWorkerは検証結果を返すだけで、単一ライター規約を破らない
- 再構築途中の失敗で既存DBが壊れない
- 隔離先がストレージルート配下で、同一ボリュームの原子操作を維持する

### 3.6 orphanの扱い

#### 推奨する決定

孤児ファイルを、マニフェストとの対応関係で二種類に分ける。

1. manifestに対応するfetchイベントがある孤児
   - イベントの`source_item_key`、パス、ハッシュを照合する
   - 一致すればDBへ再登録する
2. manifestに対応イベントがない孤児
   - UID、UIDVALIDITY、folderを推測して自動登録しない
   - `tmp/orphans/`などへ隔離する
   - 次回同期の重複候補として監査ログへ記録する

孤児の「取り込み」は、manifestで出所を確定できる場合に限ると定義する。

#### この案を選ぶ理由

EML単体からは、送信者やMessage-IDを解析できても、元のIMAPフォルダ、UID、UIDVALIDITYを一意に復元できない。
推測による登録は、正本から復元した状態ではなくなる。

#### 計画書への反映

- F-15を「すべての孤児を登録」から「出所確定済みの孤児を再登録」へ変更する
- C-1のOrphanScanResultへ、再登録対象と未確定隔離対象を分けて含める
- H-4へ、manifest対応孤児と未対応孤児の両ケースを追加する

#### 受入条件

- 対応するfetchイベントがある孤児だけが再登録される
- 対応イベントのないEMLからUIDやフォルダを推測しない
- 隔離処理はパストラバーサルを許さず、次回同期で安全に扱える
- 既存DBのメッセージとハッシュ重複する孤児は二重登録されない

### 3.7 ストレージ状態機械のイベント設計

#### 推奨する決定

通常監視、瞬断再試行、再接続時のidentity確認を別イベントとして定義する。
イベント例は次のとおり。

- `PROBE_OK`
- `PROBE_MISSING`
- `PROBE_FOREIGN`
- `IO_ERROR`
- `REPROBE_OK`
- `REPROBE_FAILED`
- `DEVICE_REMOVED`
- `DEVICE_ARRIVED`
- `USER_DETACH`
- `RECONNECT_REQUESTED`
- `IDENTITY_OK`
- `IDENTITY_FOREIGN`
- `VERIFY_OK`
- `VERIFY_FAILED`

最低限の遷移は次のように固定する。

- `ATTACHED + PROBE_MISSING -> DEGRADED`
- `ATTACHED + PROBE_FOREIGN -> DETACHED`
- `DEGRADED + REPROBE_OK -> ATTACHED`
- `DEGRADED + REPROBE_FAILED -> DETACHED`
- `DEGRADED + DEVICE_REMOVED -> DETACHED`
- `DETACHED + DEVICE_ARRIVED -> RECONNECTING`
- `RECONNECTING + IDENTITY_OK -> VERIFYING`
- `RECONNECTING + IDENTITY_FOREIGN -> DETACHED`
- `VERIFYING + VERIFY_OK -> ATTACHED`
- `VERIFYING + VERIFY_FAILED -> DETACHED`

`ATTACHED`以外では、`is_write_allowed()`と`is_remote_delete_allowed()`を常に偽とする。

#### この案を選ぶ理由

`IDENTITY_FOREIGN`を再接続中だけのイベントとして使うと、通常のハートビートで別デバイスを検出した場合の遷移が未定義になる。
probe結果を独立イベントにすることで、FOREIGN検出時に即座に書き込みを禁止できる。
また、瞬断復帰と再接続時の検証を混同しないため、誤ったATTACHED復帰を防げる。

#### 計画書への反映

- B-1へprobeイベントを追加する
- B-3へMISSINGとFOREIGNの状態遷移を追加する
- H-2へ通常probe、瞬断、デバイス抜去、再接続の各経路を追加する
- `WM_DEVICECHANGE`が通知するイベントと、状態機械が判定するイベントを分離する

#### 受入条件

- ATTACHED中にFOREIGNを検出した場合、即座に書き込みが禁止される
- MISSINGは規定回数の再probeを経てDETACHEDになる
- DEVICE_REMOVEDは再probeを待たずDETACHEDになる
- 再接続はUUID照合と範囲限定検証を完了するまでATTACHEDに戻らない
- 非Windowsでも状態機械のimportとprobe系テストが動作する

### 3.8 用語、設定値、CSVスコープ

#### 推奨する決定

ドメインおよびマニフェスト上の削除モードは、実際の操作方式を表す次の値へ統一する。

- `trash`: ゴミ箱フォルダへMOVE
- `expunge`: 対象を永久削除

既存設定に`permanent`が保存されている場合は、読み込み時に`expunge`へ移行する。
設定保存時は新しい値だけを書き出す。

CSVについては、次のようにスコープを明確化する。

- 検索結果の汎用CSVエクスポート: 実装しない
- リモート削除ドライランの監査用CSV: 実装する

#### この案を選ぶ理由

`permanent`はユーザー向けの意味、`expunge`はIMAP操作の意味であり、同じ層で混在すると設定とマニフェストの再現性が下がる。
マニフェストには実際に実行した操作方式を記録するため、`expunge`へ統一する。

CSVも「検索機能の出力」と「破壊操作前の監査証跡」は目的が異なる。
削除ドライランCSVを許可し、汎用検索CSVを対象外とすれば、既存のスコープ判断と削除安全要件を両立できる。

#### 計画書への反映

- D-11、F-31、E-2のモード表記を`trash / expunge`へ統一する
- 既存`permanent`設定の移行テストを追加する
- D-18を「検索結果の汎用CSVは実装しない」へ変更する
- F-29、E-3の削除ドライランCSVは対象内であることを明記する

#### 受入条件

- 設定、ユースケース、マニフェスト、UIで削除モードの値が一致する
- 旧設定`permanent`を失わずに読み込める
- 削除ドライランのCSVに件名、日時、サイズ、除外理由、合計が含まれる
- 本文や資格情報がCSVへ出力されない

## 4. 既存実装の計画上の扱い

次の機能は、現行コードに既に実装があるため、新規実装ではなく確認・拡張・回帰テストとして扱う。

### 4.1 WAL checkpoint

`sync_mail.py`には、バッチコミット後、10バッチごとにrepositoryのcheckpointを呼ぶ経路が既にある。
Phase 4では、次を確認する。

- checkpointがDBコミット後に実行されること
- `PRAGMA wal_checkpoint(TRUNCATE)`の失敗が適切に分類されること
- 切断中に再書き込みを試みないこと
- 10バッチごとの動作をテストで固定すること

### 4.2 ゴミ箱フォルダ探索

Onamae IMAP fetcherには、SPECIAL-USEの`\\Trash`、候補名、設定値の順に探索する経路が既にある。
Phase 4では、次を確認・拡張する。

- BaseMailFetcherの契約として、未特定時の失敗を明記する
- 設定値のモード表記を統一する
- MOVE非対応時のCOPY後処理を安全に定義する
- E-1の単体・結合テストを追加する

## 5. 推奨する実装順序

### Step 1: 正本とコミット境界

- checkpointの意味と順序を確定する
- fetch、snapshot、purge、remote deleteイベントのスキーマを確定する
- 自然キーとDB固有IDの扱いを確定する

### Step 2: 破壊操作の復旧プロトコル

- purge intentの回復を実装する
- remote deleteのuncertain状態と再照合を実装する
- フォールト注入テストを先に追加する

### Step 3: 検証・再構築ポート

- チャンク読みとEML走査のポートを追加する
- orphanの再登録条件を実装する
- 新DB作成と原子的入れ替えを実装する

### Step 4: 状態機械と切断制御

- probe系イベントと遷移表を確定する
- Monitor、DeviceWatcher、ワーカー停止、接続解放を接続する
- Windowsと非Windowsのテストを分ける

### Step 5: GUI・CLI・運用機能

- verify、reindex、purge、remote deleteのUIを配線する
- 破壊操作の件数手入力とドライラン表示を実装する
- トレイ、定期同期、監査ログ、バックアップを追加する

## 6. 受入テストの追加・変更

既存のV-2からV-4とH-1からH-5へ、次のテストを追加する。

- DBコミット前後のcheckpoint有無
- checkpoint欠落時の範囲限定検証
- snapshotイベントなしで再構築を開始した場合の明示的な失敗
- folder raw nameから新DBのfolder IDを解決できること
- purge intent後の切断から再実行できること
- EML削除後、purgedイベント前の切断から墓標化できること
- remote deleteの応答前切断をuncertainとして保持すること
- UID EXPUNGE非対応時にexpungeを拒否すること
- VerifyWorkerが物理削除やSQLite直接操作を行わないこと
- manifest対応孤児だけが再登録されること
- manifest未対応孤児が隔離されること
- ATTACHED中のPROBE_FOREIGNで書き込み禁止になること
- 旧設定`permanent`が`expunge`へ移行されること

## 7. Phase 4計画書への反映チェックリスト

- [x] A-1: checkpoint、snapshot、purge、remote deleteイベントのスキーマを更新した
- [x] A-3: checkpointの位置をDBコミット後へ変更した
- [x] C-1: 検証用ポートとチャンク読み要件を追加した
- [x] C-2: DB固有IDに依存しない再構築方式を追加した
- [x] C-2: 新DB作成と原子的入れ替えの所有者を明確にした
- [x] C-1: orphanの再登録条件と隔離条件を明記した
- [x] D-1: 未完了purge intentの回復手順を追加した
- [x] E-2: remote deleteの不確定状態と再照合を追加した
- [x] E-2: UID EXPUNGE非対応時のexpunge拒否を追加した
- [x] B-1/B-3: probeイベントとFOREIGN遷移を追加した
- [x] D-11/F-31: `trash / expunge`へ用語を統一した
- [x] D-18/F-29: 汎用CSVと削除ドライランCSVのスコープを分離した
- [x] H-1/H-5: 破壊操作の途中停止テストを追加した
- [x] V-2/V-3/V-4: 新しい復旧条件と意味的一致条件を反映した

## 8. 設計上の最終判断

Phase 4の中核は、機能数を増やすことではなく、切断や途中停止が発生しても正本から安全に前進復旧できることを証明することである。

したがって、次の4点が確定するまでGUI実装を開始しない。

1. checkpointがDBコミット完了マーカーとして定義されている
2. 再構築に必要な自然キーとスナップショットがマニフェストへ記録される
3. purgeとremote deleteの不確定状態が復旧可能である
4. VerifyWorker、単一ライター、ストレージ状態機械の責務境界が確定している

この順序を守ることで、V-2、V-3、V-4を実装後に形式的に確認するのではなく、設計段階から満たせる構造にできる。
