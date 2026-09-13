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

---

## 2. 結論・指摘事項一覧

| # | 分類 | 論点 | 推奨する決定 |
| :--- | :--- | :--- | :--- |
| **1** | Major | Gmail XOAUTH2 認証失敗時の挙動 | サーバーからの継続チャレンジ（`+`）に対し空行（`b""`）を返して正常に認証エラー（`NO`）を完結させる |
| **2** | Major | Azure AD と Google のリダイレクトURI差 | Google（`127.0.0.1`）と Azure AD（`localhost`）の動的ポート受付要件の差をプロバイダ定義で吸収可能にする |
| **3** | Major | コールバック待機時の UI 応答性 | `OAuthAuthorizeWorker`（`QThread`）と待機ダイアログを新設し、`CancelToken` でループバックサーバーを即時停止可能にする |
| **4** | Major | マニフェスト・reindex の新カラム追従 | [src/mail_dock/usecases/snapshots.py](src/mail_dock/usecases/snapshots.py) の既定値 `"onamae_imap"` 修正と新カラム追加、および [src/mail_dock/usecases/reindex.py](src/mail_dock/usecases/reindex.py) の復元タスクを明記する |
| **5** | Major | 5.2b における SQLite テーブル再構築 | SQLite 公式手順に沿い、外部キー一時無効化、子テーブル FK 付け替え、FTS5 トリガー再作成、FK 検査を確実に実施する |
| **6** | Minor | Gmail IMAP の `CONDSTORE` 非対応 | Gmail では `CONDSTORE` が使えないため、Phase 3.7 のフォールバック経路（全件/範囲フェッチ）を通ることを明記する |
| **7** | Minor | `X-GM-LABELS` の文字コード形式 | 日本語ラベルの modified UTF-7 を `decode_modified_utf7` でデコードし、UTF-8 文字列配列として永続化する |
| **8** | Minor | 通常 IMAP 移動時の `source_item_key` | `messages.source_item_key` はメッセージ作成（初回到達）時の一次識別子として不変とし、移動時は `message_folders` 側のみ更新する |
| **9** | Minor | アカウント削除時の Keyring クリーンアップ | アカウント削除時に `client_secret` や `refresh_token` などの孤児シークレットを確実に全削除する |
| **10** | Minor | テストファイルのリネーム漏れ防止 | `OnamaeImapFetcher` リネームに伴い [tests/unit/test_onamae_imap.py](tests/unit/test_onamae_imap.py) も [tests/unit/test_generic_imap.py](tests/unit/test_generic_imap.py) へリネームする |

---

## 3. 指摘事項の詳細

### 3.1 Gmail XOAUTH2 認証失敗時の継続チャレンジ（空行応答）ハンドリング
- **対象**: D-18, F-18, Group J
- **背景**:
  Google の XOAUTH2 仕様（RFC 7628 / Google OAuth 2.0 IMAP 仕様）では、アクセストークンの失効・無効化によって認証が失敗した場合、サーバーは通常の `NO [AUTHENTICATIONFAILED]` ではなく、**`+ {base64(JSON error)}` という継続チャレンジ（continuation challenge）** を返します。
  クライアントはこれに対して **空行（`\r\n` / `b""`）** を送り返す必要があり、空行を送って初めてサーバーが最終的な `NO` 応答を返して SASL 認証シーケンスが完結します。
- **リスク**:
  `imaplib.IMAP4.authenticate("XOAUTH2", callback)` を使用する際、コールバックが初回に SASL 文字列を返したあと、サーバーから継続チャレンジが届いて 2 回目のコールバックが呼ばれた場合に空行（`b""`）を返さないと、IMAP セッションがタイムアウトまでハングするか、予期せぬ例外でソケットが切断されます。
- **推奨する修正**:
  `GenericImapFetcher` の XOAUTH2 認証コールバックに、2 回目の呼び出し（サーバーが `+` エラーチャレンジを返してきた場合）に空行（`b""`）を送信するハンドリングを追加し、サーバーから正常に `NO` を受け取って `wrap_imap_errors` が `AuthenticationError` へ変換できるようにする。

---

