# **ローカルメールバックアップ＆閲覧アプリ 開発計画書**

## **1\. プロジェクト概要**

### **1.1 目的**

お名前.comをはじめとするIMAPサーバー（および将来的なGmail等のクラウドメールAPI）上に保管されているメールをローカル環境（外付けSSD等）に自動/手動でダウンロードし、安全に長期保管（バックアップ）するとともに、過去のメール資産を高速に検索・安全に閲覧できるデスクトップアプリケーションを開発する。

### **1.2 背景とコンセプト**

* **サーバー容量の節約:** メールサーバー容量の逼迫を防ぐため、ローカルへの完全保管（SHA-256による検証済み）を確認したうえで、ユーザー操作によりサーバー上のメールを削除できるようにする。  
* **ローカルデータの非破壊保存 (Append-Only):** サーバー上でメールが削除・移動されても、ローカルに保存した .eml は**システム都合では一切削除しない**。実ファイルを削除できるのは「ユーザーがアプリ上で明示的にゴミ箱へ移動し、かつ、30日の猶予期間が経過した場合またはゴミ箱の中のメールをさらに削除した場合」のみとする。  
* **可搬性の確保:** データベース（SQLite）とバイナリ（.eml）を同一のストレージルート配下で管理し、外付けドライブの挿し替えやドライブレター（D:\\, E:\\等）の変更に柔軟に対応する。EMLは標準形式のため、本アプリが無くても Thunderbird 等の一般的なメールクライアントで閲覧できる。  
* **高い拡張性（マルチプロトコル対応）:** メール取得・削除ロジックをインターフェースとして抽象化し、お名前.com等の標準IMAP（Basic認証）から、将来的なGmail/Outlook（OAuth2 \+ XOAUTH2 / REST API）への対応拡張を容易にする。
* **過去資産（.pst）の永続化:** **2029年10月の Outlook Classic サポート終了**に向けてプロトコルをIMAPへ移行することを前提に、POP3時代にローカルへ蓄積された `.pst` ファイル内のメールを標準形式の `.eml` へ変換し、Outlookに依存せず永続的に閲覧可能な状態にする。

### **1.3 設計不変条件（Design Invariants）**

本アプリの全設計判断は、以下の3原則に優先的に従う。実装時に迷った場合はこの原則に立ち返る。

1. **EMLと永続マニフェストが真実の情報源 (Sources of Truth):**
    ローカルの `.eml` ファイル群と、取得元・フォルダ・識別子・purge墓標・監査イベントを記録する append-only の永続マニフェストが正であり、`metadata.db` は両者から**いつでも再構築可能な派生キャッシュ**である。フォルダ同期状態などの可変な運用状態はDBにも保持するが、再構築に必要な確定事実はマニフェストへ逐次追記する。
2. **書き込み順序の厳守:**
    常に「EMLファイルの永続化（fsync + アトミック配置）→ 永続マニフェストへの追記とfsync → DBコミット」の順序で行う。逆順にすると「DBに存在するが実体または出所情報が無い」という復旧不能な状態が発生するため。中断時に発生し得るのは「DB未登録のEMLまたはマニフェスト済み項目」のみであり、次回同期・取込再開時に自動的に再取り込みする。
3. **削除は常に多段防御:**
   サーバー削除・ローカル実削除ともに、事前検証・確認・監査ログ・猶予期間のいずれかを必ず経由する。取り消せない操作をワンクリックで実行させない。

> **PSTアーカイブにおける不変条件1の解釈（4.10参照）:**
> PST由来の `.eml` は `readpst` による **MAPI形式からの再構成物**であり、原本 `.pst` のビット単位の複製ではない。したがって取り込み完了後は `.eml` とPST取込マニフェストを正とするが、**原本 `.pst` はユーザー自身が別途保管する**ことを前提条件とする（アプリは原本をコピーも変更もしない）。取り込み時に原本の完全な SHA-256、変換バージョン、オプション、元フォルダと各EMLの対応を記録し、後から出所を追跡できるようにする。

> **PSTアーカイブとIMAPアーカイブは独立した2つの機能である。**
> 両者は同一のストレージルート・DB・EML保管構造・検索基盤を共有するが、**データとしては一切接続しない**。重複排除・相互参照・統合ビューは行わない。`content_key` は非一意の移動候補・スレッド照合用情報に限定し、各系統の一次識別子はIMAPの `UIDVALIDITY + UID` とPSTの `source_item_key` に分離する。

### **1.4 想定規模と前提条件**

| 項目 | 想定値 | 備考 |
| :---- | :---- | :---- |
| 最大メール件数 | **50,000通** | アカウント全体の合計 |
| 最大総容量 | **100GB** | 平均約2MB/通。添付ファイルの比重が非常に大きい |
| アカウント数 | 数個（〜5程度） | |
| 同期対象フォルダ | **ユーザーが選択式** | 初回セットアップ時および設定画面で選択 |
| 利用形態 | 単一ユーザー・単一PC・常駐可 | |

**この規模から導かれる設計上の帰結:**

* 平均2MB/通のため、**同期のボトルネックはネットワーク転送**であり、DB処理ではない。→ 進捗表示は「通数」ではなく「転送バイト数」を主指標とする。
* 初回フルバックアップは**数時間規模**（100GB / 実効10MB/s ≒ 3時間）になる。→ **中断・再開（レジューム）機能は必須要件**であり、任意機能ではない。
* 巨大メール（数十MB〜）が現実に存在するため、**1通あたりの取得サイズ上限**とスキップ記録の仕組みが必要。
* 一方、**件数は5万件と小規模**であるため、FTS5インデックスサイズや検索速度は問題にならない。検索周りの過剰な最適化は不要。

**PSTアーカイブ側の前提（上記とは別勘定）**

| 項目 | 想定値 | 備考 |
| :---- | :---- | :---- |
| 対象形式 | **`.pst` のみ** | `.ost` / 単体 `.msg` / mbox は**スコープ外** |
| 内容の性質 | **固定（追記されない）** | 増分同期・再スキャンの概念を持たない。1回変換したら終わり |
| 取り込み単位 | PSTファイル1個 = 擬似アカウント1個 | |
| 一時的に必要な空き容量 | **PSTサイズ × 約2** | 抽出先（staging）＋ 最終EML。取り込み完了後は約1倍に戻る |

* PSTの中身は POP3 時代のローカル蓄積分であり、**IMAPアーカイブの想定規模（5万通/100GB）への上乗せではない**。両者は別の容量勘定として扱う。

## **2\. システム構成と技術スタック**

### **2.1 技術スタック**

* **言語:** **Python 3.13**（`pyproject.toml` の `requires-python = ">=3.13"` に統一）  
* **GUIフレームワーク:** PySide6 6.8以降 (Qt for Python)  
  * レンダリングエンジン: QWebEngineView（HTMLメールのサンドボックス表示）  
  * ※ Phase 3 冒頭で `QTextBrowser` による軽量版と比較評価し、採否を判断する（後述 6章）  
* **データベース:** SQLite（Python同梱版。FTS5 \+ trigram トークナイザーを使用）  
* **通信・プロトコル:**  
  * 標準ライブラリ imaplib, ssl（お名前.com IMAP over SSL / Port 993）  
  * 将来対応用: google-auth-oauthlib（OAuth2）  
* **メール解析:** 標準ライブラリ email \+ beautifulsoup4（HTML→テキスト変換）\+ charset-normalizer（文字コード推定）  
* **その他:** keyring（OS資格情報ストア）、platformdirs（設定ファイル配置）  
* **PST変換:** **`readpst`（libpst 0.6.76 以降）を外部実行ファイルとして同梱**し、`subprocess` から起動する（Pythonの追加依存パッケージは不要）
  * Windows向けビルド済みバイナリは MSYS2 の `mingw-w64-ucrt-x86_64-libpst` から取得する
  * ライセンスは **GPL-2.0-or-later**。本体とはプロセス分離されるが、配布時の遵守事項は 5.9 に記載
  * 検討したが**採用しなかった**代替案: `pypff`(libpff-python) はソース配布のみでMSVCビルドが必要・Python 3.13未検証・Alpha品質、`libratom` はspaCy等の重い依存を引き込む、`extract-msg` は`.msg`単体用でGPL、Outlook COM (pywin32) はOutlookデスクトップが必須
* **開発ツール:** pytest / pytest-qt / ruff / mypy

**確定依存関係（pyproject.toml）**

```toml
dependencies = [
    "PySide6>=6.8",
    "keyring>=25",
    "beautifulsoup4>=4.12",
    "charset-normalizer>=3.3",
    "platformdirs>=4",
]

[dependency-groups]
dev = ["pytest", "pytest-qt", "pytest-cov", "ruff", "mypy"]
```

### **2.2 リポジトリ構成案**

標準的な **src レイアウト**を採用する。

```Plaintext
mail-dock/
├── pyproject.toml
├── README.md
├── THIRD-PARTY-LICENSES.md   # 同梱物のライセンス表記（readpst / Qt 等）
│
├── vendor/
│   └── readpst/              # ★同梱するreadpst一式（バイナリはGit管理外。CIで取得）
│       ├── readpst.exe
│       ├── *.dll             # libpst依存DLL（iconv / zlib 等）
│       └── COPYING           # GPL-2.0 全文
│
├── src/mail_dock/
│   ├── __init__.py
│   ├── __main__.py           # エントリポイント兼コンポジションルート（DI組み立てはここに集約）
│   ├── config.py             # 設定の読み書きのみ担当（オブジェクト生成はしない）
│   │
│   ├── domain/               # 【最内層】ビジネスモデル・抽象IF（外部依存ゼロ）
│   │   ├── models.py         # Message, Account, Folder, SearchFilter 等の純粋なデータ構造
│   │   ├── fetcher.py        # BaseMailFetcher (ABC)
│   │   ├── importer.py       # BaseArchiveImporter (ABC) ※PST等のローカルアーカイブ取込
│   │   ├── repository.py     # BaseMessageRepository (ABC) ※テスト時のインメモリ差し替え用
│   │   └── errors.py         # ドメイン例外階層（AuthenticationError / StorageDetachedError 等）
│   │
│   ├── usecases/             # 【内層】業務ロジック
│   │   ├── sync_mail.py      # 同期フロー（UID増分取得→EML保存→DB登録）
│   │   ├── search_mail.py    # 検索条件の組み立てとリポジトリ呼び出し
│   │   ├── delete_remote.py  # サーバー側削除の制御（事前検証を含む）
│   │   ├── trash.py          # ローカルゴミ箱・30日経過purgeの制御
│   │   ├── verify.py         # 整合性チェック・孤児スキャン・再インデックス
│   │   ├── import_pst.py     # PST取込（Stage A: readpst抽出 → Stage B: EML取込）
│   │   └── export.py         # EML / mbox / CSV エクスポート
│   │
│   ├── infrastructure/       # 【外層】DB・通信・ファイルI/O
│   │   ├── fetchers/
│   │   │   ├── imap_common.py    # imaplib共通処理（modified UTF-7、例外ラップ）
│   │   │   ├── onamae_imap.py    # OnamaeImapFetcher
│   │   │   └── gmail_oauth.py    # (将来用) GmailOAuthFetcher
│   │   ├── importers/
│   │   │   ├── readpst_locator.py # 同梱readpstの解決・-V によるバージョン確認
│   │   │   └── readpst_runner.py  # subprocess実行・進捗監視・キャンセル・stderr捕捉
│   │   ├── database/
│   │   │   ├── connection.py     # 接続管理・PRAGMA設定・スレッドローカル接続
│   │   │   ├── migrator.py       # PRAGMA user_version によるマイグレーション
│   │   │   └── repository.py     # SQLiteMessageRepository
│   │   ├── parsing/
│   │   │   ├── eml_parser.py     # ヘッダ/本文/添付の抽出、文字コードフォールバック
│   │   │   ├── html_to_text.py   # HTML→プレーンテキスト変換
│   │   │   └── normalize.py      # 検索用テキスト正規化（NFKC + casefold）
│   │   ├── storage/
│   │   │   ├── eml_storage.py    # EMLの原子的書き込み・読み込み・ハッシュ計算
│   │   │   ├── storage_root.py   # ルートのUUID照合・ロック・空き容量確認・接続状態プローブ
│   │   │   ├── detach.py         # ★I/O例外の分類（OSError/sqlite3.Error → StorageDetachedError）
│   │   │   └── filename.py       # 添付ファイル名サニタイズ
│   │   ├── security/
│   │   │   └── keyring_store.py  # 資格情報の安全な保管
│   │   └── logging_config.py     # ロガー設定・個人情報マスキング
│   │
│   ├── presentation/         # 【最外層】PySide6固有の処理
│   │   ├── views/
│   │   │   ├── main_window.py    # 3ペインのメイン画面
│   │   │   ├── message_list.py   # QTableView + 遅延ロードモデル
│   │   │   ├── detail_view.py    # メール本文表示（サンドボックス）
│   │   │   ├── setup_wizard.py   # 初回セットアップ（ルート選択・アカウント・フォルダ選択）
│   │   │   ├── import_wizard.py  # PSTインポートウィザード（選択→probe→抽出→取込→サマリ）
│   │   │   └── dialogs/          # 削除確認・整合性チェック・設定
│   │   ├── models/               # QAbstractTableModel 実装（ページング）
│   │   ├── viewmodels/           # 画面状態とユースケースの仲介
│   │   ├── web/                  # URLインターセプタ・cid:スキームハンドラ
│   │   ├── native/
│   │   │   └── device_watcher.py # ★WM_DEVICECHANGE 監視（QAbstractNativeEventFilter）
│   │   └── threads/              # QThread ワーカー（同期・検索・検証）
│   │
│   └── migrations/
│       ├── 001_init.sql
│       ├── 002_sync_cursor.sql
│       └── 003_pst_import.sql
│
└── tests/
    ├── unit/                 # ドメイン・ユースケース・パーサの単体テスト
    ├── integration/          # DB・IMAP・readpstの結合テスト
    └── fixtures/
        ├── eml/              # 壊れたMIME・各種日本語エンコーディングのテストコーパス
        └── pst/              # 小規模な検証用PST（日本語フォルダ名・添付・破損PSTを含む）
```

> `BaseMessageRepository` を残す目的は「SQLiteを別DBへ差し替えるため」ではなく、**ユースケースの単体テストでインメモリ実装に差し替えるため**である。この目的を超えるメソッドを足さないこと。

### **2.3 アーキテクチャ（プロトコル抽象化パターン）**

UI層・DB保管層と通信層を独立させるため、アダプターパターンを採用する。上位層はプロトコル固有の例外・識別子を一切知らない。

