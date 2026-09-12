# **Phase 5: マルチプロトコル対応（汎用IMAP / Gmail OAuth2 / Microsoft 365） 実装計画書**

対象: [ローカルメールバックアップ＆閲覧アプリ 開発計画書.md](./ローカルメールバックアップand閲覧アプリ開発計画書.md) の **概要「高い拡張性（マルチプロトコル対応）」**、**4.2 IMAP同期・バックアップ機能**、**3.3 メッセージメタデータテーブル（スレッド・フラグの先行保持方針）**、**5.3 認証・セキュリティ（資格情報の保管）**、および **6章 ロードマップ Phase 5**

前提: [Phase 1: 抽象化層とIMAPコア 実装計画書](./実装計画書_Phase1_抽象化層とIMAPコア.md) の `BaseMailFetcher` / `OnamaeImapFetcher` / `imap_common.py`、[Phase 3.7: IMAPフラグの定期リフレッシュ](./実装計画書_Phase3.7_IMAPフラグの定期リフレッシュ.md) の `CONDSTORE` 対応、[Phase 4: 統合と例外処理 実装計画書](./実装計画書_Phase4_統合と例外処理.md) の切断状態機械・`ManifestWriter`/`ManifestReader`・単一ライター規約・`verify.py`/`reindex.py` が完成していること。[実装計画書_Phase4.5_PSTアーカイブ.md](./実装計画書_Phase4.5_PSTアーカイブ.md) は着手済みかどうかを問わないが、マイグレーション番号の割当（`006_pst_import.sql` まで使用済み）には影響する。

**本書は旧 [実装計画書_Phase5.1_汎用IMAPサーバー対応.md](./実装計画書_Phase5.1_汎用IMAPサーバー対応.md) を吸収し、開発計画書ロードマップの Phase 5 全体を1冊にまとめた統合計画書である。** Phase 5 を以下の4サブフェーズへ分割し、各々を独立して着手・検証可能な単位として扱う。

| サブフェーズ | 内容 | マイグレーション | 前提 |
| :---- | :---- | :---- | :---- |
| **5.1** | 汎用IMAPサーバー対応（`GenericImapFetcher`、STARTTLS、`LOGINDISABLED`→SASL PLAIN、カスタムCA証明書） | `007_generic_imap_connection.sql` | Phase 1 / 3.7 / 4 |
| **5.2a** | Gmail IMAP + XOAUTH2（ラベルは単一フォルダ相当として暫定表示） | `008_oauth_accounts.sql` | 5.1 完了 |
| **5.2b** | Gmailラベル対応（`message_folders` 中間テーブルへの移行） | `009_message_folders.sql` | 5.2a 完了 |
| **5.3** | Microsoft 365 / Outlook.com（OAuth2） | 原則スキーマ変更なし（5.2aの型を再利用） | 5.2a 完了 |

本書と開発計画書に矛盾がある場合は、**開発計画書を正**とする。設計不変条件（真実の情報源はEML＋永続マニフェスト、書き込み順序の厳守、削除は常に多段防御）は本フェーズでも変更しない。

---

## **1. 目的**

- [ ] `BaseMailFetcher` の実装をお名前.com固有のクラスから「ID/パスワード認証を使う任意のIMAPサーバー用フェッチャー」へ一般化する（5.1）
- [ ] 暗黙的TLS（993番ポート想定）に加え、STARTTLS（143番ポート想定）で接続できるようにする（5.1）
- [ ] `LOGIN` コマンドが無効化されているサーバー（`LOGINDISABLED`）でも `AUTHENTICATE PLAIN` でログインできるようにする（5.1）
- [ ] 自己署名証明書やプライベートCAを使うサーバーに対して、検証を無効化せずに接続できる手段（カスタムCA証明書ファイルの指定）を用意する（5.1）
- [ ] Gmail をID/パスワードではなく **IMAP + XOAUTH2（OAuth2）** で取得できるようにする（5.2a）
- [ ] OAuth2の認可コード＋PKCEフローを、追加のサードパーティ依存を増やさずに実装する（5.2a）
- [ ] `client_secret` / `refresh_token` / `access_token` をディスク上のいかなる永続ファイルへも書き込まず、ストレージルートへも一切保存しない（5.2a）
- [ ] Gmailの「1通が複数ラベルに属する」性質を `message_folders` 中間テーブルで正しく表現する（5.2b）
- [ ] Microsoft 365 / Outlook.com を、Gmailで確立したOAuth2基盤の再利用によって追加する（5.3）
- [ ] OAuth2（Gmail等）は本書の対象に含める一方、対話的な同意フローが必須の操作はGUI限定とし、CLIには追加しない
- [ ] 追加のサードパーティ依存パッケージを一切追加しない（`google-auth-oauthlib` は開発計画書の想定から撤回し、標準ライブラリのみで実装する）

---

## **2. 要件**

### **2.1 前提となる意思決定（要合意〜確定済み）**