### 3.2 Microsoft Entra ID（Azure AD）と Google のループバックリダイレクト URI 仕様差
- **対象**: D-22, F-38, Group H, Group N
- **背景**:
  D-22 ではリダイレクト URI を `http://127.0.0.1:{一時ポート}/` と規定しています。Google は `127.0.0.1` を強く推奨しエフェメラルポートを許可しますが、**Microsoft Entra ID（Azure AD）のパブリッククライアント（デスクトップアプリ）では、エフェメラルポートの動的受付を `http://localhost`（ホスト名）に対してのみ許可する仕様**（RFC 8252 準拠、Azure AD の制約）になっています。
  Azure AD アプリ登録で `http://127.0.0.1` を登録すると特定ポートの固定を求められ、動的ポート（`:0`）が使えません。
- **リスク**:
  Microsoft 365 連携時に動的ポートでのコールバック受付が Azure AD 側で拒否され、認証が成立しなくなります。
- **推奨する修正**:
  プロバイダ定義（許可リスト）において、リダイレクト URI のホスト部分を Google は `127.0.0.1`、Microsoft は `localhost`（または両方を許容）として切り替えられるように設計することを D-22 / F-38 に追記する。

---

### 3.3 ループバックコールバック待機時の UI ブロック防止と `CancelToken` 連携
- **対象**: D-23, Group H, Group K
- **背景**:
  `http.server.HTTPServer` によるコールバック待機（タイムアウト120秒）はブロッキング I/O です。これをメインスレッドで直接実行すると PySide6 のイベントループが最長 2 分間完全に停止し、OS から「応答なし」と判定されます。
  また、ユーザーがブラウザで認可を中断してアプリ側で「キャンセル」ボタンを押した場合の脱出経路が必要です。
- **リスク**:
  認可中の UI フリーズ、およびユーザーによる中断操作が効かなくなることによる UX 低下。
- **推奨する修正**:
  1. Presentation 層に `OAuthAuthorizeWorker`（`QThread` または既存のワーカーパターン）を新設する。
  2. 認可待機ダイアログ（プログレス表示、「ブラウザで認証を完了してください」「キャンセル」ボタン）を表示する。
  3. キャンセル時は `CancelToken` を介してループバックサーバーのソケットを `shutdown()` / `close()` して即座にワーカースレッドを終了させる構成を Group K に追加する。

---

