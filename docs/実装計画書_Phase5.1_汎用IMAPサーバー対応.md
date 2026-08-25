# **Phase 5.1: 汎用IMAPサーバー対応 実装計画書**

対象: [ローカルメールバックアップ＆閲覧アプリ 開発計画書.md](./ローカルメールバックアップand閲覧アプリ開発計画書.md) の **概要「高い拡張性（マルチプロトコル対応）」** および **4.2 IMAP同期・バックアップ機能**

前提: [Phase 1: 抽象化層とIMAPコア 実装計画書](./実装計画書_Phase1_抽象化層とIMAPコア.md) の `BaseMailFetcher` / `OnamaeImapFetcher` / `imap_common.py`、[Phase 3.7: IMAPフラグの定期リフレッシュ](./実装計画書_Phase3.7_IMAPフラグの定期リフレッシュ.md) の `CONDSTORE` 対応が完成していること。

位置づけ: 現行の `OnamaeImapFetcher` は RFC 3501 準拠の標準的な IMAP4rev1 実装だが、名称・既定値・実地検証がお名前.comの実サーバーに限定されている。本フェーズ（開発計画書ロードマップの **Phase 5: 汎用IMAP/Gmail対応** のうち **Phase 5.1**）は、**ID/パスワード認証で接続する任意のIMAPサーバー**（さくらインターネット、独自Dovecot/Postfix構成、その他レンタルサーバー等）を同一コードパスで扱えるようにする**小〜中規模の拡張フェーズ**である。同じPhase 5に属する **Phase 5.2: Gmail/OAuth2**（`GmailOAuthFetcher`、OAuth2ブラウザ認証、`message_folders` 中間テーブル）は本フェーズのスコープに含めない。OAuth2は認証モデル自体が別物であり、ID/パスワード認証の拡張では吸収できないため。

本書と開発計画書に矛盾がある場合は、開発計画書を正とする。設計不変条件（真実の情報源はEML＋マニフェスト、書き込み順序、削除の多段防御）は変更しない。

---

## **1. 目的**

- [ ] `BaseMailFetcher` の実装をお名前.com固有のクラスから「ID/パスワード認証を使う任意のIMAPサーバー用フェッチャー」へ一般化する
- [ ] 暗黙的TLS（993番ポート想定）に加え、STARTTLS（143番ポート想定）で接続できるようにする
- [ ] `LOGIN` コマンドが無効化されているサーバー（`LOGINDISABLED`）でも `AUTHENTICATE PLAIN` でログインできるようにする
- [ ] 自己署名証明書やプライベートCAを使うサーバーに対して、検証を無効化せずに接続できる手段（カスタムCA証明書ファイルの指定）を用意する
- [ ] アカウント登録・編集UIから接続方式（暗黙的TLS / STARTTLS）とCA証明書ファイルを設定できるようにする
- [ ] ゴミ箱フォルダの候補名を主要な英語圏の慣用名まで拡張しつつ、検出できない場合の手動指定（`remote_trash_folder`）を主要な回避策として維持する
- [ ] Dovecotベースの結合テスト環境をSTARTTLS・`LOGINDISABLED`・自己署名証明書のシナリオに対応させ、Onamae固有の実サーバーがなくても回帰確認できるようにする
- [ ] OAuth2（Gmail等）は別フェーズ（開発計画書 Phase 5）に委ね、本フェーズでは着手しない

## **2. 要件**

### **2.1 前提となる意思決定（要合意）**

