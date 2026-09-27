# **Phase 6: 配布とライセンス 実装計画書**

対象: [ローカルメールバックアップ＆閲覧アプリ 開発計画書.md](./ローカルメールバックアップand閲覧アプリ開発計画書.md) の **5.9 配布とライセンス**

前提: [実装計画書_Phase3_GUI基礎構築.md](./実装計画書_Phase3_GUI基礎構築.md) 7章「QtWebEngineの起動時間・メモリ・配布サイズの実測値とパッケージング時の注意（PyInstaller onedir採用）」、[実装計画書_Phase4.5_PSTアーカイブ.md](./実装計画書_Phase4.5_PSTアーカイブ.md) D-12「PyInstaller/Inno Setupによる実際のパッケージングは Phase 6 へ送る」、および Phase 5.1〜5.3（[実装計画書_Phase5_マルチプロトコル対応.md](./実装計画書_Phase5_マルチプロトコル対応.md)）が完了していること。

位置づけ: 本フェーズは mail-dock を **Windows向けの単一インストーラーとしてビルド・配布できる状態にする**フェーズである。既に Phase 4.5 で `vendor/readpst/` の同梱・`THIRD-PARTY-LICENSES.md`・簡易な `release.yml` は用意済みだが、それらは「readpstをリポジトリへ持ち込む土台」に留まり、**PyInstaller/Inno Setup本体、凍結（frozen）実行時の互換性、Qt本体を含めた対応ソースの提供、リリースCIの本実装は未着手**である。本書はこれらを実装範囲とする。

本書と開発計画書に矛盾がある場合は、開発計画書を正とする。設計不変条件（真実の情報源はEML＋マニフェスト、書き込み順序、削除の多段防御）は変更しない。**ストレージルート（EML・`metadata.db`）と keyring 上の資格情報は、インストール・アンインストールいずれの経路でも一切触れない。**

---

## **1. 目的**

- [ ] PyInstaller（onedir・windowed）で mail-dock を単一の実行イメージへビルドできるようにする
- [ ] PyInstaller化（凍結実行）によって壊れる既存コード（`__file__` 相対参照・子プロセス起動）を洗い出し修正する
- [ ] Inno Setup でインストーラーを作成し、ユーザー単位インストールを既定としつつ全ユーザーインストールも選べるようにする
- [ ] readpst（libpst）および同梱するMSYS2依存DLL群を、pacmanの現在状態に依存せずSHA-256で再現可能に取得できるようにする
- [ ] GPL/LGPLの対応ソース提供義務を、readpstだけでなくQt/PySide6を含めて満たす
- [ ] バージョン番号を単一の情報源に統一し、リリースタグとの一致をCIで検査する
- [ ] 凍結ビルドの疎通を確認する自己診断（`self-check`）を追加し、クリーン環境でのリリーススモークテストに使う
- [ ] リリースワークフローを、GPL成果物・対応ソース・バージョン整合のいずれかが欠けた場合に失敗する構成へ作り直す
- [ ] バージョン情報ダイアログを追加し、ライセンス表示の導線をアプリ内に用意する

## **2. 要件**

### **2.1 前提となる意思決定（確定済み）**

