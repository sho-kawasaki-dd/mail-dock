# Phase 5 レビュー修正案

<!-- markdownlint-disable MD024 -->

対象: [docs/実装計画書_Phase5_マルチプロトコル対応.md](docs/実装計画書_Phase5_マルチプロトコル対応.md)

作成日: 2026-09-13

## 1. 目的

Phase 5（マルチプロトコル対応: 汎用IMAP / Gmail OAuth2 / Microsoft 365）の実装計画書に対するレビュー指摘を、実装着手前に確定すべき設計判断・仕様補足として整理する。
本書は元の実装計画書を置き換えず、プロトコル仕様の落とし穴回避、セキュリティ堅牢化、および既存コードベースとの整合性を確保するための補足文書である。

本書で優先する判断基準は次のとおり。

1. **設計不変条件の厳守**: 真実の情報源はEML＋永続マニフェスト、書き込み順序の厳守、削除は常に多段防御を崩さない。
2. **秘密情報の完全防護**: パスワード・`client_secret`・トークン類は `keyring` またはメモリにのみ保持し、DB・ファイル・ログ・マニフェストへ一切永続化しない。
3. **外部依存の抑制**: OAuth2フローを含め、サードパーティ依存を追加せず標準ライブラリのみで実装する。
4. **プロトコル仕様・標準への厳密な準拠**: IMAP RFC（RFC 3501, 7628）、OAuth 2.0 PKCE（RFC 7636）、OAuth 2.0 for Native Apps（RFC 8252）に忠実に準拠し、ハングや認証不能を防ぐ。
5. **安全なマイグレーション**: スキーマ変更時もマニフェスト先行記録を維持し、SQLiteのテーブル再構築時にも参照整合性・FTS5トリガーを損なわない。

検証の結果、指摘1・4・6・7・9・10は基本方針を維持し、指摘2・3・5は現行実装と公式仕様に合わせて表現を補正する。指摘8は単なる不変性の注記では不十分であるため、通常IMAPのメッセージ同一性、重複統合、マニフェスト復元を含むMajor設計事項として扱う。

---

## 2. 結論・指摘事項一覧

| # | 分類 | 論点 | 推奨する決定 |
| :--- | :--- | :--- | :--- |
| **1** | Major | Gmail XOAUTH2 認証失敗時の挙動 | サーバーからの継続チャレンジ（`+`）に対し空行（`b""`）を返して正常に認証エラー（`NO`）を完結させる |
| **2** | Major | Microsoft Entra ID と Google のリダイレクトURI差 | GoogleはIPリテラル、Microsoftは登録済み`localhost` URIを用い、認可要求とコード交換で同一URIを使う規則をプロバイダ定義へ集約する |
| **3** | Major | コールバック待機時の UI 応答性 | 既存の汎用`Worker` / `ProgressDialog`を再利用可能とし、GUIスレッド外で待機して`CancelToken`からループバック待受を即時停止する |
| **4** | Major | マニフェスト・reindex の新カラム追従 | [src/mail_dock/usecases/snapshots.py](src/mail_dock/usecases/snapshots.py) の既定値 `"onamae_imap"` 修正と新カラム追加、および [src/mail_dock/usecases/reindex.py](src/mail_dock/usecases/reindex.py) の復元タスクを明記する |
| **5** | Major | 5.2b における SQLite テーブル再構築 | 実スキーマの子参照と依存オブジェクトを棚卸しし、SQLite公式手順、FTS5整合性検証、FK検査を確実に実施する |
| **6** | Minor | Gmail IMAP の `CONDSTORE` 非対応 | Gmail では `CONDSTORE` が使えないため、Phase 3.7 のフォールバック経路（全件/範囲フェッチ）を通ることを明記する |
| **7** | Minor | `X-GM-LABELS` の文字コード形式 | 日本語ラベルの modified UTF-7 を `decode_modified_utf7` でデコードし、UTF-8 文字列配列として永続化する |
| **8** | Major | 通常 IMAP のメッセージ同一性と移動統合 | `messages`をcanonicalな論理メッセージ、`message_folders`をリモート所属として分離し、初回キーを不変にした上で、確実な根拠がある場合だけ移動先を統合する |
| **9** | Minor | アカウント削除時の Keyring クリーンアップ | 管理対象シークレット名を列挙し、アカウント削除・連携解除時に既知の全キーを冪等に削除する |
| **10** | Minor | テストファイルのリネーム漏れ防止 | `OnamaeImapFetcher` リネームに伴い [tests/unit/test_onamae_imap.py](tests/unit/test_onamae_imap.py) も [tests/unit/test_generic_imap.py](tests/unit/test_generic_imap.py) へリネームする |

