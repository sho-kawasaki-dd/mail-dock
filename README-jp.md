# mail-dock

mail-dockは、IMAPサーバー上のメールをローカルの外付けドライブに`.eml`ファイルとしてバックアップし、オフラインで閲覧するデスクトップアプリケーションです。ローカルのEMLファイルと永続マニフェストが正本であり、SQLiteデータベースは再構築可能なメタデータキャッシュです。

## ストレージとバックアップの前提条件

- ストレージルートにはブロックレベル暗号化を推奨しますが、必須ではありません。セットアップウィザードでは、利用者の申告として`encrypted`、`unencrypted`、`unknown`のいずれかを記録し、その状態を継続して表示します。アプリケーションが暗号化製品を検出したり、ストレージが暗号化されていることを証明したりすることはありません。
- メールの認証情報は、PC側の承認済みOS資格情報ストア（またはプロセス内だけに保持する`session_only`モード）に保管してください。ストレージルートには絶対に置かないでください。
- [3-2-1バックアップルール](docs/ローカルメールバックアップand閲覧アプリ開発計画書.md#57)に従ってください。少なくとも3つのコピーを2種類の異なるメディアに保存し、そのうち1つは遠隔地に保管します。
- デバイス暗号化されたボリュームの場合、マウント済みストレージルートを通常のファイルコピーでバックアップできます。EMLファイル、マニフェスト、メタデータデータベースを含め、ルートの構造全体を一緒に保管してください。VeraCryptのファイルコンテナの場合は、mail-dockを終了してボリュームをアンマウントし、コンテナファイル全体をコピーしてください。差分バックアップを使ったり、コンテナをマウントしたままコピーしたりしないでください。バックアップ先の暗号化が元データより弱い場合、警告を明示的に受け入れない限り`db_backup_to_local_disk`を無効にしてください。

## ストレージ暗号化ガイド

### ストレージの3つの安全性レベル

| レベル | 推奨事項 | 例 | 運用上の意味 |
| --- | --- | --- | --- |
| 対応 | 推奨 | BitLocker To Go、VeraCrypt、LUKS、暗号化APFS | マウント後は通常のファイルシステムとして動作します。アプリケーションはロック、置換、fsync、WAL、大小文字の扱い、長いパスに関するストレージ互換性セルフテストを引き続き実行します。 |
| 非対応 | セルフテストで問題なしと報告されない限り使用しないでください | Cryptomator、gocryptfs、rclone crypt、Boxcryptorなどの仮想ファイルシステム | アトミック置換、排他ロック、fsync、SQLiteの動作は実装に依存します。製品名は検出されません。機能テストに失敗した場合は`UNSUPPORTED`または`DEGRADED`として報告されます。 |
| 暗号化なし | 自己責任 | 暗号化されていないローカルまたはリムーバブルボリューム | `unencrypted`を明示的に申告してください。mail-dockは選択を許可しますが、警告を常時表示し、初回同期の直前に一度確認を求めます。 |

セルフテストは互換性の確認であり、セキュリティを保証するものではありません。一度のI/O操作に成功しても、完全なアトミック性、耐久性、WALの安全性を証明することはできません。このテストはストレージルート配下の`tmp/`に一時ファイルを作成し、本番のロック、データベース、EML、マニフェストの各ファイルは変更しません。

### OSごとのセットアップ

- **Windows Pro:** リムーバブルドライブにはBitLocker To Goを使用してください。ボリュームのBitLockerを有効化し、回復キーはドライブとは別に保管してから、mail-dockの起動前にボリュームをロック解除します。
- **Windows Home:** 通常のマウント済みファイルシステムとして使えるVeraCryptなどのブロックレベル暗号化を使用してください。Windows Homeでも、BitLockerで暗号化済みのドライブをロック解除して読み書きできますが、Proと同じ方法でBitLocker暗号化を作成・管理することはできません。
- **macOS:** ディスクユーティリティまたは同等のシステム手順で作成した、暗号化APFSの外付けボリュームを使用してください。mail-dockの起動前にロック解除してマウントし、回復情報はアーカイブ用ドライブとは別に保管します。
- **Linux:** デバイスまたはボリュームにLUKSを使用し、ロック解除したボリューム内に通常のファイルシステムをマウントしてください。マウント後のボリュームが通常のローカルファイルシステムとして動作する場合、VeraCryptも使用できます。

### VeraCryptの要件

専用の外付けSSDには、ファイルコンテナではなくデバイス全体の暗号化を使用してください。他の用途とドライブを共用するためにファイルコンテナが必要な場合、次の4条件をすべて満たす必要があります。

1. 固定サイズのコンテナを使用してください。動的コンテナは、アプリケーションが実際の上限を認識できないままホストファイルシステムの空き容量が尽きるおそれがあるため使用しないでください。
2. コンテナをクラウド同期フォルダーやネットワーク共有上に置かないでください。
3. スクリーンセーバー時やアイドル時間経過時を含め、自動アンマウントを無効にしてください。
4. VeraCryptボリュームヘッダーを別途バックアップし、復旧手順が機能することを確認してください。

mail-dockの実行中は、保管庫のアイドル時自動ロックとVeraCryptの自動アンマウントを無効にしてください。数時間かかる初回同期も、これらの動作によって物理ドライブの取り外しと同じように中断されることがあります。シャットダウンまたはドライブを持ち運ぶ前に同期を停止し、mail-dockを終了して、暗号化ボリュームを明示的にアンマウントしてください。

## ストレージドライブの安全な取り外し

ドライブを物理的に取り外す前に、「ストレージ」メニューの「ストレージを安全に取り外す」を実行してください。実行中の同期／検証ワーカーがバッチ境界で停止するのを待ち、WALをチェックポイント処理し、すべてのSQLite接続とログハンドルを閉じ、ストレージロックを解放してから、ドライブを取り外せる状態になったことを通知します。同期または検証中にドライブを抜かないでください。この操作が「安全に取り外せます」の状態になるまで待ってください。

Windowsでは、リムーバブルドライブのポリシーを「高パフォーマンス」ではなく「クイック取り外し」（ディスクの管理／デバイスマネージャーのポリシータブ）に設定してください。クイック取り外しはデバイスのWindows書き込みキャッシュを無効にし、`os.replace`とfsyncの動作をmail-dockの想定に合わせます。予期しない切断のよくある原因となるため、アーカイブドライブをUSBハブ経由またはバスパワーで使用することは避けてください。

## GmailアカウントとGoogle OAuthの確認状況

mail-dockは独自のGoogle OAuthクライアントを同梱したり、中継したりしません。各利用者が独自のGoogle Cloudプロジェクトを作成してOAuth同意画面を設定し、自身のGmailアカウント（複数可）をテストユーザーとして登録します（手順を追ったコンソール説明はPhase 5.2aのクライアント設定タスクで追加予定です。この方針の検証に使用した手順は[手順書_Phase5_GroupG_Gmail-OAuth2-PoC.md](docs/手順書_Phase5_GroupG_Gmail-OAuth2-PoC.md)にあります）。

Google公式の[「確認が不要な場合」](https://support.google.com/cloud/answer/13464323)のガイダンスでは、自分や既知の利用者向けの運用と一般公開が明確に区別されています。

- **自分で使う場合、または少数の既知の利用者（mail-dockの想定用途）:** 同意画面の公開ステータスを**「テスト」**にし、自分（および信頼できる利用者、最大100人）のみをテストユーザーに追加します。これはGoogle独自の免除区分（「個人使用アプリ」および「開発／テスト／ステージングアプリ」）に該当するため、**Googleの確認審査もCASAセキュリティ評価（有料・年次）も不要です**。
- **一般公開:** 不特定または無制限の利用者に向けて同意画面を「本番環境」に公開する場合、Googleの確認審査が必要となり、制限付きスコープ`https://mail.google.com/`にはCASA評価も必要です。mail-dockはこの方法を採用しません。このプロジェクトの対象範囲外です。

「テスト」ステータスを維持する場合の注意点:

- サインインのたびにGoogleの「このアプリはGoogleで確認されていません」という警告画面が表示されます（「詳細」→「（アプリ名）に移動」を選択して続行してください）。
- 制限付きスコープ`https://mail.google.com/`のリフレッシュトークンは、同意画面が「テスト」ステータスの間は**7日後に有効期限が切れます**。期限切れ後にIMAP接続を試みると`invalid_grant`で失敗し、その場合はGoogleでアカウントを再認証する必要があります。
- 「テスト」ステータスの同意画面に登録できるテストユーザーは最大100人です。

## Microsoft 365およびOutlook.comアカウント

Microsoft IMAPでは、利用者自身が登録したMicrosoft Entraアプリケーションを使ってOAuth2認証を行います。Azureポータルでアプリ登録を作成し、対象アカウント（職場または学校のアカウント、個人用Microsoftアカウント、または両方）に合うアカウントの種類を選択してください。「認証」で「モバイルおよびデスクトップ アプリケーション」プラットフォームを追加し、リダイレクトURIに`http://localhost`を指定します。mail-dockは、動的に割り当てられるポートとともにこの登録済みループバックURIを使用します。この公開デスクトップクライアントにクライアントシークレットを設定しないでください。

「APIのアクセス許可」で、Office 365 Exchange Onlineの委任されたアクセス許可`IMAP.AccessAsUser.All`を追加し、テナントで必要な場合は同意を付与します。アプリはアクセストークンを更新できるように`offline_access`も要求します。mail-dockでOAuth2と「Microsoft 365 / Outlook.com」を選択し、アプリケーション（クライアント）IDを入力します。職場／学校アカウントでは`organizations`、個人用Outlook.comアカウントでは`consumers`を選択するか、テナントIDを入力してから、ブラウザーで認証します。既定のIMAPエンドポイントは、暗黙的TLSを使用するポート993の`outlook.office365.com`です。

サポート対象はサインイン中の利用者自身のメールボックスのみです。共有メールボックスと委任アクセスは対象外です。認証ユーザーとは別のメールボックスアドレスを代わりに入力しないでください。

## Windowsへのインストール

プロジェクトのGitHub Releasesから`mail-dock-{version}-setup.exe`をダウンロードして実行します。既定ではユーザーごとのインストール（`%LOCALAPPDATA%\Programs\mail-dock`、管理者権限不要）です。特権ダイアログで全ユーザー向けを選択するか、`/ALLUSERS`を指定すると、`Program Files`にインストールできます。インストーラーはネットワークにアクセスしません。

インストーラーと実行ファイルは**コード署名されていない**ため、Windows SmartScreenに「発行元不明」の警告が表示されることがあります。同じリリースにある`SHA256SUMS.txt`でファイルを検証したうえで、「詳細情報」から「実行」を選択してください。

配布される`mail-dock.exe`はGUI専用です。コマンドラインのサブコマンド（`sync`、`verify`、`reindex`など）は開発環境での`uv run mail-dock ...`からのみ利用できます。インストール済み実行ファイルが受け付けるのは`gui`（既定）と`self-check`だけです。`mail-dock.exe self-check --output result.json`を実行すると、インストール済み環境の読み取り専用診断を行います。同じチェックは「ヘルプ」>「バージョン情報」からも実行できます。

### アンインストール

アンインストールするとプログラムファイルが削除されます。ユーザーごとのインストールの場合、アンインストーラーはmail-dockの設定ファイル、アプリケーションログ、およびレジストリキー`HKCU\Software\mail-dock\mail-dock`も削除するか確認します。既定では保持されます。ストレージルート内のメールデータ（EMLファイル、`metadata.db`、マニフェスト）とWindows資格情報マネージャーに保存された認証情報は削除されません。設定ディレクトリが安全だと確認できない場合（たとえば、その中にストレージルートがある場合）、何も削除されません。サイレントアンインストールまたは全ユーザー向けアンインストールでは、各ユーザーの設定はすべて保持されます。

## ソースからのビルド

インストーラーのビルドには、Windows、Python 3.13、uv、MSYS2（UCRT64および`zstd`）、Windows SDK（`mt.exe`）、Inno Setup 6.3以降が必要です。`packaging/qt/qt-source.lock.json`に記載された固定バージョンのQt `qtbase`および`qtwebengine`ソースアーカイブをダウンロードし、次を実行してください。

```powershell
pwsh -NoProfile -File .\tools\build_windows.ps1 `
  -QtLicenseSource <path-to-qtbase-everywhere-src-*.tar.xz> `
  -QtWebEngineSource <path-to-qtwebengine-everywhere-src-*.tar.xz> `
  -QtWebEngineSha256 <sha256-from-the-lock-file> `
  -CompileInstaller
```

スクリプトは固定バージョンのMSYS2パッケージからreadpstを取得し、ライセンスを収集し、PyInstallerのonedirイメージを`dist/mail-dock/`にビルドします。続いて対応するQtソースを取得し、バンドル（`self-check --require-keyring`を含む）を検証して、インストーラーを`dist/`にコンパイルします。`v{version}`タグ（`mail_dock.__version__`と一致必須）をプッシュすると、リリースワークフローが起動し、GitHubドラフトリリースが作成されます。

### 対応ソースコード

mail-dockはGPL-3.0-or-laterです。各リリースではインストーラーとともに、`mail-dock-{version}-src.tar.gz`（このリポジトリ）、`mail-dock-{version}-readpst-corresponding-source.zip`（readpst/libpstおよび同梱のMSYS2ランタイムDLL）、`mail-dock-{version}-qt-corresponding-source.zip`（QtおよびPySide6。ハッシュ値は同梱の`QT-SOURCE.md`に記載）を提供します。インストール先の`licenses`フォルダーと`THIRD-PARTY-LICENSES.md`には、ライセンス本文と通知が含まれます。

## 開発環境のセットアップ

必要なもの: Python 3.13、[uv](https://docs.astral.sh/uv/)、Git。リポジトリのルートで次を実行します。

```sh
uv sync
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -m "not docker and not gui"
```

既定のテストコマンドはDockerベースの統合テストとGUIテストを除外するため、Windowsのモックベースの開発環境に適しています。

## PSTコンバーターとライセンス

PSTのインポートでは、MSYS2 UCRT64パッケージ`mingw-w64-ucrt-x86_64-libpst`に含まれる`readpst`および`lspst`プログラムを、独立したプロセスとして実行します。コンバーターはPythonプロセスに組み込まれず、アプリケーションが元のPSTを変更することもありません。PSTからEMLへの変換ではメッセージを再構成するため、インポート後も元のPSTを長期バックアップに残してください。

Windowsでは、UCRT64環境のMSYS2とWindows SDK（`mt.exe`用）をインストールし、次のコマンドでコンバーターとランタイムDLLを取得します。

```powershell
pwsh -NoProfile -File .\tools\fetch_readpst.ps1 -Msys2Root C:\msys64
```

このスクリプトは、インストール済みMSYS2パッケージのメタデータを読み取り、コンバーターとDLL依存関係を`vendor/readpst/`にコピーします。追跡対象の`readpst.exe.manifest`（`activeCodePage=UTF-8`および`longPathAware=true`）を適用し、パッケージのコミットに固定された対応libpstソースアーカイブをダウンロードします。`readpst-artifacts.json`と`SHA256SUMS`を書き出します。マニフェストには、パッケージのバージョン、ライセンス、ソースURL、Windowsマニフェストリソースの適用前後のSHA-256値が含まれます。

GPL-2.0-or-laterの通知は`vendor/readpst/COPYING`にあります。リリース準備時にソースアーカイブをURLだけの参照に置き換えないでください。リリースワークフローは、コンバーターバイナリ、GPL通知、対応ソースアーカイブ、来歴マニフェスト、チェックサムがすべて存在することを検証し、必須ファイルが欠けている場合はリリースを失敗させます。

## GUI

次のいずれかのコマンドでデスクトップアプリケーションを起動します。

```sh
uv run mail-dock
uv run mail-dock gui
```

サブコマンドを指定せずに最初のコマンドを実行するとGUIが起動します。有効なストレージルートが設定されていない場合は、GUIのセットアップウィザードが表示されます。

GUIテストはGUIマーカーを有効にしてローカルで実行できます。ヘッドレスLinux環境ではQtのオフスクリーンプラットフォームを使用してください。

```sh
MAILDOCK_GUI=1 QT_QPA_PLATFORM=offscreen uv run pytest -m gui
```

Dockerを必要としないすべてのテスト（GUIテストを含む）を実行するには、次のコマンドを使います。

```sh
MAILDOCK_GUI=1 QT_QPA_PLATFORM=offscreen uv run pytest -m "not docker"
```

## WSLでのDockerテスト

GreenMailおよびDovecotの統合テスト環境は、WSL/Linuxから起動してください。

```sh
docker compose -f tests/docker/compose.yaml up -d
MAILDOCK_DOCKER=1 uv run pytest -m docker
docker compose -f tests/docker/compose.yaml down
```

サービスはGreenMailをIMAP `3143`／IMAPS `3993`、DovecotをIMAP `3144`／IMAPS `3994`で公開します。どちらもテストアカウント`testuser`、パスワード`password`を使用します。Dovecotには`Sent`、`Drafts`、`Trash`のSPECIAL-USEメールボックスに加え、区切り文字`.`を使う日本語の階層`受信トレイ.請求書`も用意されています。

アプリケーションのCLIでは`uv run mail-dock migrate`または`uv run mail-dock verify`を使用します。`--storage-root`オプションでアーカイブルートを指定します。`migrate`はデータベースのマイグレーションを適用し、`verify`は読み取り専用の整合性チェックを実行します。

`verify`では`--mode quick|range|full|orphans|manifest`を指定できます（既定値は`quick`）。

```sh
uv run mail-dock verify --storage-root D:\mail-archive --mode quick
uv run mail-dock verify --storage-root D:\mail-archive --mode full --account main-onamae
uv run mail-dock verify --storage-root D:\mail-archive --mode orphans
uv run mail-dock verify --storage-root D:\mail-archive --mode manifest
```

`quick`は各メッセージのファイルが存在し、記録されたサイズと一致することを確認します。`range`は最後のチェックポイント以降に書き込まれたEMLファイルだけを再ハッシュします。`full`はすべてのEMLを再ハッシュし、対象を絞るための`--account`を指定できます。`orphans`はデータベースに登録されていないEMLファイルをスキャンします。`manifest`はマニフェストのチェックサムを検証し、末尾の不完全なレコードを修復します。

`reindex`は`metadata.db`を破棄し、EMLファイルと永続マニフェストから再構築します。実行前に確認を求めます。再構築するアカウントを絞る場合は`--account`を指定できます。

```sh
uv run mail-dock reindex --storage-root D:\mail-archive
```

`verify`も`reindex`もメールを削除しません。サーバー削除やローカル消去を行うCLIサブコマンドはありません。これらの破壊的な操作はGUIからのみ実行でき、明示的な確認手順が必要です。

## FTS PoCのチェック

FTSベンチマークは手動で実行するもので、pytestには含まれません。計画されたコーパスを生成し、A-3の測定を行うには次を実行します。

```sh
uv run python tools/bench_fts.py --measure --results tools/.bench_fts/a3.json
```

既定の測定では1,000、5,000、10,000件のメッセージを対象にします。データベースとFTSのサイズ、3／5／10文字の語句を使うMATCHケース（単独、AND、OR、除外）、2文字のLIKEスキャン、FTSトリガーの有無による挿入スループット、最初のページと深い位置からのキ​​ーセットページング、構造化フィルターを報告します。最後のセクションでは、p95レイテンシーとFTSサイズを50,000件まで線形外挿します。`PASS`は、MATCHのp95が300 ms以下、LIKEのp95が3秒以下、FTSと検索ペイロードの比率が5倍以下であることを示します。外部コンテンツ方式ではソーステキストがFTSテーブルに重複保存されないため、比率が1倍未満でも問題ありません。

`--warmups N`と`--iterations N`で計測サンプルを調整できます。データセットごとの詳細な測定値と外挿値は、`--results`で指定したJSONパスに書き出されます。`queries`、`sorting`、`structured_filter`、`insert_throughput`オブジェクトには、個別のp50／p95値とヒット件数が含まれます。以前生成したコーパスを削除してクリーンに再実行する場合は`--force`を指定します。

```sh
uv run python tools/bench_fts.py --measure --force \
  --warmups 2 --iterations 7 \
	--output tools/.bench_fts --results tools/.bench_fts/a3.json
```

短い語句のMATCH動作、エスケープ、LIKEの再利用、`detail=`の比較を含むA-4のトライグラム動作チェックを実行するには、次を使います。

```sh
uv run python tools/bench_fts.py --check-a4 --counts 1000 --results tools/.bench_fts/a4.json
```

生成されたEMLファイル、データベース、JSONレポートはローカルのベンチマーク出力です。
