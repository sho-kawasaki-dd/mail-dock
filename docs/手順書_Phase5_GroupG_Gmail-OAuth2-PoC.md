# 手順書: Phase 5 Group G（Gmail OAuth2 PoC・方式ブロッカー判定）

対象: [実装計画書_Phase5_マルチプロトコル対応.md](./実装計画書_Phase5_マルチプロトコル対応.md) の **Group G**（G-1〜G-8）。
本書はGroup Gの各タスクを実際にどう検証するかの手順のみを記述する。要件・意思決定（D-13, D-18, D-19, D-25, D-26等）の根拠は本書ではなく実装計画書を正とする。

Group Gの目的は、**Group H〜L（本実装）に着手する前に**、以下が成立するかを実機で確認することである。

1. サードパーティ依存を追加せず、標準ライブラリのみでAuthorization Code + PKCE + ループバックリダイレクトが成立する
2. 得られたトークンでGmail IMAPへXOAUTH2接続し、`X-GM-MSGID` / `X-GM-THRID` / `X-GM-LABELS` を含む`UID FETCH`が成功する
3. Docker Dovecotでの結合テストとしてXOAUTH2を再現できるか、それとも手動確認止まりにするか（D-26）を判定する

検証に使うPoCスクリプトは [tools/gmail_oauth_poc/gmail_poc.py](../tools/gmail_oauth_poc/gmail_poc.py) に用意した。**このスクリプトはアプリ本体（`src/mail_dock/`）には含めない使い捨てツールであり、CLIのOAuthサブコマンド追加禁止（D-28）には抵触しない。**

---

## 事前準備・安全上の注意

- 秘密情報（`client_secret` / `access_token` / `refresh_token`）は環境変数からのみ読み込み、ディスク・git・ログへ書き込まない。スクリプトはこれらの値そのものを標準出力へ出さず、長さや有無（`present=True/False`）のみ表示する。
- ターミナル出力やチャット・Issueへ、コード・トークンの値そのものを貼り付けない。誤って貼り付けた場合は直ちに https://myaccount.google.com/permissions で当該アプリのアクセスを取り消す。
- PoCで使うGoogleアカウントは、私物の本番メールアカウントではなく検証用アカウント（またはテスト用に許容できるアカウント）を推奨する。

---

## G-1: Google Cloud プロジェクトとOAuth同意画面の準備