---

## 3. 指摘事項の詳細

### 3.1 Gmail XOAUTH2 認証失敗時の継続チャレンジ（空行応答）ハンドリング

- **対象**: D-18, F-18, Group J
- **背景**:
  Google の XOAUTH2 仕様（RFC 7628 / Google OAuth 2.0 IMAP 仕様）では、アクセストークンの失効・無効化によって認証が失敗した場合、サーバーは通常の `NO [AUTHENTICATIONFAILED]` ではなく、**`+ {base64(JSON error)}` という継続チャレンジ（continuation challenge）** を返します。
  クライアントはこれに対して **空行（`\r\n` / `b""`）** を送り返す必要があり、空行を送って初めてサーバーが最終的な `NO` 応答を返して SASL 認証シーケンスが完結します。
- **リスク**:
  `imaplib.IMAP4.authenticate("XOAUTH2", callback)`を使用する際、コールバックがSASL初期応答を返したあと、サーバーから追加の継続チャレンジが届いた場合に空行（`b""`）を返さないと、IMAPセッションがタイムアウトまでハングするか、予期せぬ例外でソケットが切断されます。
- **推奨する修正**:
  `GenericImapFetcher` の XOAUTH2 認証コールバックを状態付きにし、最初の呼び出しではSASL初期応答を返し、それ以降にサーバーからエラーチャレンジを受けた場合は空行（`b""`）を返す。`None`は認証中断を意味するため使用しない。これによりサーバーから最終的な`NO`を受け取り、`wrap_imap_errors`が`AuthenticationError`へ変換できるようにする。正常系、エラーチャレンジ後の`NO`、コールバック複数回呼び出しを単体テストで固定する。

---

### 3.2 Microsoft Entra ID と Google のループバックリダイレクト URI 仕様差

- **対象**: D-22, F-38, Group H, Group N
- **背景**:
  D-22ではリダイレクトURIを`http://127.0.0.1:{一時ポート}/`に固定しています。GoogleのデスクトップアプリはループバックIPリテラルとエフェメラルポートを利用できます。一方、Microsoft Entra IDのデスクトップアプリでは、動的ポートを使う場合にポータルへ登録した`http://localhost`系URIを基準に扱う必要があり、Googleと同じURI文字列を一律に使用できません。
- **リスク**:
  プロバイダごとの登録条件と異なるURIを送ると認可要求またはコード交換が拒否されます。また、認可要求とトークン交換で異なる`redirect_uri`を使うと認証が成立しません。
- **推奨する修正**:
  - 許可リスト付きプロバイダ定義に`loopback_redirect_host`と登録規則を持たせ、Googleは`127.0.0.1`、Microsoftは登録済みの`localhost` URIを選択する。
  - OSが割り当てた実ポートからURIを1回だけ生成し、認可要求とコード交換へ同一文字列を渡す。任意のホストやユーザー入力URLは受け付けない。
  - 待受ソケットはループバックインターフェースだけへbindし、Microsoft経路では`localhost`のIPv4/IPv6名前解決差を結合テストで確認する。
  - 「両方を許容」のような曖昧なフォールバックは設けず、プロバイダ登録と一致しない場合は設定エラーとして扱う。

---

### 3.3 ループバックコールバック待機時の UI ブロック防止と `CancelToken` 連携

- **対象**: D-23, Group H, Group K
- **背景**:
  `http.server.HTTPServer` によるコールバック待機（タイムアウト120秒）はブロッキング I/O です。これをメインスレッドで直接実行すると PySide6 のイベントループが最長 2 分間完全に停止し、OS から「応答なし」と判定されます。
  また、ユーザーがブラウザで認可を中断してアプリ側で「キャンセル」ボタンを押した場合の脱出経路が必要です。