```Plaintext
[ UI (PySide6) ] ─ [ ViewModel ] ─ [ UseCases ]
                                        │
        ┌──────────────────┬────────────┴─────────┬────────────────┐
        ▼                  ▼                      ▼                ▼
 BaseMailFetcher  BaseArchiveImporter   BaseMessageRepository  EmlStorage
        │                  │                      │                │
        │                  ▼                      ▼                ▼
        │           ReadpstImporter            SQLite          eml/*.eml
        │           (subprocess:            (metadata.db)
        │            vendor/readpst)
        ├─ OnamaeImapFetcher  (Basic Auth + imaplib)
        └─ GmailOAuthFetcher  (将来実装: OAuth2 + XOAUTH2)
```

**`BaseMailFetcher` と `BaseArchiveImporter` は統合しない。** 前者は「接続・増分同期・サーバー削除」を持つ生きた接続の抽象であり、後者は「1回きりの一括変換」である。共通点が `EmlStorage` / `BaseMessageRepository` への出力だけであるため、無理に共通の基底へまとめると両者の制約（レジューム可否・キャンセル粒度・削除操作の有無）が噛み合わなくなる。

### **2.4 保存先ディレクトリ・ストレージ構造**

データ保存先（外付けSSD等）を「ストレージルート」とし、**すべて相対パスで管理**する。DB・EML・ログはすべてこのルート配下に配置し、ルートごとコピーすれば完全なバックアップになる構造とする。

```Plaintext
[ストレージルート] (例: E:\MailArchive\)
├── metadata.db              # メタデータ・インデックス用 SQLite DB
├── metadata.db-wal          # WALモード用（自動生成）
├── metadata.db.bak          # 定期バックアップ（sqlite3 backup API）
├── .maildock_root           # ルート識別マーカー（後述の再検出に使用）
├── .lock                    # 多重起動防止用ロックファイル
├── manifests/               # ★DB再構築用のappend-only永続マニフェスト
│   ├── imap/{account_id}/   # 取得元フォルダ・UID・EMLパス・監査イベント
│   └── pst/{import_uuid}/   # import.json / folders.json / items.jsonl
├── tmp/                     # 書き込み中の一時ファイル（起動時に清掃）
│   └── pstimp/{job_id}/     # ★PST抽出の一時領域（readpstの出力先。取込完了後に削除）
├── logs/
│   ├── sync-2026-07-25.log  # 同期詳細ログ（90日保持）
│   └── pstimp-{job_id}.log  # readpstのデバッグログ（-d）と取込結果
└── eml/
    ├── {account_id}/        # IMAPアカウント (例: user@example.com)
    │   └── 2026/            # ★ INTERNALDATE（サーバー受信時刻）基準
    │       └── 07/
    │           └── {sha256の先頭32桁}.eml
    └── pst_{12桁hex}_{8桁UUID}/ # ★PST取込世代ごとの擬似アカウント（4.10参照）
        ├── 2008/09/{sha256の先頭32桁}.eml
        └── unknown/         # Date ヘッダが解釈できなかったもの
```

**設計上のポイント**

1. **ファイル名は `sha256(eml_bytes)` の先頭32桁**とする（Message-IDのハッシュではない）。理由:
   * Message-ID が欠損しているメールでも一意に決定できる
    * 完全に同一内容のメールは物理ファイルを共有できる（メール項目自体は重複排除しない）
   * ファイル名自体が整合性チェックサムとして機能する
2. **年月ディレクトリは `Date` ヘッダではなく IMAP の `INTERNALDATE`** を使う。`Date` ヘッダは偽装・欠損・不正フォーマットがあり、パス決定に使うと分散が壊れるため。
3. **ストレージルートの再検出:** ルート直下に `.maildock_root`（UUIDと作成日時を記録したJSON）を置く。設定ファイルには「直近のパス候補リスト」を保存し、起動時に各候補の `.maildock_root` のUUIDを照合することで、**ドライブレターが変わっても自動追従**する。見つからない場合のみユーザーに再選択を求める。
4. **ネットワークドライブの制限:** SQLiteのWALモードはSMB共有上で正しく動作しない。ルート選択時にドライブ種別を判定し、ネットワークドライブの場合は警告を表示する（続行時は `journal_mode=DELETE` にフォールバック）。
5. **空き容量チェック:** ルート選択時および同期開始前に空き容量を確認し、**残り20GB未満で警告、5GB未満で同期を中止**する。PSTインポート開始前は別途 **PSTサイズ × 2.5** の空きを確認し、不足時は開始させない。
6. **アカウントIDはファイルシステム安全でなければならない。** `eml/{account_id}/` にそのまま使うため、Windowsで使用できない文字（`: \ / * ? " < > |`）を含めてはならない。IMAPはメールアドレスをそのまま使えるが、**PST擬似アカウントは `pst_{原本SHA-256の先頭12桁}_{import_uuidの先頭8桁}` 形式**とする。`import_uuid` は取込開始前に生成する。完全な原本SHA-256はDBとマニフェストで保持し、短縮値を同一性判定には使わない。
7. **永続マニフェスト:** PSTでは `import.json` に原本情報と変換条件、`folders.json` に元フォルダ対応、`items.jsonl` に `source_item_key`・元相対パス・最終EMLパス・完全ハッシュ・状態イベントを保存する。purge時も行を削除せずイベントを追記する。IMAPも再構築に必要な取得元情報を同様に記録する。
   * **マニフェストの各JSONL行末にペイロードのCRC32を付与する。** 「JSONとしては読めるが内容が途中で切れている」torn write を検出可能にするため。復旧時は末尾の不正行だけを切り離す（4.8「マニフェスト検証」）。
8. **共有EMLの削除:** 同一内容を指す複数レコードが同じ `relative_path` を共有し得る。purgeでは非purgedの参照が残っていないことを確認し、最後の参照が消える場合だけ実ファイルを削除する。
    * 物理共有は同一アカウント内に限定し、DBの完全な `file_hash` で候補を検索した後、既存EMLをその場で再ハッシュして一致した場合だけ行う。ファイル名に使うSHA-256先頭32桁だけでは同一性を判定しない。
9. **`tmp/` は必ずストレージルート配下（＝EMLと同一ボリューム）に置く。** `os.replace` の原子性は同一ボリューム内でのみ成立し、`%TEMP%`（C:）を経由させると「コピー＋削除」に退化して、切断時に中途半端なEMLが本番ディレクトリへ残る。**この配置を「ただの慣習」として動かしてはならない。**
10. **ルートの同定は常に `.maildock_root` のUUIDで行う。** 外付けドライブでは、再接続時に**別のデバイスが同じドライブレターを取得し得る**。「パスが存在する＝自分のルート」という判定は成立しない。プローブ結果は `OK` / `MISSING` / `FOREIGN`（UUID不一致）の3値とし、**`FOREIGN` は `MISSING` より危険**（他人のドライブへの書き込み事故）として即座に全書き込みを禁止する。

## **3\. データベース設計 (SQLite)**

### **3.1 アカウント管理テーブル (accounts)**

```sql
CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,   -- アカウントID。★ファイルシステム安全な文字列であること
                                      --   IMAP  : "user@example.com"
                                      --   PST   : "pst_{原本SHA-256の先頭12桁}_{import_uuidの先頭8桁}"
    provider_type TEXT NOT NULL,      -- 'onamae_imap' / 'gmail_oauth' / 'pst_import'
    display_name  TEXT,
    host          TEXT,               -- pst_import では NULL
    port          INTEGER DEFAULT 993,-- pst_import では NULL
    username      TEXT,               -- パスワードは keyring 側に保管（DBには保存しない）
    is_enabled    INTEGER NOT NULL DEFAULT 1,
    created_at    DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
```

* `provider_type = 'pst_import'` のアカウントに対しては、**同期・サーバー削除・フォルダ選択・定期同期をコードレベルで無効化**する（4.10参照）。UIで隠すだけではなく、ユースケース入口でガードすること。

### **3.2 フォルダ・同期状態テーブル (folders)**

IMAPの正式な識別子は Message-ID ではなく **UIDVALIDITY + UID** である。増分同期を成立させるため、フォルダ単位の同期状態を保持する専用テーブルを設ける。同期対象の選択（ユーザー選択式）もここで管理する。

```sql
CREATE TABLE IF NOT EXISTS folders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     TEXT NOT NULL REFERENCES accounts(id),
    raw_name       TEXT NOT NULL,     -- IMAPへ発行する生の名前（modified UTF-7）
    display_name   TEXT NOT NULL,     -- デコード済み表示名
    uidvalidity    INTEGER,           -- 変化したら当該フォルダを再同期
    last_seen_uid  INTEGER NOT NULL DEFAULT 0,  -- ここまで取得済み（レジューム用）
    is_sync_target INTEGER NOT NULL DEFAULT 0,  -- ★ユーザー選択式。既定は同期しない
    last_synced_at DATETIME,
    UNIQUE(account_id, raw_name)
);
```

* 新規フォルダをサーバー上に検出した場合、`is_sync_target = 0` で登録し、UIに「新しいフォルダが見つかりました」と通知する（**勝手に同期を開始しない**）。
* `last_seen_uid` は新着取得済みの最大UID（高水位）として使う。Phase 1の `002_sync_cursor.sql` で `backfill_next_uid` と `initial_sync_completed` を追加し、初回履歴同期の下向きカーソルを分離する。
* 初回／UIDVALIDITY変更時はサーバー最大UIDを `last_seen_uid` と `backfill_next_uid` の両方へ設定する。新着・履歴とも最新優先で降順処理する。新着の `last_seen_uid` は同期開始時に固定した最大UIDまで全件処理した後だけ更新し、中断時は同じ範囲を冪等に再走査する。履歴の `backfill_next_uid` はバッチごとに更新する。
* **PSTアーカイブでの使い方:** `raw_name` にはreadpst出力ルートからの**相対ディレクトリパス**を登録する。`display_name` はlspst結果と一意に対応づけられた場合だけ元のPST表示名を使い、対応が曖昧な場合はreadpst出力名を使ってマニフェストに `original_name_unresolved=true` を記録する。`uidvalidity = NULL` / `last_seen_uid = 0` / `is_sync_target = 0` のまま固定とし、**同期対象として拾われないことを保証**する。

### **3.3 メッセージメタデータテーブル (messages)**

```sql
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   TEXT    NOT NULL REFERENCES accounts(id),
    folder_id    INTEGER NOT NULL REFERENCES folders(id),

    -- 識別子（Message-ID は欠損・重複し得るため単独UNIQUEにはしない）
    message_id   TEXT,                -- ヘッダーの Message-ID。NULL 許容
    content_key  TEXT NOT NULL,       -- 非一意の照合用。message_id、無ければ 'sha256:xxxx'
    source_item_key TEXT NOT NULL,    -- 取得元での一次識別子。IMAPはUID組、PSTはStage A確定項目キー
    uid          INTEGER,             -- IMAP UID
    uidvalidity  INTEGER,

    -- 状態管理（サーバー側とローカル側を直交した2軸で管理）
    remote_state TEXT NOT NULL DEFAULT 'present',
    -- 'present'   : サーバー上に存在
    -- 'deleted'   : サーバー上から削除済み（ローカル保管のみ）
    -- 'moved'     : 別フォルダへ移動された（moved_to_folder_id を参照）
    -- 'unknown'   : 未確認（同期エラー等）
    -- 'no_remote' : ★対応するサーバーが存在しない（PSTインポート由来）。永久にこの値
    moved_to_folder_id INTEGER REFERENCES folders(id),

    local_state  TEXT NOT NULL DEFAULT 'active',
    -- 'active'  : 通常表示
    -- 'trashed' : アプリ上のゴミ箱へ移動（EMLは保持）
    -- 'purged'  : 30日経過し実ファイル削除済み（墓標レコードのみ残る）
    trashed_at   DATETIME,            -- ゴミ箱投入日時。+30日で purge 対象

    -- 実体
    relative_path TEXT,               -- purged 後は NULL
    file_hash     TEXT,               -- SHA-256（整合性検証・削除前検証に使用）

    -- 主要表示ヘッダー
    subject      TEXT,
    sender       TEXT,
    recipient    TEXT,
    cc           TEXT,
    date_sent    DATETIME,            -- Date ヘッダ（表示・ソート用、UTC ISO8601）
    internal_date DATETIME,           -- IMAP INTERNALDATE（保存パス決定用、UTC ISO8601）
    size_bytes   INTEGER,
    has_attachment INTEGER NOT NULL DEFAULT 0,

    -- IMAPフラグのスナップショット（★後付けだと全件再取得が必要なため初期から保持）
    imap_flags   TEXT,               -- '\\Seen \\Flagged \\Answered' 等をスペース区切りで保存
    flags_seen_at DATETIME,          -- このフラグを確認した日時（あくまで過去のスナップショット）

    -- スレッド情報（★後付けすると全EML再解析が必要なため初期から保持）
    in_reply_to    TEXT,              -- In-Reply-To ヘッダの Message-ID
    references_ids TEXT,              -- References ヘッダ（スペース区切りの生値）
    thread_key     TEXT,              -- 会話ルートの Message-ID（同期時に算出）

    last_seen_at DATETIME,            -- 最後にサーバーで確認できた日時
    created_at   DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX idx_msg_key    ON messages(account_id, content_key);  -- 移動検知の横断照合用
CREATE INDEX idx_msg_list   ON messages(folder_id, date_sent DESC);-- 一覧表示用
CREATE INDEX idx_msg_thread ON messages(thread_key, date_sent);    -- スレッド表示用
CREATE INDEX idx_msg_trash  ON messages(local_state, trashed_at);  -- purge 対象の抽出用

CREATE UNIQUE INDEX uq_imap_message
ON messages(account_id, folder_id, uidvalidity, uid)
WHERE uid IS NOT NULL;

CREATE UNIQUE INDEX uq_archive_message
ON messages(account_id, folder_id, source_item_key)
WHERE uid IS NULL;
```

* IMAPの `source_item_key` は `"{uidvalidity}:{uid}"` とし、UIDVALIDITY変更前後の項目を混同しない。
* PSTの `source_item_key` はStage A完了時にstaging内相対パスから安定的に生成し、`pst_import_items` と永続マニフェストへ同じ値を保存する。
* `content_key` には一意性を期待せず、同一Message-IDを持つ複数項目を欠落させない。

**スレッド情報の扱い**

* `thread_key` は「`References` の先頭ID → 無ければ `In-Reply-To` → 無ければ自身の `Message-ID`」の順で決定し、**同期時に確定して列に保存**する（検索時に毎回計算しない）。
* 参照先メールが後から取り込まれるケースがあるため、`thread_key` は**同期バッチの最後に再解決するパス**を用意する。

**IMAPフラグの扱い（スナップショット方式）**

* 同期時点の `\Seen` / `\Flagged` / `\Answered` 等をそのまま保存し、**閲覧時の表示にのみ使用**する。
* **双方向同期は行わない**。ローカルでの操作をサーバーへ反映する機能はスコープ外とする（バックアップ用途では不要であり、複雑度に見合わない）。
* **ローカル独自の既読管理も行わない**。本アプリは閲覧専用アーカイブとして位置づける。
* `flags_seen_at` を併記することで、UI上で「この情報はいつ時点のものか」を示せるようにする。

