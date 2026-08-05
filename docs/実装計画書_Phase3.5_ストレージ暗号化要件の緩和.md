# **Phase 3.5: ストレージ暗号化要件の緩和 実装計画書**

対象: [ローカルメールバックアップ＆閲覧アプリ 開発計画書.md](./ローカルメールバックアップand閲覧アプリ開発計画書.md) の **5.3 認証・セキュリティ**（保存データの暗号化・資格情報の保管）および **2.4 保存先ディレクトリ・ストレージ構造**

前提: [Phase 3: GUI基礎構築 実装計画書](./実装計画書_Phase3_GUI基礎構築.md) の成果物（`StorageSession`・`AppContext`・セットアップウィザード・メイン画面・設定ダイアログ）が完成していること。

位置づけ: **Phase 3 と Phase 4 の間に差し込む小規模フェーズ**。Phase 4（統合 & 例外処理）が切断状態機械・purge・整合性チェックを本格実装する前に、ストレージ保管先の前提条件そのものを確定させる。

本書と開発計画書に矛盾がある場合は、**開発計画書を正**とする。ただし本フェーズは開発計画書 5.3 の書き換えを含むため、書き換え後の 5.3 を正とする。

---

## **1. 目的**

**「ストレージルートは BitLocker To Go で暗号化されていること」という必須の前提条件を撤廃し、Windows 11 Home・macOS・Linux のユーザーが本アプリを安全に利用できるようにする。**

現行の 5.3 は BitLocker To Go を必須の前提条件としているため、事実上 Windows 11 Pro 以上でしか正規の運用ができない。これはアプリのアクセシビリティを不必要に狭めている。

一方で、この制約を単純に外すと「保管先が暗号化されているかどうかを誰も知らない」状態になる。本フェーズは、要件を緩和しつつ**保管先の安全性に関する事実を測定・記録・表示する**仕組みを導入することで、緩和と安全性を両立させる。

具体的には、以下を満たす。

1. **暗号化手段をベンダー中立にすること** — BitLocker To Go に加えて VeraCrypt / LUKS / APFS暗号化を推奨手段とし、暗号化なしの運用も自己責任で許可する。
2. **「安全でない保管先」を製品名ではなく能力で判定すること** — Cryptomator 等のファイル単位暗号化ツール（仮想ファイルシステム）上では、設計不変条件2が依存する `os.replace` の原子性・排他ロック・fsync の保証が実装依存になる。これを**ストレージ適合性セルフテスト**で測定し、既知の非互換性を検出する。ただし、単発のI/O成功から原子性・永続性・WALの安全性を完全に証明することはできないため、本テストは安全性の保証ではなく**互換性プローブ**として扱う。
3. **暗号化状態について虚偽の保証をしないこと** — BitLocker / VeraCrypt / LUKS のマウント状態を確実に判定する手段は存在しない。したがって自動検出は実装せず、**ユーザーの申告**として記録し、UI にもそのように表示する。
4. **資格情報の保護レベルを保管先の暗号化状態から独立させること** — 資格情報は PC 側（`keyring`）にあり、ストレージルートには無い。この分離を維持することで、未暗号化ドライブを紛失しても「生きているアカウントの乗っ取り」へ波及しない。併せて安全でない keyring バックエンドへの暗黙のフォールバックを禁止する。
5. **アプリ層暗号化を採用しないことを決定として固定すること** — 7z / AES-ZIP / SQLCipher による暗号化は、可搬性（開発計画書 1.2）の毀損・`metadata.db` と FTS インデックスが平文で残ること・鍵喪失によるデータ全損リスクから、恒久的にスコープ外とする。

**Phase 3.5 のゴール判定:** 開発計画書 5.3 が3層モデルへ書き換えられ、セルフテストが安全でない保管先を検出し、暗号化申告が UI に常時表示され、6章の検証項目がすべて成功して CI が緑になること。

---

## **2. 要件**

### **2.1 前提となる意思決定（確定済み）**