1. [Google Cloud Console](https://console.cloud.google.com/) で新規プロジェクトを作成する（例: `mail-dock-poc`）。
2. 左メニュー「APIとサービス」→「OAuth同意画面」を開き、User Typeで **「外部 (External)」** を選択して作成する。
3. アプリ情報（アプリ名、ユーザーサポートメール、デベロッパー連絡先）を入力して保存する。
4. 「スコープを追加または削除」から `https://mail.google.com/`（制限付きスコープ）を検索して追加し、保存する。
5. 「テストユーザー」にPoCで実際にログインするGoogleアカウントを追加する（公開ステータスが「テスト中」の間はここに登録したアカウントのみ認可できる）。
6. 「認証情報」→「認証情報を作成」→「OAuthクライアントID」で、アプリケーションの種類 **「デスクトップアプリ」** を選び、クライアントID・クライアントシークレットを発行する。

**完了条件**: 同意画面にスコープ`https://mail.google.com/`が設定され、テストユーザーが登録され、デスクトップアプリ用クライアントID/Secretが発行されていること。

---

## G-2 / G-3: PKCE認可フロー・トークン交換・XOAUTH2接続の実行

1. 発行したクライアント情報とテストユーザーのメールアドレスを環境変数に設定する（値は貼り付け履歴に残さないよう、シェルの当該セッションのみで設定する）。

   ```bash
   export GMAIL_CLIENT_ID="xxxxxxxx.apps.googleusercontent.com"
   export GMAIL_CLIENT_SECRET="xxxxxxxx"
   export GMAIL_USER_EMAIL="test-user@gmail.com"
   ```

2. PoCスクリプトを実行する。

   ```bash
   python tools/gmail_oauth_poc/gmail_poc.py
   ```

3. スクリプトが既定ブラウザを開くので、対象アカウントでログインし、同意画面（「Google はこのアプリを確認していません」の警告が出た場合は「詳細」→「(アプリ名)に移動」を選択）を完了する。
4. ブラウザが `http://127.0.0.1:{一時ポート}/` へリダイレクトされ、スクリプトが`state`検証・認可コード取得（G-2）→ トークン交換（G-2後半）→ Gmail IMAPへのXOAUTH2接続・`LIST`・`UID FETCH`（G-3）を自動的に進める。
5. 標準出力に表示される以下を確認する。
   - `[G-2] Authorization code received and state verified.`
   - `[G-2] Token exchange succeeded (access_token length=..., refresh_token present=True)`
     - `refresh_token present=False` の場合は、[Googleアカウントの権限ページ](https://myaccount.google.com/permissions)で当該アプリの連携を解除してから再実行する（`prompt=consent`により本来は毎回発行されるはずのため）。
   - `[G-3] XOAUTH2 authenticate: OK`
   - `[G-3] LIST returned N folder(s):` に `[Gmail]/All Mail`（または `[Gmail]/すべてのメール`）や `[Gmail]/Trash` 等が含まれること
   - `[G-3] UID FETCH ... response:` の行に `X-GM-MSGID` / `X-GM-THRID` / `X-GM-LABELS` がすべて含まれること（`WARNING: ... not found` が出ないこと）

**完了条件（G-2）**: ブラウザでの同意後、ローカルポートへリダイレクトされ、`state`一致・認可コード取得・トークン交換（`access_token`取得）まで成功すること。
**完了条件（G-3）**: `imap.gmail.com:993`へのXOAUTH2認証が`OK`となり、`UID FETCH`応答に`X-GM-MSGID` / `X-GM-THRID` / `X-GM-LABELS`が含まれること。

### トラブルシューティング

| 症状 | 対処 |
| :---- | :---- |
| ブラウザが自動で開かない | スクリプトが表示するURLを手動でブラウザへ貼り付ける |
| `state mismatch` で中断 | ブラウザの多重タブ・多重実行を疑い、単一のスクリプト実行・単一タブでやり直す |
| 120秒でタイムアウト | 同意画面の操作が120秒を超えている。スクリプトを再実行し、手早く同意を完了する |
| `invalid_grant` (token exchange失敗) | 認可コードは1回しか使えない。スクリプトを最初から再実行する |
| `refresh_token present=False` | 既存の連携が残っている。https://myaccount.google.com/permissions で解除してから再実行する |
| XOAUTH2が`NO`で失敗 | スコープに`https://mail.google.com/`が付与されているか、テストユーザー登録が正しいかをG-1に戻って確認する |

---

## G-4: 7日間トークン失効の扱い

- OAuth同意画面が外部・テスト中の間、`https://mail.google.com/`を含むrefresh tokenは**7日で失効する**（Google公式仕様）。これは実装のブロッカーとしては扱わない（D-25）。
- 本タスクの完了条件は、以下を**READMEに記録すること**であり、7日間の実待機は必須にしない。
  1. 7日失効が公式仕様である旨
  2. 失効時に`invalid_grant`が返り、UIで「Googleと再連携」を促す想定の受入条件（実装はGroup Jで行う）
- 任意で長期検証したい場合のみ、G-2/G-3のPoCで取得した`refresh_token`を安全な一時変数に保持し、7日後に`grant_type=refresh_token`でのトークン更新が`invalid_grant`になることを確認してよい（必須ではない）。

**完了条件**: READMEへ7日失効の仕様と再連携の受入条件を記録すること。

---

## G-5: 公開ステータス・審査・CASAの整理

- 本タスクはコードでの検証を伴わない、**運用方針の整理**である。
- 次の区分を明文化する（README・引き継ぎメモ等、実装計画書のGroup Pドキュメントタスクへ引き継ぐ）。
  - 自己利用・少数の既知ユーザー（テストユーザー登録の範囲内、100人未満）向け運用は、外部・テスト中のままで良い
  - 不特定多数への一般公開は、Googleの確認（検証済みアプリ）とCASAセキュリティ評価（有償・年次）が必要になり、本PoCの範囲外
- 技術的な接続可否（G-2/G-3）と、この運用判断を混同しないこと。

**完了条件**: 上記の区分をドキュメント（README等）に整理して記載すること。

---

## G-6: Gmailのスロットル応答の扱い

- `[THROTTLED]` / `[OVERQUOTA]` / `[LIMIT]`等への意図的な到達（大量リクエストでの再現）は**行わない**（D-19）。
- 対応:
  1. Googleの公式ドキュメント、または開発中に偶然観測した実際のレスポンス文字列があれば、そのままfixture化する
  2. 現時点で実観測が無い場合は、合成したIMAP応答文字列（例: `NO [OVERQUOTA] Quota exceeded.`）を使い、`wrap_imap_errors`が`TransientError`へ分類することを単体テストで固定する（実装はGroup J/Oで行う）
- 本タスク自体はコード実装を伴わず、「意図的な帯域制限到達は行わない」方針の確認と、観測済みレスポンスの記録のみを行う。

**完了条件**: 観測できた実レスポンス（あれば）を記録し、無い場合は合成応答での検証方針を確認すること。

---

## G-7: Docker DovecotでのXOAUTH2結合テスト再現性の確認

1. `tests/docker/compose.yaml`のDovecotイメージ（`dovecot/dovecot:2.3.21.1`、[/memories/repo/phase5-group-e-tls-docker-findings.md](../../memories) 参照）で`auth_mechanisms = xoauth2`を有効化する設定を試作する。
   - Dovecotの`xoauth2`メカニズムは、外部トークン検証スクリプト（`mail_plugins`の`imap_xoauth2`等）やLDAP/PAM連携を前提とすることが多く、Googleの実トークンをそのまま検証させるには追加のプロキシ実装が必要になる場合がある。
2. 最小構成として、Dovecotの`auth_mechanisms`に`xoauth2`を追加し、`passdb`にダミーの検証ロジック（固定トークン文字列を許可する等）を設定し、`GenericImapFetcher`のXOAUTH2認証コードパス（SASL初期応答＋継続チャレンジ処理）だけを結合テストとして通せるか確認する。
3. 再現できた場合: Dockerサービスとして`tests/docker/compose.yaml`へ追加し、`tests/integration/`にXOAUTH2結合テストを追加する方針をGroup Jへ引き継ぐ。
4. **再現できない場合（D-26の想定どおりの可能性が高い）**: 無理に作り込まず、以下の代替方針を確定する。
   - `GenericImapFetcher`のXOAUTH2分岐は、既存の`FakeImap`（`tests/unit/test_generic_imap.py`）を使った単体テストで、SASL文字列の組み立て・継続チャレンジへの空行応答・`NO`応答の`AuthenticationError`変換を決定的に検証する
   - 実Gmailサーバーに対する検証は、本書のPoCスクリプトによる**手動確認**に留める（Phase 4レビュー修正案の「ソケット切断の実際の再現は狙わない」と同じ考え方）

**完了条件**: Docker Dovecotでの`xoauth2`再現可否を判定し、再現できない場合は上記の代替方針（Fake単体テスト＋手動確認）で合意すること。

---

## G-8: PoC結果の実装計画書への反映

Group G完了後、以下を[実装計画書_Phase5_マルチプロトコル対応.md](./実装計画書_Phase5_マルチプロトコル対応.md)へ反映し、必要なら該当D項目を更新する。

| 反映先 | 反映する内容 |
| :---- | :---- |
| D-13 | IMAP+XOAUTH2で`X-GM-MSGID`/`X-GM-THRID`/`X-GM-LABELS`が実機取得できたことの実測結果 |
| D-18 | 実際に観測した`X-GM-LABELS`のフォーマット（modified UTF-7の実例）と、`decode_modified_utf7`適用要否の再確認結果 |
| D-19 | G-6で観測できたスロットル応答の実例（あれば）、無ければ合成応答方針の確定 |
| D-25 | G-4で記録した7日失効の受入条件・README記載内容 |
| D-26 | G-7で判定したDocker結合テストの再現可否と、再現できない場合の代替方針 |

その後、Group Gの全チェックボックス（G-1〜G-8）を実装計画書上でチェックし、Group H（OAuth2基盤の本実装）へ進む。

---

## 参照

- PoCスクリプト: [tools/gmail_oauth_poc/gmail_poc.py](../tools/gmail_oauth_poc/gmail_poc.py)
- 実装計画書: [実装計画書_Phase5_マルチプロトコル対応.md](./実装計画書_Phase5_マルチプロトコル対応.md)
- レビュー修正案（XOAUTH2継続チャレンジの根拠）: [実装計画書_Phase5_レビュー修正案.md](./実装計画書_Phase5_レビュー修正案.md) 3.1節