**PSTインポート由来レコードの列の使い方**

| 列 | PST由来での値 |
| :---- | :---- |
| `remote_state` | 常に `'no_remote'` |
| `uid` / `uidvalidity` | `NULL`（IMAPの概念がないため） |
| `source_item_key` | Stage A完了時に固定する項目キー（例: staging内相対パスのハッシュ）。同一フォルダ内で同じMessage-IDが重複しても別項目として保持する |
| `content_key` | `Message-ID` があればそれ、無ければ `'sha256:xxxx'`。非一意の照合用で、PSTとIMAPの突合には使わない |
| `internal_date` | 常に `NULL`。保存年月には有効な `Date` ヘッダだけを使い、失敗・未来日時の場合は `unknown/` とする |
| `imap_flags` / `flags_seen_at` | `NULL`。readpstが出力し得る `Status: RO` はPST既読状態として取り込まない |
| `last_seen_at` | `NULL` |
| `local_state` | IMAP側と完全に同じ。ゴミ箱・30日purge は共通で機能する |

### **3.4 全文検索テーブル (message\_contents / messages\_fts)**

FTS5 の external content 方式は「content テーブルに**同名の列が実在すること**」が前提である。`messages` に本文列は無いため、**本文専用テーブルを別に設け、そちらを content テーブルにする**。

```sql
CREATE TABLE IF NOT EXISTS message_contents (
    message_id       INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    subject_norm     TEXT,   -- 正規化済み件名
    sender_norm      TEXT,   -- 正規化済み差出人（表示名＋アドレス）
    body_text        TEXT,   -- 正規化済み本文（HTMLはタグ除去後）
    attachment_names TEXT    -- 添付ファイル名（スペース区切り、インライン画像は除外）
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject_norm,
    sender_norm,
    body_text,
    attachment_names,
    content='message_contents',
    content_rowid='message_id',
    tokenize='trigram'
);
```

**external content は自動同期されないため、以下3本のトリガーが必須。**

```sql
CREATE TRIGGER mc_ai AFTER INSERT ON message_contents BEGIN
  INSERT INTO messages_fts(rowid, subject_norm, sender_norm, body_text, attachment_names)
  VALUES (new.message_id, new.subject_norm, new.sender_norm, new.body_text, new.attachment_names);
END;

CREATE TRIGGER mc_ad AFTER DELETE ON message_contents BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, subject_norm, sender_norm, body_text, attachment_names)
  VALUES ('delete', old.message_id, old.subject_norm, old.sender_norm, old.body_text, old.attachment_names);
END;

CREATE TRIGGER mc_au AFTER UPDATE ON message_contents BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, subject_norm, sender_norm, body_text, attachment_names)
  VALUES ('delete', old.message_id, old.subject_norm, old.sender_norm, old.body_text, old.attachment_names);
  INSERT INTO messages_fts(rowid, subject_norm, sender_norm, body_text, attachment_names)
  VALUES (new.message_id, new.subject_norm, new.sender_norm, new.body_text, new.attachment_names);
END;
```

> `local_state = 'purged'` にする際は `message_contents` の行を削除する（トリガーによりFTSからも消える）。`messages` の墓標レコードは残すため、「かつて存在した」記録は失われない。

### **3.5 運用テーブル (sync\_failures / audit\_log)**

```sql
-- 個別メールの取得・解析失敗を記録し、次回同期時に自動再試行する
CREATE TABLE IF NOT EXISTS sync_failures (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     TEXT NOT NULL,
    folder_id      INTEGER NOT NULL,
    uid            INTEGER NOT NULL,
    error_class    TEXT NOT NULL,     -- 'transient' / 'permanent' / 'parse' / 'oversize'
    error_message  TEXT,
    attempt_count  INTEGER NOT NULL DEFAULT 1,
    first_failed_at DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_failed_at  DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(account_id, folder_id, uid)
);

-- 破壊的操作の監査記録（★永久保存。purge対象にしない。永続マニフェストにも同じイベントを追記）
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    operation   TEXT NOT NULL,        -- remote_delete/remote_trash/local_purge/pst_import/
                                      -- pst_reimport/pst_supersede/pst_import_abandon
    account_id  TEXT,
    message_id  TEXT,
    subject     TEXT,
    size_bytes  INTEGER,
    detail      TEXT
);
```

* Phase 1の `002_sync_cursor.sql` で `sync_failures.uidvalidity` を追加し、一意性を `(account_id, folder_id, uidvalidity, uid)` へ変更する。UIDVALIDITY変更後は現在世代の失敗だけを再試行し、旧世代の失敗履歴は保持する。

### **3.5.1 PSTインポート履歴テーブル (pst\_imports)**

原本 `.pst` はアプリ管理外に置くため、DBと永続マニフェストの双方に「どのPSTからいつ・どの条件で取り込んだか」を記録する。同一PSTの二重取り込み検出、中断したStage Bの再開、再変換時の世代交代に使う。

```sql
CREATE TABLE IF NOT EXISTS pst_imports (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    import_uuid      TEXT NOT NULL UNIQUE, -- 取込開始前に生成。パス・マニフェスト・再開の安定キー
    account_id       TEXT NOT NULL REFERENCES accounts(id),
    source_filename  TEXT NOT NULL,     -- 取込時のファイル名（パスではなく名前のみ）
    source_sha256    TEXT NOT NULL,     -- ★原本PST全体のSHA-256。二重取込検出のキー
    source_size_bytes INTEGER,
    source_mtime     DATETIME,
    readpst_version  TEXT,              -- readpst -V の出力（後で再変換判断に使う）
    options_json     TEXT,              -- 実行時オプション（charset / 削除済み含む 等）
    status           TEXT NOT NULL,     -- extracting/ready_to_ingest/ingesting/cancelled_resumable/
                                        -- failed_resumable/completed/completed_with_errors/abandoned/superseded
    is_active        INTEGER NOT NULL DEFAULT 0,
    replaces_id      INTEGER REFERENCES pst_imports(id),
    superseded_at    DATETIME,
    total_files      INTEGER,           -- Stage A 完了後に確定
    ingested_count   INTEGER NOT NULL DEFAULT 0,
    failed_count     INTEGER NOT NULL DEFAULT 0,
    staging_path     TEXT,              -- ストレージルートからの相対パス（再開用。完了後 NULL）
    started_at       DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    finished_at      DATETIME,
    error_message    TEXT
);

CREATE INDEX idx_pst_src ON pst_imports(source_sha256);
CREATE UNIQUE INDEX uq_active_pst_source
ON pst_imports(source_sha256) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS pst_import_items (
    import_id            INTEGER NOT NULL REFERENCES pst_imports(id),
    source_item_key      TEXT NOT NULL,
    source_relative_path TEXT NOT NULL,
    folder_relative_path TEXT NOT NULL,
    source_size_bytes    INTEGER,
    source_sha256        TEXT,
    final_relative_path  TEXT,
    message_row_id       INTEGER REFERENCES messages(id),
    status               TEXT NOT NULL,
    error_class          TEXT,
    error_message        TEXT,
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (import_id, source_item_key)
);
```

* 取込開始時に完全な `source_sha256` を照合する。未完了ジョブがあれば同じ `import_id`・staging・項目マニフェストを使って「再開」または「未完了ジョブを破棄」を選ばせ、新しい取込行を作らない。
* `is_active=1` の完成済み世代があれば原則として通常取り込みを禁止し、明示的な「再変換」フローだけを許可する。
* `pst_import_items` はStage A完了時に全項目を固定し、Stage Bは未完了項目だけを処理する。ディレクトリ走査順や連番には依存しない。
* PST固有の失敗は `pst_import_items` に記録し、IMAP UIDを前提とする `sync_failures` は流用しない。

### **3.6 DB運用方針（接続・並行性・マイグレーション）**

**接続時に必ず設定するPRAGMA**

```python
conn.execute("PRAGMA journal_mode=WAL")  # 起動時に1回（ネットワークドライブでは不可）
conn.execute("PRAGMA synchronous=NORMAL")  # WAL時はNORMALで十分
conn.execute("PRAGMA foreign_keys=ON")  # 接続ごとに必須（既定でOFF）
conn.execute("PRAGMA busy_timeout=10000")
conn.execute("PRAGMA temp_store=MEMORY")
conn.execute("PRAGMA cache_size=-64000")  # 64MB
```

* `synchronous=NORMAL` は「DBはEML＋マニフェストから再構築可能な派生キャッシュである」（不変条件1）という前提の上で成立する。**マニフェスト追記のfsyncを省略した瞬間にこの前提は崩れる。**
* リムーバブルメディア運用では、バッチコミット時のみ `synchronous=FULL` へ切り替える案（案B）も選択肢とする。安全性は上がるが外付けSSDでは体感差が出るため、**Phase 4 の実機テストで実測してから決定する**（既定は `NORMAL`）。

**WALサイズの抑制（不意の切断への備え）**

* 長時間同期で `-wal` が肥大すると、切断時に失われ得る範囲＝復旧時に再検証すべき範囲がそのまま広がる。
* **バッチコミット10回ごと（＝約1,000通ごと）に `PRAGMA wal_checkpoint(TRUNCATE)` を実行**し、露出範囲を一定以下に保つ。
* アプリ終了時および「安全な取り外し」実行時にも必ずチェックポイントする（5.7.1参照）。

**スレッドモデル（単一ライター方式）**

| スレッド | 接続 | 役割 |
| :---- | :---- | :---- |
| UIスレッド | 読み取り専用 | 一覧・詳細の表示。WALのためライターをブロックしない |
| 同期ワーカー×1 | 書き込み専用 | **すべての書き込みをここに集約**。100通ごとにバッチコミット |
| 検索ワーカー | 読み取り専用 | スレッドローカル接続 |

* SQLite接続は**スレッドをまたいで共有しない**（`threading.local()` で管理）。
* 1通ごとのコミットは外付けSSDで極端に遅くなるため、**必ずバッチコミット**する。
* **PST取込ワーカーも同期ワーカーと同じ「単一ライター」枠を使う**。同期とPST取込の**同時実行は許可しない**（先行する方が完了するまでUIでブロックする）。

**スキーマバージョン管理**

* `PRAGMA user_version` を採用し、`migrations/001_init.sql` から順次適用する。
* **マイグレーション実行前に `metadata.db.bak.{version}` へ自動バックアップ**を取る。
* `source_item_key` とプロバイダー別一意インデックスは `001_init.sql` から導入する。Phase 1の `002_sync_cursor.sql` で二カーソルとUIDVALIDITY別失敗管理を追加し、`003_pst_import.sql`（Phase 4.5）で `pst_imports` / `pst_import_items` を追加する。`remote_state='no_remote'` はCHECK制約を置かずアプリ側で検証する。
* Phase 5（Gmail対応）では「1通が複数ラベルに属する」ため、`messages.folder_id` を `message_folders` 中間テーブルへ移行する想定。この移行計画を最初からマイグレーション履歴に織り込んでおく。

**多重起動防止とスタールロックの検出**

* ストレージルート直下の `.lock` を `msvcrt.locking` で排他ロックする。取得失敗時は「他のインスタンスが使用中です」を表示して起動を中止する。
* **不意の切断ではロックハンドルだけが消え、`.lock` ファイル自体は再マウント後に残る。** 単純な「ファイルの存在＝使用中」判定にすると、切断のたびに起動できなくなる。
* `.lock` の中身に `{pid, instance_uuid, machine_id, heartbeat_at}` をJSONで書き、稼働中は10秒ごとに `heartbeat_at` を更新する。判定は以下のとおり。

| ロック実体 | `heartbeat_at` | 判定 |
| :---- | :---- | :---- |
| 取得できない | ― | 他プロセスが本当に使用中。起動を中止 |
| 取得できる | 十分に新しい | 稀な競合。短時間リトライ後に中止 |
| 取得できる | 古い / 読めない | **前回の異常終了**。5.7.1 の復帰フロー（クリーンシャットダウンフラグ検査）へ進む |

## **4\. 機能要件とモジュール設計**

### **4.1 プロトコル抽象化層 (BaseMailFetcher)**

すべてのメール取得プロバイダが実装すべき統一インターフェース。**イテレータによる遅延評価・キャンセル対応・フォルダコンテキストの明示**を設計の要件とする。

```python
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RemoteFolder:
    raw_name: str  # IMAPへ発行する生の名前
    display_name: str  # modified UTF-7 デコード済み
    uidvalidity: int | None


@dataclass(frozen=True)
class RemoteMessageRef:
    uid: int
    message_id: str | None
    internal_date: datetime | None
    size_bytes: int | None  # 事前にサイズが分かるとスキップ判定に使える


class CancelToken:
    """UIスレッドから threading.Event を set() することで長時間処理を中断する"""

    def __init__(self, event):
        self._event = event

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise OperationCancelled()


class BaseMailFetcher(ABC):
    @abstractmethod
    def connect(self) -> None: ...  # 失敗は例外で通知（boolを返さない）
    @abstractmethod
    def disconnect(self) -> None: ...
    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()

    @abstractmethod
    def list_folders(self) -> list[RemoteFolder]: ...
    @abstractmethod
    def select_folder(self, raw_name: str) -> int: ...  # 戻り値: UIDVALIDITY

    @abstractmethod
    def iter_message_refs(
        self, raw_name: str, since_uid: int = 0, *, cancel: CancelToken
    ) -> Iterator[RemoteMessageRef]: ...  # ページングしながら遅延生成

    @abstractmethod
    def list_existing_uids(self, raw_name: str) -> set[int]: ...  # 削除検知用（UIDのみで軽量）

    @abstractmethod
    def download_eml_bytes(self, raw_name: str, uid: int) -> bytes: ...

    @abstractmethod
    def delete_remote_message(
        self,
        raw_name: str,
        uid: int,
        *,
        mode: str = "trash",  # "trash" | "expunge"
    ) -> None: ...
```

**ドメイン例外階層 (`domain/errors.py`)**

infrastructure 層が `imaplib.IMAP4.error` / `ssl.SSLError` / `socket.timeout` などを以下へラップし、上位層はプロトコル固有の例外を一切知らない。

```
MailDockError
├── AuthenticationError   # 再試行不可。UIで資格情報の再入力を促す
├── TransientError        # 再試行可（ネットワーク断、一時的なNO応答）
├── PermanentError        # 再試行不可（フォルダ不存在、権限不足）
├── UidValidityChanged    # 当該フォルダの再同期をトリガー
├── OversizeError         # 1通あたりのサイズ上限超過
└── OperationCancelled
```

* **リトライはユースケース層に集約**し、Fetcher実装には入れない。`TransientError` のみ指数バックオフ（1s→2s→4s、最大3回、jitter付き）。
* お名前.comは同時接続数に制限があるため、**1アカウントあたりの接続は1本に直列化**し、並列フェッチは行わない。

### **4.2 IMAP同期・バックアップ機能**

**1. 同期対象の決定（ユーザー選択式）**