| # | 項目 | 決定内容 |
| :--- | :---- | :---- |
| D-1 | 暗号化要件のモデル | **3層モデルへ移行する**。**推奨（Supported）**: BitLocker To Go / VeraCrypt / LUKS / macOS APFS暗号化などの**ブロックレベル暗号化**。マウント後は通常のファイルシステムであり、`os.replace` の原子性・fsync・WAL・バイト範囲ロックの前提がすべて成立する。**非推奨（Unsupported）**: Cryptomator / gocryptfs / rclone crypt / Boxcryptor などの**仮想ファイルシステム上のファイル単位暗号化**。**自己責任（Unencrypted）**: 暗号化なし。明示的な申告を記録して続行を許可する |
| D-2 | アプリ層暗号化 | **恒久的に採用しない**。理由は3点。①開発計画書 1.2 が謳う可搬性（本アプリが無くても Thunderbird 等で `.eml` を直接閲覧できる）を毀損する ②EMLだけ暗号化しても `metadata.db` の `message_contents`（本文全文 約1GB）と trigram FTS インデックス（約4GB）が平文で残り、脅威モデルをほぼ満たさない ③鍵喪失＝全データ喪失であり、長期保管アプリの存在意義と矛盾する。SQLCipher も 5.3 の既存判断どおり採用しない |
| D-3 | 安全でない保管先の判定方法 | **製品名ではなく能力で判定する**。Cryptomator 等を確実に検出する手段は存在せず、製品名ベースの判定は必ず漏れる。ストレージルート配下で別プロセスとの排他ロック競合、`os.replace` 上書き、WAL可否等を測定する**ストレージ適合性セルフテスト**を新設する。ただし、これは既知の非互換性を検出する互換性プローブであり、安全性を完全に証明するものではない。Cryptomator 等でも `OK` になり得る限界をUIとドキュメントで明示する |
| D-4 | 暗号化状態の自動検出 | **実装しない**。`Win32_EncryptableVolume` / `manage-bde` は管理者権限を要し、かつ Windows Home には管理ツールが存在しない。VeraCrypt / LUKS のマウント検出にも確実な手段が無い。暗号化状態は `encrypted` / `unencrypted` / `unknown` の3値の**ユーザー申告**として扱い、`unknown` を異常ではなく正常な状態として設計する |
| D-5 | セルフテスト失敗時の扱い | 致命的失敗（排他ロック不可・`os.replace` 不可）のとき、**ウィザードでの新規ルート選択は拒否**して次へ進ませない。一方、**既に運用中の既存ルート**（アプリ更新後に初めて検査して失敗した場合）は、測定結果を `config.json` へ即時保存してから、`root_uuid` と文字列の `capability_level` だけを持つ構造化 `StorageUnsupportedError` を送出する。GUIはこれを捕捉し、**1回だけ強い確認を出して続行を許可**する。承認（ack）を保存後、設定を再ロードしてセッションを1回だけ再生成し、以後ステータスバーに常時警告を表示する。例外へ infrastructure の型は格納しない |
| D-6 | 検査結果・申告の保存場所 | **`config.json`（内蔵ディスク側）に `root_uuid` 紐付けで保存する**。各プロファイルには測定時の正規化パスとストレージ指紋も保存し、現在値と一致するときだけ能力キャッシュを再利用する。指紋は Windows のボリュームシリアル、POSIX の `st_dev` 等の標準APIで得られる媒体識別値と正規化パスから構成し、取得不能時は正規化パスへフォールバックする。これにより、`.maildock_root` ごと複製された同一UUIDの別媒体へ古い検査結果を持ち回らない。指紋は暗号化状態の判定には使用しない |
| D-7 | `journal_mode` の決定 | **セルフテスト結果と `DriveKind` の安全側の組合せで決定する**。ローカルドライブは `PRAGMA journal_mode=wal` の戻り値が `wal` の場合だけ `WAL`、WAL不可または `DriveKind.NETWORK` の場合は常に `DELETE` とする。単発のPRAGMA成功だけではSMB上のWAL安全性を証明できないため、ネットワークドライブでの `WAL` は許可しない。`connect()` / `ConnectionManager` の `network_drive: bool` 引数は `journal_mode: str` へ置換し、`DriveKind` は判定・表示・警告ログ用に残す（`VIRTUAL` は追加しない） |
| D-8 | セルフテストの非破壊性 | **すべてのテストを `root/tmp/` 配下の一時ファイルだけで完結させる**。本番の `.lock` / `metadata.db` / `eml/` / `manifests/` には一切触れない。`tmp/` はストレージルート配下（＝EMLと同一ボリューム）にあるため、本番と同じ媒体・同じファイルシステムを測定できる。テスト用ファイルは `finally` で必ず削除し、`tmp/pstimp/` には触れない |
| D-9 | `os.replace` テストの範囲 | **「宛先ファイルを開いたまま `os.replace` する」ケースはテストしない**。Windows では正常なローカルNTFS上でも共有違反で失敗するため、判定基準として成立しないため。テストは「既存ファイルへの上書き配置が成功すること」に限定する |
| D-10 | CLI での扱い | **CLI では承認（ack）を発行しない**。`UNSUPPORTED` かつ ack 未記録の状態で CLI コマンドを実行した場合は `StorageUnsupportedError` で必ず失敗させる。取り消しの効かない安全判断を、確認ダイアログの無い経路で通過させないため |
| D-11 | 設定スキーマ | **`schema_version` を 1 → 2 へ上げる**。検査結果・測定時パス・ストレージ指紋・承認・暗号化申告・初回同期確認を `storage_profiles`（キー = `root_uuid`）の**1フィールドへ集約**する。既存の v0 → v1 upgrader はリテラル `1` を設定し、v0 → v1 → v2 を順に通す |
| D-12 | keyring バックエンドの判定 | **許可リスト方式で検査する**。Linux では D-Bus やデスクトップ環境が無い場合に `keyrings.alt` の平文バックエンドへフォールバックし得るため、拒否リストではなく許可リストで判定する。許可外バックエンドでは資格情報を**保存しない** |
| D-13 | 資格情報の代替保存方式 | **`session_only` モードを新設する**。プロセス内のメモリにのみ保持し、ファイル・DB・ログへ一切書かない。許可外バックエンド検出時の自動フォールバック先であると同時に、共用PCで使いたいユーザーが明示的に選べる選択肢でもある。プロセス再起動後は既存アカウントの資格情報が失われるため、ネットワーク操作をワーカーへ投入する前にGUIではパスワード入力ダイアログ、CLIでは `getpass` で再入力させ、当該プロセスの `SessionCredentialStore` だけへ保存する |
| D-14 | 資格情報とストレージルートの分離 | **資格情報をストレージルート配下へ書き込むことを明示的な禁止事項へ格上げする**。「ドライブを別PCへ挿したら設定ごと復元したい」という要求に応じて資格情報を持たせると、未暗号化ドライブの紛失が即座にアカウント乗っ取りへ波及する。別PCではパスワード再入力が正しい挙動である |
| D-15 | 依存関係 | **追加しない**。セルフテストは標準ライブラリ（`os` / `sqlite3` / `msvcrt` / `fcntl`）のみで実装する。暗号化ライブラリ・アーカイブライブラリを一切導入しない（D-2） |
| D-16 | `db_backup_to_local_disk` | 条件を「C:のBitLockerが有効なときだけ」から「**複製先の暗号化状態が保管元より弱い場合は警告し、既定でOFF**」へ一般化する。本フェーズでは**文言と `AppConfig` の定義のみ**を扱い、実処理は Phase 4 のままとする |
| D-17 | セルフテストの再実行契機 | **`config.storage_profiles[root_uuid]` に有効なキャッシュがあり、測定時の正規化パスとストレージ指紋が現在値に一致する場合だけ再実行しない**（起動時間 3秒以内の維持のため）。再実行するのは ①該当UUIDの記録が無いまたは不正なとき ②パスまたは指紋が変わったとき ③ウィザードでルートを選択したとき ④設定ダイアログの「再検査」を押したとき、の4つとする。媒体識別値を取得できず同一パスへ別媒体を差し替えたケースは自動識別できないため、設定画面からの再検査を案内する |
| D-18 | 未暗号化での続行確認 | **強い確認は初回同期の開始直前に1回だけ**とする。ウィザード時点ではまだ本物のメールが1通も媒体に書かれていないため。以後は警告ダイアログを繰り返さず、**メインウィンドウのステータスバーに状態として常時表示**する。警告の連打によるユーザーの鈍感化を避ける |
| D-19 | VeraCrypt ファイルコンテナ | **条件付きで推奨層に含める**。マウント後は本物のファイルシステムであり技術的な問題は無い。ただし ①固定サイズコンテナであること（動的コンテナはホスト側の空き容量枯渇で書き込みが失敗し、空き容量チェックが機能しない） ②クラウド同期フォルダ・ネットワーク共有に置かないこと ③自動アンマウントを無効化すること ④ボリュームヘッダのバックアップを取ること、の4条件をドキュメントに明記する。専用の外付けSSDならデバイス全体の暗号化を第一推奨とする |
| D-20 | 非推奨ツールの扱い | Cryptomator 等は**ドキュメント上は明確に非推奨と記載する**が、**実装は製品名を検出しない**（D-3）。セルフテストが実際の能力不足を検出した場合にのみ `UNSUPPORTED` / `DEGRADED` として扱う。将来登場する同種のツールにも自動的に対応できる |

### **2.2 機能要件**