| # | 項目 | 決定内容 |
| :--- | :---- | :---- |
| D-1 | サブフェーズ分割 | Phase 5 を **5.1（汎用IMAP）→ 5.2a（Gmail・単一フォルダ扱い）→ 5.2b（Gmailラベル対応）→ 5.3（MS365）** の順に実装する。5.2b は `messages.folder_id` の一意性・検索・削除検知・reindexに広く波及する大規模変更であるため、5.2a で先に「動くGmail対応」を用意してから着手する |
| D-2 | クラス構成 | 新しいプロバイダ別サブクラスを増やさず、`OnamaeImapFetcher` を `GenericImapFetcher` へリネームして汎用実装1本に統合する。プロトコル分岐（TLSモード・`LOGINDISABLED`・認証方式）はコンストラクタ引数と `connect()` 内の分岐で表現し、`BaseMailFetcher` の実装は依然として1クラスのみとする |
| D-3 | `provider_type` の扱い | DBの `accounts.provider_type` は新規登録時に `"imap"` を書き込む。既存行の `"onamae_imap"` は `007` マイグレーションで `"imap"` へ正規化する。**`usecases/register_account.py` と `infrastructure/database/message_repository.py::upsert_account` の両方にある `"onamae_imap"` の既定値をこの機会に修正する**（両者に同じ既定値がハードコードされていることを実装前に確認済み） |
| D-4 | account_snapshot の正規化 | マニフェストの `account_snapshot` イベントは書き込み時の `provider_type` をそのまま記録するため、DB側だけを `007` で正規化しても **EML＋マニフェストからの再構築（`reindex.py`）は旧い `"onamae_imap"` を復元してしまう**。`007` 適用と同時に、既存アカウントの `account_snapshot` を現在の接続設定で再記録するタスクを追加する（この抜けは旧Phase5.1計画書には無かった） |
| D-5 | 接続方式（TLSモード） | `tls_mode` を `"implicit"`（既定・現行動作＝`IMAP4_SSL`、既定ポート993）と `"starttls"`（`IMAP4` で接続後 `STARTTLS`、既定ポート143）の2値とする。ユーザーがポート番号を明示している場合はそちらを優先し、既定ポートはUIのプレースホルダとしてのみ用いる |
| D-6 | `LOGINDISABLED` への対応 | `STARTTLS` 完了後に `CAPABILITY` を再取得し、`LOGINDISABLED` が含まれる場合は `imaplib` の `authenticate("PLAIN", ...)` を使い、SASL PLAIN（`\0username\0password`）でログインする。暗黙的TLS経路でも同様に確認し、該当すればPLAIN認証にフォールバックする |
| D-7 | 証明書検証 | 既定は `ssl.create_default_context()` によるシステム信頼ストア検証を維持し、**検証を無効化するオプションは提供しない**（OWASP的に中間者攻撃を招くため）。自己署名証明書やプライベートCA向けに、ユーザーが指定した **CA証明書ファイル1点** を `load_verify_locations()` で読み込む経路のみを追加する |
| D-8 | クライアント証明書 | クライアント証明書（mTLS）は5.1〜5.3のいずれでも要件化しない。将来必要になった場合に別途検討する |
| D-9 | ゴミ箱候補名の拡張 | `_TRASH_CANDIDATES` に英語圏の慣用名（`"Bin"` は誤検出防止のため対象外とし、`"Deleted"` 等）を追加する。SPECIAL-USE `\Trash` を最優先とする既存の優先順位は変えない |
| D-10 | 階層区切り文字 | `list_folders()` が保持する `RemoteFolder.delimiter` は本フェーズでもフォルダ操作コマンドの組み立てには使わない。NAMESPACE拡張への対応は行わず、異常値（空文字列・複数文字等）の防御的な検証のみ追加する |
| D-11 | 同時接続数・レート制限 | サーバー固有の同時接続数上限やレート制限は既存の `usecases/retry.py` の `TransientError` リトライ機構に委ねる。Gmail固有のスロットル応答（D-19参照）はこの機構へ長めのバックオフ経路を追加する形で対応し、フェッチャー実装へリトライを持ち込まない |
| D-12 | 実サーバー検証の代替（5.1） | Dockerの Dovecot 設定を拡張し、STARTTLS・`LOGINDISABLED`・自己署名証明書の3シナリオを結合テストで再現する。実運用前には少なくとも1つの非Onamaeサーバー（さくら等）での手動検証を推奨する |
| D-13 | Gmail取得方式 | **IMAP + XOAUTH2** を採用する。Gmail REST API（`users.messages` / `historyId` 差分同期）は不採用とする。理由: 既存の `GenericImapFetcher` / UID増分同期 / EML保存パイプラインをほぼそのまま再利用でき、`BaseMailFetcher` の契約（UID・`RFC822` 生EML取得）と自然に整合するため。REST APIは、将来ラベル同期の精度向上等が必要になった場合の拡張余地としてのみ設計上残す（具体実装はしない） |
| D-14 | OAuthクライアントの配布方式 | mail-dock は特定の `client_id` / `client_secret` を同梱・配布しない。**ユーザー自身がGoogle Cloudプロジェクトを作成し、OAuth同意画面とデスクトップアプリ用クライアントIDを用意する**運用とする。理由: `https://mail.google.com/` は制限付きスコープであり、公開アプリとして提供するにはGoogleのCASAセキュリティ評価（有償・年次）が必須であり、OSSデスクトップアプリとして現実的ではない。README に手順（同意画面の設定、テストユーザー登録、スコープ追加、クライアントID発行）を記載する |
| D-15 | 依存関係 | **追加のサードパーティ依存パッケージを追加しない。** OAuth2（認可コード＋PKCE＋ループバックリダイレクト）は標準ライブラリ（`http.server` / `urllib` / `secrets` / `hashlib` / `base64` / `json`）のみで実装する。開発計画書 2.1 に残る「`google-auth-oauthlib`（将来対応用）」の記述はこの決定に合わせて削除する（Group Pでタスク化） |
| D-16 | ラベルと `message_folders` | Gmailの「1通が複数ラベルに属する」性質への対応を **5.2a と 5.2b に分割**する。5.2a では同期対象を既定でSPECIAL-USE `\All`（「すべてのメール」相当フォルダ）のみとし、既存の `messages.folder_id` 単一列のまま「1メッセージ=1フォルダ」の枠組みで動かす（ラベルは `gmail_labels` 列に文字列として保持するだけで検索・フィルタには使わない）。5.2b で `message_folders` 中間テーブルへ移行し、複数フォルダ所属・削除検知・検索フィルタを正式対応させる |
| D-17 | INBOXとAll Mailの二重登録（5.2a の暫定挙動） | 5.2a では `\All` 以外のフォルダも `is_sync_target` に追加できる（ユーザー選択式の既存挙動を変えない）が、INBOXと「すべてのメール」を同時に同期対象にすると同一メールが別UIDで二重に保存される。**5.2aではこれを「既知の暫定挙動」として許容し、UIに警告を表示するに留める**（5.2bの `message_folders`移行で解消する）。理由: 5.2aの目的はまず「動くGmail接続」を確立することであり、二重登録の完全排除は `message_folders` 移行と不可分であるため、5.2a単体で作り込む投資対効果が低い |
| D-18 | `X-GM-MSGID` / `X-GM-THRID` / `X-GM-LABELS` の先行取得 | **5.2aの時点で取得・保存する。** 後付けすると5万通規模で全件再FETCHが必要になり、開発計画書 3.3 が `imap_flags` / `thread_key` を「後付けだと全件再取得が必要なため初期から保持する」とした理由と同じ理由による。5.2aでは表示・将来の重複検知の補助情報としてのみ使い、一次識別子への昇格は5.2bで行う |
| D-19 | Gmail固有の応答分類 | `[THROTTLED]` / `[OVERQUOTA]` / `[LIMIT]` 等のGmail拡張レスポンスコードを `imap_common.wrap_imap_errors` で `TransientError` へ分類し、Gmail特有の帯域制限（実務上 1日あたり数GB規模で発生しうる）に対応する長めのバックオフ（分〜時間オーダー）を `usecases/retry.py` に追加する。リトライロジックはusecases層に集約する既存方針を維持し、フェッチャー実装には入れない |
| D-20 | Gmailでのサーバー削除モード | **`remote_delete_mode='expunge'` をGmailアカウントでは拒否する。** Gmail IMAPにおける `EXPUNGE` は選択中フォルダ（ラベル）に対する操作であり、ラベルを1つ外す操作なのか完全削除なのかが文脈依存になるため、開発計画書1.3の不変条件3（削除は常に多段防御）を単純な `UIDPLUS EXPUNGE` 実装のままでは満たせない。Gmailアカウントは `trash`（`[Gmail]/ゴミ箱` へのMOVE）のみを許可し、`delete_remote.py` の入口でアカウントの `oauth_provider` を見て拒否する |
| D-21 | 秘密情報の保存先 | `client_secret`・`refresh_token` は **`keyring` にのみ**保存する。`access_token` はプロセス内メモリにのみ保持し、いかなる永続先（DB・`config.json`・マニフェスト・ログ）にも書かない。`BaseCredentialStore` に **名前空間付きの汎用シークレット操作**（`set_secret` / `get_secret` / `delete_secret`）を追加し、パスワード専用の `set_password` 等と役割を分離する（複合キー文字列の手組みによる衝突を避けるため、実装は `(account_id, secret_name)` の2引数を素直に受け取る） |
| D-22 | OAuth2フローの方式 | **Authorization Code + PKCE（S256）+ ループバックリダイレクト**（`http://127.0.0.1:{一時ポート}/`）を採用する。OOB方式（`urn:ietf:wg:oauth:2.0:oob`）はGoogleが廃止済みのため使わない。`state` パラメータの往復検証を必須とし、コールバック受信サーバーは127.0.0.1にのみバインドし、1リクエストを受けたら即座に停止し、タイムアウト（既定120秒）を設ける |
| D-23 | OAuth2実装の置き場所 | `infrastructure/security/oauth2.py`（Qt非依存）に認可URL生成・PKCE・ループバック受信・コード交換・リフレッシュを実装する。ブラウザを開く操作（`QDesktopServices.openUrl`）のみ `presentation` 層に置く。既存のレイヤー依存方向（`domain` ← `usecases` ← `infrastructure` ← `presentation`）を維持する |
| D-24 | トークン更新のタイミング | `connect()` の直前に有効期限マージン120秒でアクセストークンの事前リフレッシュを行う。IMAPセッション確立後の失効は扱わない（次回同期時に再接続することで解決する）。リフレッシュ失敗（`invalid_grant` 等、ユーザーによるアクセス取消・失効）は `AuthenticationError` へラップし、UIで「Googleと再連携」を促す |
| D-25 | Gmailの7日間トークン失効（PoCブロッカー） | Google OAuth同意画面の公開ステータスが「テスト中」の場合、リフレッシュトークンは**7日で失効する**。この制約下で実運用可能かどうかは実装着手前に **PoC（Group Gタスク）で実測してから判断する**。Phase 4.5がreadpstのPoCを冒頭の方式ブロッカー判定としたのと同じ位置づけとし、PoCが「7日ごとの再認可が必要」という結論になった場合でも、その旨を計画書へ明記した上で5.2a以降の実装へ進む（実装を止めない）。「本番」ステータスへの切替に審査が不要な範囲で通せるかを最優先で確認する |
| D-26 | Dovecotでの結合テスト可否 | GmailのXOAUTH2は実サーバーでのみ確実に検証できるため、Dockerの Dovecot で `auth_mechanisms = xoauth2` によるローカルトークン検証が再現できるかをGroup GのPoCで確認する。再現できない場合は、Fakeフェッチャー・ローカルHTTPサーバーを用いた決定的な単体テストへ倒し、実サーバーでの検証は手動確認に留める（Phase4 レビュー修正案の「ソケット切断の実際の再現は狙わない」と同じ考え方） |
| D-27 | MS365の位置づけ | Microsoft 365 / Outlook.com 対応は **Phase 5.3** とし、5.2aで確立したOAuth2基盤（`infrastructure/security/oauth2.py` のエンドポイント・スコープを設定値として差し替え可能な設計）を再利用する。共有メールボックス・委任アクセスはスコープ外とする |
| D-28 | CLIへの公開範囲 | OAuth2の同意フロー（ブラウザ起動・トークン初回取得）は対話確認が必須のため **GUI限定**とし、CLIにはOAuth関連サブコマンドを追加しない。Phase 4 D-3・Phase 4.5 D-4 と同じ思想である。既存の `verify` / `reindex` はGmail/MS365アカウントにもそのまま対応する（`account_snapshot` に非秘密の接続情報のみを記録するため、再構築ロジックへの影響は無い） |