| # | 項目 | 決定内容（案） |
| :--- | :---- | :---- |
| D-1 | スコープ境界 | 本フェーズ（Phase 5.1）は **ID/パスワード認証で接続するIMAPサーバー全般** を対象とする。OAuth2/XOAUTH2（Gmail・Microsoft 365等）は開発計画書に既定路線がある同じPhase 5内の別サブフェーズ（Phase 5.2）とし、本フェーズのクラス・スキーマ変更には混在させない |
| D-2 | クラス構成 | 新しいプロバイダ別サブクラスを増やさず、`OnamaeImapFetcher` を `GenericImapFetcher` へリネームして汎用実装1本に統合する。プロトコル分岐（TLSモード・LOGINDISABLED）はコンストラクタ引数と `connect()` 内の分岐で表現し、`BaseMailFetcher` の実装は依然として1クラスのみとする |
| D-3 | `provider_type` の扱い | DBの `accounts.provider_type` は新規登録時に `"imap"` を書き込む。既存行の `"onamae_imap"` はマイグレーションで `"imap"` へ正規化する（現状この値を分岐条件に使うコードは無いため、意味的な区別を維持する必要がない） |
| D-4 | 接続方式（TLSモード） | `tls_mode` を `"implicit"`（既定・現行動作＝`IMAP4_SSL`、既定ポート993）と `"starttls"`（`IMAP4` で接続後 `STARTTLS`、既定ポート143）の2値とする。ユーザーがポート番号を明示している場合はそちらを優先し、既定ポートはUIのプレースホルダとしてのみ用いる |
| D-5 | `LOGINDISABLED` への対応 | `STARTTLS` 完了後に `CAPABILITY` を再取得し、`LOGINDISABLED` が含まれる場合は `imaplib` の `authenticate("PLAIN", ...)` を使い、SASL PLAIN（`\0username\0password`）でログインする。暗黙的TLS経路でも同様に `LOGINDISABLED` を確認し、該当すればPLAIN認証にフォールバックする |
| D-6 | 証明書検証 | 既定は `ssl.create_default_context()` によるシステム信頼ストア検証を維持し、**検証を無効化するオプションは提供しない**（OWASP的に中間者攻撃を招くため）。自己署名証明書やプライベートCAを使うサーバー向けに、ユーザーが指定した **CA証明書ファイル1点** を `ssl.SSLContext.load_verify_locations()` で読み込む経路のみを追加する |
| D-7 | クライアント証明書 | クライアント証明書（mTLS）は現時点で要件化しない。将来必要になった場合に別途検討する |
| D-8 | ゴミ箱候補名の拡張 | `_TRASH_CANDIDATES` に英語圏の慣用名（`"Bin"`, `"Junk"` は誤検出防止のため対象外とし、`"Deleted"` 等）を追加する。SPECIAL-USE `\Trash` を最優先とする既存の優先順位は変えない。網羅は不可能なため `remote_trash_folder` 手動指定を主要な回避策として維持する |
| D-9 | 階層区切り文字 | `list_folders()` は既に `RemoteFolder.delimiter` を保持しているが、フォルダ操作コマンドは常に `raw_name` をそのまま渡しており区切り文字を組み立てに使っていない。NAMESPACE拡張への対応は本フェーズでは行わず、異常値（空文字列・複数文字等）の防御的な検証のみ追加する |
| D-10 | 同時接続数・レート制限 | サーバー固有の同時接続数上限やレート制限は、既存の `usecases/retry.py` の `TransientError` リトライ機構に委ね、プロバイダ別の追加設定は行わない |
| D-11 | 実サーバー検証の代替 | Onamae以外の実IMAPサーバーを継続的なCIで使うことはできないため、Dockerの Dovecot 設定を拡張し、STARTTLS・`LOGINDISABLED`・自己署名証明書の3シナリオを結合テストで再現する。実運用前には少なくとも1つの非Onamaeサーバー（さくら等）での手動検証を推奨する |