| # | 要件 | 根拠 |
| :--- | :---- | :---- |
| F-1 | 開発計画書 5.3 の「保存データの暗号化」が3層モデルへ書き換えられ、BitLocker To Go が「必須の前提条件」ではなく「推奨手段のひとつ」として記述されていること | D-1 |
| F-2 | 開発計画書 5.3 に、アプリ層暗号化を採用しない理由が3点とも記録されていること | D-2 |
| F-3 | ストレージルートの初期化時にストレージ適合性セルフテストが実行され、別プロセスとの排他ロック競合・`os.replace` 上書き・WAL・fsync・大文字小文字の区別・長パスの各項目が測定されること | D-3 |
| F-4 | セルフテストが `root/tmp/` 配下の一時ファイルのみを使用し、本番の `.lock` / `metadata.db` / `eml/` / `manifests/` / `tmp/pstimp/` に一切触れず、終了時に残骸を残さないこと | D-8 |
| F-5 | セルフテストが `OK` / `DEGRADED` / `UNSUPPORTED` の3値へ集約されること。排他ロックまたは `os.replace` の失敗は `UNSUPPORTED`、WAL または fsync の失敗は `DEGRADED` とすること | D-3 / D-5 |
| F-6 | ウィザードのルート選択でセルフテスト結果が表示され、`UNSUPPORTED` の場合は次のページへ進めないこと | D-5 |
| F-7 | 既存の運用中ルートが `UNSUPPORTED` と判定された場合、測定結果を先に保存して構造化 `StorageUnsupportedError` を送出し、GUIで1回だけ強い確認を出して続行を許可すること。承認保存後は設定を再ロードしてセッションを1回だけ再生成し、ステータスバーに警告を常時表示すること | D-5 / D-18 |
| F-8 | CLI では `UNSUPPORTED` かつ承認未記録のとき `StorageUnsupportedError` で必ず失敗すること。CLI から承認を発行する手段が存在しないこと | D-10 |
| F-9 | SQLite の `journal_mode` がセルフテスト結果と `DriveKind` から安全側に決定され、WALが使えない保管先とネットワークドライブでは `DELETE` になること。`journal_mode` の値が許可リストで検証されてからPRAGMAへ渡されること | D-7 |
| F-10 | ウィザードで暗号化状態を `encrypted` / `unencrypted` / `unknown` の3値から申告でき、`unencrypted` を選んだ場合のみ確認チェックボックスが必須になること。自動検出を行わないこと | D-4 |
| F-11 | セルフテスト結果・測定時パス・ストレージ指紋・承認・暗号化申告・初回同期確認が `config.json` に `root_uuid` 紐付けで保存され、ストレージルート配下には書き込まれないこと。パスまたは指紋が変われば能力キャッシュを再利用しないこと | D-6 / D-11 |
| F-12 | `config.json` の `schema_version` が 1 → 2 へ無損失でアップグレードされ、未知キーが保持されること | D-11 |
| F-13 | メインウィンドウのステータスバーに保管先の暗号化状態が常時表示され、暗号化ガイドへの導線があること | D-18 |
| F-14 | 暗号化申告が `unencrypted` または `unknown` の場合、初回同期の開始直前に1回だけ確認を表示し、確認後は繰り返さないこと | D-18 |
| F-15 | 設定ダイアログから暗号化状態の再申告・資格情報の保存方式の変更・ストレージ適合性の再検査ができること | D-17 |
| F-16 | `keyring` のバックエンドが許可リスト方式で検査され、許可外バックエンドでは資格情報を保存しないこと | D-12 |
| F-17 | 許可外バックエンド検出時に `session_only` へ自動フォールバックし、警告をログとUIへ出すこと | D-12 / D-13 |
| F-18 | `session_only` モードの資格情報ストアが、ファイル・DB・ログへ一切書き込まず、既存アカウントの資格情報が無い場合はネットワーク操作開始前にGUI／CLIで再入力できること | D-13 |
| F-19 | 開発計画書 5.3 に、資格情報をストレージルート配下へ書き込むことが明示的な禁止事項として記載されていること | D-14 |
| F-20 | セルフテストが Qt に依存せず、`gui` マーカー外の通常CIでテストできること | D-3 |
| F-21 | README に OS 別の暗号化手順・VeraCrypt の4条件・非推奨ツールの理由・自動ロック無効化の注意が記載されていること | D-19 / D-20 |
| F-22 | 新しい依存パッケージが追加されていないこと | D-15 |

### **2.3 非機能要件・制約**

| # | 指標 | 目標値 | 備考 |
| :--- | :---- | :---- | :---- |
| N-1 | セルフテストの所要時間 | **1秒以内** | 通常のローカルSSD上で。初回のみ実行される |
| N-2 | アプリ起動時間 | **3秒以内を維持** | キャッシュにより2回目以降はセルフテストを実行しない（D-17。開発計画書 5.1） |
| N-3 | 既存データへの影響 | **ゼロ** | セルフテストは `tmp/` 配下のみを使用する（D-8） |
| N-4 | 設定の後方互換 | **無損失** | schema v1 の `config.json` から v2 へ、未知キーを保持したままアップグレードする |

* `ruff check` / `mypy` / `pytest` を通すこと。
* レイヤーの依存方向を厳守する。セルフテストは `infrastructure/storage/` に置き、`domain` / `usecases` / `presentation/views` から infrastructure の具象を直接importしない。
* セルフテストの全I/Oを `detach.storage_io()` で包み、`OSError` を `StorageDetachedError` 等のドメイン例外へ分類してから上位へ渡す。
* 資格情報・パスワードをログへ出力しない。`SessionCredentialStore` は `__repr__` でも値を露出しない。
* `journal_mode` を SQL 文字列へ連結する前に必ず許可リストで検証する（SQLインジェクション経路を作らない）。

---

## **3. タスク**

> 依存関係: **(A は独立して並行) / (B・C を並行) → D → E**。F（テスト）は各グループと並行して作成する。

### **3.1 グループA: ドキュメント改訂（*他グループと並行可*）**

#### **A-1. 開発計画書 5.3「保存データの暗号化」の全面書き換え**

- [x] 「BitLocker To Go 等で暗号化することを必須の前提条件とする」を削除し、**3層モデルの表**へ差し替える（D-1）
- [x] 推奨層に OS 別の手段を列記する（Windows Pro: BitLocker To Go / Windows Home: VeraCrypt / macOS: APFS暗号化・VeraCrypt / Linux: LUKS・VeraCrypt）
- [x] Windows Home でも**BitLockerで暗号化済みのドライブを解錠して読み書きすることは可能**であり、できないのは作成のみである旨を注記する
- [x] VeraCrypt について、**デバイス全体の暗号化を第一推奨**、ファイルコンテナを「ドライブを他用途と共用する場合の選択肢」と位置づけ、4条件（固定サイズ必須 / クラウド同期フォルダ・ネットワーク共有に置かない / 自動アンマウント無効化 / ヘッダバックアップ）を明記する（D-19）
- [x] 非推奨層（ファイル単位暗号化・仮想ファイルシステム）について、`os.replace` の原子性・排他ロック・fsync が実装依存になり**設計不変条件2が保証できなくなる**ことを理由として明記する（D-20）
- [x] 自己責任層について、明示的な申告を記録して続行を許可する旨と、UI に常時表示する旨を明記する
- [x] **暗号化状態の自動検出は行わず、ユーザー申告として記録する**（技術的検証を装わない）ことを明記する（D-4）
- [x] **アプリ層暗号化（7z / AES-ZIP / SQLCipher）を採用しない理由を3点とも記録する**（D-2）
- [x] 既存の「アプリ内でのDB暗号化（SQLCipher等）は採用しない」の記述を、新しい理由付けと整合するよう更新する

#### **A-2. 開発計画書 5.3「資格情報の保管」への追記**

- [ ] `keyring` バックエンドを**許可リスト方式**で検査すること、許可外では保存しないことを明記する（D-12）
- [ ] Linux で D-Bus / デスクトップ環境が無い場合に `keyrings.alt` の平文バックエンドへフォールバックし得る危険性を明記する
- [ ] `session_only` モード（プロセス内メモリのみ）の存在と、その用途（許可外バックエンドのフォールバック先 / 共用PCでの明示的選択）を追記する（D-13）
- [ ] **資格情報をストレージルート配下へ書き込むことを明示的な禁止事項として格上げする**。理由（未暗号化ドライブの紛失がアカウント乗っ取りへ波及しない分離を維持するため）を併記する（D-14）
- [ ] 資格情報は PC 側、メールデータはストレージルート側という**保管場所の分離**が、暗号化要件の緩和後もそのまま有効であることを明記する

#### **A-3. 開発計画書 2.4「設計上のポイント」への項目追加**