### **2.2 機能要件**

#### **Phase 5.1: 汎用IMAP**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-1 | `GenericImapFetcher.__init__` に `tls_mode: Literal["implicit", "starttls"] = "implicit"` と `ca_cert_path: str \| None = None` を追加する | D-5, D-7 |
| F-2 | `tls_mode="starttls"` のとき `imaplib.IMAP4`（非TLS）で接続し、`starttls(ssl_context=...)` を呼んだ後にログインする | D-5 |
| F-3 | `ca_cert_path` が指定されている場合、`load_verify_locations(cafile=ca_cert_path)` を適用したコンテキストを構築する。読み込み失敗時は `ConfigError` を送出し、検証を無効化してフォールバックしない | D-7 |
| F-4 | `connect()` で `CAPABILITY` に `LOGINDISABLED` が含まれる場合、SASL PLAINで `connection.authenticate("PLAIN", callback)` を呼ぶ | D-6 |
| F-5 | `wrap_imap_errors` が STARTTLS失敗・SASL認証失敗を適切に `AuthenticationError` / `TransientError` へ分類する | D-6 |
| F-6 | `migrations/007_generic_imap_connection.sql` で `accounts.tls_mode TEXT NOT NULL DEFAULT 'implicit'` / `accounts.ca_cert_path TEXT` を追加し、`provider_type='onamae_imap'` を `'imap'` へ正規化する | D-3, D-5, D-7 |
| F-7 | `register_account` / `update_account` / `SqliteMessageRepository.upsert_account` の `provider_type` 既定値を `"imap"` に統一し、`tls_mode` / `ca_cert_path` 引数を追加する | D-3, F-6 |
| F-8 | `007` 適用に合わせて既存アカウントの `account_snapshot` を現行の接続設定で再記録する移行タスクを用意する | D-4 |
| F-9 | `AppContext.create_fetcher()` / `create_fetcher_for_credentials()`、`__main__._account_fetcher()` を新カラムに対応させる | F-6 |
| F-10 | `AccountDialog` に接続方式（暗黙的TLS / STARTTLS）とCA証明書ファイル選択欄を追加し、変更時は接続テストを再度必須にする | F-1〜F-4 |
| F-11 | `_TRASH_CANDIDATES` に英語圏の主要な慣用名を追加する | D-9 |
| F-12 | `tests/docker/dovecot` の設定を拡張し、平文+STARTTLS必須・`LOGINDISABLED`・自己署名証明書の3構成を用意する | D-12 |