| # | 項目 | 決定内容 |
| :--- | :---- | :---- |
| D-1 | 配布物の構成 | 配布する実行ファイルは **GUI用の windowed `mail-dock.exe` 1本のみ**とする。CLI（`sync` / `verify` / `reindex` 等）は開発環境（`uv run mail-dock ...`）でのみ提供し、配布物には含めない |
| D-2 | 凍結ビルドでのコマンド制限 | 凍結ビルドの `mail-dock.exe` は `gui`（既定）と `self-check` 以外のサブコマンドが指定された場合、何も実行せず終了コード **2** を返し、`{config_dir}/logs/app.log` へ理由を記録する。windowed exeには標準出力が無いため、誤操作や自動化スクリプトの流用による無応答を防ぐ |
| D-3 | インストール権限 | 既定は**ユーザー単位インストール**（管理者権限不要、`%LOCALAPPDATA%\Programs\mail-dock`）とする。Inno Setupの `PrivilegesRequiredOverridesAllowed` により、必要な場合は全ユーザーインストール（`Program Files`、管理者権限要）も選択できるようにする |
| D-4 | 対応ソースの提供方式 | **readpstおよび同梱するMSYS2依存DLL群**は、各パッケージの `.src.tar.zst`（PKGBUILD・適用パッチ込み）をすべてRelease資産として添付する。**Qt/PySide6**は、ビルドで実際に収集したQtモジュール（`qtbase` / `qtwebengine` / `qtwebchannel` 等、収集DLLから決定）と `pyside-setup` の対応ソースを取得・SHA-256照合したうえでRelease資産として添付する（D-19: GPL-3.0-or-laterで配布するため必須ではなく、Qt自体がLGPL/GPL成果物として対応ソース提供義務を持つため）。`THIRD-PARTY-LICENSES.md` 内の `QT-SOURCE.md`（ビルド生成物）に、取得元URLと添付アセット名・ハッシュの両方を記載する |
| D-5 | MSYS2依存の再現性 | `mingw-w64-ucrt-x86_64-libpst` および依存DLLの取得元パッケージは、`repo.msys2.org` 上の**正確なパッケージファイル名とSHA-256をロックファイルに固定**して取得する（pacmanの現在の同期状態に依存しない）。あわせて、取得したパッケージファイル一式（`.pkg.tar.zst`）を保守者用の固定タグ（`readpst-msys2-mirror`）のGitHub Releaseへミラー保存し、`repo.msys2.org` から当該版が削除された場合でも同一SHA-256で取得を継続できるようにする。ロックの更新手順（新版取得→差分確認→検証→PoC再確認の要否判断→ライセンス表更新）を本書4章に明文化する |
| D-6 | アンインストール時の扱い | アンインストール時は確認ダイアログを表示し、**既定では設定・ログ（`%LOCALAPPDATA%\mail-dock` と `HKCU\Software\mail-dock\mail-dock`）を残す**。ユーザーが明示的に選んだ場合のみ削除する。**ストレージルート（EML・`metadata.db`・`manifests/`）と keyring 上の資格情報は、いずれの場合も一切削除しない**。全ユーザーインストールをアンインストールする場合、他ユーザーの設定は自動削除できないため案内表示のみとする |
| D-7 | 自己診断コマンド | 読み取り専用の診断サブコマンド `self-check` を追加する。秘密情報・ストレージルートには一切触れず、`--output <path>` でJSON結果を書き出し、失敗があれば終了コード1を返す。クリーンなWindows環境でのリリーススモークテストに使う。GUIのバージョン情報ダイアログからも同じ診断ロジックをプロセス内呼び出しで実行できるようにする |
| D-8 | 初回配布バージョン | 初回リリースは **0.1.0** とする。リリースタグ `v{version}` と `mail_dock.__version__` の不一致をCIで検出し失敗させる |
| D-9 | アプリアイコン | 本フェーズのスコープ外とする。アイコン未指定のままPyInstaller/Inno Setupの既定アイコンでビルドし、アイコン画像が用意され次第 `mail-dock.spec` の `icon=` と `mail-dock.iss` の `SetupIconFile` を設定できるよう導線だけ用意する（7章引き継ぎ事項） |

### **2.2 機能要件**

#### **凍結ランタイム対応（最優先。他のすべての工程の前提）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-1 | 新規 `infrastructure/app_paths.py` に `is_frozen() -> bool`（`sys.frozen` 判定）と `bundle_root() -> Path`（凍結時は `Path(sys._MEIPASS)`、非凍結時はリポジトリルート）を実装すること | D-1 |
| F-2 | `infrastructure/importers/readpst_locator.py` の `default_vendor_dir()` を `app_paths.bundle_root() / "vendor" / "readpst"` に置き換え、`Path(__file__).resolve().parents[4]` への依存を除去すること | F-1 |
| F-3 | `presentation/views/main_window.py` の `_open_encryption_guide()` を `app_paths.bundle_root() / "README.md"` に置き換えること | F-1 |
| F-4 | `infrastructure/storage/capabilities.py` の排他ロックプローブ用子プロセス起動を、インラインの `_CHILD_LOCK_SCRIPT` 文字列から新規 `infrastructure/storage/lock_probe.py`（`run_lock_probe_child(path: str) -> int` と `if __name__ == "__main__":` エントリ）へ切り出すこと。非凍結時は `[sys.executable, "-m", "mail_dock.infrastructure.storage.lock_probe", path]`、凍結時は `[sys.executable, "--maildock-internal-lock-probe", path]` で起動すること | F-1 |
| F-5 | 新規 `packaging/pyinstaller/entry_gui.py` が起動直後（重い import の前）に `--maildock-internal-lock-probe <path>` 引数を検出した場合、`lock_probe.run_lock_probe_child()` の戻り値でそのまま終了し、それ以外は `mail_dock.__main__.main()` を呼ぶこと。ストレージ適合性セルフテストの2秒タイムアウト内に確実に応答できること | F-4 |
| F-6 | `__main__.main()` に凍結判定を追加し、`app_paths.is_frozen()` が真かつ `command` が `None` / `"gui"` / `"self-check"` 以外のとき、`LOGGER.error(...)` を記録して終了コード2を返すこと（D-2） | D-2, F-1 |