* 初回セットアップおよび設定画面で、`list_folders()` の結果からユーザーが同期対象フォルダを選択する（`folders.is_sync_target`）。
* 新規フォルダを検出した場合は非対象で登録し、UIに通知するのみとする。
* **アカウント間は常に順次処理**とし、並列同期は行わない（サーバー側の同時接続数制限と帯域競合を避けるため）。

**2. 初回同期の取り込み順序**

* **新しいメールから順に取り込む**（UID降順）。日付範囲の事前指定は求めない。
* 初回は100GB規模で数時間を要するため、**いつでも中断し、次回起動時に続きから再開できる**ことを前提とする。途中でも取得済み分は即座に閲覧・検索できる。
* 進捗UIには「取得済み / 推定全体」をバイト数と通数の両方で表示し、残り時間を推定表示する。

**3. 取得サイズ上限**

* 1通あたりの上限を設ける（**既定 50MB**、設定で変更可）。
* 上限超過分は `sync_failures` に `'oversize'` として記録し、**ヘッダ情報のみをDBに登録**して一覧に表示する（存在したこと自体は見失わない）。
* スキップされたメールは、UIから**個別に「それでも取得する」を実行できる**ようにする。
* `iter_message_refs()` が返す `size_bytes` により、**ダウンロード前に判定**する（無駄な転送を発生させない）。

**4. 増分ダウンロード（UIDベース）**

```
for folder in 同期対象フォルダ:
    uidvalidity = select_folder(folder.raw_name)
    if 初回 or uidvalidity != folder.uidvalidity:
        max_uid = get_max_uid(folder.raw_name)
        → last_seen_uid=max_uid, backfill_next_uid=max_uid で二カーソルを初期化

    # 初回同期中に到着した新着。開始時の範囲を固定して最新から降順。
    new_max_uid = get_max_uid(folder.raw_name)
    for ref in iter_message_refs(
        folder.raw_name,
        min_uid=folder.last_seen_uid + 1,
        max_uid=new_max_uid,
        descending=True,
    ):
        → EMLを保存（4.7の順序を厳守）→ 解析 → messages / message_contents へ登録
    → 固定した新着範囲を全件処理した場合だけ last_seen_uid=new_max_uid

    # 初回履歴。最新から利用可能にするため下向きカーソルから降順。
    for ref in iter_message_refs(
        folder.raw_name, max_uid=folder.backfill_next_uid, descending=True
    ):
        if ref.size_bytes > 上限:  → sync_failures に 'oversize' で記録しスキップ
        raw = download_eml_bytes(folder.raw_name, ref.uid)
        → EMLを保存（4.7の順序を厳守）→ 解析 → messages / message_contents へ登録
    100通ごと（または50MBごと）に、メッセージと該当カーソルを同一トランザクションでコミット
```

* **`Message-ID` の全件取得による差分検出は行わない。** 5万通規模で毎回全ヘッダをFETCHするのは非現実的であり、UIDの範囲指定で取得する。
* 進捗表示は**転送バイト数を主指標**とする（平均2MB/通のため通数では実感と乖離する）。
* 新着同期は固定範囲を完了したときだけ `last_seen_uid` を更新する。途中で中断した場合は同じ範囲を再走査し、確定済み行をupsert・dedupeして欠損を防ぐ。履歴同期は `backfill_next_uid` をバッチごとに更新し、途中から再開する。

**5. 消去・移動の検知**

* `list_existing_uids()` でサーバー上のUID集合を取得し、ローカルに存在してサーバーに無いUIDを抽出する。
* 抽出された各メールについて、同一アカウント内の他フォルダにある `remote_state='present'` の候補を照合する。
    * `content_key` と完全な `file_hash` が一致する候補が1件だけ → `remote_state = 'moved'`、`moved_to_folder_id` を設定
    * 候補が存在しない → `remote_state = 'deleted'`
    * 候補が複数、または完全な `file_hash` が得られず一意に確定できない → `remote_state = 'unknown'`、`moved_to_folder_id = NULL`
* `content_key` は非一意であるため、単独一致だけで移動と確定しない。
* いずれの場合も **EMLファイルは絶対に削除しない**。

### **4.3 サーバーメール手動削除機能**

**取り返しがつかない唯一の操作**であるため、多段の安全装置を必須要件とする。

**削除実行の必須事前条件（コードで強制し、満たさないメールは対象から自動除外する）**

1. ローカルにEMLファイルが実在する
2. その場で再計算した SHA-256 が `messages.file_hash` と一致する
3. `message_contents` に解析結果が存在する（＝パースに成功済み）

**実行フロー**

| 段階 | 内容 |
| :---- | :---- |
| ドライラン | 削除対象の一覧（件名・日付・サイズ・合計容量）を表示。CSV保存可 |
| 確認ダイアログ | 件数と合計サイズを表示し、**件数を手入力させる**（誤クリック防止） |
| 既定動作 | **ゴミ箱フォルダへ MOVE**（`STORE +FLAGS \Deleted` ＋ `EXPUNGE` による完全抹消は明示オプション） |
| 監査ログ | `audit_log` へ日時・アカウント・Message-ID・件名・サイズ・モードを記録（永久保存） |
| 事後処理 | `remote_state = 'deleted'` に更新。一覧ではグレーアウト表示 |
| レート制限 | 1回の操作で削除できる上限を設ける（既定1,000通） |
| ★切断時ガード | ストレージルートが `ATTACHED` 以外の間は**ユースケース入口で無条件に拒否**する。ローカルEMLのハッシュ検証ができない状態でサーバー削除を通してはならない（不変条件3） |

**ゴミ箱フォルダの特定**

サーバーによってゴミ箱フォルダ名は異なる（`Trash` / `ゴミ箱` / `Deleted Items` / `INBOX.Trash` 等）ため、以下の順で決定する。

1. IMAPの **SPECIAL-USE 拡張（RFC 6154）** で `\Trash` 属性を持つフォルダを探す。
2. 見つからなければ、一般的な候補名を自動探索する。
3. それでも特定できなければ、**設定画面でユーザーに明示的に指定させる**（未指定の間は削除機能を無効化する）。

自動検出結果は設定画面に表示し、ユーザーが上書きできるようにする。

### **4.4 ローカルゴミ箱・実ファイル削除 (purge)**

Append-Onlyの例外として、ユーザーの明示操作に限り実ファイル削除を許可する。

1. ユーザーが「ローカルから削除」を実行 → `local_state = 'trashed'`、`trashed_at` を記録。**この時点ではEMLは残る。**
2. ゴミ箱ビューから「元に戻す」で `active` へ復帰できる。
3. `trashed_at` から**30日経過**したものが purge 候補となる。
4. purge実行時: マニフェストへ `purge_intent` を追記・fsync → 同じ `relative_path` を参照する非purgedレコードが無い場合だけEMLファイル削除 → マニフェストへ `purged` を追記・fsync → `message_contents` 削除（トリガーでFTSからも除去）→ `local_state='purged'`, `relative_path=NULL` に更新。
   * **`messages` の行は墓標レコードとして残す**（「かつて存在した」記録と監査可能性を維持するため）。
5. purge操作も `audit_log` に記録する。

**purgeの実行契機（設定画面で選択可能）**

| モード | 動作 | 備考 |
| :---- | :---- | :---- |
| **A: 手動のみ** | 自動実行しない。ゴミ箱画面で「今すぐ完全削除」を押したときのみ purge する | **既定値**。最も安全 |
| **B: 確認あり自動** | 起動時に30日経過分を検出し、対象一覧と確認ダイアログを表示してから実行 | ユーザーがキャンセルできる |
| **C: 完全自動** | 30日経過分を確認なしで purge する | 選択時に警告を表示する |

* いずれのモードでも、purge 実行前に**対象件数と合計サイズをログに記録**する。
* モードCを選択した場合は、設定画面上で「確認なしでファイルが削除されます」という警告を常時表示する。
* 猶予日数（既定30日）も設定で変更可能とする。

### **4.5 高速検索機能**

**検索対象:** 件名、差出人、本文、添付ファイル名。加えて構造化フィルタ（アカウント／フォルダ／日付範囲／添付有無／状態）とAND結合する。

> 添付ファイルは**ファイル名のみ**をインデックス化する。PDFやOffice文書の中身抽出は、依存ライブラリの増加と同期時間の大幅な増加（100GB規模では致命的）に見合わないため**スコープ外**とする。

**文字正規化ポリシー（最重要）**

インデックス投入時と検索時で**同一の正規化関数**を必ず適用する。片方だけだと恒久的にヒットしなくなる。

```python
def normalize_for_search(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)  # 全角英数→半角、半角カナ→全角カナ
    t = t.casefold()  # 大文字小文字の同一視
    t = re.sub(r"\s+", " ", t)  # 連続空白の圧縮
    return t.strip()
```

* ひらがな／カタカナの同一視は**行わない**（検索ノイズが増えるため）。
* 表示用の原文は `messages` 側、正規化済みテキストは `message_contents` 側に保持する（二重持ちを許容）。

**クエリ構築ロジック**

1. ユーザー入力を全角／半角スペースで分割し、各キーワードを正規化する。
2. **3文字以上**のキーワード → FTS5 `MATCH`。フレーズは `"` で囲み、内部の `"` は `""` にエスケープしてParse Errorを防ぐ。
3. **2文字以下**のキーワード → `message_contents` に対する `LIKE '%kw%' ESCAPE '\'`。
   * ※ trigramトークナイザーは3文字未満をインデックス化しないため、`MATCH` に短い語を渡しても**自動フォールバックはせず単に0件になる**。アプリ側での明示的な分岐が必須である。
   * 短い語が含まれる場合はテーブルスキャンが発生するため、UIに「短い語を含むため時間がかかる場合があります」と表示する。
4. AND検索なら結果ID集合を `INTERSECT`、OR検索なら `UNION` で合成し、構造化フィルタで最終的に絞り込む。

**スレッド表示**

* 一覧は**フラット表示を既定**とする（グルーピングしない）。検索結果との相性が良く、実装も軽いため。
* スレッドは**詳細画面から辿る**。本文プレビュー上部に「この会話のN件を表示」を置き、`thread_key` で絞り込んだ一覧を開く。
* 一覧のグルーピング表示（Gmail風）は将来拡張とし、`thread_key` 列を保持しているためいつでも追加可能とする。

### **4.6 閲覧・UI機能 (PySide6)**

**1. メイン画面**

* 3ペイン構成（左: アカウント／フォルダ／状態フィルタ、中央: メール一覧、右: 本文プレビュー）。
* 左ペインは **2つのルートノードに分ける**。これが「IMAPアーカイブ画面」と「PSTアーカイブ画面」の切り替え手段を兼ねる（別ウィンドウにはしない）。

```
▾ メールアカウント              ▾ PSTアーカイブ
   ▸ すべてのアカウント            ▸ すべてのPSTアーカイブ
   ▸ user@example.com            ▸ archive2015.pst
   ▸ info@example.com            ▸ backup2019.pst
```

* **「すべてのアカウント」による横断表示・横断検索は、各ルートの内側でのみ行う**。IMAPとPSTをまたいだ横断ビューは提供しない（1.3参照）。
* PSTアーカイブを選択中は、ツールバーの「同期」「サーバーから削除」を**非表示**にする。
* 一覧は `QAbstractTableModel` を自作し、`canFetchMore` / `fetchMore` による**200件単位の遅延ロード**を行う。ソート・フィルタは Qt 側ではなく **SQL側**（`ORDER BY ... LIMIT`）で処理する。
* 一覧にはアカウント列を表示し、横断表示時も出所が分かるようにする。

**2. 視覚的ステータス表示**

| 状態 | 表示 |
| :---- | :---- |
| `remote_state='deleted'` | グレーアウト＋サーバー削除済みアイコン |
| `remote_state='moved'` | 移動先フォルダ名をツールチップ表示 |
| `local_state='trashed'` | ゴミ箱ビューにのみ表示。残り日数を併記 |
| `local_state='purged'` | 「実体なし」表示。本文プレビュー不可 |
| `imap_flags` に `\Seen` 無し | 未読アイコン（**同期時点のスナップショットである旨をツールチップで明示**） |
| `imap_flags` に `\Flagged` | スターアイコン |
| `sync_failures` に `oversize` | 「未取得（サイズ上限超過）」バッジと個別取得ボタン |
| `remote_state='no_remote'` | バッジなし（PSTアーカイブではこれが常態のため、いちいち表示しない） |

> フラグは**表示専用**であり、UIから変更する手段は提供しない（ローカル既読管理も行わない）。

**3. 安全なHTML表示（5層防御）**

JavaScriptの無効化だけでは外部画像によるトラッキングを防げないため、以下をすべて実装する。

1. **オフレコプロファイル:** `QWebEngineProfile` をオフレコで生成し、`NoCache` / `NoPersistentCookies` を設定。
2. **属性の無効化:** `page.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, False)` を筆頭に、`LocalStorageEnabled` / `PluginsEnabled` / `LocalContentCanAccessRemoteUrls` / `LocalContentCanAccessFileUrls` / `AllowRunningInsecureContent` / `ScreenCaptureEnabled` 等をすべて `False` にする。
3. **リクエストインターセプタ:** `QWebEngineUrlRequestInterceptor` で**既定は全リクエストをブロック**し、インライン画像の `cid:` と本文配信の `maildock:` スキームのみ通す。「外部画像を表示」はメール単位の明示操作で解除する。
4. **cid: カスタムスキームハンドラ:** EML内の `Content-ID` 付きパートをインライン画像として供給する。本文HTML自体も `setHtml()` の約2MB制限を避けるため、カスタムスキーム経由で配信する。
5. **CSPの注入:** `default-src 'none'; img-src cid:; style-src 'unsafe-inline'; form-action 'none'; frame-src 'none'` を `<meta>` として挿入する。

加えて、`acceptNavigationRequest` をオーバーライドし、リンククリックは**`https` / `http` のみを許可**する。許可URLはURLを提示した確認ダイアログを経て `QDesktopServices.openUrl()` で外部ブラウザへ渡し、`file:` / `javascript:` / `data:` / OS登録済みカスタムスキーム等は確認なしで拒否する。アプリ内では遷移させない。`<meta http-equiv="refresh">` は除去する。

**4. 添付ファイル操作**

プレビュー画面から任意の場所へ保存できる。添付ファイル名は**送信者が制御できる敵性入力**であるため、保存時に以下を必ず適用する。

* パス成分（`../`、`\`、`/`）の除去
* 制御文字およびNTFS禁止文字 `<>:"/\|?*` の置換（`:` を含めることで代替データストリームを封じる）
* 末尾のドット・空白の除去、Windows予約名（`CON` `PRN` `AUX` `NUL` `COM1`〜 `LPT1`〜）の回避
* NFC正規化、長さ制限（パス長260対策）
* 保存直前に `Path(dest).resolve()` が指定ディレクトリ配下であることを再検証
* 実行可能拡張子（`.exe .scr .js .vbs .lnk .bat .cmd .ps1`）は保存時に警告を表示