#### **Phase 5.2a: Gmail（IMAP + XOAUTH2、単一フォルダ扱い）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-13 | `accounts` に `auth_type TEXT NOT NULL DEFAULT 'password'`（`password` \| `xoauth2`）、`oauth_provider TEXT`（`google` 等）、`oauth_client_id TEXT`、`oauth_scopes TEXT`、`oauth_auth_endpoint TEXT`、`oauth_token_endpoint TEXT` を `008_oauth_accounts.sql` で追加する。`client_secret` はDBへ書かない | D-21, D-27 |
| F-14 | `messages` に `gmail_msgid TEXT` / `gmail_thrid TEXT` / `gmail_labels TEXT` を追加し、`UID FETCH` の応答項目 `X-GM-MSGID` / `X-GM-THRID` / `X-GM-LABELS` をパースして保存する | D-18 |
| F-15 | `CREATE INDEX idx_msg_gmsgid ON messages(account_id, gmail_msgid) WHERE gmail_msgid IS NOT NULL` を追加する | D-18 |
| F-16 | `infrastructure/security/oauth2.py` に、認可URL生成・PKCE（S256）・`state`検証・ループバックHTTPサーバーでのコード受信・トークン交換・リフレッシュを実装する。標準ライブラリのみを使用する | D-15, D-22, D-23 |
| F-17 | `BaseCredentialStore` に `set_secret(account_id, name, value)` / `get_secret(account_id, name) -> str \| None` / `delete_secret(account_id, name)` を追加し、`KeyringCredentialStore` / `SessionCredentialStore` / テストダブルへ実装する | D-21 |
| F-18 | `GenericImapFetcher` に `auth_type="xoauth2"` の分岐を追加し、`imaplib` の `authenticate("XOAUTH2", callback)` で `user={email}\x01auth=Bearer {token}\x01\x01` 形式のSASL文字列を渡す | D-13, F-16 |
| F-19 | フェッチャーの `connect()` 直前にアクセストークンの有効期限を確認し、マージン120秒以内ならリフレッシュしてから接続する | D-24 |
| F-20 | `wrap_imap_errors` にGmail固有の応答コード（`[THROTTLED]` / `[OVERQUOTA]` / `[LIMIT]`）の分類を追加し、`usecases/retry.py` へ長めのバックオフ経路を追加する | D-19 |
| F-21 | `delete_remote.py` の入口で `accounts.oauth_provider='google'` のアカウントに対する `mode="expunge"` を拒否し、`trash` のみ許可する | D-20 |
| F-22 | `usecases/oauth_authorize.py` を新設し、認可フロー開始・コールバック待受・トークン初回取得・`keyring`への保存・再認可（トークン取消検出時）を提供する | F-16, F-17 |
| F-23 | `register_account` / `update_account` に `auth_type` / `oauth_provider` / `oauth_client_id` / `oauth_scopes` / エンドポイント引数を追加する。OAuth系アカウントでは `password` を要求しない | F-13, F-22 |
| F-24 | `AccountDialog` / `SetupWizard` に認証方式選択（ID/パスワード or Googleでログイン）、OAuthクライアントID入力欄、「Googleで認証」ボタン、トークン状態表示（未連携・連携済み・要再連携）を追加する | F-22, F-23 |
| F-25 | 同期対象フォルダ選択UIで、Gmailアカウントに対し `\All` を既定候補として提示し、INBOXなど他フォルダとの重複選択時に警告文言を表示する（D-17の暫定挙動の明示） | D-17 |
| F-26 | README にGoogle Cloudプロジェクト作成・OAuth同意画面設定・スコープ追加・デスクトップアプリ用クライアントID発行の手順を記載する | D-14 |

#### **Phase 5.2b: Gmailラベル対応（`message_folders`）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-27 | `migrations/009_message_folders.sql` で `message_folders(message_id INTEGER NOT NULL REFERENCES messages(id), folder_id INTEGER NOT NULL REFERENCES folders(id), uid INTEGER, uidvalidity INTEGER, PRIMARY KEY(message_id, folder_id))` を新設し、既存 `messages.folder_id`/`uid`/`uidvalidity` からバックフィルする | D-16 |
| F-28 | Gmailアカウントの一次識別子を `source_item_key = "gmsgid:{X-GM-MSGID}"` へ再設計し、ラベルごとのUIDは `message_folders` 側の属性として扱う。非Gmail IMAP・PSTは「1メッセージ=1 message_folders行」として従来の一意制約と等価に振る舞う | F-14, D-16 |
| F-29 | `uq_imap_message` 一意インデックスを `message_folders` 側の一意性へ置き換え、`messages` 側は `(account_id, source_item_key)` の一意性のみを担保する形へ改める | F-28 |
| F-30 | `domain/search.py` の `MessageFilter.folder_ids` を用いる検索クエリ・一覧クエリを `message_folders` 経由のJOINへ書き換える | F-27 |
| F-31 | `presentation/models/folder_tree_model.py` のフォルダノード・メッセージ件数・一覧表示を `message_folders` を前提に更新する | F-27 |
| F-32 | 削除検知（`list_existing_uids()` との比較、`remote_state` 判定）をラベル単位（`message_folders` の行単位）で行うよう `sync_mail.py` を更新する | F-27 |
| F-33 | `delete_remote.py` のGmail向け削除を「ラベルを外す（該当フォルダの `message_folders` 行を除去）」と「完全にゴミ箱へ移動する（全ラベルから除去し `[Gmail]/ゴミ箱` へ）」に区別する | F-27, D-20 |
| F-34 | ローカルゴミ箱・purgeにおける共有EML判定（`count_path_references`）を `message_folders` を考慮した形へ更新する | F-27 |
| F-35 | `reindex.py` によるDB完全再構築が `message_folders` の行（ラベル所属）までEML＋マニフェストから復元できるようにする | F-27 |
| F-36 | マニフェストの `fetch` イベントへ `gmail_labels` / `gmail_msgid` / `gmail_thrid` を追加する | F-14, F-27 |