#### **バージョン単一化**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-7 | `pyproject.toml` を `dynamic = ["version"]` とし、`[tool.hatch.version] path = "src/mail_dock/__init__.py"` を追加すること。`src/mail_dock/__init__.py` の `__version__` を唯一の情報源とすること | D-8 |
| F-8 | `[dependency-groups]` に `build` を新設し、`pyinstaller`（PySide6 6.11系・Python 3.13に対応する版）を追加すること。既存の `dev` グループには追加しないこと | ― |

#### **自己診断（`self-check`）とバージョン情報ダイアログ**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-9 | 新規 `infrastructure/diagnostics.py` に `run_self_check() -> DiagnosticsReport` を実装すること。検査項目は次を含むこと: `__version__`、`importlib.resources` 経由でのマイグレーションSQL一覧の列挙、`:memory:` SQLiteでのFTS5 trigramテーブル作成、`iso2022_jp_ext`/`cp932`/`euc_jp` コーデックの利用可否、`ReadPstLocator().get_version()`、`keyring_store.detect_backend()` の状態、`PySide6.QtWebEngineCore` のimport可否。凍結ビルドでは、加えて同梱ライセンス資料一式（`vendor/readpst/COPYING` 等、後述F-16〜F-19の生成物）の存在確認も行うこと | D-7 |
| F-10 | `run_self_check()` は秘密情報（パスワード・トークン・keyring資格情報の値）を一切含めず、失敗項目がある場合のみ全体結果を失敗とすること（keyringバックエンド未対応など、`session_only`へ正規にフォールバックする状態は失敗として扱わないこと） | D-7, セキュリティ |
| F-11 | `__main__.py` に `self-check [--output PATH]` サブコマンドを追加し、`--output` 指定時はJSONをファイルへ書き出し、未指定時は標準出力へ書くこと。失敗があれば終了コード1、無ければ0を返すこと | F-9 |
| F-12 | 新規 `presentation/views/dialogs/about_dialog.py`（`AboutDialog`）を実装し、バージョン・GPL-3.0-or-laterと無保証の告知・`LICENSE`/`THIRD-PARTY-LICENSES.md`/同梱ライセンスフォルダを開くボタン・ソースリポジトリURLを表示すること。「実行環境を診断」ボタンから `run_self_check()` をプロセス内で呼び出し、結果一覧を表示すること | F-9 |
| F-13 | `main_window.py` のヘルプメニューに「バージョン情報」（`AboutDialog` を開く）と「Qtについて」（`QMessageBox.aboutQt()`）を追加すること | F-12 |

#### **readpst依存のロック化・対応ソース（D-5, D-4）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-14 | 新規 `packaging/readpst/msys2-packages.lock.json` に、`mingw-w64-ucrt-x86_64-libpst` を含む全依存パッケージについて、名称・版・バイナリパッケージ（ファイル名・URL・SHA-256）・ソースパッケージ（`.src.tar.zst` のファイル名・URL・SHA-256）・ライセンス・展開対象ファイルを記録すること。初期値は現行 `THIRD-PARTY-LICENSES.md` に記録済みの版に合わせること | D-5 |
| F-15 | `tools/fetch_readpst.ps1` を、ロックファイルに従って `repo.msys2.org` から取得しSHA-256を照合したうえで展開する方式へ書き換えること。取得に失敗した場合は `readpst-msys2-mirror` タグのミラー資産から同一SHA-256で再取得すること。`mt.exe` によるマニフェストパッチ適用（D-19）の工程は維持すること。ソースパッケージ（`.src.tar.zst`）は `build/sources/msys2/` へ取得すること | D-5, F-14 |
| F-16 | 新規 `tools/update_readpst_lock.ps1` を、保守者がローカルのMSYS2環境からロックファイルを再生成するために作成すること。生成時に依存DLLの完全な集合（`ldd` 相当の結果）を検査し、`readpst_locator._WINDOWS_READPST_DLLS` との不一致を警告すること | F-14 |
| F-17 | 新規 `tools/verify_readpst_bundle.ps1` に、既存 `release.yml` に直書きされているGPL検査ロジック（必須ファイルの存在・`COPYING`のGPL文言・`readpst-artifacts.json`のライセンス欄・SHA-256照合）を切り出し、ソースパッケージ（`.src.tar.zst`）の存在とハッシュの検査を追加すること | 既存release.yml |
| F-18 | 単体テストで、ロックファイルに記録された依存DLL集合と `readpst_locator._WINDOWS_READPST_DLLS` が一致することを検証すること（更新漏れの機械的検出） | F-14 |