- **リスク**:
  認可中の UI フリーズ、およびユーザーによる中断操作が効かなくなることによる UX 低下。
- **推奨する修正**:
  1. OAuth認可ユースケース全体をGUIスレッド外で実行する。現行の汎用`Worker` / `QThread`パターンを第一候補とし、OAuth固有のシグナルや状態が必要な場合だけ`OAuthAuthorizeWorker`を新設する。
  2. 認可待機ダイアログ（プログレス表示、「ブラウザで認証を完了してください」「キャンセル」ボタン）を表示する。
  3. `CancelToken`のキャンセルをループバック待受の停止へ接続し、`shutdown()` / `server_close()`相当でブロッキング待機を解除してワーカー終了を待つ。タイムアウト時も同じクリーンアップ経路を通す。
  4. 成功、ユーザーキャンセル、120秒タイムアウト、ブラウザを閉じたままのキャンセルについて、GUIイベントループが応答し続け、待受ソケットとスレッドが残らないことをテストする。

---

### 3.4 マニフェスト復元・スナップショット記録の新カラム・既定値追従

- **対象**: Group B, Group I, セクション 4（主要成果物）
- **背景**:
  現行の [src/mail_dock/usecases/snapshots.py](src/mail_dock/usecases/snapshots.py#L40-L55) には、`"provider_type": str(account.get("provider_type", "onamae_imap"))` のハードコードが残っており、`_ACCOUNT_FIELDS` に新カラム（`tls_mode`, `ca_cert_path`, `auth_type`, `oauth_*`）が含まれていません。
  また、[src/mail_dock/usecases/reindex.py](src/mail_dock/usecases/reindex.py#L120-L140) の `_account_record()` もマニフェストから新カラムを読み取って DB レコードへ復元する処理が未定義です。
- **リスク**:
  DB 再構築（`reindex`）時に新アカウント属性（TLS設定やOAuth連携設定）が失われ、再接続不能になります。
- **推奨する修正**:
  - Group B / Group Iに「[src/mail_dock/usecases/snapshots.py](src/mail_dock/usecases/snapshots.py)の`_ACCOUNT_FIELDS` / `_account_event()`へ`tls_mode`、`ca_cert_path`、`auth_type`、`oauth_provider`、`oauth_client_id`、`oauth_tenant`を追加し、既定値の`"onamae_imap"`を`"imap"`に修正する」を追加する。`client_secret`、アクセストークン、リフレッシュトークン、任意のOAuthエンドポイントURLは含めない。
  - Group B / Group Iに「[src/mail_dock/usecases/reindex.py](src/mail_dock/usecases/reindex.py)の`_account_record()`で同じ非秘密カラムを正しく復元し、旧snapshotに対しては確定済みの後方互換既定値を適用する」を追加する。
  - セクション 4 の主要成果物テーブルに [src/mail_dock/usecases/snapshots.py](src/mail_dock/usecases/snapshots.py) と [src/mail_dock/usecases/reindex.py](src/mail_dock/usecases/reindex.py) を追加する。

---

### 3.5 5.2b における SQLite `messages` テーブル再構築時の外部キー・FTS5・トリガー保全手順

- **対象**: F-27, Group M
- **背景**:
  5.2bのfinalizerで`messages`テーブルから`folder_id`や`uid`等の列を削除して再構築する。現行スキーマで`messages(id)`を外部キー参照する子テーブルは`message_contents.message_id`と`pst_import_items.message_row_id`である。`audit_log.message_id`は監査表示用の`TEXT`であり、外部キーではない。また、FTS5同期トリガー`mc_ai` / `mc_ad` / `mc_au`は`messages`ではなく`message_contents`に属する。
- **リスク**:
  実スキーマを誤認した再構築や、`PRAGMA foreign_keys=OFF`をトランザクション開始後に実行する手順では、子参照、インデックス、FTS5索引の整合性を保証できない。重複行統合を同時に行う場合は、子参照の付け替え漏れやFTS5の孤児行も発生し得る。
- **推奨する修正**:
  - finalizer開始前に`sqlite_schema`から`messages`と`message_contents`に依存するインデックス・トリガー・ビューを棚卸しし、期待する定義をテストで固定する。
  - SQLite公式の一般化された再構築手順に従い、`PRAGMA foreign_keys=OFF`を`BEGIN IMMEDIATE`より前に設定してから、新テーブル作成、データ移行、旧テーブル削除、リネーム、必要なインデックス・トリガー・ビューの再作成を1トランザクションで行う。
  - 子テーブルは外部キー定義自体が変わらない限り不要に再構築せず、重複統合で行IDが変わる場合だけ`message_contents`と`pst_import_items`の参照をcanonicalな`messages.id`へ付け替える。`audit_log`はFK付け替え対象に含めない。
  - FTS5トリガーの存在と定義を確認し、重複統合または`message_contents`変更後にFTS5のrebuild/整合性検査を行う。
  - COMMIT前に`PRAGMA foreign_key_check`と`PRAGMA integrity_check`を実行し、失敗時はロールバックする。検査成功後にCOMMITし、最後に`PRAGMA foreign_keys=ON`へ戻す。

---

### 3.6 Gmail IMAP の `CONDSTORE` 非対応と Phase 3.7 フォールバック経路の明記

- **対象**: D-11, D-19, F-32
- **背景**:
  Gmail IMAP は `CONDSTORE` 拡張（RFC 4551 / 7162）をサポートしていません（`CAPABILITY` に `CONDSTORE` が含まれない）。
  そのため、Phase 3.7 で導入したフラグリフレッシュ機能は、Gmail アカウントでは自動的に「非 CONDSTORE フォールバック経路（全件/範囲フェッチ）」を通ることになります。
- **リスク**:
  `CONDSTORE` 前提の実装と誤認され、定期フラグリフレッシュのテストや挙動確認で混乱を招く。
- **推奨する修正**:
  「Gmail IMAP は `CONDSTORE` 非対応のため、定期フラグ更新は Phase 3.7 のフォールバック経路を通る」旨を D-11 または D-19 に注記する。

---

### 3.7 `X-GM-LABELS` の文字コード形式（modified UTF-7 vs UTF-8）の明記

- **対象**: D-18, F-14, Group J
- **背景**:
  Gmail IMAP の `X-GM-LABELS` に含まれる日本語ラベル（例: `&T2pLcg-`）は modified UTF-7 で返されます。またシステムラベル（`\Inbox`, `\Starred`, `\Trash` 等）はバックスラッシュを含みます。
- **リスク**:
  生文字列のまま DB やマニフェストへ記録すると、検索・一覧表示で文字化けや表示不整合が生じる。
- **推奨する修正**:
  DB および fetch マニフェストに記録する `gmail_labels`（JSON 配列）は、[src/mail_dock/infrastructure/fetchers/imap_common.py](src/mail_dock/infrastructure/fetchers/imap_common.py) の `decode_modified_utf7` を適用した **人間可読な UTF-8 文字列の配列**（例: `["\\Inbox", "重要"]`）として正規化して保存することを明記する。

---

### 3.8 通常IMAPのメッセージ同一性、MOVE統合、`source_item_key`の不変性

- **対象**: D-29, D-30, D-31, D-34, F-27〜F-29, F-32, F-35, Group M
- **背景**:
  D-29では通常IMAPの`source_item_key`を`imap:{folder_key}:{uidvalidity}:{uid}`と規定している。しかし、UIDはフォルダとUIDVALIDITY世代の中でのみ有効なリモート所在識別子であり、フォルダをまたぐ論理メッセージの安定IDではない。通常IMAPでFolder AからFolder BへMOVEすると、移動先では別UIDとして観測される。

  現行同期処理は`content_key`と`file_hash`が一致する候補を探索し、候補が1件なら移動元行を`moved`へ更新するが、移動先として取得済みの`messages`行との統合は行わない。この状態で「`message_folders`だけ更新する」と規定すると、同一EMLに対する複数の`messages`行が残り、一覧、purge、監査、reindexのcanonical行が不定になる。
- **リスク**:
  - 移動のたびに`messages.source_item_key`を書き換えると、fetch・監査・purgeイベントとEMLの永続的な対応が失われる。
  - `Message-ID`、`content_key`、ファイルハッシュのいずれかだけで統合すると、COPY、重複配送、同一内容の別メッセージをMOVEと誤認する。
  - DB内だけで重複統合すると、DB削除後のreindexで同じcanonical構造を復元できない。
- **推奨するデータモデル**:
  1. `messages`をcanonicalな論理メッセージとし、`messages.id`および初回作成時の`source_item_key`をその行の存続中は変更しない。
  2. `message_folders`をリモート所属・所在とし、`folder_id` / `uidvalidity` / `uid` / `remote_state` / `moved_to_folder_id` / `imap_flags` / `flags_seen_at` / `last_seen_at`を保持する。
  3. 派生キャッシュとして`message_identity_aliases(account_id, observed_source_item_key, message_id, evidence_kind)`を追加し、過去または移動先で観測したキーをcanonicalな`messages.id`へ解決する。`UNIQUE(account_id, observed_source_item_key)`を設定する。
  4. `UNIQUE messages(account_id, source_item_key)`と、UIDがNULLでない場合の`UNIQUE message_folders(folder_id, uidvalidity, uid)`を維持する。
- **自動統合の判定**:
  - Gmailは同一の`X-GM-MSGID`を強い同一性根拠として統合する。
  - アプリ自身が実行したMOVEは、サーバーが返した`COPYUID`等から新旧UIDの対応を確定できた場合に統合する。
  - 外部クライアントによるMOVE推定は、移動元UIDの消失、移動先UIDの新規観測、両フォルダの同一同期サイクルでの完全走査成功、`file_hash`完全一致、メタデータに矛盾がないこと、および候補が1対1であることをすべて満たす場合だけ自動統合する。
  - 移動元が残っている場合はCOPYまたは重複配送の可能性があるため、確定したUID対応がない限り統合しない。候補が複数、走査失敗、ハッシュ不一致などの曖昧な場合も別の`messages`行として保持する。データ保全を優先し、誤統合より重複表示を許容する。
- **統合と永続化の手順**:
  1. 移動先EMLを通常の書き込み順序で保存し、移動先のfetchイベントを追記・fsyncする。
  2. `message_identity_linked`イベントへ`canonical_source_item_key`、`alias_source_item_key`、`evidence_kind`、`file_hash`を記録し、完全な`message_membership_snapshot`とともに追記・fsyncする。
  3. `BEGIN IMMEDIATE`後、移動先membershipをcanonical行へ付け替え、aliasを登録する。移動先の重複行が存在する場合は、`message_contents`、`pst_import_items`等の子参照をcanonical行へ付け替えてから重複行を削除する。
  4. canonical行は原則として最古のfetchイベントを持つ行とする。`local_state`は1件でもactiveならactiveを優先し、EMLハッシュ不一致は自動統合せずエラーとして扱う。
- **reindexと冪等性**:
  - マニフェスト上ではDB固有の整数`messages.id`を使用せず、canonical keyとalias keyで統合関係を表現する。
  - reindexはfetchイベントを読み、`message_identity_linked`からaliasをcanonical keyへ正規化した後、同一canonical keyのfetchを1つの`messages`行へ畳み込み、最後の完全なmembership snapshotから`message_folders`とalias表を復元する。
  - 同一linkイベントの再適用は結果を変えないものとし、alias循環、1つのaliasに対する複数canonical指定、ハッシュ不一致は復元エラーとして拒否する。
- **完了条件として追加するテスト**:
  - `COPYUID`で対応が確定したMOVEが1つのcanonical行と移動先membershipへ統合される。
  - 外部MOVE推定は全条件を満たす1対1候補だけを統合し、COPY、候補複数、片側走査失敗、ハッシュ不一致を統合しない。
  - マニフェストfsync後・DBコミット前の中断から再実行でき、同じlinkイベントを重複適用しても結果が変わらない。
  - `metadata.db`削除後、EML、fetch、identity link、最後のmembership snapshotから同じcanonical構造を復元できる。

---

### 3.9 アカウント削除時における Keyring シークレットの一括クリーンアップ

- **対象**: D-21, F-17, Group I
- **背景**:
  現行の `delete_password(account_id)` はパスワードのみを削除します。OAuth アカウントを削除または再連携解除する際、`client_secret` や `refresh_token` が Keyring 内に孤児として残存する可能性があります。
- **リスク**:
  不要な秘密情報の残存、再登録時の意図しないトークン再利用。
- **推奨する修正**:
  Keyringバックエンドにはアカウント単位の安全な列挙を期待せず、アプリが管理するシークレット名（現行パスワード、`client_secret`、`refresh_token`、将来追加時に更新する列挙定数）をコード上で一元管理する。アカウント削除・OAuth連携解除ユースケースは、その既知キーすべてに`delete_secret`を実行する。存在しないキーの削除を成功扱いにした冪等な`delete_all_secrets(account_id)`を`BaseCredentialStore`へ用意してもよいが、その契約はバックエンド全体の列挙ではなく管理対象名の全削除と定義する。途中失敗はログへ秘密値を含めず通知し、DB上のアカウント削除を成功扱いにするか再試行可能状態にするかをユースケースで明示する。

---

### 3.10 テストファイルのリネーム漏れの防止

- **対象**: Group A, Group E
- **背景**:
  [tests/unit/test_onamae_imap.py](tests/unit/test_onamae_imap.py) はクラス名変更に伴い [tests/unit/test_generic_imap.py](tests/unit/test_generic_imap.py) へリネームする必要があります。
- **リスク**:
  旧ファイル名が残り、テストカバレッジや保守性の見通しが悪化する。
- **推奨する修正**:
  Group E または Group A のチェックリストに「[tests/unit/test_onamae_imap.py](tests/unit/test_onamae_imap.py) を [tests/unit/test_generic_imap.py](tests/unit/test_generic_imap.py) へリネームする」と明記する。

---

## 4. 実装計画書（Phase 5）への反映推奨チェックリスト

- [ ] **D-11 / D-19**: Gmail IMAP の `CONDSTORE` 非対応と Phase 3.7 フォールバック経路の適用を注記
- [ ] **D-18 / F-14 / Group J**: `X-GM-LABELS` の modified UTF-7 デコード（UTF-8 文字列配列での永続化）を明記
- [ ] **D-21 / F-17 / Group I**: 管理対象シークレット名を一元化し、アカウント削除・連携解除時に既知の全キーを冪等に削除する手順を追記
- [ ] **D-22 / F-38**: GoogleとMicrosoft Entra IDの登録規則に合うリダイレクトURIをプロバイダ定義から生成し、認可要求とコード交換で同一URIを使う仕様を追記
- [ ] **D-23 / Group K**: 既存の汎用`Worker`を第一候補としてOAuth待機をGUIスレッド外へ移し、待機ダイアログと`CancelToken`によるループバック待受停止を追記
- [ ] **D-29〜D-31 / D-34 / F-27〜F-29 / F-32 / F-35 / Group M**: 通常IMAPのcanonical message、`message_identity_aliases`、保守的なMOVE統合条件、`message_identity_linked`、完全membership snapshot、重複行統合、reindex、冪等性テストを追記
- [ ] **F-18 / Group J**: XOAUTH2のSASL初期応答後に届く追加の継続チャレンジ（`+`）に対する空行（`b""`）送信ハンドリングを追記
- [ ] **F-27 / Group M**: 実スキーマの子参照・依存オブジェクトを棚卸しし、SQLite公式再構築手順、canonical行への参照付け替え、FTS5整合性検査、FK・DB整合性検査を明記
- [ ] **Group A / Group E**: [tests/unit/test_onamae_imap.py](tests/unit/test_onamae_imap.py) のリネームタスクを追記
- [ ] **Group B / Group I / セクション 4**: [src/mail_dock/usecases/snapshots.py](src/mail_dock/usecases/snapshots.py) の新フィールド対応・既定値修正、および [src/mail_dock/usecases/reindex.py](src/mail_dock/usecases/reindex.py) の復元処理を主要成果物とタスクに追記