### **4.7 EML解析・保存の方針**

**保存手順（この順序を厳守。1.3の不変条件2に対応）**

```
1. tmp/{uuid}.eml へ書き込み → flush() → os.fsync(fd)
2. SHA-256 を計算
3. os.replace(tmp, eml/{account}/{YYYY}/{MM}/{hash32}.eml)   ← アトミック配置
4. 永続マニフェストへ追記 → flush() → os.fsync()（DBバッチと同じ単位で可）
5. BEGIN IMMEDIATE → messages / message_contents を INSERT → COMMIT（バッチ）
```

**文字コードのフォールバック順序**

1. パートが宣言する charset（別名を正規化: `x-sjis` / `shift-jis` → `cp932`、`iso-2022-jp-ms` → `iso2022_jp_ext` 等）
2. `iso-2022-jp` → **`cp932`**（`shift_jis` ではなく機種依存文字に対応するcp932）→ `euc_jp` → `utf-8`
3. `charset-normalizer` による推定
4. 最終手段として `errors="replace"` で強制デコードし、**警告ログに記録する（例外は投げない）**

**ヘッダ・本文の抽出**

* ヘッダは `email.header.decode_header` + `make_header`（RFC 2047）。
* 添付ファイル名は **RFC 2231 の分割形式**（`filename*0*`, `filename*1*`）と、**Outlookが出す `filename=` へRFC2047を直接埋めた非標準形式**の両方に対応する（`get_filename()` は後者を処理しない）。
* 本文は `text/plain` を優先し、無ければ `text/html` をタグ除去して使用する。`multipart/alternative` は plain 優先、`multipart/related` は本体を辿る。
* `Content-ID` を持つインライン画像は**添付ファイル名リストから除外**する（検索ノイズになるため）。
* `Date` のパースは必ず try/except で包む。IMAPでは失敗時・未来日時（現在+1日超）の場合は `INTERNALDATE` にフォールバックする。PSTでは `internal_date=NULL` とし、有効な `Date` が無ければ `date_sent=NULL`、保存先は `unknown/` とする。日時は**常にUTCのISO8601文字列としてDBに保存**する。

**パース失敗時の扱い**

* **EMLは必ず保存する。** 生データさえ残っていれば、解析ロジックを改善した後に再解析で復旧できる。
* `message_contents` を空で登録する。IMAPは `sync_failures`、PSTは `pst_import_items` に `'parse'` として記録し、設定画面から最終EMLを使って「再解析」を実行できるようにする。

### **4.8 整合性チェック・再構築機能**

1.3の不変条件1（EMLと永続マニフェストが真実の情報源）を担保するため、以下を機能要件とする。

| 機能 | 内容 |
| :---- | :---- |
| クイック検証 | DB上の `relative_path` の存在確認とファイルサイズ照合 |
| ★範囲限定検証 | マニフェストの最終チェックポイント以降のEMLに対してのみSHA-256を再計算する。**不意の切断からの復帰専用**（5.7.1参照） |
| フル検証 | 全EMLのSHA-256を再計算してDBと照合し、破損ファイルを一覧表示 |
| 孤児スキャン | `eml/` 配下を走査し、DB未登録のファイルを検出して取り込む |
| マニフェスト検証 | EMLパス・完全ハッシュ・取得元キー・状態イベントの整合性を検証し、末尾の不完全JSONLレコードを安全に切り離す |
| 再インデックス | DBを破棄し、EML群と永続マニフェストからアカウント・フォルダ・墓標・監査情報・メタデータ・FTSを全再構築 |
| 再解析 | パースに失敗したメールのみを対象に、本文・添付名の抽出をやり直す |

**実行契機**

* **起動時はクイック検証のみ**を自動実行する（存在確認とサイズ照合だけで数秒。不一致があれば通知バーに警告を出す）。
* **範囲限定検証は、前回が異常終了（クリーンシャットダウンフラグが立っていない）の場合に自動実行する。** 対象はせいぜい数百件（数百MB）に収まるため、起動パスに置いても実用的なコストで済む。
* フル検証は100GB分のSHA-256再計算となり数十分を要するため、**メニューからの手動実行のみ**とする（バックグラウンド実行・進捗表示・キャンセル可）。
* 孤児スキャンは同期完了後に定期的に実行する。

### **4.9 エクスポート機能**

| 機能 | 優先度 |
| :---- | :---- |
| 選択メールの `.eml` 保存 | 必須（Phase 3） |
| フォルダ／検索結果単位の **mbox エクスポート**（標準ライブラリ `mailbox`） | 推奨（Phase 4） |
| 添付ファイルの一括抽出 | 推奨（Phase 4） |
| 検索結果のCSVエクスポート（メタデータのみ） | 任意 |

### **4.10 PSTアーカイブ機能（ローカル .pst の .eml 変換）**

POP3時代に蓄積された `.pst` を、Outlookに依存しない標準形式へ変換して永続保存する。**IMAPアーカイブとは独立した機能**であり、ストレージ・DB・検索・整合性チェックの基盤だけを共有する。

#### **1. 変換エンジン（readpst）**

`vendor/readpst/readpst.exe` を `subprocess` で起動する。使用するオプションは以下に固定する。

```
readpst -e -t e -8 -j 0 -q -C {charset} [-D] -d {logs/pstimp-{job_id}.log} -o {staging} {pst}
```

| オプション | 意図 |
| :---- | :---- |
| `-e` | 1通1ファイルの rfc822 出力（`.eml` 拡張子付き）。**添付も同一ファイルに同梱**され、from quoting が入らない |
| `-t e` | メールのみ処理。**予定表・連絡先・仕事（a/c/j）はスコープ外** |
| `-8` | UTF-8版が利用可能なら本文をUTF-8で出力 |
| `-C {charset}` | charset未宣言アイテムの既定文字セット。**既定 `cp932`**（国内POP3時代のメールを想定）。ウィザードで `iso-2022-jp` / `utf-8` に変更可 |
| `-D` | 削除済みアイテムを含める。**既定OFF**（Outlookで削除したはずのメールが復活して意図しない結果になるため）。ウィザードのチェックボックスでONにできる |
| `-j 0` | 並列ジョブを抑制。進捗推定を安定させ、外付けSSDへのI/O竞合を避ける |
| `-w` | **使わない**。`-S` 併用時に出力先を全削除する危険なオプションであり、毎回新規 staging を作る本設計では不要 |

* 起動前に `readpst -V` でバージョンを取得し、`pst_imports.readpst_version` に記録する（後日「この版にバグがあったので再変換」と判断できるように）。
* `lspst` を併せて同梱するが、その出力は安定した機械可読APIではなく、PST種別（Unicode/ANSI）や正確な階層・件数も保証しない。`probe()` は長時間処理・キャンセル対応とし、取得できた範囲のフラットなフォルダ名と推定件数だけを任意情報として表示する。正確な階層と件数はStage A完了後に確定する。

#### **2. 2ステージ構成（readpst の制約を吸収する）**

**readpst にはレジュームも逐次進捗APIも無い。** この制約を前段に閉じ込め、後段に既存の堅牢なパイプラインを使う。

| | **Stage A: 抽出** | **Stage B: 取り込み** |
| :---- | :---- | :---- |
| 処理 | readpst を `tmp/pstimp/{job_id}/` へ向けて実行 | staging 配下の `.eml` を 1件ずつ 4.7 の手順で保存・解析・DB登録 |
| レジューム | **不可**。中断時は staging を破棄してやり直し | **可能**。確定済み `pst_import_items` / `items.jsonl` の未完了項目だけを処理する |
| 進捗 | 粗い（出力ファイル数のポーリングと経過時間） | 1件粒度（`ingested_count / total_files`） |
| キャンセル | `Popen.terminate()` → 応答が無ければ `kill()` | `CancelToken` |
| 失敗時 | `status='abandoned'`、stderr と `-d` ログを保存し不完全stagingを削除 | 当該ファイルを `pst_import_items` に記録し**次へ進む**。未保存項目が残れば `failed_resumable` |

* Stage A 完了後に staging を安全に走査し、項目ごとの `source_item_key`・相対パス・フォルダ・サイズ・ハッシュを `pst_import_items` と `items.jsonl` に固定する。総数を確定して `status='ready_to_ingest'` としてからStage Bを開始する。
* Stage B の途中でキャンセルまたはアプリ終了した場合は `status='cancelled_resumable'` とし、同じ `import_uuid`・staging・項目マニフェストを保持する。次回は新規取り込みではなく同一ジョブの再開として扱う。
* 全EMLが最終保存済みで解析失敗だけが残る場合は `completed_with_errors` とし、再解析は最終EMLから行えるためstagingを削除する。未保存項目が残る場合は `failed_resumable` としてstagingを保持する。
* 全項目成功時は `completed` とし、stagingを削除して `staging_path=NULL` にする。未完了ジョブをユーザーが明示的に破棄した場合だけ `abandoned` としてstagingを削除する。

**PST取込状態**

| 状態 | 意味 |
| :---- | :---- |
| `extracting` | Stage A実行中 |
| `ready_to_ingest` | Stage A完了、項目マニフェスト確定済み |
| `ingesting` | Stage B実行中 |
| `cancelled_resumable` | Stage B中断、再開可能 |
| `failed_resumable` | 未保存項目を残して失敗、再開可能 |
| `completed` | 全項目の保存・解析完了 |
| `completed_with_errors` | 全EML保存済み、一部解析失敗あり |
| `abandoned` | ユーザーが未完了ジョブを破棄 |
| `superseded` | 再変換により旧世代となった完成済み取込 |

#### **3. 抽象化層 (BaseArchiveImporter)**

```python
@dataclass(frozen=True)
class ArchiveFolder:
    relative_path: str  # staging ルートからの相対ディレクトリパス（folders.raw_name へ）
    display_name: str  # 解決できればPST表示名、曖昧ならreadpst出力名
    estimated_count: int | None


@dataclass(frozen=True)
class ArchiveInfo:
    format: str  # 'pst_unicode' | 'pst_ansi' | 'unknown'
    folders: list[ArchiveFolder]
    estimated_total: int | None
    source_sha256: str
    source_size_bytes: int


@dataclass(frozen=True)
class ExtractResult:
    staging_root: Path
    file_count: int
    stderr_tail: str


class BaseArchiveImporter(ABC):
    @abstractmethod
    def probe(
        self,
        source: Path,
        *,
        cancel: CancelToken,
        on_progress: Callable[[int], None],
    ) -> ArchiveInfo: ...

    @abstractmethod
    def extract(
        self,
        source: Path,
        staging: Path,
        options: ImportOptions,
        *,
        cancel: CancelToken,
        on_progress: Callable[[int], None],
    ) -> ExtractResult: ...
```

例外は `domain/errors.py` の階層へラップする。readpst 固有の終了コードやメッセージを上位層に漏らさない。

```
MailDockError
└── ArchiveImportError      # ★追加
     ├── ConverterNotFound   # readpst 本体または依存DLLが見つからない
     ├── ConverterFailed     # 非ゼロ終了・クラッシュ
     └── UnreadableArchive   # PSTが破損している・形式が違う
```

#### **4. 取り込みマッピング**

| 項目 | 内容 |
| :---- | :---- |
| アカウント | PST取込世代ごとに1つ作成。`id = pst_{原本SHA-256の先頭12桁}_{import_uuidの先頭8桁}`、`provider_type='pst_import'`。UIには世代サフィックスを出さずユーザー指定名を表示する |
| フォルダ | staging のディレクトリ階層を `folders.raw_name` へ登録。元PST名を一意に復元できない場合はreadpst出力名を表示し、未解決フラグをマニフェストへ残す |
| 本文・添付・ヘッダ | **4.7 のEML解析・保存方針をそのまま適用**（文字コードフォールバック、RFC2231、Outlook非標準形式への対応を含む） |
| 保存パス | `eml/pst_{...}/{YYYY}/{MM}/{sha256の先頭32桁}.eml`。`Date` が解釈できなければ `unknown/` |
| スレッド情報 | `in_reply_to` / `references_ids` / `thread_key` をIMAP側と同じ規則で算出して保存 |
| 監査 | `audit_log` に `operation='pst_import'` で、原本ファイル名・SHA-256・件数・オプションを記録 |

**readpst 出力の限界（事前に周知する）**

* PST内のメールはMAPI形式で保持されており、`PR_TRANSPORT_MESSAGE_HEADERS` を持たない送信済みメール等では、**出力されるヘッダは readpst による再構成**である。
* `Message-ID` は欠損・重複し得るため、一次識別には使わない。Stage Aで固定した `source_item_key` により全項目を保持し、`content_key` は照合情報に限定する（3.3参照）。
* readpstは既読アイテムに `Status: RO` を出力し得るが、本アプリではPSTの既読状態として取り込まない。
* 変換の忠実度に不満がある場合に備え、**原本 `.pst` を捨てないこと**をインポート完了画面とREADMEに明記する。

#### **5. セキュリティ・堅牢性**

1. **subprocess の安全な起動:** `shell=False`・引数はリストで渡す・実行ファイルは同梱パスの絶対パスで解決する。ユーザー入力をコマンド文字列へ連結しない。`CREATE_NO_WINDOW` でコンソールを出さない。
2. **パストラバーサル対策:** readpst が作るディレクトリ名は **PST内のフォルダ名＝敵性入力**である。Stage B の走査時に全件、`Path(p).resolve()` が staging ルート配下であることを再検証し、外れたものはスキップして警告ログを残す。
3. **Windows出力名の限界:** readpst上流はフォルダ名の `/`・`\\`・`:` 以外のWindows禁止文字、予約名、末尾ドット・空白を十分に処理しない。また、サニタイズ後に異なる名前が衝突し得る。Stage A前に解決できないためPhase 4.5冒頭のブロッカー判定対象とし、禁止文字全種、`CON`等の予約名、末尾ドット・空白、同名、正規化後衝突を実PSTで検証する。
4. **パス長:** 日本語フォルダ名＋連番で MAX_PATH 260 を超え得る。staging は短いパス（`tmp/pstimp/{import_uuidの先頭8桁}/`）に置き、長パス対応を前提にする。
5. **原本の保護:** 原本 `.pst` は**読み取りのみ**で開く。コピーも移動も変更も行わない。
6. **メモリ:** 1ファイルを丸ごと読むため、**100MBを超える `.eml`** はハッシュをチャンク計算して保存は行い、本文解析はスキップして `pst_import_items.error_class='oversize'` として記録する（後から「再解析」で復旧できる）。
7. **空き容量:** 開始前に **PSTサイズ × 2.5** の空きを確認し、不足時は開始させない。再変換では旧世代を保持したまま新世代を作るため、新規取込分に加えて旧世代を保持できる空きを確認する。

#### **6. 既存機能との関係**