### **2.2 機能要件**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-1 | `GenericImapFetcher.__init__` に `tls_mode: Literal["implicit", "starttls"] = "implicit"` と `ca_cert_path: str \| None = None` を追加する | D-4, D-6 |
| F-2 | `tls_mode="starttls"` のとき `imaplib.IMAP4`（非TLS）で接続し、`starttls(ssl_context=...)` を呼んだ後にログインする。`ssl_context` 未指定時は `ssl.create_default_context()` を使う | D-4 |
| F-3 | `ca_cert_path` が指定されている場合、`ssl.create_default_context()` に対し `load_verify_locations(cafile=ca_cert_path)` を適用したコンテキストを構築する。ファイルが存在しない・読み込めない場合は `ConfigError` を送出し、検証を無効化してフォールバックしない | D-6 |
| F-4 | `connect()` で（STARTTLS完了後、またはTLS確立直後の）`CAPABILITY` に `LOGINDISABLED` が含まれる場合、`connection.login(...)` の代わりに SASL PLAIN で `connection.authenticate("PLAIN", callback)` を呼ぶ | D-5 |
| F-5 | `wrap_imap_errors` が STARTTLS失敗・SASL認証失敗を適切に `AuthenticationError` / `TransientError` へ分類する（既存の `AUTHENTICATIONFAILED` 判定に加え、SASL関連の失敗文字列を判定に追加する） | D-5 |
| F-6 | `accounts` テーブルに `tls_mode TEXT NOT NULL DEFAULT 'implicit'` と `ca_cert_path TEXT` を追加するマイグレーション `005_generic_imap_connection.sql` を作成する。既存行の `provider_type='onamae_imap'` を `'imap'` に正規化するUPDATE文を含める | D-3, D-4, D-6 |
| F-7 | `usecases/register_account.py` の `register_account` / `update_account` に `tls_mode` / `ca_cert_path` 引数を追加し、`provider_type` の書き込み値を `"imap"` に変更する | D-3, F-6 |
| F-8 | `domain/repository.py` の `MessageRecord` 相当の型・`SqliteMessageRepository` の `_ACCOUNT_COLUMNS`（該当箇所）に新カラムを反映する | F-6 |
| F-9 | `presentation/context.py` の `AppContext.create_fetcher()` / `create_fetcher_for_credentials()` と `__main__._account_fetcher()` を、新カラムを読み取って `GenericImapFetcher` へ渡すよう更新する | F-1, F-6 |
| F-10 | `AccountDialog`（`settings_dialog.py`）に「接続方式」`QComboBox`（暗黙的TLS / STARTTLS）と、「CA証明書ファイル」を選択する `QLineEdit` + `QPushButton`（`QFileDialog.getOpenFileName`）を追加する。接続方式を変更した場合は既存の「接続テスト必須」判定（`_connection_fields_changed`）の対象に含める | D-4, D-6 |
| F-11 | 接続テスト（`_test_connection` → `create_fetcher_for_credentials`）に新パラメータを伝播させる | F-10 |
| F-12 | `_TRASH_CANDIDATES` に英語圏の主要な慣用名を追加する | D-8 |
| F-13 | `tests/docker/dovecot` の設定を拡張し、平文ポート＋STARTTLS必須の設定、`LOGINDISABLED`（STARTTLS前）を強制する設定、自己署名証明書を使う設定の3構成を用意する（既存の暗黙的TLS構成と共存させる） | D-11 |

### **2.3 非機能要件・制約**

- レイヤー境界を守る: TLSモード・SASL PLAINの分岐と実装は `infrastructure/fetchers/generic_imap.py`（リネーム後）と `infrastructure/fetchers/imap_common.py` に閉じ込め、`usecases` 層・`domain` 層はホスト名・ポート・証明書パスをそのまま受け渡すだけの不透明な値として扱う。
- 証明書検証を無効化する設定・フラグ・環境変数は一切追加しない（OWASP Top 10: 暗号化の失敗対策）。
- パスワードは引き続き `keyring` のみに保存し、`ca_cert_path` のようなファイルパスであっても機密情報（パスワード・トークン）はDBへ書かない。
- `provider_type` の値変更はマイグレーションで一度だけ行い、アプリケーションコードが `"onamae_imap"` という文字列に依存している箇所（テストのフィクスチャ含む）を洗い出してから実施する。
- リネーム (`OnamaeImapFetcher` → `GenericImapFetcher`) は `vscode_renameSymbol` 相当の全参照更新を伴うため、実装時は影響ファイル（`context.py` / `__main__.py` / 各種テスト）を全て洗い出してから着手する。
- `CancelToken` によるキャンセル・リトライ方針など、Phase 1 / Phase 3.7 で確定済みの契約は変更しない。

---

## **3. タスク**

### **Group A: ドメイン層・フェッチャー**

- [ ] `infrastructure/fetchers/onamae_imap.py` を `infrastructure/fetchers/generic_imap.py` にリネームし、クラス名を `GenericImapFetcher` に変更する（`vscode_renameSymbol` 等で全参照を更新する）
- [ ] クラス docstring を「ID/パスワード認証を使う任意のIMAP4rev1サーバー向けの実装」に書き改め、Onamae固有の記述（既定値の由来）は「実サーバーでの検証例」として残す
- [ ] コンストラクタに `tls_mode: Literal["implicit", "starttls"] = "implicit"` と `ca_cert_path: str | None = None` を追加する
- [ ] `connect()` を分岐させる: `tls_mode="implicit"` は現行の `IMAP4_SSL` 経路、`tls_mode="starttls"` は `IMAP4` 接続 → `starttls(ssl_context=...)` → 以降は現行と同じ流れにする
- [ ] `ca_cert_path` 指定時に `ssl.create_default_context()` へ `load_verify_locations(cafile=...)` を適用するヘルパーを追加し、ファイル未存在・読み込み失敗を `ConfigError` として送出する
- [ ] STARTTLS完了後（または暗黙的TLS確立直後）に再取得した `CAPABILITY` に `LOGINDISABLED` が含まれる場合、SASL PLAINでの `authenticate()` に切り替えるログイン分岐を実装する
- [ ] `imap_common.wrap_imap_errors` に STARTTLS失敗・SASL認証失敗のエラー分類を追加する
- [ ] `_TRASH_CANDIDATES` に英語圏の主要な慣用名を追加する
- [ ] 既存の `iter_message_refs` / `iter_flags` / `iter_flags_since` / `delete_remote_message` 等の挙動・シグネチャは変更しない（回帰させない）