#### **Qt対応ソースの取得（D-4 B案）**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-19 | ビルド工程で、PyInstallerが実際に収集したQt関連DLLからモジュール集合（`qtbase` / `qtwebengine` / `qtwebchannel` 等）を機械的に決定し、対応する `pyside-setup` のタグ付きソースおよび該当Qtモジュールのソースを取得してSHA-256を記録すること | D-4 |
| F-20 | 取得したQt対応ソースを `mail-dock-{version}-qt-corresponding-source.zip` としてまとめ、リリース資産に含めること | F-19 |
| F-21 | 新規 `licenses/QT-SOURCE.md`（ビルド生成物。リポジトリには追跡しない）に、ビルドに使用した正確なQt/PySide6の版、ソース所在URL、および同梱した対応ソース資産名とSHA-256を記載すること | F-19, F-20 |

#### **同梱ライセンス資料の生成**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-22 | 新規 `tools/collect_licenses.py`（標準ライブラリの `importlib.metadata` のみ使用）が、mail-dockの実行時依存を辿り、各パッケージのライセンスファイルを `build/licenses/python/{name}-{version}/` へコピーし、`python-packages.json` を出力すること。ライセンスファイルが見つからない依存があれば失敗すること | ― |
| F-23 | 同ツールが、Python本体の `LICENSE.txt`（同梱するOpenSSL/SQLite等の告知を含む）、PyInstaller bootloaderのCOPYING、Qt LGPL-3/GPL-3全文も `build/licenses/` へ収集すること | F-22 |
| F-24 | `--check-inventory THIRD-PARTY-LICENSES.md` オプションで、収集した依存パッケージがすべて `THIRD-PARTY-LICENSES.md` に記載されているかを検査し、未記載があれば失敗すること | F-22 |

#### **PyInstaller**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-25 | 新規 `packaging/pyinstaller/mail-dock.spec` を作成し、onedir・`console=False`（windowed）でビルドすること。エントリポイントは `packaging/pyinstaller/entry_gui.py` とすること | D-1, F-5 |
| F-26 | 同梱データとして、`src/mail_dock/migrations/*.sql`、`vendor/readpst/` 一式、`README.md`/`LICENSE`/`THIRD-PARTY-LICENSES.md`、`build/licenses/` を `_internal` 配下へ含めること | F-2, F-22 |
| F-27 | `__version__` からWindowsバージョンリソース（`ProductVersion`/`FileVersion`/`LegalCopyright`にGPL-3.0-or-laterを明記）を生成し、EXEへ適用すること | F-7 |
| F-28 | 出力exeのマニフェストに `longPathAware=true` が含まれることを確認し、含まれない場合はカスタムマニフェストを明示的に付与すること（`activeCodePage` はアプリ本体には不要なので付けない） | ― |
| F-29 | 新規 `tools/build_windows.ps1` に、依存同期→readpst取得検証→ライセンス収集→PyInstallerビルド→出力物検証→Inno Setupの一連の手順をまとめること | F-15, F-17, F-22, F-25, 後述F-30〜 |
| F-30 | 新規 `tools/verify_release_bundle.ps1` に、同梱readpstのSHA-256が `SHA256SUMS` と一致すること、必須ライセンス資料が揃っていること、ビルド済みexeで `self-check` が成功することの検証を実装すること | F-9, F-17 |