- [ ] 新項目「**ストレージ適合性セルフテスト**」を追加し、測定項目・判定値（`OK` / `DEGRADED` / `UNSUPPORTED`）・失敗時の挙動を記述する（D-3 / D-5）
- [ ] 判定を**製品名ではなく能力で行う**理由（確実な検出手段が無く、将来のツールにも対応する必要があるため）を明記する
- [ ] `tmp/` 配下のみを使う非破壊テストであることを明記する（D-8）
- [ ] 既存の項目4（ネットワークドライブの制限）を更新し、`journal_mode` をセルフテスト結果と `DriveKind` の安全側の組合せで決定すること、ネットワークドライブは常に `DELETE` とすることを明記する（D-7）
- [ ] 検査結果を**内蔵ディスク側の `config.json` へ `root_uuid` 紐付けで保存し、測定時パスとストレージ指紋が一致するときだけ再利用する**理由（ドライブ丸ごとコピーが同じUUIDを引き継いでも別媒体の判定を持ち回らないため）を明記する（D-6）

#### **A-4. 開発計画書 5.7 / 5.7.1 の更新**

- [ ] 5.7 の「ドライブ丸ごとのコピーが完全なバックアップになる」を、**暗号化方式別に書き分ける**（デバイス暗号化時＝通常のファイルコピーで可 / ファイルコンテナ運用時＝アンマウントしてコンテナファイルを丸ごとコピー、差分バックアップ不可、マウント中のコピー禁止）
- [ ] VeraCrypt のボリュームヘッダバックアップを、コンテナ運用時の必須手順として追記する（D-19）
- [ ] 5.7 の `metadata.db.bak` に関する但し書きを、BitLocker 固有の条件から「**複製先の暗号化状態が保管元より弱い場合は警告し、既定でOFF**」へ一般化する（D-16）
- [ ] 5.7.1 に、**Vault のアイドル自動ロック**と **VeraCrypt の自動アンマウント**（スクリーンセーバー起動時等）を切断イベントの発生源として追記する。数時間規模の初回同期中に発火し得るため、無効化を推奨する旨を併記する

#### **A-5. 開発計画書 5.10 / 5.11 / 7章 / 8章の更新**

- [ ] 5.10 テスト方針に「**ストレージ適合性セルフテスト**」の行を追加する（各能力の失敗を注入する単体テスト。Qt非依存で通常CIで実行）
- [ ] 5.11 主要設定項目に3行を追加する: 「保管先の暗号化状態（申告）＝ 既定 `unknown`」「資格情報の保存方式 ＝ 既定 `keyring`」「ストレージ適合性の確認結果 ＝ ルート確定時に自動測定」
- [ ] 7章リスク管理の「SSD紛失・盗難による情報漏洩」行の対応策を、3層モデルと申告・常時表示の仕組みへ書き換える
- [ ] 7章リスク管理に新規行「**ファイル単位暗号化ツール上での運用による原子性・ロックの喪失**」を追加する（検知方法＝セルフテスト、対応策＝`UNSUPPORTED` 判定と続行時の常時警告）
- [ ] 8章決定事項ログの「保存データ暗号化」行を3層モデルへ書き換える
- [ ] 8章決定事項ログに3行を追加する: 「**アプリ層暗号化の不採用**（理由3点）」「**資格情報の保存方式**（keyring 許可リスト / session_only）」「**ストレージ適合性の判定方法**（製品名ではなく能力測定）」

#### **A-6. 既存実装計画書の該当箇所の修正**

- [ ] `実装計画書_Phase0_基盤整備.md` の前提条件「ストレージルートの BitLocker To Go による暗号化（5.3）」を3層モデルの記述へ修正する
- [ ] 同 D-9（`metadata.db.bak` の内蔵ディスク複製）の条件記述を D-16 の一般化された条件へ修正する
- [ ] 同 Phase 4 への申し送り（`db_backup_to_local_disk` の実処理条件）を同様に修正する
- [ ] `実装計画書_Phase3_GUI基礎構築.md` の F-3（ウィザードのルート選択要件）に、セルフテストと暗号化申告が Phase 3.5 で追加された旨の追記を行う
- [ ] 同 E-1（ウィザード）の「暗号化状態を確認できない場合に警告を表示する」タスクに、Phase 3.5 で3値申告へ置き換えた旨の追記を行う

#### **A-7. README への暗号化ガイド追加**

- [ ] 「保管先の暗号化」セクションを新設し、3層モデルを簡潔に説明する
- [ ] OS別の暗号化手順（Windows Pro / Windows Home / macOS / Linux）を記載する
- [ ] VeraCrypt の4条件を記載する（D-19）
- [ ] 非推奨ツールとその理由を記載する（D-20）
- [ ] 自動ロック・自動アンマウントの無効化と、アンマウントしてからシャットダウンする運用を記載する
- [ ] 3-2-1ルールのバックアップ推奨（開発計画書 5.7）への導線を張る

---

### **3.2 グループB: 基盤実装（*A と並行可。D の前提*）**

#### **B-1. `domain/errors.py` — 例外の追加**

- [ ] `StorageUnsupportedError(StorageError)` を追加する（保管先が本アプリの安全要件を満たさないことを表す）
- [ ] 既存の `StorageError` 配下の並びに合わせ、docstring で「必須の排他ロックまたは上書き配置操作が成立しない保管先」であることを1行で示す
- [ ] `root_uuid: str` と `capability_level: str` を読み取り専用属性として持たせる。infrastructure の `StorageCapabilities` / `CapabilityLevel` は保持せず、詳細は保存済み `storage_profiles` から取得する

#### **B-2. `infrastructure/storage/capabilities.py` — ストレージ適合性セルフテスト（*本フェーズの中核*）**

- [ ] `class CapabilityLevel(StrEnum)` に `OK` / `DEGRADED` / `UNSUPPORTED` を定義する
- [ ] `@dataclass(frozen=True) class StorageCapabilities` を定義する
  - [ ] フィールド: `exclusive_lock: bool` / `replace_overwrite: bool` / `wal_supported: bool` / `fsync_supported: bool` / `case_sensitive: bool` / `long_path_ok: bool` / `checked_at: str`（UTC ISO8601）。`replace_overwrite` は操作の成功だけを表し、原子性の完全な証明を意味しない
  - [ ] `as_dict() -> dict[str, JSONValue]` と `from_dict(...) -> StorageCapabilities | None` を実装する（`config.json` とのラウンドトリップ用。不正な辞書は `None` を返して再測定させる）