#### **Phase 5.3: Microsoft 365 / Outlook.com**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-37 | `oauth_provider='microsoft'` を追加し、`oauth_auth_endpoint` / `oauth_token_endpoint` にAzure ADのテナント別エンドポイント（`consumers` / `organizations` / 固有テナントID）を設定可能にする | D-27, F-13 |
| F-38 | スコープ `https://outlook.office.com/IMAP.AccessAsUser.All offline_access` を用いた認可コード＋PKCEフローを、5.2aの `oauth2.py` の設定差し替えのみで動作させる | F-16, D-27 |
| F-39 | `outlook.office365.com:993` を既定ホストとして `GenericImapFetcher` に暗黙的TLS + XOAUTH2で接続する | F-18 |
| F-40 | README にAzure ADアプリ登録手順（リダイレクトURI・APIアクセス許可・テナント選択）を記載する | D-14, D-27 |
| F-41 | 共有メールボックス・委任アクセスをコードレベルで要件外とし、通常のOAuth2アカウント登録フローのみで完結することを確認する | D-27 |

### **2.3 非機能要件・制約**

- レイヤー境界を守る: TLS・認証方式・OAuth2の分岐と実装は `infrastructure/fetchers/generic_imap.py`・`infrastructure/fetchers/imap_common.py`・`infrastructure/security/oauth2.py` に閉じ込め、`usecases` / `domain` はホスト名・ポート・証明書パス・トークンを不透明な値として受け渡すだけとする。
- 証明書検証を無効化する設定・フラグ・環境変数は一切追加しない（OWASP Top 10: 暗号化の失敗対策）。
- **秘密情報（パスワード・`client_secret`・`refresh_token`・`access_token`）は `keyring` またはプロセス内メモリにのみ保持し、DB・`config.json`・ログ・永続マニフェストのいずれにも書かない。** この制約を自動テストで固定する（Group Oタスク）。
- `provider_type` の値変更・`message_folders` への移行はいずれもマイグレーションで一度だけ行い、アプリケーションコードが旧文字列・旧スキーマに依存している箇所（テストのフィクスチャ含む）を洗い出してから実施する。
- リネーム（`OnamaeImapFetcher` → `GenericImapFetcher`）は全参照更新を伴うため、影響ファイルを全て洗い出してから着手する。
- `CancelToken` によるキャンセル・リトライ方針など、Phase 1 / Phase 3.7 / Phase 4 で確定済みの契約は変更しない。
- OAuth2のループバックリダイレクトサーバーは `127.0.0.1` にのみバインドし、外部ネットワークからの接続を受け付けない。1認可フローにつき1リクエストで停止し、待受ポートは毎回新規に確保する（固定ポートを使わない）。
- 新規依存パッケージを追加しない（D-15）。

---

## **3. タスク**

> 依存関係: **Group A〜F（5.1）→ Group G（5.2a PoC・ブロッカー判定）→ Group H〜L（5.2a 実装）→ Group M（5.2b）→ Group N（5.3）**。Group O（テスト）・Group P（ドキュメント）は各グループと並行して作成する。

### **Group A: 5.1 ドメイン層・フェッチャー**

- [ ] `infrastructure/fetchers/onamae_imap.py` を `infrastructure/fetchers/generic_imap.py` にリネームし、クラス名を `GenericImapFetcher` に変更する（全参照を更新する）
- [ ] クラス docstring を「ID/パスワード認証・XOAUTH2の両方に対応する任意のIMAP4rev1サーバー向けの実装」へ書き改める
- [ ] コンストラクタに `tls_mode: Literal["implicit", "starttls"] = "implicit"` と `ca_cert_path: str | None = None` を追加する
- [ ] `connect()` を分岐させる: `tls_mode="implicit"` は現行の `IMAP4_SSL` 経路、`tls_mode="starttls"` は `IMAP4` 接続 → `starttls(ssl_context=...)` → 以降は現行と同じ流れにする
- [ ] `ca_cert_path` 指定時に `load_verify_locations(cafile=...)` を適用するヘルパーを追加し、失敗時は `ConfigError` を送出する
- [ ] STARTTLS完了後（または暗黙的TLS確立直後）の `CAPABILITY` に `LOGINDISABLED` が含まれる場合、SASL PLAIN認証へ切り替える
- [ ] `imap_common.wrap_imap_errors` に STARTTLS失敗・SASL認証失敗のエラー分類を追加する
- [ ] `_TRASH_CANDIDATES` に英語圏の主要な慣用名を追加する
- [ ] 既存の `iter_message_refs` / `iter_flags` / `iter_flags_since` / `delete_remote_message` 等の挙動・シグネチャを変更しない（回帰させない）

### **Group B: 5.1 DBスキーマとリポジトリ**

- [ ] `migrations/007_generic_imap_connection.sql` を追加し、`accounts.tls_mode` / `accounts.ca_cert_path` を追加、`provider_type='onamae_imap'` を `'imap'` へ正規化する
- [ ] `SqliteMessageRepository._ACCOUNT_COLUMNS` と `upsert_account` の `provider_type` 既定値を `"imap"` に変更する
- [ ] `usecases/register_account.py` の `register_account` / `update_account` の `provider_type` ハードコードを `"imap"` に変更し、`tls_mode` / `ca_cert_path` 引数を追加する
- [ ] `tests/support/in_memory_repository.py` のアカウント関連実装に新カラムを反映する
- [ ] 既存アカウントの `account_snapshot` を現行の接続設定で再記録する移行タスクを `007` 適用の後処理として実装する（D-4）