| 機能 | PSTアーカイブでの扱い |
| :---- | :---- |
| 同期（手動・定期・起動時） | **無効**。ユースケース入口で `provider_type` を見て拒否する |
| サーバー削除（4.3） | **無効**。削除対象の列挙クエリからも除外する |
| フォルダ選択（`is_sync_target`） | **無効**。常に 0 のまま |
| 検索（4.5） | **有効**。同じFTS基盤・同じ正規化関数を使う。ただし検索スコープはルート内に限定 |
| ローカルゴミ箱・30日purge（4.4） | **有効**。IMAP側と完全に同一の振る舞い |
| 整合性チェック・孤児スキャン・再インデックス（4.8） | **有効**。`.eml` とPST永続マニフェストを使って検証・再構築する |
| エクスポート（4.9） | **有効**。eml / mbox / CSV とも共通 |
| 重複排除 | **行わない**。IMAP側に同じメールがあっても両方保持する |

#### **7. 同一PSTの再開・再変換**

同一性はファイル名や短縮IDではなく、原本PST全体の完全な `source_sha256` で判定する。

* 未完了ジョブがある場合は「同一ジョブを再開」または「未完了ジョブを破棄」を提示する。再開では新しい `pst_imports` 行やアカウントを作らない。
* `is_active=1` の完成済みアーカイブがある場合、通常の再取り込みは禁止する。ユーザーが明示的に「再変換」を選んだ場合のみ新しい `import_uuid` と `replaces_id` を持つ世代を作る。
* 再変換中も旧世代を閲覧可能なまま保持する。新世代の全EML・永続マニフェスト・DB登録を検証後、単一DBトランザクションで新世代を `is_active=1`、旧世代を `is_active=0` / `status='superseded'` に切り替える。
* 新世代が中断・失敗した場合は旧世代を一切変更しない。切替完了後の旧世代はアーカイブ単位でローカルゴミ箱へ移し、通常の30日猶予またはゴミ箱内での再確認を経てpurgeする。
* 旧世代のpurgeでも共有EML参照を確認し、最後の参照である場合だけ実ファイルを削除する。世代交代・破棄・purgeはマニフェストと `audit_log` の双方へ記録する。

#### **8. インポートウィザードのフロー**

1. PSTファイルを選択
2. キャンセル可能な `probe()` を実行し、取得できたPST種別・フラットなフォルダ名・推定件数・原本SHA-256を表示。不明な値は「不明」とし、確定値として扱わない
3. 同じSHAの未完了ジョブがあれば「再開／破棄」、完成済み世代があれば「中止／再変換」を提示
4. オプション指定（アーカイブ表示名 / 既定文字セット / 削除済みアイテムを含めるか）
5. 空き容量チェック（PSTサイズ × 2.5。再変換では旧世代保持分も考慮）
6. **Stage A 実行**（進捗・キャンセル）
7. **Stage B 実行**（件数進捗・キャンセル）
8. 再変換の場合は新世代を検証してアクティブ世代を切替
9. サマリ表示（成功N件 / 失敗M件 / スキップK件、ログへの導線、**原本PSTの保管を促す注意書き**）

Stage Aのキャンセルではreadpst停止後に不完全stagingを削除し `abandoned` とする。Stage Bのキャンセルではstagingと項目マニフェストを保持し `cancelled_resumable` とする。ユーザーが「未完了ジョブを破棄」を明示した場合だけstagingを削除する。

## **5\. 非機能要件 & セキュリティ**

### **5.1 性能目標**

想定規模（5万通 / 100GB、平均2MB/通）に基づく目標値。

| 指標 | 目標値 | 備考 |
| :---- | :---- | :---- |
| 初回同期スループット | 実効 **8MB/s 以上** | 100GBで約3.5時間。回線とサーバー側の制限に依存 |
| 初回同期の中断耐性 | **任意の時点で中断・再開可能** | `last_seen_uid` によるレジューム |
| 増分同期（新着0件） | **10秒以内** | 対象フォルダ数 × `UID SEARCH` のみ |
| 検索応答（3文字以上） | **300ms 以内** | 5万通・trigramインデックス使用時 |
| 検索応答（2文字以下） | 3秒以内 | LIKEスキャン経路。UIに警告表示 |
| 一覧スクロール | 60fps | 200件単位の遅延ロード必須 |
| アプリ起動時間 | 3秒以内 | QtWebEngine採用時は別途評価 |
| メモリ使用量 | 同期中も **600MB 以下** | 1通2MBを都度バッファするため上限管理が必要 |

### **5.2 ストレージ見積もり**

| 内訳 | 見積もり |
| :---- | :---- |
| EMLファイル本体 | **100GB**（サーバー上と同等。Base64のまま保存） |
| `messages` メタデータ | 約 50MB（5万行 × 約1KB） |
| `message_contents` 本文テキスト | 約 1GB（平均20KB × 5万通） |
| **trigram FTSインデックス** | 約 4GB（本文テキストの3〜5倍） |
| DBバックアップ (`metadata.db.bak`) | 約 5GB |
| 一時ファイル (`tmp/`) | 数百MB |
| **合計** | **約 110GB** |

* **推奨する空き容量: 130GB以上**（増加分と作業領域の余裕を含む）。
* 添付ファイルが容量の大半を占める一方、**FTSインデックスは本文テキストのみに依存する**ため、容量比では小さい。検索性能の懸念は無い。

**PSTアーカイブ分（上記とは別勘定）**

| 内訳 | 見積もり |
| :---- | :---- |
| 変換後のEML | PSTサイズと同程度（―圧縮PSTでは少し増える） |
| メタデータ・本文・FTS | EMLサイズの数％程度（IMAP側と同じ比率） |
| **取込中の一時領域** (`tmp/pstimp/`) | **PSTサイズと同等**。取込完了後に開放される |
| 取込中に必要な空き | **PSTサイズ × 2.5**（余裕含む。これを下回る場合は開始させない） |

* 複数のPSTを取り込む場合も**1ファイルずつ順次処理**するため、一時領域のピークは「最大のPST 1個分」に収まる。

### **5.3 認証・セキュリティ**

* **資格情報の保管:** IMAPパスワードおよび将来のOAuth2リフレッシュトークンは `keyring`（Windows Credential Manager）に保管し、**DBおよび設定ファイルには一切書き込まない**。
* **脅威モデルの明示:** Windows Credential Manager は同一ログインユーザーの任意プロセスから復号可能である。したがって本アプリは「**PC自体が侵害された場合の攻撃者**」からは資格情報を保護できない。可能な限りメールサービス側の**アプリ専用パスワード**を使用し、メインパスワードを直接入力しない運用を推奨する。
* **保存データの暗号化:** 本アプリはEML、`metadata.db`、FTSインデックスをストレージルートへ**平文で保存する**。暗号化方式は必須条件ではなく、次の3層モデルで扱う。

    | 層 | 位置づけ | 対象となる方式・運用 | アプリの扱い |
    | :--- | :--- | :--- | :--- |
    | **推奨（Supported）** | ブロックレベル暗号化 | BitLocker To Go、VeraCrypt、LUKS、macOSのAPFS暗号化 | マウント後は通常のファイルシステムとして扱う。`os.replace` の原子性、排他ロック、`fsync`、SQLite WALの前提を満たすことを期待するが、ルート確定時のストレージ適合性セルフテストで能力を測定する。 |
    | **非推奨（Unsupported）** | ファイル単位暗号化・仮想ファイルシステム | Cryptomator、gocryptfs、rclone crypt、Boxcryptor等 | 仮想ファイルシステムの実装により `os.replace` の原子性、排他ロック、`fsync` の保証が変わり、設計不変条件2（書き込み順序と原子配置）を保証できない。製品名は自動検出せず、セルフテストで能力不足が検出された場合に `UNSUPPORTED` または `DEGRADED` として扱う。 |
    | **自己責任（Unencrypted）** | 暗号化なし | 暗号化されていないローカルドライブ | 明示的なユーザー申告を記録して利用を許可する。申告状態はUIに常時表示し、初回同期の開始直前に一度だけ強い確認を行う。 |

    * 推奨層のOS別の選択肢は、Windows Proでは **BitLocker To Go**、Windows Homeでは **VeraCrypt**、macOSでは **APFS暗号化**またはVeraCrypt、Linuxでは **LUKS**またはVeraCryptとする。Windows Homeでも、BitLockerで暗号化済みのドライブを解錠して読み書きすることは可能であり、Windows HomeでできないのはBitLockerによる暗号化の作成・管理である。
    * VeraCryptは、専用の外付けSSDでは**デバイス全体の暗号化を第一推奨**とする。ドライブを他用途と共用する場合にファイルコンテナを選択してもよいが、固定サイズのコンテナ（動的コンテナは不可）を使用し、クラウド同期フォルダやネットワーク共有には置かず、自動アンマウントを無効化し、ボリュームヘッダのバックアップを取得することを必須条件とする。
    * 暗号化状態の自動検出は行わない。BitLocker、VeraCrypt、LUKS等のマウント状態をすべての対象OSで確実に判定できる手段はないため、状態は `encrypted`（暗号化済み）/ `unencrypted`（未暗号化）/ `unknown`（不明）の3値による**ユーザー申告**として記録する。`unknown` は異常ではなく正常な申告状態であり、ストレージ適合性セルフテストの結果を暗号化状態の証明として扱わない。セルフテストは製品名ではなく能力を測る互換性プローブであり、`OK` であっても安全性を完全に証明するものではない。
    * **アプリ層暗号化（7z、AES-ZIP、SQLCipher等）は恒久的に採用しない。** 理由は、(1) 本アプリがなくてもThunderbird等でEMLを直接閲覧できるという可搬性を損なうこと、(2) EMLだけを暗号化しても `metadata.db` の `message_contents` とtrigram FTSインデックスが平文で残り、脅威モデルをほぼ満たせないこと、(3) 鍵を失うと長期保管データ全体を失うこと、の3点である。したがってアプリ内のDB暗号化（SQLCipher等）も採用しない。
* **通信:** IMAP over SSL（Port 993）。証明書検証を無効化するオプションは提供しない。

### **5.4 応答性 (UI Thread Separated)**

* EML取得・解析・DB書き込み・検索・整合性検証はすべて `QThread` / `QRunnable` で非同期実行し、UIをフリーズさせない。
* 長時間処理には必ず**進捗表示とキャンセルボタン**を設ける（`CancelToken` 経由で中断）。
* 同期中もアプリの閲覧・検索機能は通常どおり使用できること（WALモードにより読み取りはブロックされない）。

### **5.5 ロギング・監査**

| 出力先 | 内容 | 保持期間 |
| :---- | :---- | :---- |
| `{config_dir}/logs/app.log` | アプリ全般（`RotatingFileHandler` 5MB × 5世代） | 自動ローテーション |
| `{storage_root}/logs/sync-{date}.log` | 同期の詳細ログ | 90日 |
| `audit_log` テーブル | サーバー削除・ローカルpurge・PST取込/破棄/世代交代の記録 | **永久** |

* **個人情報のマスキング:** 本文は絶対に出力しない。件名は先頭20文字まで、メールアドレスは `us***@example.com` 形式にマスクする。パスワード・トークンは出力しない。
* **ストレージ切断時の出力先:** `{storage_root}/logs/` は切断時に書けないため、**切断・復帰イベントは必ず内蔵ディスク側の `{config_dir}/logs/app.log` へ記録する**（5.7.1参照）。
* GUIアプリでは標準エラー出力が失われるため、ファイル出力は必須。UIに「ログフォルダを開く」導線と、同期結果サマリ（成功N件 / 失敗M件 / スキップK件 / 転送量）を表示する画面を用意する。

### **5.6 エラーリカバリ**

**原則: 1通の失敗で同期全体を停止させない。**

* `TransientError` は指数バックオフで3回まで再試行し、それでも失敗した場合は `sync_failures` に記録して次のメールへ進む。**次回同期時に自動的に再試行**される。
* パースエラーはEMLを保存したうえで記録し、解析ロジック改善後に「再解析」で復旧する。
* サイズ上限超過は `'oversize'` として記録し、UIから個別に「それでも取得する」を選べるようにする。
* `attempt_count >= 10` のものはUIに「要確認」として一覧表示する。

### **5.7 データ完全性と例外処理**

* **ストレージルート未接続時:** `.maildock_root` による自動再検出を試み、見つからない場合はダイアログを表示して「ドライブを再選択」または「終了」を選ばせる。DBが読めない以上、閲覧機能も動作しないため**中途半端な読み込み専用モードは提供しない**。
* **自動同期のスキップ:** ルートが未接続の状態での定期同期は、ダイアログを出さず静かにスキップする。
* **多重起動:** `.lock` ファイルによる排他制御（3.6参照）。
* **DBバックアップ:** `sqlite3.Connection.backup()` により週1回および終了時に `metadata.db.bak` を作成する。
* **バックアップの多重化:** ストレージルートは単純なファイル構造であるため、ドライブ丸ごとのコピー（robocopy / FreeFileSync 等）が完全なバックアップになる。この点をREADMEおよびアプリ内ヘルプに明記し、**3-2-1ルール（3コピー・2媒体・1オフサイト）**を推奨する。

> `metadata.db.bak` をストレージルート内だけに置くと、ドライブ障害で同時に失われる。内蔵ディスク（`{config_dir}`）へメタデータのみのバックアップを1世代置く案は有効だが、**DBには件名・差出人が含まれる**ため「外付けはBitLocker前提だがC:は暗号化されていないかもしれない」という 5.3 の脅威モデルと矛盾する。採用する場合は **C:のBitLockerが有効なときだけ**という条件付きの明示的オプトインとする。

### **5.7.1 稼働中のストレージ切断（外付けドライブの不意の取り外し）**

ストレージルートを外付けドライブに置く運用では、**アプリ稼働中に物理的に切断される**ことを常態の異常系として扱う。5.7 は「起動時に未接続」を扱うが、本節は「使用中に消える」を扱う。

**前提: Windowsで実際に起きること**

| 層 | 起きること |
| :---- | :---- |
| 開いているファイルハンドル | 即座に無効化。以降の `write` / `os.fsync` が `OSError`（`winerror` = 6 / 21 / 55 / 433 / 995 / 1117 / 1167 等） |
| SQLite接続 | `SQLITE_IOERR_*` / `SQLITE_READONLY_DBMOVED` / `SQLITE_CANTOPEN`。**その接続は以後一切信用できず、再接続が必須** |
| `-wal` / `-shm` | 途中で切れる。SQLiteは最後の有効コミットフレームまで巻き戻すため**破損はしないが直近コミットは失われ得る** |
| `os.replace` 直後のEML | メタデータ操作は原子的だが、ドライブ側の書き込みキャッシュと `synchronous=NORMAL` の組み合わせにより**媒体に到達していない可能性がある** |
| ドライブレター | 再接続時に**別デバイスが同じレターを取り得る**（2.4-10参照） |

#### **1. 予防**