- [ ] `probe_capabilities(root: Path) -> StorageCapabilities` を実装する
  - [ ] すべての一時ファイルを `root/tmp/.captest-{uuid4}*` として作成し、`finally` で確実に削除する（D-8）
  - [ ] **排他ロック**: `tmp/.captest-{uuid}.lock` を親プロセスでロックしたまま、標準ライブラリの別プロセスから同じ範囲の非ブロッキングロック取得を試み、競合側が失敗することを確認する。Windows は `msvcrt.locking(LK_NBLCK, 1)`、POSIX は `fcntl.flock(LOCK_EX | LOCK_NB)` を使用する。子プロセスの終了時間を制限し、必ず回収する。**本番の `.lock` には触れない**
  - [ ] **`os.replace` 上書き**: `tmp/.captest-{uuid}.a` を、既に存在する `tmp/.captest-{uuid}.b` へ上書き配置する。**宛先を開いたままのケースはテストしない**（D-9）
  - [ ] **WAL**: `tmp/.captest-{uuid}.db` へ `sqlite3.connect` し、`PRAGMA journal_mode=wal` の**戻り値**が `wal` であることを検証する。**本番の `metadata.db` には触れない**
  - [ ] **fsync**: ファイルの `os.fsync` を実行し、POSIX ではディレクトリの fsync も実行する
  - [ ] **大文字小文字の区別**: `tmp/.captest-{uuid}A` と `tmp/.captest-{uuid}a` を作り分けられるか測定する（記録のみ。判定には使わない）
  - [ ] **長パス**: `tmp/.captest-{uuid}/` 以下だけで、`eml/{account_id}/{YYYY}/{MM}/{hash32}.eml` の想定最大長に相当する深さ・長さのパスを再現して作成できるか測定する
  - [ ] すべてのI/Oを `detach.storage_io()` で包む。個別テストの失敗は例外にせず `False` として記録し、`StorageDetachedError` だけは上位へ送出する
- [ ] `capability_level(capabilities: StorageCapabilities) -> CapabilityLevel` を実装する
  - [ ] `exclusive_lock` または `replace_overwrite` が `False` → `UNSUPPORTED`
  - [ ] `wal_supported` または `fsync_supported` が `False` → `DEGRADED`
  - [ ] それ以外 → `OK`
- [ ] `journal_mode_for(capabilities: StorageCapabilities, *, network_drive: bool) -> str` を実装する。`network_drive=True` または `wal_supported=False` なら `"DELETE"`、それ以外は `"WAL"` とする
- [ ] `storage_fingerprint(root: Path) -> str` を実装する。Windowsはボリュームシリアル、POSIXは `st_dev` と正規化パスを使用し、媒体識別値を取得できない場合は正規化パスへフォールバックする。暗号化状態の推測には使用しない
- [ ] モジュール docstring に「**製品名ではなく能力を測る既知の非互換性検出用プローブであり、安全性を完全には証明しない。`tmp/` 配下以外に一切書き込まない**」と明記する

#### **B-3. `config.py` — schema v2 への拡張（*B-2 と並行可*）**

- [ ] `CURRENT_SCHEMA_VERSION` を `2` へ変更する
- [ ] `_upgrade_v1_to_v2` を実装し、`_CONFIG_UPGRADERS` へ登録する（新フィールドの既定値を補うだけ。既存値を書き換えない）
- [ ] `_upgrade_v0_to_v1` が `CURRENT_SCHEMA_VERSION` ではなくリテラル `1` を設定するよう修正し、v0 → v1 → v2 のアップグレードを順に通す
- [ ] `AppConfig` に `storage_profiles: Mapping[str, JSONValue]` を追加する
  - [ ] キーは `root_uuid`、値は `{capabilities, capability_level, checked_path, storage_fingerprint, capability_ack_at, encryption, encryption_declared_at, first_sync_confirmed_at}`
  - [ ] `encryption` の許可値を `ENCRYPTION_DECLARATIONS = frozenset({"encrypted", "unencrypted", "unknown"})` として定義する
- [ ] `AppConfig` に `credential_storage: str = "keyring"` を追加し、`CREDENTIAL_STORAGE_MODES = frozenset({"keyring", "session_only"})` を定義する
- [ ] `_KNOWN_FIELDS` へ両フィールドを追加する
- [ ] `_validate_config` に検証を追加する（`storage_profiles` の構造・キーの型・値の型・列挙値、`credential_storage` の許可値）。**不正値を既定へ黙って倒さず `ConfigError` を送出する**
- [ ] `db_backup_to_local_disk` のコメントを D-16 の一般化された条件へ更新する（実処理は Phase 4 のまま）
- [ ] 未知キーの `extra` 保持がスキーマ変更後も機能することを維持する

#### **B-4. `infrastructure/database/connection.py` — `journal_mode` への置換（*B-2 に依存*）**

- [ ] `_configure_connection(connection, *, readonly: bool, journal_mode: str)` へシグネチャを変更する
- [ ] `journal_mode` を許可リスト（`{"WAL", "DELETE"}`）で検証してから PRAGMA を発行する。許可外は `DatabaseError` とする
- [ ] `connect(db_path, *, readonly=False, journal_mode: str = "WAL")` へ変更する
- [ ] `ConnectionManager.__init__(db_path, *, readonly=False, journal_mode: str = "WAL")` へ変更する
- [ ] `network_drive: bool` 引数を**削除**する（呼び出し元は `__main__.py` と `presentation/app.py` の2箇所のみ）
- [ ] `journal_mode` の決定責務が呼び出し元（セルフテスト結果と `DriveKind` の安全側の組合せ）にある旨をコメントで1行残す

---

### **3.3 グループC: 資格情報（*B と並行可。D の前提*）**

#### **C-1. `infrastructure/security/keyring_store.py` — バックエンド検査**

- [ ] `class KeyringBackendStatus(StrEnum)` に `SUPPORTED` / `UNSUPPORTED` / `UNAVAILABLE` を定義する
- [ ] `_ALLOWED_BACKENDS` を**許可リスト**として定義する（`keyring.backends.Windows.WinVaultKeyring` / `keyring.backends.macOS.Keyring` / `keyring.backends.SecretService.Keyring` / `keyring.backends.kwallet.DBusKeyring`）
- [ ] `detect_backend() -> KeyringBackendStatus` を実装する。判定は `type(keyring.get_keyring())` の `__module__` + `__qualname__` を許可リストと照合して行う
- [ ] `backend_name() -> str` を実装する（設定ダイアログでの表示用）
- [ ] `KeyringCredentialStore` が許可外バックエンドで `set_password` を実行せず `CredentialStoreError` を送出するようにする
- [ ] 拒否リスト方式にしない理由（将来のバックエンド追加で漏れるため）をコメントで1行残す

#### **C-2. `infrastructure/security/session_store.py` — メモリのみの資格情報ストア（*C-1 と並行可*）**

- [ ] `SessionCredentialStore(BaseCredentialStore)` を実装する（プロセス内 `dict` のみ）
- [ ] `set_password` / `get_password` / `delete_password` を実装する。ファイル・DB・ログへ一切書き込まない
- [ ] `__repr__` を実装し、保持している値はもちろんアカウントIDも出力しない
- [ ] モジュール docstring に「プロセス終了で失われる。永続化しないことが仕様である」と明記する

---

### **3.4 グループD: 合成ルート統合（*B・C 完了後*）**

#### **D-1. `__main__.py` — `StorageSession` へのセルフテスト統合**