### **Group C: 5.1 usecases**

- [ ] `list_accounts()` の戻り値に新カラムが含まれ、既存の「パスワードを返さない」制約が維持されることを確認する

### **Group D: 5.1 GUIとコンポジションルート**

- [ ] `AppContext.create_fetcher()` / `create_fetcher_for_credentials()`、`__main__._account_fetcher()` を `GenericImapFetcher` と新カラムに対応させる
- [ ] `AccountDialog` に接続方式コンボボックス（暗黙的TLS / STARTTLS）を追加し、選択に応じてポート番号の既定プレースホルダ（993 / 143）を切り替える
- [ ] `AccountDialog` にCA証明書ファイル指定欄（`QLineEdit` + 参照ボタン）を追加する
- [ ] `_connection_fields_changed()` の比較対象に `tls_mode` / `ca_cert_path` を含める
- [ ] `_test_connection()` / `create_fetcher_for_credentials()` 呼び出しに新パラメータを伝播させる
- [ ] `strings.py` に新規ラベル・ヒント文言を追加する

### **Group E: 5.1 テスト**

- [ ] `tests/docker/dovecot` に、平文+STARTTLS必須構成・`LOGINDISABLED`構成・自己署名証明書構成を追加する
- [ ] `tests/integration/` に STARTTLS接続・PLAINフォールバック・カスタムCA証明書接続の結合テストを追加する
- [ ] `tests/unit/` にTLSモード分岐・CA証明書読み込み失敗時の `ConfigError`・SASL PLAINコールバックの単体テストを追加する
- [ ] `tests/gui/test_settings_dialog.py` に接続方式・CA証明書欄の入力とダイアログ再検証ロジックのテストを追加する
- [ ] `provider_type` 正規化マイグレーションと `account_snapshot` 再記録の結合テストを追加する

### **Group F: 5.1 ドキュメント整合**

- [ ] 開発計画書中の `OnamaeImapFetcher` 表記・`provider_type` の例示値（`'onamae_imap'`）を更新する
- [ ] `.github/copilot-instructions.md` の該当記述を更新する

### **Group G: 5.2a PoC（★最優先の方式ブロッカー判定）**

- [ ] G-1: Google Cloud プロジェクトを作成し、OAuth同意画面（External）で制限付きスコープ `https://mail.google.com/` を追加し、テストユーザーとしてPoC用アカウントを登録する
- [ ] G-2: デスクトップアプリ用OAuthクライアントを発行し、Authorization Code + PKCE + ループバックリダイレクトで認可コードを取得できることを確認する
- [ ] G-3: 取得した `refresh_token` / `access_token` でXOAUTH2によるIMAP接続・`LIST`・`UID FETCH`（`X-GM-MSGID`/`X-GM-THRID`/`X-GM-LABELS`を含む）が成功することを実機で確認する
- [ ] G-4: OAuth同意画面が「テスト中」ステータスのまま、リフレッシュトークンが実際に7日で失効することを確認し、「本番」へのステータス変更が未審査のまま可能か（制限付きスコープを含むアプリの公開ステータス変更の実際の挙動）を確認する（D-25）
- [ ] G-5: G-4の結果を本書2.1 D-25の記述へ反映する。7日失効が回避できない場合は「7日ごとの再認可が必要な既知の制約」として明記した上で5.2a実装へ進む（実装を止めるブロッカーにはしない）
- [ ] G-6: Gmailの帯域制限に達した際の実際のレスポンス文字列（`[OVERQUOTA]`等）を記録し、D-19の分類実装へ反映する
- [ ] G-7: Docker Dovecot で `auth_mechanisms = xoauth2` によるローカルトークン検証が結合テストとして再現可能かを確認する。再現できない場合はD-26に従いFakeフェッチャーへの切替方針を確定する
- [ ] G-8: PoCの実測結果を本書の該当D項目（D-13, D-18, D-19, D-25, D-26）へ反映し、必要であれば意思決定を更新する

### **Group H: 5.2a OAuth2基盤**

- [ ] `infrastructure/security/oauth2.py` を新設し、認可URL生成（`response_type=code&code_challenge=...&code_challenge_method=S256&access_type=offline&prompt=consent`）を実装する
- [ ] PKCE の `code_verifier` / `code_challenge` を `secrets` + `hashlib.sha256` で生成する
- [ ] `state` パラメータを生成・検証する
- [ ] `http.server.HTTPServer` を `127.0.0.1:0`（一時ポート）でバインドし、1リクエスト受信後に停止するループバックコールバックサーバーを実装する。タイムアウト（既定120秒）を設ける
- [ ] 認可コード→トークン交換（`urllib.request` によるPOST）を実装する
- [ ] リフレッシュトークンによるアクセストークン再取得を実装し、有効期限を戻り値で管理する
- [ ] `invalid_grant` 等の失効レスポンスを `AuthenticationError` へラップする
- [ ] エンドポイント・スコープを引数化し、Google以外（Phase 5.3のMicrosoft）でも再利用できる形にする（D-27を見据える）

### **Group I: 5.2a 資格情報・DBスキーマ**

- [ ] `domain/ports.py` の `BaseCredentialStore` に `set_secret` / `get_secret` / `delete_secret` を追加する
- [ ] `KeyringCredentialStore` / `SessionCredentialStore` / テストダブルに実装する
- [ ] `migrations/008_oauth_accounts.sql` を追加し、`accounts.auth_type` / `oauth_provider` / `oauth_client_id` / `oauth_scopes` / `oauth_auth_endpoint` / `oauth_token_endpoint` を追加する
- [ ] `messages` に `gmail_msgid` / `gmail_thrid` / `gmail_labels` を追加し、`idx_msg_gmsgid` を作成する
- [ ] `account_snapshot` イベントの非秘密フィールドへ `auth_type` / `oauth_provider` / `oauth_client_id` / `oauth_scopes` を追加する（`client_secret`・トークン類は含めない）