* **「ストレージを安全に取り外す」メニューを必ず提供する。** 不意の切断の最大の原因は「アプリがハンドルを掴んでいてWindowsが取り外しを拒否 → ユーザーが痺れを切らして引き抜く」であり、これを潰すことが最も費用対効果が高い。手順は以下を厳守する。
  1. 同期／PST取込ワーカーへ `CancelToken` を送り、**バッチ境界で停止するまで待つ**
  2. `PRAGMA wal_checkpoint(TRUNCATE)` で `-wal` を空にする
  3. 全SQLite接続を `close()`（スレッドローカル接続を漏れなく破棄する仕組みが要る）
  4. QWebEngineプロファイルを破棄（本文・添付のファイルハンドル解放）
  5. `logs/` のファイルハンドルを閉じる
  6. `.lock` を解放して削除
  7. 状態を `DETACHED_BY_USER` にし「取り外して構いません」を表示
* セットアップウィザードでOS側の推奨設定を案内する: ドライブポリシーを**「クイック取り外し」**（書き込みキャッシュ無効）にする、USBハブ経由・バスパワー運用を避ける。

#### **2. 検知（3系統を併用）**

| 系統 | 手段 | 位置づけ |
| :---- | :---- | :---- |
| 受動 | I/O例外の分類 | **必須** |
| 能動 | `.maildock_root` のハートビート | **必須** |
| 能動 | `WM_DEVICECHANGE` の監視 | 推奨 |

**(a) I/O例外の分類（`infrastructure/storage/detach.py`）**

`EmlStorage` とDB接続層の**一箇所**で例外を分類し、ドメイン例外 `StorageDetachedError` へ変換する。上位層に `winerror` やSQLiteのエラーコードを漏らさない（2.3のアダプターパターンと整合）。

```python
_DETACH_WINERRORS = frozenset({6, 21, 55, 433, 995, 1117, 1167})


def classify_os_error(e: OSError) -> Exception:
    if getattr(e, "winerror", None) in _DETACH_WINERRORS:
        return StorageDetachedError(...)
    return e


def classify_sqlite_error(e: sqlite3.Error) -> Exception:
    name = getattr(e, "sqlite_errorname", "")  # Python 3.11+
    if name.startswith("SQLITE_IOERR") or name in {"SQLITE_READONLY_DBMOVED", "SQLITE_CANTOPEN"}:
        return StorageDetachedError(...)
    return e
```

**(b) ハートビート**

UIスレッドの `QTimer` で5秒ごとに `.maildock_root` を**読み直してUUIDを照合**し、`OK` / `MISSING` / `FOREIGN` を判定する。存在確認だけでは「別デバイスが同じレターを取った」を検知できない（2.4-10）。

**(c) `WM_DEVICECHANGE`（`QAbstractNativeEventFilter`）**

| メッセージ | 対応 |
| :---- | :---- |
| `DBT_DEVICEQUERYREMOVE (0x8001)` | **本命の分岐。** ここでハンドルを閉じて取り外しを許可する。拒否するとユーザーが引き抜く |
| `DBT_DEVICEREMOVECOMPLETE (0x8004)` | I/Oエラーを待たずに `DETACHED` へ即遷移 |
| `DBT_DEVICEARRIVAL (0x8000)` | 再検出をトリガー |

`DBT_DEVTYP_VOLUME` のブロードキャストは登録不要で届き、`dbcv_unitmask` からドライブレターを復元できる。

**(d) 瞬断と抜去の区別**

USBの再列挙による瞬断で毎回フル復旧を走らせるのは過剰である。I/Oエラー検知後、**500ms間隔で最大3回**リプローブし、UUIDが一致して復帰した場合は「接続は生きているがハンドルだけが死んでいる」扱いとして、全接続を張り直しバッチ境界から同期を再開する。3回失敗した場合のみ `DETACHED` へ遷移する。

#### **3. 縮退（状態機械）**

```
ATTACHED ──I/Oエラー──> DEGRADED ──リプローブ成功──> ATTACHED
                            └──失敗 / REMOVECOMPLETE──> DETACHED
ATTACHED ──安全な取り外し──> DETACHED_BY_USER
DETACHED ──ARRIVAL / 手動再試行──> RECONNECTING ──UUID一致+ロック再取得──> VERIFYING
VERIFYING ──範囲限定検証OK──> ATTACHED
VERIFYING ──検証失敗──> DETACHED（ユーザー判断を求める）
```

`DETACHED` 遷移時に行うこと:

1. **書き込みを一切試みない。** 死んだハンドルへの再書き込みは、再マウント後に中途半端なファイルを残す。
2. ワーカーを `CancelToken` で停止する。ただし**停止処理自体がI/Oを伴わない**よう設計する（「終了時にログを書く」で固まるのが典型的な失敗）。
3. 全SQLite接続を `close()` し、プールに残さない。
4. `.lock` のハートビート更新を停止する（3.6のスタールロック検出が受ける）。
5. **ログ出力先を `{config_dir}/logs/app.log`（内蔵ディスク）へ切り替える。** 同期ログは `{storage_root}/logs/` にあるため、切断時はまさに書けない。切断イベントこそ内蔵ディスク側に残す必要がある。
6. UIはモーダルで「再接続を試す／終了」を提示する。5.7の方針どおり**読み取り専用の縮退モードは提供しない**。
7. **「サーバーから削除」を無条件で無効化**する（4.3の切断時ガード）。

#### **4. 復帰**

1. **同定と排他:** `.maildock_root` のUUID照合 → `.lock` の再取得 → `PRAGMA user_version` 確認 → `PRAGMA quick_check`（`integrity_check` は重いので使わない）。
2. **マニフェスト末尾の修復:** 最終行がtorn（改行欠落／JSON不正／CRC32不一致）なら切り離す。切断復旧では**必ず通る経路**になるため、起動時パスに常設する。
3. **範囲限定検証（4.8）:** マニフェストの最後にfsyncが確認できたチェックポイント以降のEMLのみ再ハッシュする。不一致のレコードは未取得へ戻し、当該EMLを `tmp/` へ隔離したうえで次回同期で再取得する。これにより「クイック検証では見抜けない、キャッシュに残ったまま消えた末尾ブロック」を実用的なコストで潰せる。
4. **クリーンシャットダウンフラグ:** DBに1行のメタテーブル（`app_state(key, value)`）を置き、正常終了時に `clean_shutdown=1`、起動時に `0` へ戻す。起動時に `0` のままなら前回異常終了と判定し、上記2〜3を自動実行する。
5. **`tmp/` の掃除:** `tmp/*.eml` は無条件削除でよい（`os.replace` 前のものに価値はない）。ただし **`tmp/pstimp/{job_id}/` は再開用であり削除してはならない**。この区別はコード上に明記する。

#### **5. PST取込中に切断された場合**

`readpst` は外部プロセスであり、**stagingの完成度をアプリ側で検証できない**。したがって:

* `subprocess` を `terminate()` → タイムアウト後 `kill()` する。ハンドルが死んだプロセスはハングし得るため、**待ちっぱなしにしない**。
* Stage A完了マーカー（fsync済みの `stageA_done.json`）が**無い**場合、stagingを `suspect` 扱いとし、再開ではなく**破棄＋再抽出**を促す（4.10-7の「再開」フローの例外）。
* マーカーがある場合のみStage Bから再開する。`pst_import_items` は確定済みのため冪等に処理できる。

### **5.8 自動同期**

* アプリ常駐型のシンプルな方式を採用し、外部スケジューラ（タスクスケジューラ等）との連携は行わない。
* 設定項目: 「起動時に同期」ON/OFF、「N分ごとに同期」（既定60分、0で無効）。
* `QTimer` で駆動し、前回の同期が実行中であればスキップする。
* システムトレイに常駐し、最小化中も同期を継続する。トレイアイコンで同期状態を表示する。
* IMAP IDLE によるプッシュ受信は**スコープ外**とする（バックアップ用途では不要であり、接続維持のコストに見合わない）。

### **5.9 配布とライセンス**

**パッケージング**

* **PyInstaller（onedirモード）** でパッケージングする。onefileはQtWebEngineとの相性が悪く起動が遅いため採用しない。
* PyInstallerで実行ファイル化した成果物をもとに、**Inno Setupでインストーラーを作成したうえで配布する**。
* QtWebEngine採用時は `QtWebEngineProcess.exe` およびリソース類の同梱を確認する。
* **`vendor/readpst/` 一式（`readpst.exe` / `lspst.exe` / 依存DLL）を同梱**する。依存DLLの漏れは実行時まで発覚しないため、CIで**クリーンなWindows上で `readpst -V` が成功することをスモークテスト**する。
* 単一ユーザー利用のためコード署名は当面不要。第三者配布を行う場合はSmartScreen警告への対処として署名証明書が必要になる。
* 自動更新機構はスコープ外とする。
* CI（GitHub Actions）で `ruff` + `mypy` + `pytest` を実行する。

**OSS公開とライセンス（★GitHubでOSSとして公開する前提）**

mail-dock本体は **GPL-3.0-or-later** で公開する。同梱する `readpst`（libpst）は **GPL-2.0-or-later** の独立した外部プログラムとして扱い、各成果物の対応ソースとライセンスを確実に提供する。

| 項目 | 方針 |
| :---- | :---- |
| 本体ライセンス | **GPL-3.0-or-later**。リポジトリにライセンス全文、対応ソース、再現可能なビルド手順を置く |
| 連携方式 | `readpst` / `lspst` は **`subprocess` による別プロセス起動のみ**とし、libpstをライブラリとしてリンクせずヘッダも取り込まない。ライセンス義務を回避する根拠にはせず、独立成果物として由来と対応ソースを管理する |
| バイナリのGit管理 | **`vendor/readpst/` のバイナリはリポジトリにコミットしない**（`.gitignore`）。CIがMSYS2から取得してリリース成果物に同梱する |
| バイナリ配布時の義務 | GitHub Releases に、同梱したlibpstの**実際の完全な対応ソース一式**、MSYS2のPKGBUILD・適用パッチ・ビルド情報・バイナリとソースのSHA-256、GPL全文を必ず同時に置く。取得元URLだけで代替しない |
| 依存DLL | 同梱DLLごとに名称・バージョン・ライセンス・対応ソース・取得元・SHA-256を記録し、再配布条件をCIで検査する |
| 改変 | **libpst は一切改変しない**（改変すればその部分のソース公開義務が発生し、保守コストも上がる）。必要な回避策はすべてアプリ側で行う |
| 表記 | `THIRD-PARTY-LICENSES.md` に readpst / Qt(PySide6) / その他依存のライセンスを列記し、README とアプリの「バージョン情報」から参照できるようにする |
| ソースのみの配布 | READMEにMSYS2からのreadpst入手方法と検証手順を記載する。本体はGPL-3.0-or-laterとして常にライセンス表示とソース提供条件を満たす |

> ★ リリースワークフローに「**GPL成果物（バイナリ＋COPYING＋ソース）が揃っていなければリリースを失敗させるチェック**」を入れる。人間の手順書に任せると必ず忘れる。

### **5.10 テスト方針**

| 層 | 手段 |
| :---- | :---- |
| 単体テスト | ドメイン・ユースケース・パーサ・ファイル名サニタイズ。`BaseMessageRepository` のインメモリ実装に差し替えて実行 |
| EMLコーパス | `tests/fixtures/eml/` に、壊れたMIME、charsetラベル誤り、ISO-2022-JP / CP932 / EUC-JP、RFC2231分割ファイル名、Outlook非標準形式、Message-ID欠損、巨大添付、インライン画像のサンプルを蓄積する |
| IMAP結合テスト | **Docker 上の Dovecot / GreenMail** を使用する。CIでも実行可能にし、UIDVALIDITY変化・フォルダ移動・接続切断などの異常系を再現する |
| DB結合テスト | 一時ディレクトリ上の実ファイルSQLiteで実行（FTSトリガーとWALの検証に必須） |
| PST結合テスト | 小規模PSTを実際にreadpstへ通す。日本語・文字コード・添付・破損PSTに加え、Windows禁止文字全種、予約名、末尾ドット/空白、同名・正規化後衝突、深い階層を含める。readpst未同梱環境ではskipする |
| PST単体テスト | Stage Bの冪等性・項目順変更耐性・再開・キャンセル、`completed_with_errors`、未完了破棄、再変換の世代切替失敗時に旧世代が維持されることを検証する |
| マニフェスト試験 | EML＋マニフェストだけからDBを再構築し、PSTフォルダ、取得元キー、purge墓標、監査イベントを復元できること、末尾の不完全JSONLを回復できることを検証する |
| ★切断試験（フォールト注入） | `EmlStorage` とDB接続をラップし、N回目の書き込みで `OSError(winerror=1167)` / `SQLITE_IOERR` を投げる。**fsync前 / `os.replace` 直前 / マニフェスト追記の行途中 / DBコミット中** の4点それぞれで切断し、**不変条件2で許容される状態（DB未登録EML）にしかならないこと**を検証する |
| ★切断試験（実デバイス） | VHDXを `diskpart` の `detach vdisk` で強制切離し、状態機械・スタールロック検出・範囲限定検証・再接続後の同期再開を通しで確認する。`FOREIGN`（別デバイスが同じレターを取る）もVHDXの差し替えで再現する |
| lspst互換試験 | 対応するlibpstバージョンごとに出力パーサを結合テストし、不明形式では値を推測せず `unknown` / `None` へフォールバックすることを検証する |
| 実機テスト | お名前.com の実アカウントで Phase 1（小規模フォルダ）と Phase 4（フルスケール）の2回実施。実PSTでの変換検証は Phase 4.5 で実施 |
| UIテスト | `pytest-qt` でモデル層・遅延ロードの振る舞いを検証。画面描画の自動テストは行わない |

### **5.11 主要設定項目と既定値**

| 設定項目 | 既定値 | 備考 |
| :---- | :---- | :---- |
| ストレージルート | 未設定（初回ウィザードで選択） | `.maildock_root` のUUIDで自動追従 |
| 同期対象フォルダ | なし（ユーザー選択） | 新規フォルダは常に非対象で追加 |
| 1通あたりの取得サイズ上限 | **50MB** | 超過分はスキップして記録。個別取得可 |
| 起動時に同期 | ON | |
| 定期同期間隔 | **60分**（0で無効） | |
| サーバー削除モード | **ゴミ箱へ移動** | `EXPUNGE` は明示オプション |
| サーバーゴミ箱フォルダ | 自動検出 | 失敗時は手動指定が必須 |
| 1回の削除上限 | **1,000通** | |
| ゴミ箱の猶予日数 | **30日** | 変更可 |
| purge実行モード | **A: 手動のみ** | A / B / C を選択可（4.4参照） |
| 外部画像の読み込み | ブロック | メール単位で手動解除 |
| 起動時の整合性チェック | **クイック検証** | フル検証は手動。異常終了検出時は範囲限定検証を自動実行 |
| ストレージ接続のハートビート間隔 | **5秒** | 0で無効化不可（安全上の必須機能） |
| 瞬断時のリプローブ回数 | **3回（500ms間隔）** | 超過したら `DETACHED` へ遷移 |
| リムーバブル時の `synchronous` | **`NORMAL`** | Phase 4 の実測結果により「コミット時のみ `FULL`」へ変更可（3.6参照） |
| ログ保持期間 | 90日（同期ログ） | 監査ログは永久 |
| PST取込: 既定文字セット (`-C`) | **`cp932`** | ウィザードで `iso-2022-jp` / `utf-8` に変更可 |
| PST取込: 削除済みアイテム (`-D`) | **OFF** | ウィザードのチェックボックスでONにできる |
| PST取込: 対象アイテム種別 (`-t`) | **`e`（メールのみ）固定** | 予定表・連絡先・仕事はスコープ外。設定項目にしない |
| PST取込: 失敗時の staging | **保持** | 再開のため。UIから手動で破棄できる |