- [ ] `ensure_layout(root)` / `cleanup_tmp(root)` の直後に現在の正規化パスとストレージ指紋を取得し、`config.storage_profiles[root_uuid]` のキャッシュが有効か検証する。記録が無い／不正、パス不一致、指紋不一致の場合だけ `probe_capabilities(root)` を実行する（D-17）
- [ ] 新たに測定した `capabilities` / `capability_level` / `checked_path` / `storage_fingerprint` は、通常終了時の `_save_settings()` に依存せず、判定直後に専用処理で `config.json` へ原子的に保存する
- [ ] `capability_level` が `UNSUPPORTED` かつ `capability_ack_at` が未記録の場合、測定結果の保存成功後に限り、`StorageUnsupportedError(root_uuid, capability_level.value)` を送出する（D-5 / D-10）
- [ ] `journal_mode_for(capabilities, network_drive=self.network_drive)` の結果を `ConnectionManager` へ渡す
- [ ] `self.network_drive` は削除せず、`DriveKind.NETWORK` 検出時の警告ログ用として維持する
- [ ] `self.capabilities` / `self.capability_level` / `self.encryption_declaration` を公開属性として持たせ、GUI から参照できるようにする
- [ ] `_save_settings` で暗号化申告・承認等の `storage_profiles` 更新を保持する。掃除はUUIDだけで媒体を同定できない前提で行い、現在の候補パスに対応しないエントリだけを削除する
- [ ] `config.credential_storage` と `detect_backend()` から資格情報ストアを1回だけ選択し、`StorageSession` が `BaseCredentialStore` として所有する。`keyring` 指定でバックエンドが許可外なら `SessionCredentialStore` へフォールバックし、警告をログへ出す（D-12 / D-13）
- [ ] CLIと`AppContext`は独自に `KeyringCredentialStore` を生成せず、`StorageSession.credential_store` を共有する
- [ ] CLIで `session_only` を使用し、資格情報が無いアカウントへのネットワーク操作を開始する場合は `getpass` で再入力してセッションストアへ保存する。非対話環境で入力できない場合は `CredentialStoreError` とし、引数・環境変数による秘密情報の受け渡しは追加しない
- [ ] `_exit_code` で `StorageUnsupportedError` を `StorageLockedError` と同じ終了コード3へ割り当てる
- [ ] CLI から承認を発行する引数・環境変数を**追加しない**ことをコメントで明記する（D-10）

#### **D-2. `presentation/app.py` — GUI側の合成ルート更新**

- [ ] `ConnectionManager` 生成箇所を `journal_mode` へ差し替える
- [ ] `StorageSession` が保持する適合性・暗号化申告・資格情報ストアの選択結果を `AppContext` 経由で `MainWindow` へ渡す
- [ ] 既存ルートが `UNSUPPORTED` かつ未承認の場合、保存済みの測定結果を指す構造化 `StorageUnsupportedError` を捕捉して**続行確認ダイアログ**を出す。承認されたら `capability_ack_at` を保存し、`config.load()` で設定を再ロードして `StorageSession` を再生成する（D-5）
- [ ] 承認後のセッション再生成は1回だけ許可し、再度 `StorageUnsupportedError` になった場合はループせず終了する
- [ ] 承認されなかった場合は終了コード **3**（既存の `StorageLockedError` と同じ規約）で終了する

---

### **3.5 グループE: UI（*D 完了後*）**

#### **E-1. `presentation/strings.py` — 文言の追加**

- [ ] 暗号化申告の3値ラベルと説明文を追加する
- [ ] セルフテスト結果（`OK` / `DEGRADED` / `UNSUPPORTED`）の表示文言を追加する
- [ ] `UNSUPPORTED` の続行確認ダイアログ本文（何が保証できなくなるかを具体的に記述）を追加する
- [ ] 未暗号化での初回同期確認ダイアログ本文を追加する
- [ ] keyring バックエンド警告と `session_only` の説明文を追加する
- [ ] 既存の `WIZARD_WARNING_ENCRYPTION_UNKNOWN` / `WIZARD_ENCRYPTION_UNKNOWN` を新しい3値モデルの文言へ置き換える

#### **E-2. `presentation/views/setup_wizard.py` — ルートページの拡張（*E-1 に依存*）**

- [ ] `presentation/app.py` から注入されたコールバックを `_validate_root` で呼び、プリミティブ値だけの測定結果を新設の `_capability_label` へ表示する。Viewから `infrastructure.storage.capabilities` を直接importしない
- [ ] 注入コールバックはルートを測定し、`capabilities` / `capability_level` / `checked_path` / `storage_fingerprint` と暗号化申告を `config.json` へ保存する。新規ルートの `UNSUPPORTED` は保存後もセッションを開始しない
- [ ] `UNSUPPORTED` の場合は `_on_root_confirmed` を呼ばず `False` を返し、**次のページへ進ませない**（D-5）。理由と対処（ファイル単位暗号化ツール上ではないかの確認）をインラインで提示する
- [ ] `DEGRADED` の場合は警告を表示したうえで続行を許可する
- [ ] 暗号化申告用の `QComboBox`（暗号化済み / 未暗号化 / わからない）を追加する
- [ ] `未暗号化` を選択した場合のみ、確認チェックボックス（「暗号化されていないことを理解しました」）のチェックを必須にする
- [ ] `_encryption_label` の固定文言表示を廃止し、申告結果とセルフテスト結果を `config.storage_profiles` へ保存する
- [ ] `drive_kind()` / `free_space()` の既存表示はそのまま維持する（D-7）

#### **E-3. `presentation/views/main_window.py` — ステータス常時表示と初回同期確認（*E-1 に依存*）**

- [ ] `_build_status_bar` に `_encryption_status_label` を追加する（`_storage_status_label` の隣）
- [ ] `set_storage_encryption(state)` と `set_storage_capability(level)` を実装する。`UNSUPPORTED` を承認して続行中の場合は常時警告表示にする（D-5 / D-18）
- [ ] ラベルのツールチップから暗号化ガイドへ誘導し、ヘルプメニューに「保管先の暗号化について」を追加する
- [ ] 手動同期・起動時同期を含む全同期経路に、ワーカーへジョブを投入する**前**の共通ゲートを設ける。当該 `root_uuid` に `first_sync_confirmed_at` が無く暗号化申告が `unencrypted` / `unknown` の場合、**1回だけ**モーダル確認を表示する（D-18）。`sync_worker.sync_account()` / `sync_all_accounts()` を呼んだ後に確認してはならない
- [ ] 確認後に `first_sync_confirmed_at` を記録し、以後は確認を出さない
- [ ] 資格情報が `session_only` で動作している場合、その旨をステータスバーまたは通知で1回示す
- [ ] `session_only` で対象アカウントの資格情報が無い場合、ネットワーク操作をワーカーへ投入する前にパスワード入力ダイアログを表示し、入力値を当該プロセスのセッションストアだけへ保存する。キャンセル時は操作を開始しない

#### **E-4. `presentation/views/dialogs/settings_dialog.py` — 設定項目の追加（*E-1 に依存*）**

- [ ] 「保管先の暗号化状態」の再申告（3値コンボ）を追加し、変更を `config.storage_profiles` へ保存する
- [ ] 「資格情報の保存方式」（`keyring` / `session_only`）を追加する。検出済みバックエンド名を読み取り専用で表示する
- [ ] `keyring` を選べない環境（許可外バックエンド）では選択肢を無効化し、理由を表示する
- [ ] 「ストレージ適合性」の結果表示と「**再検査**」ボタンを追加する（D-17）
- [ ] Phase 3 の D-15（実際に効く項目だけを表示する）の方針を維持する

---

### **3.6 グループF: テスト・検証（*各グループと並行して作成*）**

#### **F-1. `tests/unit/test_capabilities.py`（新規）**