### **Group J: 5.2a フェッチャー・usecases**

- [ ] `GenericImapFetcher` に `auth_type="xoauth2"` 分岐を追加し、`authenticate("XOAUTH2", callback)` でSASL文字列を渡す
- [ ] `connect()` 直前のトークン有効期限チェックとリフレッシュを実装する
- [ ] `UID FETCH` の応答パーサ（`imap_common.py`）へ `X-GM-MSGID` / `X-GM-THRID` / `X-GM-LABELS` の抽出を追加する
- [ ] `wrap_imap_errors` にGmail固有応答コードの分類を追加する
- [ ] `usecases/retry.py` にGmailスロットル向けの長期バックオフ経路を追加する
- [ ] `usecases/oauth_authorize.py` を新設し、認可開始・トークン保存・再認可を提供する
- [ ] `register_account` / `update_account` に `auth_type` / `oauth_*` 引数を追加する
- [ ] `delete_remote.py` の入口でGmailアカウントの `mode="expunge"` を拒否する

### **Group K: 5.2a GUI**

- [ ] `AccountDialog` に認証方式選択（ID/パスワード or Googleでログイン）を追加する
- [ ] OAuthクライアントID入力欄と「Googleで認証」ボタンを追加し、クリックでブラウザを起動して認可フローを開始する
- [ ] トークン状態表示（未連携／連携済み／要再連携）と再連携導線を追加する
- [ ] `SetupWizard` に同じ認証方式選択を追加する
- [ ] フォルダ選択ページでGmailアカウントの場合、`\All` を既定候補として提示し、他フォルダとの重複選択に警告文言を表示する

### **Group L: 5.2a テスト**

- [ ] `infrastructure/security/oauth2.py` の単体テスト（PKCE生成・`state`検証・ループバックサーバー・トークン交換・リフレッシュ・失効処理）をローカルHTTPスタブで実施する
- [ ] `GenericImapFetcher` のXOAUTH2認証分岐・トークン事前リフレッシュの単体テストをFakeフェッチャー相当のスタブで実施する
- [ ] 秘密情報がDB・`config.json`・ログ・マニフェストへ出力されないことを固定するテストを追加する
- [ ] `delete_remote.py` のGmailアカウントに対する `expunge` 拒否テストを追加する
- [ ] 可能であれば実Gmailアカウントでの結合テスト（`pst`マーカーと同様に通常CIでは除外し、手動実行専用のマーカーを新設する）を追加する

### **Group M: 5.2b Gmailラベル対応（`message_folders`）**

- [ ] `migrations/009_message_folders.sql` を追加し、`message_folders` テーブルを新設して既存データをバックフィルする
- [ ] Gmailアカウントの `source_item_key` を `"gmsgid:{X-GM-MSGID}"` へ再設計する
- [ ] `uq_imap_message` を `message_folders` 側の一意性へ置き換える
- [ ] `domain/search.py::MessageFilter` と検索・一覧クエリを `message_folders` 経由のJOINへ更新する
- [ ] `presentation/models/folder_tree_model.py` を `message_folders` を前提に更新する
- [ ] `sync_mail.py` の削除検知（`list_existing_uids`比較）をラベル単位に更新する
- [ ] `delete_remote.py` のGmail向け削除を「ラベル除去」と「ゴミ箱への完全移動」に区別する
- [ ] `count_path_references` 等の共有EML判定を `message_folders` 考慮へ更新する
- [ ] `reindex.py` が `message_folders` までEML＋マニフェストから再構築できるようにする
- [ ] マニフェストの `fetch` イベントへ `gmail_labels` / `gmail_msgid` / `gmail_thrid` を追加する
- [ ] 5.2aで許容したINBOX/All Mailの二重登録を解消する重複統合ロジックを実装する

### **Group N: 5.3 Microsoft 365 / Outlook.com**

- [ ] `oauth_provider='microsoft'` のエンドポイント・スコープ設定を追加する
- [ ] `outlook.office365.com:993` を既定ホストとしてGmailと同じXOAUTH2経路で接続できることを確認する
- [ ] Azure ADアプリ登録手順をREADMEへ追記する
- [ ] テナント選択（`consumers` / `organizations` / 固有テナントID）のUI・設定項目を追加する
- [ ] 共有メールボックス・委任アクセスを要件外として明記し、通常のOAuth2アカウント登録のみで完結することを確認する

### **Group O: 静的テスト・CI（全サブフェーズ共通）**

- [ ] `tests/unit/test_ports.py` を拡張し、`domain` / `usecases` に PySide6 の import が無いこと、`presentation` に `sqlite3` / `mail_dock.infrastructure` の import が無いことを確認する
- [ ] CLIにOAuth関連サブコマンドが存在しないことを固定する静的テストを追加する
- [ ] 秘密情報（`client_secret` / `refresh_token` / `access_token`）がDB・`config.json`・ログ・マニフェストへ一切出力されないことを固定するテストを追加する
- [ ] `uv run ruff format --check .` / `uv run ruff check .` / `uv run mypy` / `uv run pytest -m "not docker and not gui and not pst"` を実行し、全テスト通過を確認する
- [ ] `message_folders` 移行後も既存の全結合テスト・単体テストが壊れていないことを確認する

### **Group P: ドキュメント整合**

- [ ] 開発計画書 2.1 の依存関係一覧から「`google-auth-oauthlib`（将来対応用）」の記述を削除し、標準ライブラリのみでOAuth2を実装する方針へ更新する
- [ ] 開発計画書 6章ロードマップのPhase 5行を、5.1 / 5.2a / 5.2b / 5.3 の4サブフェーズへ書き分ける
- [ ] 開発計画書 8章決定事項ログへ本フェーズの主要決定（サブフェーズ分割、OAuth2自前実装、秘密情報の保管方針、Gmailの`expunge`拒否等）を追記する
- [ ] 旧 [実装計画書_Phase5.1_汎用IMAPサーバー対応.md](./実装計画書_Phase5.1_汎用IMAPサーバー対応.md) の冒頭に、本書へ統合された旨の注記を追加する
- [ ] `.github/copilot-instructions.md` に、OAuth2はGUI限定・秘密情報はkeyring/メモリのみという頻出ルールを追記する
- [ ] `ruff check .` / `mypy .` / `pytest` を実行し、全テスト通過を確認する

---