## **6\. 開発ロードマップ・フェーズ分割**

| フェーズ | 期間目安 | 主なタスク内容 |
| :---- | :---- | :---- |
| **Phase 0: 基盤整備** | 数日 | srcレイアウトへの移行、依存関係の確定、ロギング基盤、設定管理（platformdirs）、DBマイグレーション機構（`user_version`）、ruff/mypy/pytest とCIのセットアップ、**Docker（Dovecot / GreenMail）によるテスト用IMAP環境の構築** |
| **Phase 1: 抽象化層 & IMAPコア** | 1〜2週間 | `BaseMailFetcher` と例外階層の定義、`OnamaeImapFetcher` 実装、EML解析（文字コード・RFC2231・スレッドヘッダ）、原子的なEML保存とDB登録。**小規模フォルダでのお名前.com実機検証をここで実施**（フォルダ区切り文字、modified UTF-7、同時接続数制限、タイムアウト挙動の確認） |
| **Phase 2: DB & 検索エンジン** | 1週間 | **冒頭でFTS5+trigramの実測PoC**（1万通規模でインデックスサイズ・検索速度・2文字検索の挙動を計測し設計を確定）。その後 external content スキーマ・トリガー・正規化・AND/OR検索・構造化フィルタを実装 |
| **Phase 3: GUI基礎構築 (PySide6)** | 2週間 | **QtWebEngine を採用する（確定）。`QTextBrowser` 版の比較試作は行わない**。`QTextBrowser` ではリクエストインターセプタ・カスタムスキーム・CSPを含む5層防御を満たせないため、3ペインレイアウト、遅延ロード対応の一覧モデル、QThreadによる非同期同期、HTML表示の5層サンドボックス、添付保存を実装する。QtWebEngineの起動時間・メモリ・配布サイズはPhase 3で実測し、Phase 4のパッケージング判断へ渡す |
| **Phase 4: 統合 & 例外処理** | 1〜2週間 | サーバー削除の安全装置一式、ゴミ箱・purge、整合性チェック・再インデックス、mboxエクスポート、ドライブ非接続・移動の例外処理、**稼働中の物理切断対策一式（5.7.1）と VHDX detach による切断シナリオテスト**、**フルスケール（5万通/100GB）での実機同期テスト**（ここで `synchronous` の最終決定を行う） |
| **Phase 4.5: PSTアーカイブ** | 1〜2週間 | **冒頭で readpst の実PST PoC（ブロッカー判定）**。その後マイグレーション002、PST永続マニフェスト、項目状態管理、Stage A/Bと世代交代、ウィザード、機能ガード、実PST検証、readpst同梱とGPL表記 |
| **Phase 5: （将来拡張）Gmail/OAuth2** | 随時 | `GmailOAuthFetcher` 実装、OAuth2ブラウザ認証フロー、`message_folders` 中間テーブルへのマイグレーション（ラベル対応） |

**Phase 4.5 の内訳と依存関係**

| # | タスク | 依存 |
| :---- | :---- | :---- |
| 1 | **readpst PoC（最優先・方式のブロッカー判定）**。日本語・文字コード・添付・階層・性能・必要DLL・破損時挙動に加え、Windows禁止文字、予約名、末尾ドット/空白、同名・正規化後衝突、長パス、lspst出力の限界を実測。致命的問題があれば方式を再検討する | ― |
| 2 | マイグレーション `003_pst_import.sql`、`pst_imports` / `pst_import_items`、永続マニフェスト、`remote_state='no_remote'` 対応 | ―（1と並行可） |
| 3 | `BaseArchiveImporter` / `readpst_locator` / `readpst_runner` | 1 |
| 4 | `usecases/import_pst.py`（Stage A→項目確定→Stage B、同一ジョブ再開、世代交代、監査記録） | 2, 3 |
| 5 | インポートウィザード UI | 4 |
| 6 | 左ペイン2ルート化と機能ガード（同期・サーバー削除の無効化） | 2（5と並行可） |
| 7 | 実PSTでのフルスケール検証。整合性チェック・再インデックス・ゴミ箱がPST由来にも効くことの確認 | 5, 6 |
| 8 | 配布物への readpst 同梱、GPL表記、リリースCIの遵守チェック | 1, 7 |

## **7\. リスク管理**

| リスク | 発生確率 | 影響度 | 検知方法 | 対応策 |
| :---- | :---- | :---- | :---- | :---- |
| サーバー削除の誤操作によるデータ消失 | 中 | **致命的** | 監査ログ | 削除前のハッシュ検証必須化、ドライラン、件数手入力による確認、既定はゴミ箱移動、1回1,000通の上限 |
| 外付けSSDの物理故障 | 中 | **致命的** | 起動時の接続確認 | 3-2-1ルールの推奨をヘルプに明記、DBの定期バックアップ、ドライブ丸ごとコピーで完結する構造 |
| SSD紛失・盗難による情報漏洩 | 低 | **致命的** | ― | BitLocker To Go を前提条件として明示。未暗号化時は警告を表示 |
| DB破損 | 低 | 中 | 起動時の整合性チェック | EML＋永続マニフェストからの再構築。`metadata.db.bak` からの復元 |
| 中断による孤児EML／孤児レコード | 高 | 低 | マニフェスト検証・孤児スキャン | EML→マニフェスト→DBの順序を厳守し、次回同期または同一PSTジョブ再開で回収 |
| ドライブレター変更 | 高 | 低 | 起動時の自動再検出 | 絶対パス参照を全面排除。`.maildock_root` のUUID照合による自動追従 |
| **稼働中の物理切断（外付けドライブの抜去）** | 高 | 中 | I/O例外の分類・ハートビート・`WM_DEVICECHANGE` | 「安全な取り外し」メニューによる発生確率の低減を主対策とし、`DETACHED` 状態機械で即座に書き込みを停止。復帰時はマニフェスト末尾修復＋範囲限定検証（5.7.1） |
| 切断後の再接続で別デバイスが同じドライブレターを取得 | 中 | **高** | `.maildock_root` のUUID照合（`FOREIGN` 判定） | パス存在を同定根拠にしない。`FOREIGN` 検出時は即座に全書き込みを禁止し警告を表示 |
| 切断によるスタール `.lock` で起動不能 | 中 | 低 | `heartbeat_at` の鑑定 | ロック実体の取得可否とハートビート鮮度の2軸で判定し、古いロックは回収する（3.6参照） |
| PST取込中の物理切断 | 中 | 中 | Stage A完了マーカーの有無 | `readpst` を terminate→killし、マーカーが無ければstagingを `suspect` として再抽出を促す（再開させない） |
| 初回同期の長時間化・中断 | 高 | 中 | 進捗表示 | `last_seen_uid` によるレジューム。バイト数ベースの進捗と残り時間表示 |
| 巨大メール・破損MIMEによる同期停止 | 中 | 中 | `sync_failures` | 1通の失敗で全体を止めない設計。サイズ上限とスキップ記録、再解析機能 |
| メールプロバイダの認証仕様変更 | 中 | 中 | 認証エラー | 通信部を `BaseMailFetcher` として分離済み。通信モジュール単体の改修で対応可能 |
| trigramインデックスの性能・容量が想定外 | 低 | 中 | Phase 2 のPoC | 実測してから作り込む。問題があれば contentless 方式やLIKE中心へ切り替え |
| readpst の変換品質が想定以下（日本語化け・添付欠損） | 中 | **高** | Phase 4.5 冒頭のPoC | 実PSTで先に実測する。`-C cp932` / `-8` で改善しなければ別方式（Outlook COM等）を再検討。**原本PSTを保管させておくことが最後の防御線** |
| readpst のクラッシュ・無応答 | 中 | 中 | 終了コード・stderr・`-d`ログ | Stage A はやり直し可能な設計。staging を破棄して再実行するだけで復帰できる |
| PSTフォルダ名によるStage A失敗・衝突・長パス | 中 | **高** | Phase 4.5冒頭のWindows実機PoC | 禁止文字・予約名・衝突・長パスをブロッカー判定し、Stage Bでは全件 `resolve()` してstaging配下を再検証 |
| PST再変換失敗による既存アーカイブ喪失 | 低 | **高** | 世代交代テスト・監査ログ | 旧世代を保持したまま新世代を構築し、検証後にのみアクティブ世代を切替。旧世代は30日猶予付きでpurge |
| GPL遵守漏れ（OSS公開時） | 中 | 中 | リリースCIのチェック | バイナリをGit管理外とし、COPYING＋ソースが揃わないリリースをCIで失敗させる（5.9参照） |
| PST取込中のディスク充満 | 中 | 中 | 開始前の空き容量チェック | PSTサイズ×2.5を下回る場合は開始させない。再変換では旧世代保持分も加算し、再開不要になったstagingだけを削除 |

## **8\. 決定事項ログ**

| 項目 | 決定内容 |
| :---- | :---- |
| FTS5スキーマ | 本文専用テーブル `message_contents` を content テーブルとする方式を採用 |
| 短いキーワードの検索 | trigramの自動フォールバックは存在しないため、アプリ側で `LIKE` 経路へ明示的に分岐 |
| メール識別子 | `content_key` は非一意の照合用。IMAPは `account_id + folder_id + uidvalidity + uid`、PSTは `account_id + folder_id + source_item_key` を一意キーとする |
| 同期方式 | UID増分同期（`last_seen_uid`）。全Message-ID取得は行わない |
| 移動の扱い | `content_key` によるアカウント横断照合で `moved` として判定 |
| 状態管理 | `remote_state` と `local_state` の2列に分離 |
| ローカル削除 | ゴミ箱移動 → 30日経過で実ファイル削除（purge）。墓標レコードは残す |
| 想定規模 | 最大 50,000通 / 100GB |
| 同期対象フォルダ | ユーザー選択式（新規フォルダは既定で非対象） |
| スレッド情報 | `in_reply_to` / `references_ids` / `thread_key` を初期から保持 |
| 初回同期の範囲 | 日付指定は行わず、新しい順に自動取得し、いつでも中断・再開可能とする |
| 1通のサイズ上限 | 既定50MB。超過分はスキップして記録し、UIから個別取得可 |
| サーバーゴミ箱フォルダ | SPECIAL-USE→候補名探索→手動指定の順で決定 |
| IMAPフラグ | 同期時点のスナップショットを保存し表示のみ。双方向同期は行わない |
| ローカル既読管理 | 行わない（閲覧専用アーカイブとして位置づける） |
| スレッド表示 | 一覧はフラット。スレッドは詳細画面から辿る |
| 添付の検索 | ファイル名のみ。文書の中身抽出はスコープ外 |
| 複数アカウント | 「すべてのアカウント」による横断表示に対応 |
| 同期の同時実行 | 常に1アカウントずつ順次処理 |
| ストレージ切断時の振る舞い | `ATTACHED` / `DEGRADED` / `DETACHED` / `RECONNECTING` / `VERIFYING` の状態機械で管理し、読み取り専用の縮退モードは提供しない |
| ルートの同定方法 | 常に `.maildock_root` のUUID。パスの存在だけでは同定しない（`FOREIGN` を明示的に扱う） |
| 切断復帰時の検証範囲 | マニフェストの最終チェックポイント以降のみを再ハッシュする「範囲限定検証」を新設 |
| リムーバブルメディアでの `synchronous` | 既定 `NORMAL`（DBは再構築可能な派生物であるため）。Phase 4 の実測で必要と判断された場合のみコミット時 `FULL` へ変更 |
| purgeの実行契機 | 設定で A（手動）/ B（確認あり自動）/ C（完全自動）を選択。**既定はA** |
| 起動時の検証 | クイック検証のみ自動実行。フル検証は手動 |
| テスト環境 | Docker上の Dovecot / GreenMail を使った結合テスト |
| Pythonバージョン | 3.13 に統一 |
| 保存データ暗号化 | アプリでは実装せず、BitLocker To Go を前提条件とする |
| **PST変換方式** | **`readpst`（libpst）バイナリを同梱し `subprocess` で起動**。pypff / libratom / extract-msg / Outlook COM は採用しない |
| **PSTアーカイブの位置づけ** | **IMAPアーカイブとは別機能**。ストレージ・DB・検索基盤は共有するが、データとしては接続しない |
| **PSTのアカウント** | PST取込世代ごとに擬似アカウントを作る。`provider_type='pst_import'`、`id = pst_{原本SHA-256先頭12桁}_{import_uuid先頭8桁}`。同一性判定は完全SHAを使う |
| **原本PSTの扱い** | アプリ管理外（ユーザーが保管）。DBにはファイル名・SHA-256・サイズ・取込日時のみ記録する |
| **PSTの重複排除** | 行わない。IMAP側と同じメールがあっても両方保持する |
| **PSTの対応形式** | `.pst` のみ。`.ost` / 単体 `.msg` / mbox はスコープ外 |
| **PST取込の構造** | 2ステージ（Stage A: readpst抽出、レジューム不可 / Stage B: EML取込、レジューム可） |
| **PST取込の再開** | 未完了時は同じ `import_uuid`・staging・項目マニフェストを再利用し、新規取込として扱わない |
| **PSTの再変換** | 通常の二重取込は禁止。旧世代を保持したまま新世代を完成・検証し、アクティブ世代を切り替えてから旧世代を30日猶予付きで破棄する |
| **永続マニフェスト** | EMLとappend-onlyマニフェストを正本とし、DBは両者から再構築する。PSTでは原本・フォルダ・項目・変換条件・状態イベントを保持する |
| **PST取込の既定オプション** | `-t e`（メールのみ、固定） / `-C cp932` / `-D` はOFF / `-8` 有効 |
| **PST由来の状態** | `remote_state='no_remote'` を新設。同期・サーバー削除はコードレベルで無効化 |
| **UIの切り替え** | 左ペインを「メールアカウント」と「PSTアーカイブ」の2ルートに分ける。横断表示はルート内に限定 |
| **PST機能の開発順序** | Phase 4 の後に Phase 4.5 として実施。冒頭に readpst の実PST PoC を置く |
| **公開形態とライセンス** | **GitHubでGPL-3.0-or-laterとして公開**する。readpstはGPL-2.0-or-laterの独立成果物として、完全な対応ソース、MSYS2ビルド情報・パッチ、COPYING、依存DLL情報を同梱する |