### 3.4 マニフェスト復元・スナップショット記録の新カラム・既定値追従
- **対象**: Group B, Group I, セクション 4（主要成果物）
- **背景**:
  現行の [src/mail_dock/usecases/snapshots.py](src/mail_dock/usecases/snapshots.py#L40-L55) には、`"provider_type": str(account.get("provider_type", "onamae_imap"))` のハードコードが残っており、`_ACCOUNT_FIELDS` に新カラム（`tls_mode`, `ca_cert_path`, `auth_type`, `oauth_*`）が含まれていません。
  また、[src/mail_dock/usecases/reindex.py](src/mail_dock/usecases/reindex.py#L120-L140) の `_account_record()` もマニフェストから新カラムを読み取って DB レコードへ復元する処理が未定義です。
- **リスク**:
  DB 再構築（`reindex`）時に新アカウント属性（TLS設定やOAuth連携設定）が失われ、再接続不能になります。
- **推奨する修正**:
  - Group B に「[src/mail_dock/usecases/snapshots.py](src/mail_dock/usecases/snapshots.py) の `_ACCOUNT_FIELDS` / `_account_event()` に `tls_mode`, `ca_cert_path` を追加し、既定値の `"onamae_imap"` を `"imap"` に修正する」を追加する。
  - Group B / Group I に「[src/mail_dock/usecases/reindex.py](src/mail_dock/usecases/reindex.py) の `_account_record()` で新カラムを正しく復元できるように拡張する」を追加する。
  - セクション 4 の主要成果物テーブルに [src/mail_dock/usecases/snapshots.py](src/mail_dock/usecases/snapshots.py) と [src/mail_dock/usecases/reindex.py](src/mail_dock/usecases/reindex.py) を追加する。

---

### 3.5 5.2b における SQLite `messages` テーブル再構築時の外部キー・FTS5・トリガー保全手順
- **対象**: F-27, Group M
- **背景**:
  5.2b の finalizer で `messages` テーブルから `folder_id` や `uid` 等の列を削除して再構築する際、SQLite では `messages(id)` を外部キー参照している子テーブル（`message_contents`, `audit_log`, `pst_import_items`）および `messages` に紐づく FTS5 トリガー（`insert_message_fts`, `update_message_fts`, `delete_message_fts`）が存在します。
- **リスク**:
  単純に `CREATE TABLE new_messages` → `INSERT INTO ...` → `DROP TABLE messages` を行うと、トリガーが失われたり外部キー参照整合性が破壊されたりします。
- **推奨する修正**:
  F-27 / Group M に、SQLite 公式のテーブル再構築手順（`PRAGMA foreign_keys=OFF;` → 新テーブル作成 → データ移行 → 旧テーブル削除 → リネーム → インデックス/トリガー再作成 → `PRAGMA foreign_key_check;`）に沿って実行する旨を明記する。

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

### 3.8 通常 IMAP メール移動（MOVE）と `messages.source_item_key` の不変性
- **対象**: D-29, F-28, F-32
- **背景**:
  D-29 で通常 IMAP の `source_item_key` は `imap:{folder_key}:{uidvalidity}:{uid}` と規定されています。
  通常 IMAP でメールが Folder A から Folder B へ移動（MOVE）された場合、`message_folders` 側で所属レコードが Folder B（新 UID）へ更新されますが、`messages.source_item_key` は初回取得時のキーを保持し続けるのかどうかが自明ではありません。
- **リスク**:
  移動時に `messages.source_item_key` を書き換えるとマニフェスト履歴や EML 紐付けの不変条件が揺らぐ。
- **推奨する修正**:
  「`source_item_key` はメッセージ作成（初回到達）時の一次識別子であり、メールが別フォルダへ移動しても `messages.source_item_key` は不変とし、`message_folders` 側の `folder_id` / `uid` / `remote_state` のみが更新される」ことを D-29 に明記する。

---

### 3.9 アカウント削除時における Keyring シークレットの一括クリーンアップ
- **対象**: D-21, F-17, Group I
- **背景**:
  現行の `delete_password(account_id)` はパスワードのみを削除します。OAuth アカウントを削除または再連携解除する際、`client_secret` や `refresh_token` が Keyring 内に孤児として残存する可能性があります。
- **リスク**:
  不要な秘密情報の残存、再登録時の意図しないトークン再利用。
- **推奨する修正**:
  `BaseCredentialStore` に `delete_all_secrets(account_id: str) -> None`（または特定のアカウントに属するシークレットを全削除するメソッド）を追加するか、`delete_account` ユースケースで `delete_secret` を全キーに対して呼び出す手順を Group I に追加する。

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
- [ ] **D-21 / F-17 / Group I**: アカウント削除時における Keyring 内シークレット一括クリーンアップを追記
- [ ] **D-22 / F-38**: Microsoft Entra ID 用にリダイレクト URI のホスト名（`localhost`）をプロバイダ定義で設定可能にする仕様を追記
- [ ] **D-23 / Group K**: Presentation 層の `OAuthAuthorizeWorker`、非同期待機ダイアログ、`CancelToken` によるループバックサーバー停止を追記
- [ ] **D-29**: 通常 IMAP でのフォルダ移動時における `messages.source_item_key` の不変性を注記
- [ ] **F-18 / Group J**: XOAUTH2 認証失敗時の 2 回目コールバック（継続チャレンジ `+`）に対する空行（`b""`）送信ハンドリングを追記
- [ ] **F-27 / Group M**: 5.2b の `messages` 再構築時における SQLite 公式手順（外部キー OFF、子テーブル FK、FTS トリガー再作成、FK チェック）を明記
- [ ] **Group A / Group E**: [tests/unit/test_onamae_imap.py](tests/unit/test_onamae_imap.py) のリネームタスクを追記
- [ ] **Group B / Group I / セクション 4**: [src/mail_dock/usecases/snapshots.py](src/mail_dock/usecases/snapshots.py) の新フィールド対応・既定値修正、および [src/mail_dock/usecases/reindex.py](src/mail_dock/usecases/reindex.py) の復元処理を主要成果物とタスクに追記