### **Group B: DBスキーマとリポジトリ**

- [ ] `migrations/005_generic_imap_connection.sql` を追加し、`accounts.tls_mode TEXT NOT NULL DEFAULT 'implicit'` と `accounts.ca_cert_path TEXT` を追加する
- [ ] 同マイグレーション内で `UPDATE accounts SET provider_type = 'imap' WHERE provider_type = 'onamae_imap'` を実行する
- [ ] `SqliteMessageRepository` の該当カラム定義（アカウント読み書きに関わる箇所）に `tls_mode` / `ca_cert_path` を追加する
- [ ] `tests/support/in_memory_repository.py` のアカウント関連実装に新カラムを反映する

### **Group C: usecases**

- [ ] `register_account` / `update_account` のシグネチャに `tls_mode` / `ca_cert_path` を追加し、`provider_type` の書き込み値を `"imap"` に変更する
- [ ] `list_accounts()` の戻り値に新カラムが含まれることを確認する（既存の「パスワードを返さない」制約は維持する）

### **Group D: GUIとコンポジションルート**

- [ ] `AppContext.create_fetcher()` / `create_fetcher_for_credentials()`、`__main__._account_fetcher()` を `GenericImapFetcher` と新カラムに対応させる
- [ ] `AccountDialog`（`settings_dialog.py`）に接続方式コンボボックス（暗黙的TLS / STARTTLS）を追加し、選択に応じてポート番号の既定プレースホルダ（993 / 143）を切り替える
- [ ] `AccountDialog` にCA証明書ファイル指定欄（`QLineEdit` + 参照ボタン）を追加し、空欄時は `ca_cert_path=None` として扱う
- [ ] `_connection_fields_changed()` の比較対象に `tls_mode` / `ca_cert_path` を含め、変更時は接続テストを再度必須にする
- [ ] `_test_connection()` / `create_fetcher_for_credentials()` 呼び出しに新パラメータを伝播させる
- [ ] `strings.py` に新規ラベル・ヒント文言を追加する

### **Group E: テスト**

- [ ] `tests/docker/dovecot` に、平文+STARTTLS必須構成・`LOGINDISABLED`（STARTTLS前）構成・自己署名証明書構成を追加する（既存の暗黙的TLS構成は維持する）
- [ ] `tests/integration/` に、STARTTLS接続・`LOGINDISABLED`からのPLAINフォールバック・カスタムCA証明書による接続の結合テストを追加する
- [ ] `tests/unit/test_fetcher.py` 等に、TLSモード分岐・CA証明書読み込み失敗時の `ConfigError`・SASL PLAINコールバックの単体テストを追加する
- [ ] `tests/unit/test_config.py` 相当（アカウント関連の検証テスト）に `tls_mode` / `ca_cert_path` のバリデーションテストを追加する
- [ ] `tests/gui/test_settings_dialog.py` に接続方式・CA証明書欄の入力とダイアログ再検証ロジックのテストを追加する
- [ ] `provider_type` 正規化マイグレーションの単体テスト（`tests/integration/test_migrator.py` 等）を追加する
- [ ] 全既存テスト（`onamae_imap.py` → `generic_imap.py` のリネームに伴うインポート修正含む）が壊れていないことを確認する

### **Group F: ドキュメント整合**

- [x] 開発計画書のロードマップ（6章）に本計画を **Phase 5.1** として位置づけ、Gmail/OAuth2を **Phase 5.2** として書き分ける
- [ ] 開発計画書中の `OnamaeImapFetcher` 表記・`provider_type` の例示値（`'onamae_imap'`）を本フェーズの変更内容に合わせて更新する
- [ ] `.github/copilot-instructions.md` の該当記述（該当があれば）を更新する
- [ ] `ruff check .` / `mypy .` / `pytest` を実行し、全テスト通過を確認する