#### **Inno Setup**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-31 | 新規 `packaging/inno/mail-dock.iss` を作成すること。`AppId` は固定GUID、`PrivilegesRequired=lowest`、`PrivilegesRequiredOverridesAllowed=dialog commandline`、`DefaultDirName={autopf}\mail-dock`、`ArchitecturesInstallIn64BitMode=x64compatible`、`LicenseFile=LICENSE`、日本語・英語の2言語とすること | D-3 |
| F-32 | 上書きインストール時に旧ファイルが残らないよう、`[InstallDelete]` で `{app}\_internal` を削除してから新ファイルを配置すること | ― |
| F-33 | スタートメニューに本体とライセンス情報フォルダへのショートカットを作成すること。デスクトップアイコンは既定オフの任意タスクとすること | F-26 |
| F-34 | `[Code]` の `CurUninstallStepChanged`（`usPostUninstall`）で、ユーザー単位インストールかつ非サイレント実行時のみ確認ダイアログ（既定「いいえ」）を表示し、「はい」の場合に限り `%LOCALAPPDATA%\mail-dock` と `HKCU\Software\mail-dock\mail-dock`（`QSettings("mail-dock","mail-dock")` の実体）を削除すること。全ユーザーインストールのアンインストール時は自動削除を行わず、案内メッセージのみ表示すること | D-6 |
| F-35 | インストーラーの文言に「ストレージルート上のメールデータと、keyringに保存された資格情報は削除されません」と明記すること | D-6 |

#### **CI・リリースワークフロー**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-36 | `.github/workflows/ci.yml` に `workflow_call` トリガーを追加し、リリースワークフローから再利用できるようにすること | ― |
| F-37 | `.github/workflows/release.yml` を次のジョブ構成へ作り直すこと: `verify`（ci.yml呼び出し＋タグとバージョンの一致確認）、`build-windows`（`tools/build_windows.ps1` を実行）、`smoke-clean`（MSYS2を導入しない別ランナーでインストーラーを検証）、`publish`（タグ時のみ、必須アセット一覧の充足確認後にDraft Releaseを作成） | D-8, F-29 |
| F-38 | `smoke-clean` ジョブは `/VERYSILENT /CURRENTUSER` でインストールし、PATHを `C:\Windows\System32;C:\Windows` に絞ったうえで `readpst.exe -V` と `mail-dock.exe self-check --output <path>` を実行すること。サイレントアンインストール後、インストール先が削除され設定ディレクトリが残ることを確認すること | F-9, F-31 |
| F-39 | `publish` ジョブの必須アセットは `mail-dock-{version}-setup.exe` / `mail-dock-{version}-src.tar.gz`（`git archive`） / `mail-dock-{version}-readpst-corresponding-source.zip` / `mail-dock-{version}-qt-corresponding-source.zip` / `SHA256SUMS.txt` とし、1つでも欠ければジョブを失敗させること。旧来のreadpst単体zipアセットの生成は廃止すること | F-15, F-20 |

### **2.3 非機能要件・制約**

| # | 指標・制約 | 内容 | 備考 |
| :--- | :---- | :---- | :---- |
| N-1 | インストーラーのネットワークアクセス | インストーラー実行時は一切のネットワークアクセスを行わない | GPLの対応ソース提供義務はビルド時・Release資産で満たす（D-4） |
| N-2 | 署名 | コード署名は行わない（Phase 6スコープ外） | SmartScreen警告が出ることをREADMEに明記する |
| N-3 | ストレージルート・資格情報の不可侵 | インストール・アンインストールのいずれの経路でも、ストレージルート配下（EML・`metadata.db`・`manifests/`）とkeyring上の資格情報に触れない | D-6, 開発計画書5.3 |
| N-4 | レイヤー境界 | 凍結判定・パス解決は `infrastructure/app_paths.py` に閉じ込め、`domain`/`usecases` 層はこれに依存しない | 既存アーキテクチャ（開発計画書2.3） |
| N-5 | ロールバック安全性 | PyInstallerビルドおよびInno Setupパッケージングの失敗は、既存の `uv run mail-dock` 開発運用に影響しないこと | ― |
| N-6 | 配布サイズ・起動時間の記録 | Phase 3で実測を予告していたQtWebEngine込みの配布サイズ・起動時間を、本フェーズのビルド完了後に実測し本書6章へ記録する | Phase 3 引き継ぎ |

---

## **3. タスク**

### **3.1 グループA: 凍結ランタイム修正（*最優先。全グループの前提*）**