- [ ] 各能力の失敗を monkeypatch で個別に注入し、`CapabilityLevel` の分岐（`OK` / `DEGRADED` / `UNSUPPORTED`）を固定する
- [ ] `journal_mode_for` が `wal_supported` に応じて `WAL` / `DELETE` を返すことを検証する
- [ ] `network_drive=True` ではWALプローブが成功しても `journal_mode_for` が必ず `DELETE` を返すことを検証する
- [ ] 親プロセスが保持するロックを別プロセスが取得できないこと、および子プロセスが必ず終了・回収されることを検証する
- [ ] テスト実行後に `tmp/` へ残骸が残らないことを検証する（例外発生経路も含む）
- [ ] 本番の `.lock` / `metadata.db` / `eml/` / `manifests/` / `tmp/pstimp/` に触れないことを検証する（D-8）
- [ ] `StorageDetachedError` が握り潰されずに送出されることを検証する
- [ ] `StorageCapabilities.from_dict` が不正な辞書に対して `None` を返すことを検証する
- [ ] Qt を import しないこと（通常CIで実行できること）を確認する

#### **F-2. `tests/unit/test_config.py` の拡張**

- [ ] schema v1 → v2 のアップグレードが無損失であり、未知キーが保持されることを検証する
- [ ] schema v0 → v1 → v2 が順に適用され、v1 → v2 を飛ばさないことを検証する
- [ ] `storage_profiles` の構造・列挙値の検証と、不正値が `ConfigError` になることを検証する
- [ ] `credential_storage` の許可値外が `ConfigError` になることを検証する
- [ ] 新フィールドを含む `AppConfig` のラウンドトリップ（`save` → `load`）を検証する

#### **F-3. `tests/unit/test_connection.py` の拡張**

- [ ] `journal_mode` の許可リスト外が `DatabaseError` になることを検証する
- [ ] `WAL` / `DELETE` がそれぞれ実際にデータベースへ適用されることを検証する
- [ ] `readonly` 接続が `journal_mode` を変更しないことを検証する（既存挙動の維持）

#### **F-4. `tests/unit/test_keyring_store.py` の拡張と `tests/unit/test_session_store.py`（新規）**

- [ ] 許可外バックエンドが `UNSUPPORTED` として検出されることを検証する
- [ ] 許可外バックエンドで `set_password` が資格情報を保存せず `CredentialStoreError` になることを検証する
- [ ] `SessionCredentialStore` の保存・取得・削除の挙動を検証する
- [ ] `SessionCredentialStore` がファイルを一切作成しないこと、`__repr__` に値が現れないことを検証する
- [ ] プロセス再起動相当の新しい `SessionCredentialStore` には資格情報が無く、再入力後はそのインスタンスからだけ取得できることを検証する

#### **F-5. `tests/unit/test_main.py` の拡張**

- [ ] `UNSUPPORTED` かつ未承認では、測定結果が `config.json` へ保存された**後**に、`root_uuid` と文字列レベルだけを持つ `StorageUnsupportedError` が送出されることを検証する
- [ ] 承認済みなら続行できることを検証する
- [ ] UUID・正規化パス・ストレージ指紋が一致する有効なキャッシュではセルフテストを再実行せず、パスまたは指紋が変われば同じUUIDでも再実行することを検証する（D-17）
- [ ] `storage_root_candidates` に対応しない `root_uuid` エントリが掃除されることを検証する
- [ ] 許可外バックエンド検出時に `SessionCredentialStore` へフォールバックすることを検証する
- [ ] CLIと`AppContext`が `StorageSession` 所有の同じ資格情報ストアを使用し、独自の `KeyringCredentialStore` を生成しないことを検証する
- [ ] `StorageUnsupportedError` の終了コードが3であることを検証する

#### **F-6. GUIテストの拡張（`gui` マーカー）**

- [ ] `tests/gui/test_setup_wizard.py`: 注入コールバック経由で測定しViewがinfrastructureをimportしないこと、`UNSUPPORTED` で次へ進めないこと、`DEGRADED` では警告付きで進めること、暗号化申告が `config` へ保存されること、`未暗号化` 選択時に確認チェックが必須になること
- [ ] `tests/gui/test_main_window.py`: ステータスバーへ暗号化状態と適合性が表示されること、初回同期確認がワーカーへのジョブ投入前に1回だけ出て記録されること、`session_only` の資格情報再入力をキャンセルした場合は操作が開始されないこと
- [ ] `tests/gui/test_settings_dialog.py`: 再申告の保存、資格情報の保存方式の切り替え、`keyring` 不可環境での選択肢無効化、再検査ボタンの動作

---

## **4. 主要成果物**

| ファイル | 内容 | 対応タスク |
| :---- | :---- | :---- |
| `docs/ローカルメールバックアップand閲覧アプリ開発計画書.md` | 2.4 / 5.3 / 5.7 / 5.7.1 / 5.10 / 5.11 / 7章 / 8章の改訂 | A-1 〜 A-5 |
| `docs/実装計画書_Phase0_基盤整備.md` | 前提条件・D-9・Phase 4 申し送りの修正 | A-6 |
| `docs/実装計画書_Phase3_GUI基礎構築.md` | F-3・E-1 への追記 | A-6 |
| `README.md` | 「保管先の暗号化」セクション | A-7 |
| `src/mail_dock/domain/errors.py` | `StorageUnsupportedError` | B-1 |
| `src/mail_dock/infrastructure/storage/capabilities.py` | **新規**。セルフテストの中核 | B-2 |
| `src/mail_dock/config.py` | schema v2・`storage_profiles`・`credential_storage` | B-3 |
| `src/mail_dock/infrastructure/database/connection.py` | `network_drive` → `journal_mode` | B-4 |
| `src/mail_dock/infrastructure/security/keyring_store.py` | バックエンド許可リスト検査 | C-1 |
| `src/mail_dock/infrastructure/security/session_store.py` | **新規**。`SessionCredentialStore` | C-2 |
| `src/mail_dock/__main__.py` | `StorageSession` へのセルフテスト統合・資格情報ストア選択 | D-1 |
| `src/mail_dock/presentation/app.py` | `journal_mode` 差し替え・続行確認経路 | D-2 |
| `src/mail_dock/presentation/strings.py` | 新規文言 | E-1 |
| `src/mail_dock/presentation/views/setup_wizard.py` | セルフテスト結果表示・`UNSUPPORTED` 拒否・暗号化申告UI | E-2 |
| `src/mail_dock/presentation/views/main_window.py` | ステータスバー常時表示・初回同期前の1回確認 | E-3 |
| `src/mail_dock/presentation/views/dialogs/settings_dialog.py` | 再申告・資格情報の保存方式・再検査 | E-4 |
| `tests/unit/test_capabilities.py` | **新規**。セルフテストの単体テスト | F-1 |
| `tests/unit/test_session_store.py` | **新規**。メモリのみストアの単体テスト | F-4 |

---

## **5. スコープ境界**

### **5.1 含むもの**

セクション3のグループA〜F。「暗号化要件の3層モデルへの文書改訂 → ストレージ適合性セルフテストの実装 → `journal_mode` 決定経路の置き換え → 暗号化申告UIと常時表示 → keyring バックエンド検査と `session_only` → 設定ダイアログ拡張 → 設定スキーマ v2」の一式。

### **5.2 含まないもの（明示的に除外）**