## **4. 主要成果物**

| パス | 内容 | タスク |
| :---- | :---- | :---- |
| `src/mail_dock/infrastructure/fetchers/generic_imap.py` | `OnamaeImapFetcher`から一般化したフェッチャー（TLSモード・PLAIN・XOAUTH2） | Group A, J |
| `src/mail_dock/infrastructure/fetchers/imap_common.py` | エラー分類拡張（STARTTLS/SASL/Gmailスロットル）・`X-GM-*`パース | Group A, J |
| `src/mail_dock/infrastructure/security/oauth2.py` | OAuth2認可コード+PKCEフロー（標準ライブラリのみ） | Group H |
| `src/mail_dock/domain/ports.py` | `BaseCredentialStore` の名前空間付きシークレット操作 | Group I |
| `src/mail_dock/migrations/007_generic_imap_connection.sql` | `tls_mode` / `ca_cert_path` 追加、`provider_type`正規化 | Group B |
| `src/mail_dock/migrations/008_oauth_accounts.sql` | OAuth関連カラム、Gmailメタデータ列 | Group I |
| `src/mail_dock/migrations/009_message_folders.sql` | `message_folders`中間テーブルとバックフィル | Group M |
| `src/mail_dock/usecases/oauth_authorize.py` | 認可フロー開始・トークン保存・再認可 | Group J |
| `src/mail_dock/usecases/register_account.py` | `tls_mode`/`ca_cert_path`/`auth_type`/`oauth_*`対応 | Group B, J |
| `src/mail_dock/usecases/delete_remote.py` | Gmailの`expunge`拒否・ラベル区別削除 | Group J, M |
| `src/mail_dock/presentation/views/dialogs/settings_dialog.py` | 接続方式・認証方式・OAuth連携UI | Group D, K |
| `src/mail_dock/presentation/views/setup_wizard.py` | 認証方式選択・フォルダ重複警告 | Group K |
| `tests/docker/dovecot` | STARTTLS/LOGINDISABLED/自己署名証明書構成 | Group E |

---

## **5. スコープ境界**

### **5.1 含むもの**

セクション3のGroup A〜P。「汎用IMAP対応 → GmailのOAuth2 PoC → Gmail実装（単一フォルダ扱い）→ Gmailラベル対応 → Microsoft 365対応」の一式。

### **5.2 含まないもの（明示的に除外）**

| 除外項目 | 実施フェーズ・理由 |
| :---- | :---- |
| Gmail REST API（`users.messages` / `historyId`差分同期） | **恒久的にスコープ外**（D-13）。IMAP+XOAUTH2で代替する |
| クライアント証明書・mTLS | **恒久的にスコープ外**（D-8） |
| OAuthクライアントの同梱・代理発行 | **恒久的にスコープ外**（D-14）。ユーザー自身が用意する |
| 共有メールボックス・委任アクセス（MS365） | **恒久的にスコープ外**（D-27） |
| Gmail/MS365以外のOAuth2プロバイダ（Yahoo!メール等） | **本フェーズ外**。必要になれば個別に追加検討 |
| IMAPフラグの双方向同期・ローカル既読管理・IMAP IDLE | **恒久的にスコープ外**（開発計画書の既定方針） |
| PSTアーカイブとの統合・重複排除 | **恒久的にスコープ外**（開発計画書1.3） |
| PyInstaller / Inno Setup によるパッケージング | Phase 6（配布） |

---

## **6. 検証**

各項目の完了を確認したうえで、対応するタスクのチェックボックスを埋めること。

- [ ] V-1（ブロッカー）. Group G のPoCが完了し、GmailのXOAUTH2接続・トークン取得・7日失効の実際の挙動・帯域制限応答が実測されていること
- [ ] V-2. 汎用IMAP（5.1）で、STARTTLS・`LOGINDISABLED`・カスタムCA証明書の3シナリオがDocker結合テストで通ること
- [ ] V-3. `provider_type` 正規化マイグレーション適用後、`reindex.py` によるDB再構築が新しい `provider_type` を復元すること（`account_snapshot` 再記録が機能していること）
- [ ] V-4. 実Gmailアカウント（またはOAuth2スタブ）でXOAUTH2接続・同期・EML保存が成功すること
- [ ] V-5. Gmailアカウントに対する `delete_remote` の `mode="expunge"` が拒否され、`trash` のみ実行できること
- [ ] V-6. 秘密情報（`client_secret`/`refresh_token`/`access_token`）がDB・`config.json`・ログ・マニフェストのいずれにも出力されないことが自動テストで固定されていること
- [ ] V-7（5.2b）. `metadata.db` を削除し、EML＋マニフェストだけからGmailアカウントの `message_folders`（ラベル所属）まで完全復元できること
- [ ] V-8（5.2b）. 同一メールが複数ラベルに属する場合でも、検索・一覧・削除検知・共有EML判定が二重カウントなく機能すること
- [ ] V-9（5.3）. Microsoft 365アカウントで、`oauth2.py` の設定差し替えのみでOAuth2接続が成立すること
- [ ] V-10. `uv run ruff format --check .` / `uv run ruff check .` / `uv run mypy` が成功すること
- [ ] V-11. `uv run pytest -m "not docker and not gui and not pst"` がCIで緑になること
- [ ] V-12. `domain` / `usecases` の層依存方向が維持されていること（PySide6・OAuthブラウザ起動を含まないこと）
- [ ] V-13. CLIにOAuth関連・PST関連の対話的サブコマンドが存在しないことが静的テストで固定されていること

---

## **7. 引き継ぎ事項**

- [ ] Phase 5.2a 完了後、Group GのPoC結果（特にG-4/G-5のトークン失効の扱い）を開発計画書へ反映し、運用ドキュメント（README）に7日ごとの再認可が必要かどうかを明記する
- [ ] Phase 5.2b 完了後、`message_folders` 移行がPSTアーカイブ側（Phase 4.5）のスキーマ・reindexに影響しないことを確認する（PST側は元々1メッセージ=1フォルダのため無影響のはずだが、共有コード経路の回帰確認は必須）
- [ ] Phase 5.3 完了後、OAuth2プロバイダ追加の一般的な手順（エンドポイント・スコープ・README追記の型）を本書またはリポジトリメモリへ整理し、将来のプロバイダ追加を容易にする