- [ ] `infrastructure/app_paths.py` を新設し `is_frozen()` / `bundle_root()` を実装する
- [ ] `readpst_locator.default_vendor_dir()` を `app_paths.bundle_root()` 基準へ置き換える
- [ ] `main_window._open_encryption_guide()` を `app_paths.bundle_root()` 基準へ置き換える
- [ ] `infrastructure/storage/lock_probe.py` を新設し、`capabilities._CHILD_LOCK_SCRIPT` のロジックを移設する
- [ ] `capabilities._probe_exclusive_lock()` の子プロセス起動を凍結判定で分岐させる（開発時は `-m` 実行、凍結時は内部フラグ経路）
- [ ] `packaging/pyinstaller/entry_gui.py` を新設し、内部フラグ検出とメイン処理呼び出しを実装する
- [ ] `__main__.main()` に凍結時のサブコマンド制限（`gui`/`self-check`以外を拒否、終了コード2、`app.log`記録）を実装する

### **3.2 グループB: バージョン単一化**

- [ ] `pyproject.toml` を `dynamic = ["version"]` + `[tool.hatch.version]` へ変更する
- [ ] `[dependency-groups] build = ["pyinstaller"]` を追加し `uv.lock` を更新する
- [ ] タグ `v{version}` と `__version__` の一致検査ロジックを追加する（`verify` ジョブで使用）

### **3.3 グループC: 自己診断・バージョン情報ダイアログ（*Aに依存*）**

- [ ] `infrastructure/diagnostics.py` に `run_self_check()` を実装する（F-9・F-10の全検査項目）
- [ ] `__main__.py` に `self-check [--output PATH]` サブコマンドを追加する
- [ ] `presentation/views/dialogs/about_dialog.py`（`AboutDialog`）を実装する
- [ ] `main_window.py` のヘルプメニューに「バージョン情報」「Qtについて」を追加する
- [ ] `tests/unit/test_diagnostics.py`：各検査項目の成功・失敗パス、秘密情報が出力に含まれないこと
- [ ] `tests/gui/test_about_dialog.py`：ダイアログ表示・診断ボタンの動作

### **3.4 グループD: readpst依存のロック化・対応ソース（*Aと並行可*）**

- [ ] `packaging/readpst/msys2-packages.lock.json` を新設し、現行の同梱版に合わせた初期値を記録する
- [ ] `tools/fetch_readpst.ps1` をロックファイル駆動の取得・検証・ミラーフォールバック方式へ書き換える
- [ ] `tools/update_readpst_lock.ps1` を新設する（保守者用のロック再生成・DLL集合の警告付き）
- [ ] `tools/verify_readpst_bundle.ps1` を新設し、`release.yml` のGPL検査ロジックを移設・拡張する
- [ ] 保守者用の `readpst-msys2-mirror` Releaseへパッケージ一式をミラー保存する手順を `tools/update_readpst_lock.ps1` に組み込む
- [ ] `tests/unit/test_readpst_lock_consistency.py`：ロックのDLL集合と `_WINDOWS_READPST_DLLS` の一致検証

### **3.5 グループE: 同梱ライセンス資料・Qt対応ソース（*Dと並行可*）**

- [ ] `tools/collect_licenses.py` を新設する（Python依存の収集、Python本体/PyInstaller/Qtライセンス全文の収集、`--check-inventory` オプション）
- [ ] Qt対応ソース取得ロジック（収集DLLからモジュール決定→取得→SHA-256照合→zip化）を実装する
- [ ] `licenses/QT-SOURCE.md` 生成ロジックを実装する
- [ ] `THIRD-PARTY-LICENSES.md` に Python本体・PyInstaller・Inno Setup・その他ビルド時依存の記載を追記する

### **3.6 グループF: PyInstaller（*A・C・D・Eに依存*）**

- [ ] `packaging/pyinstaller/mail-dock.spec` を新設する（onedir、データ同梱、バージョンリソース、manifest）
- [ ] `tools/build_windows.ps1` を新設し、一連のビルド手順をまとめる
- [ ] `tools/verify_release_bundle.ps1` を新設する
- [ ] ローカルビルドで生成したexeが起動し、`self-check` が成功することを確認する

### **3.7 グループG: Inno Setup（*Fに依存*）**

- [ ] `packaging/inno/mail-dock.iss` を新設する（権限設定、`[InstallDelete]`、ショートカット、多言語）
- [ ] アンインストール時の確認ダイアログとレジストリ・設定ディレクトリ削除の `[Code]` セクションを実装する
- [ ] ローカルでインストール／上書きインストール／アンインストールを手動確認する

### **3.8 グループH: CI・リリースワークフロー（*F・Gに依存*）**