| 除外項目 | 実施フェーズ |
| :---- | :---- |
| アプリ層暗号化（7z / AES-ZIP / AEAD による EML 暗号化） | **恒久的にスコープ外**（D-2） |
| DB / FTS の暗号化（SQLCipher 等） | **恒久的にスコープ外**（D-2） |
| 暗号化鍵・パスフレーズ・リカバリコードの管理 | **恒久的にスコープ外**（D-2） |
| BitLocker / VeraCrypt / LUKS のマウント状態の自動検出 | **恒久的にスコープ外**（D-4） |
| Cryptomator 等の製品名による検出 | **恒久的にスコープ外**（D-3 / D-20） |
| `DriveKind` への `VIRTUAL` の追加 | 実施しない（D-7。セルフテストで代替） |
| `db_backup_to_local_disk` の実処理（内蔵ディスクへのDB複製） | Phase 4（本フェーズは文言と `AppConfig` の定義のみ） |
| 切断の状態機械・`WM_DEVICECHANGE` 監視・ハートビート駆動 | Phase 4 |
| 整合性チェックの操作UI・修復・再インデックス | Phase 4 |
| 起動のたびにセルフテストを再実行する運用 | 実施しない（D-17。起動時間の維持のため） |
| macOS / Linux 向けのインストーラー配布 | **恒久的にスコープ外**（スクリプト実行のみ対象。PST機能も Windows 限定） |
| 新規依存パッケージの追加 | 実施しない（D-15） |

---

## **6. 検証**

各項目の完了を確認したうえで、対応するタスクのチェックボックスを埋めること。

- [ ] V-1. `uv sync` → `uv run ruff format --check .` → `uv run ruff check .` → `uv run mypy` がすべて成功する
- [ ] V-2. `uv run pytest -m "not docker and not gui"` が全緑になり、既存テストが回帰していない（特に `test_connection.py` / `test_main.py` / `test_config.py`）
- [ ] V-3. `uv run pytest tests/unit/test_capabilities.py -v` で、全能力の失敗注入ケースが期待どおり `OK` / `DEGRADED` / `UNSUPPORTED` へ分岐する
- [ ] V-4. セルフテストが Qt を import せず、`gui` マーカー外の通常CIで実行される（F-20）
- [ ] V-5. GUIテストがローカルで全緑になる（`gui` マーカーのオプトイン実行）
- [ ] V-6. 設定の後方互換: schema v1 の既存 `config.json` を持つ環境で起動し、v2 へ無損失アップグレードされ、未知キーが保持される。schema v0 は v0 → v1 → v2 を順に通る（N-4）
- [ ] V-7. 通常のローカルドライブでウィザードを一周し、セルフテストが `OK`、`journal_mode=WAL` が適用される。セルフテストの所要時間が1秒以内である（N-1）
- [ ] V-8. UUID・正規化パス・ストレージ指紋が一致する2回目以降の起動ではセルフテストが再実行されず、起動時間が3秒以内を維持する。同じUUIDでもパスまたは指紋が変われば再実行される（N-2 / D-17）
- [ ] V-9. ネットワークドライブ（SMB共有）をルートに指定すると、WALプローブの戻り値にかかわらず `journal_mode=DELETE` が適用される
- [ ] V-10. 非破壊性: セルフテストの実行前後で `.lock` / `metadata.db` / `eml/` / `manifests/` / `tmp/pstimp/` が変化せず、`tmp/` に残骸が残らない（N-3 / F-4）
- [ ] V-11. 暗号化申告: ウィザードで3値を申告でき、`未暗号化` では確認チェックが必須になり、結果が `config.json` の `storage_profiles` へ `root_uuid` 紐付けで保存される。ストレージルート配下には書き込まれない（F-11）
- [ ] V-12. 未暗号化での運用: 初回同期の開始直前に確認モーダルが**1回だけ**表示され、確認後は繰り返されず、ステータスバーに状態が常時表示される（F-14）
- [ ] V-13. CLI の安全性: `UNSUPPORTED` かつ未承認の状態では、測定結果が先に `config.json` へ保存された後、構造化 `StorageUnsupportedError` で終了コード3となる。CLIから承認を発行する手段が存在しない（F-8）
- [ ] V-14. 資格情報: 許可外の keyring バックエンドを模擬した状態で、資格情報が保存されず `session_only` へフォールバックし、警告が表示される。`SessionCredentialStore` がファイルを一切作成せず、GUI／CLIともネットワーク操作前に既存アカウントのパスワードを再入力できる（F-16 〜 F-18）
- [ ] V-15. 依存関係: `pyproject.toml` に新しい依存が追加されていない（F-22）
- [ ] V-16. ドキュメント: 開発計画書 5.3 / 2.4 / 5.7 / 5.7.1 / 5.10 / 5.11 / 7章 / 8章と README が改訂され、「BitLocker To Go を必須の前提条件とする」という記述がリポジトリ内に残っていない（F-1 / F-2 / F-19 / F-21）
- [ ] V-17. 実測（可能なら）: Cryptomator Vault をストレージルートに指定し、`OK` / `DEGRADED` / `UNSUPPORTED` の実測結果を本書「7.」へ記録する。`OK` でも安全性が保証されたとは扱わず、互換性プローブの検出限界として開発計画書 5.3 とUIへ反映する
- [ ] V-18. 実測（可能なら）: VeraCrypt の固定サイズファイルコンテナをストレージルートに指定し、セルフテストが `OK`、`journal_mode=WAL`、空き容量チェックが正しい値を返すことを確認する

---

## **7. Phase 4 への引き継ぎ事項**

> *実装中に判明した事項・実測値をここへ追記する。*

* **V-17 / V-18 の実測結果を必ず記録すること。** Cryptomator 上でセルフテストが `OK` を返す可能性は互換性プローブの既知の限界であり、それだけで安全性が保証されたとは扱わない。追加可能な測定項目（例: `os.replace` 後の即時 `stat` 整合性）を検討しつつ、検出不能な性質は開発計画書 2.4 とUIへ明記する。
* **`db_backup_to_local_disk` の実処理は Phase 4。** 本フェーズで一般化した条件（複製先の暗号化状態が保管元より弱い場合は警告し既定OFF）を実装すること。暗号化状態は自動検出できない（D-4）ため、複製先についてもユーザー申告として扱う設計にする必要がある。
* **切断状態機械（5.7.1）との統合。** Vault のアイドル自動ロックと VeraCrypt の自動アンマウントは、`_DETACH_WINERRORS` に含まれないエラーコードで観測される可能性がある。Phase 4 で状態機械を実装する際、`DEGRADED` / `UNSUPPORTED` と判定された保管先では I/O 例外を安全側（`StorageDetachedError`）へ倒す方針を検討すること。
* **整合性チェック（Phase 4）との関係。** 適合性が `DEGRADED` の保管先では、fsync の保証が弱いことを前提に、起動時のクイック検証の範囲を広げるべきかを判断すること。
* **設定ダイアログの構造。** Phase 4 / 4.5 の項目（purge モード・ゴミ箱猶予日数・ハートビート間隔・サーバー削除モード・PST取込設定）を追加できる構造を維持すること（Phase 3 D-15）。
* **`storage_profiles` の肥大化。** ルートを頻繁に付け替えるユーザーではエントリが増える。`_save_settings` の掃除ロジックはUUIDだけでなく候補パスとの対応を使うため、Phase 4で複数ルート候補を扱う場合は掃除条件を見直すこと。
* **PST取込（Phase 4.5）との関係。** `readpst` は外部プロセスとして `tmp/pstimp/` へ平文EMLを出力する。ブロックレベル暗号化なら問題にならないが、この事実は「アプリ層暗号化を採用した場合には塞げない穴」として D-2 の根拠のひとつであることを記録として残す。