- [ ] `.github/workflows/ci.yml` に `workflow_call` を追加する
- [ ] `.github/workflows/release.yml` を `verify`/`build-windows`/`smoke-clean`/`publish` の4ジョブへ作り直す
- [ ] `smoke-clean` ジョブでPATHを絞ったクリーン環境スモークテストを実装する
- [ ] `publish` ジョブの必須アセット充足チェックを実装する
- [ ] `workflow_dispatch` での試験実行と、必須アセットを1つ欠落させた失敗ケースを確認する

### **3.9 グループI: ドキュメント整合（*各グループと並行*）**

- [ ] README にインストール手順・SmartScreen警告の説明・アンインストールの挙動・ソースからのビルド手順・対応ソース入手方法・「CLIは開発環境のみ」の注記を追加する
- [ ] 開発計画書 5.9・6章・8章にPhase 6の実績・決定事項を追記する
- [ ] `.github/copilot-instructions.md` にビルドコマンド（`tools/build_windows.ps1` 等）を追記する
- [ ] `ruff check .` / `mypy .` / `pytest -m "not docker and not gui and not pst"` を実行し、全テスト通過を確認する

---

## **4. MSYS2依存ロックの運用**

### **4.1 ロックファイルの構造（`packaging/readpst/msys2-packages.lock.json`）**

各パッケージについて次を記録する。

| フィールド | 内容 |
| :---- | :---- |
| `name` | MSYS2パッケージ名（例: `mingw-w64-ucrt-x86_64-libpst`） |
| `version` | パッケージ版文字列 |
| `binary.filename` / `binary.url` / `binary.sha256` | `repo.msys2.org` 上の `.pkg.tar.zst` |
| `source.filename` / `source.url` / `source.sha256` | 対応する `.src.tar.zst` |
| `license` | パッケージのライセンス識別子 |
| `extracted_files` | このパッケージから取り出すファイル名一覧 |

### **4.2 取得の優先順位**

1. ロックに記録された `binary.url`（`repo.msys2.org`）から取得を試み、SHA-256を照合する。
2. 失敗した場合、`readpst-msys2-mirror` タグのGitHub Release資産から同一ファイル名で取得し、同じSHA-256で照合する。
3. いずれも失敗した場合はビルドを失敗させる（黙って古い版や別経路にフォールバックしない）。

### **4.3 ロック更新手順（保守者向け）**

1. `tools/update_readpst_lock.ps1` をローカルのMSYS2環境（`pacman -Syu` 済み）で実行し、新しいロックファイルを生成する。
2. 生成結果の差分（版・SHA-256・依存DLL集合の増減）を確認する。依存DLL集合が変わった場合は `readpst_locator._WINDOWS_READPST_DLLS` を追随させる。
3. `tools/verify_readpst_bundle.ps1` を実行し、GPL資料・ハッシュの整合を確認する。
4. libpst本体（`mingw-w64-ucrt-x86_64-libpst`）自体の版が変わった場合は、Phase 4.5のPoC相当（日本語フォルダ名・禁止文字・予約名・長パス・`lspst`出力）を再確認する。
5. `pytest -m pst` と `self-check` を実行し、回帰が無いことを確認する。
6. `THIRD-PARTY-LICENSES.md` のハッシュ表を更新する。
7. `tools/update_readpst_lock.ps1` の仕上げ処理として、新しいパッケージファイル一式を `readpst-msys2-mirror` Releaseへアップロードする。

### **4.4 最終手段（MSYS2からlibpstが削除された場合）**

`repo.msys2.org` およびミラーの双方から取得できなくなった場合、Release資産として保存済みの `.src.tar.zst`（PKGBUILD・パッチ込み）を用いて `makepkg-mingw` により自前ビルドする。この手順は本フェーズでは自動化せず、7章の引き継ぎ事項として手順の概要のみ残す。

---

## **5. スコープ境界**

### **5.1 含むもの**

セクション3のグループA〜I。凍結ランタイム修正、バージョン単一化、自己診断、readpst依存のロック化と対応ソース、Qt対応ソース、同梱ライセンス資料生成、PyInstaller、Inno Setup、CI・リリースワークフローの一式。

### **5.2 含まないもの（明示的に除外）**

| 除外項目 | 理由・扱い |
| :---- | :---- |
| コード署名 | 単一ユーザー利用が前提のため当面不要（開発計画書5.9）。第三者配布規模になった場合に別途検討 |
| 自動更新機構 | 開発計画書5.9で恒久的にスコープ外と明記済み |
| macOS/Linux向けパッケージ、ポータブルzip版 | 本フェーズはWindows向けInno Setupインストーラーのみを対象とする |
| 配布物へのCLI用exeの同梱 | D-1により配布はGUI用windowed exeのみとする |
| VHDX切断試験・フルスケール実機同期試験 | Phase 4から延期中の項目であり、本フェーズの対象外（別途手動検証） |
| アプリアイコンの用意 | D-9によりユーザー側で別途用意する |
| MSYS2からlibpstが削除された場合の自動再ビルド | 4.4節のとおり手順のみ残し自動化しない |

---

## **6. 検証**

各項目の完了を確認したうえで、対応するタスクのチェックボックスを埋めること。

- [ ] V-1（ブロッカー）. `readpst_locator` / `_open_encryption_guide` / `capabilities` の凍結対応を単体テストで確認し、PyInstallerビルドしたexeで実際に排他ロックプローブが2秒以内に完了し `OK` と判定されること
- [ ] V-2. 凍結ビルドの `mail-dock.exe sync` 等が終了コード2を返し `app.log` に記録されること
- [ ] V-3. `self-check` が非凍結・凍結の両方で実行でき、秘密情報を含まないJSONを出力すること。readpst未検出時や同梱ライセンス資料欠落時に失敗（終了コード1）すること
- [ ] V-4. バージョン情報ダイアログからライセンス表示・診断実行ができること
- [ ] V-5. `tools/fetch_readpst.ps1` がロックファイル駆動で取得・検証でき、公式URLを一時的に無効化してもミラーから同一ハッシュで取得できること
- [ ] V-6. ロックファイルのDLL集合と `_WINDOWS_READPST_DLLS` の一致テストが機能すること（意図的に不一致を作り検出できることを確認）
- [ ] V-7. `tools/collect_licenses.py --check-inventory THIRD-PARTY-LICENSES.md` が、未記載の依存を検出して失敗すること
- [ ] V-8. Qt対応ソースの取得・zip化・ハッシュ記録が実行でき、`QT-SOURCE.md` に記載されること
- [ ] V-9. `tools/build_windows.ps1` によるローカルビルドでインストーラーが生成され、インストール・起動・HTMLメール表示（QtWebEngineProcess）・PST取込（readpst）・ストレージルート選択時のセルフテスト`OK`判定・上書きインストール・アンインストール（設定削除の確認ダイアログを含む）を手動確認できること
- [ ] V-10. `release.yml` を `workflow_dispatch` で実行し、`verify`→`build-windows`→`smoke-clean`→`publish` が成功すること。必須アセットを1つ欠落させると `publish` が失敗すること
- [ ] V-11. `smoke-clean` ジョブが、MSYS2非導入・PATH制限環境で `readpst.exe -V` と `self-check` に成功し、サイレントアンインストール後に設定ディレクトリのみが残ることを確認すること
- [ ] V-12. `uv run ruff format --check .` / `uv run ruff check .` / `uv run mypy` / `uv run pytest -m "not docker and not gui and not pst"` が成功すること
- [ ] V-13. 配布サイズ・起動時間を実測し、本書に記録すること（N-6, Phase 3引き継ぎ）

## **6.1 実測記録（グループF・G完了後に記入）**

| 項目 | 実測値 |
| :---- | :---- |
| インストーラーサイズ | 未計測 |
| インストール後のディスク使用量 | 未計測 |
| 初回起動時間 | 未計測 |
| `self-check` 所要時間 | 未計測 |

---

## **7. 引き継ぎ事項**

- [ ] アプリアイコンが用意され次第、`mail-dock.spec` の `EXE(icon=...)` と `mail-dock.iss` の `SetupIconFile` を設定する（D-9）
- [ ] MSYS2から対象パッケージ版が完全に削除された場合の `makepkg-mingw` による自前ビルド手順を、実際に一度実行して検証し、恒久的な手順書として整備する（4.4節）
- [ ] コード署名・自動更新・macOS/Linux配布が必要になった場合は、別フェーズとして計画すること
- [ ] VHDX切断試験・フルスケール実機同期試験（Phase 4から延期中）の実施要否を判断すること
- [ ] `readpst-msys2-mirror` Releaseの資産容量が増え続けた場合の整理方針（古い版の削除可否）を検討すること
