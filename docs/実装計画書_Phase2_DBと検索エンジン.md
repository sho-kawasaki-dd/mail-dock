# **Phase 2: DB & 検索エンジン 実装計画書**

対象: [ローカルメールバックアップ＆閲覧アプリ 開発計画書.md](./ローカルメールバックアップand閲覧アプリ開発計画書.md) の「6. 開発ロードマップ」における **Phase 2: DB & 検索エンジン**

前提: [Phase 1: 抽象化層 & IMAPコア 実装計画書](./実装計画書_Phase1_抽象化層とIMAPコア.md) の成果物（`BaseMailFetcher`・EML解析・原子的保存・永続マニフェスト・`SqliteMessageRepository`・二カーソル同期・`normalize_for_search()`）が完成していること。

本書と開発計画書に矛盾がある場合は、**開発計画書を正**とする。

---

## **1. 目的**

**ヘッドレス（UIを持たない）状態で、Phase 1 が蓄積したメールを高速に検索・一覧できる読み取り基盤を完成させる。**

具体的には、以下を満たす。

1. **FTS5 + trigram の実測に基づいて設計を確定すること** — 開発計画書 6章が Phase 2 冒頭に指定する PoC を最初に実施し、インデックスサイズ・検索速度・2文字検索の挙動を計測してから作り込みに入る。仮定のまま実装を進めない。
2. **正規化の一貫性をコード上で保証すること** — `normalize_for_search()` を投入時と検索時の**唯一の経路**とする。片方だけ変更すると恒久的にヒットしなくなるため、これをテストで固定する。
3. **Phase 3 の UI が SQL 側だけで駆動できる読み取り API を確定させること** — 200件単位の遅延ロード（`canFetchMore` / `fetchMore`）、構造化フィルタ、スレッド絞り込み、件数取得を、Qt に依存しない形で提供する。
4. **短いキーワードの経路分岐を明示的に実装すること** — trigram は3文字未満をインデックス化せず、`MATCH` に渡しても自動フォールバックせず単に0件になる。アプリ側での分岐を必須とする。
5. **読み取り関心を書き込み関心から分離すること** — `BaseMessageRepository`（同期ユースケースの単体テスト差し替え用）に検索メソッドを足さず、読み取り専用ポートを新設する。

**Phase 2 のゴール判定:** PoC の計測結果が本書へ記録され、`mail-dock search` が Phase 1 で同期した実データに対して正しくヒットし、6章の検証項目がすべて成功し、CIが緑になること。

---

## **2. 要件**

### **2.1 前提となる意思決定（確定済み）**

| # | 項目 | 決定内容 |
| :--- | :---- | :---- |
| D-1 | 検索ポートの置き場所 | **`domain/search.py` に `BaseSearchRepository` を新設**する（読み取り専用）。`BaseMessageRepository` は変更しない。Phase 1 で定めた「目的を超えたメソッドを足さない」制約を維持する |
| D-2 | クエリ構文の範囲 | **スペース分割 + AND/OR モード + フレーズ `"..."` + 除外 `-kw`** まで。フィールド指定（`from:` / `subject:` / `has:attachment`）は採用しない（構造化フィルタで代替する） |
| D-3 | ソートとページング | **`date_sent` 降順固定 + keyset（seek）ページング**。`bm25()` による関連度順は採用しない（trigram のスコアは日本語で直感に合わないため） |
| D-4 | スニペット・ハイライト | **Phase 2 では実装しない**。検索結果は `messages` 側の表示用原文だけを返す。必要性は Phase 3 の UI 設計時に判断する |
| D-5 | PoC の計測データ | **合成EML 1万通**を生成して計測する。スクリプトは `tools/bench_fts.py` に置き、**通常の `pytest` では実行しない**（手動実行） |
| D-6 | PoC が目標未達の場合 | 代替案（`unicode61` + アプリ側 2-gram 分割 等）を**同一スクリプトで比較**し、必要なら FTS 再構築マイグレーションを作る。`trigram` の `detail=column` / `detail=none` は性能比較だけを行い、3文字超の検索とフレーズ検索を満たせないため**採用不可**とする。判断は A-5 で行う |
| D-7 | 検索なしの一覧クエリ | フォルダ内一覧・スレッド一覧・件数取得・1件詳細取得も **Phase 2 に含める**（検索と同じフィルタ／ページング基盤の上に実装する） |
| D-8 | 検証導線 | **CLI に `search` サブコマンドを追加**する（Phase 1 と同じヘッドレス方針）。GUI は Phase 3 |
| D-9 | 既定の状態フィルタ | 既定は **`local_state='active'` のみ**。`remote_state` は問わない（`deleted` / `moved` もヒットする）。呼び出し側がフィルタで明示的に広げられる |
| D-10 | 既定のスコープ | **全アカウント横断**。`account_id` は任意フィルタとする（開発計画書 4.6-1 の「一覧にアカウント列を表示し、横断表示時も出所が分かるようにする」に合わせる） |
| D-11 | 長時間クエリの中断 | LIKE スキャン経路は **`sqlite3.set_progress_handler`** で `CancelToken` を監視し、途中で中断できるようにする |
| D-12 | スキーマ変更 | `001_init.sql` / `002_sync_cursor.sql` は変更しない。**A-5 の計測で索引が必要と判明した場合にだけ** `003_search_index.sql` を作る。不要と判明したら作らず、本書「7. Phase 3 への引き継ぎ事項」へその旨を記録する |
| D-13 | FTS 保守 | Phase 2 は **`integrity-check` による検査のみ**。external-content との乖離も検出するため `rank=1` を指定する。このコマンドは SQL 上 `INSERT` なので、通常検査の読み取り専用接続とは別に、検査時だけ書き込み可能な専用接続を開く。原本データの変更、`rebuild` / `optimize` の運用導線、再インデックスは Phase 4 |
| D-14 | 合成コーパス生成 | `tests/support/eml_builder.py` を再利用する（重複実装を作らない）。`tools/` から `tests/support` を import する形とし、逆方向の依存は作らない |
| D-15 | 正規化関数の配置 | `normalize_for_search()` は外部依存のない純粋関数として **`domain/normalize.py` へ移す**。投入側と検索側はこの同一関数を参照し、usecases → infrastructure の逆依存を作らない |

### **2.2 機能要件**

| # | 要件 | 根拠（開発計画書） |
| :--- | :---- | :---- |
| F-1 | Phase 2 冒頭で FTS5 + trigram の実測 PoC を行い、**インデックスサイズ倍率・検索応答時間・2文字検索の挙動**を計測してから設計を確定すること | 6章 |
| F-2 | `domain/search.py` に読み取り専用ポート `BaseSearchRepository` と、`MessageFilter` / `PageCursor` / `MessageSummary` / `SearchPage` / `SearchPlan` が定義され、外部依存がゼロであること | 2.2 / D-1 |
| F-3 | ユーザー入力が**全角／半角スペース**で分割され、各キーワードに `domain.normalize.normalize_for_search()` が適用されること。投入時と検索時がこの**同一関数**を参照すること | 4.5 / D-15 |
| F-4 | 正規化後の長さが **3文字以上**のキーワードは FTS5 `MATCH` 経路、**2文字以下**は `message_contents` に対する `LIKE '%kw%' ESCAPE '\'` 経路へ**明示的に分岐**すること。`MATCH` への自動フォールバックに頼らないこと | 4.5 |
| F-5 | フレーズは `"` で囲み、内部の `"` は `""` にエスケープして FTS5 の Parse Error を防ぐこと。LIKE 経路では `\` / `%` / `_` をエスケープすること | 4.5 |
| F-6 | AND 検索は結果ID集合を `INTERSECT`、OR 検索は `UNION`、除外（`-kw`）は `EXCEPT` で合成し、その後に構造化フィルタで絞り込むこと | 4.5 |
| F-7 | 構造化フィルタとして **アカウント／フォルダ／日付範囲／添付有無／状態** を指定でき、キーワード条件と AND 結合されること | 4.5 |
| F-8 | 検索対象が **件名・差出人・本文・添付ファイル名** の4列であること。添付は**ファイル名のみ**を対象とし、中身は抽出しないこと | 4.5 |
| F-9 | 結果が `date_sent` 降順で返り、**keyset（seek）ページング**により200件単位の継続取得ができること。ページ境界で重複・欠損が発生しないこと | 4.6-1 / D-3 |
| F-10 | `date_sent` が NULL の行（`Date` 欠損）もソート・ページングから脱落しないこと。ソートキーは `COALESCE(date_sent, internal_date, '')` とし、継続条件は行値比較 `(sort_key, id) < (?, ?)` で表現すること | 4.7 / D-3 |
| F-11 | 既定の状態フィルタが `local_state='active'` のみであり、`remote_state` を問わないこと。`purged` の墓標レコードが既定で結果に混ざらないこと | 4.4 / D-9 |
| F-12 | 検索なしの一覧（フォルダ内一覧）、スレッド一覧（`thread_key` 絞り込み）、件数取得、1件詳細取得が、検索と同一のフィルタ／ページング基盤の上で提供されること | 4.5 / 4.6-1 / D-7 |
| F-13 | 2文字以下のキーワードを含む場合に `has_slow_path` が立ち、呼び出し側が「短い語を含むため時間がかかる場合があります」を提示できること | 4.5 |
| F-14 | LIKE スキャン経路が `CancelToken` により**クエリ実行中に**中断できること。progress handler は `finally` で必ず解除され、キャンセル済みの `SQLITE_INTERRUPT` は一般的なDB例外分類より先に `OperationCancelledError` へ変換されること | 5.4 / D-11 |
| F-15 | `message_contents` の INSERT / UPDATE / DELETE に対して `messages_fts` がトリガー経由で追随すること。`local_state='purged'` 化で `message_contents` を削除すると FTS からも消えること | 3.4 / 4.4 |
| F-16 | 検索ユースケースが `sqlite3` を import せず、`BaseSearchRepository` のみに依存すること | 2.2 / 5章 |
| F-17 | `sqlite3.Error` が `detach.classify_sqlite_error()` 経由でドメイン例外へラップされ、上位層へ生の例外を漏らさないこと | 5.7 |
| F-18 | 不正なクエリ（空文字・除外のみ・未閉鎖の引用符・空フレーズ・単独 `-`・正規化後に空になる語）が `SearchQueryError` として拒否され、SQL エラーとして表面化しないこと | — |
| F-19 | CLI `search` で Phase 1 の実データに対する検索が実行でき、結果と `next_cursor` を確認できること | D-8 |
| F-20 | `verify` サブコマンドが専用の書き込み可能接続で `INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)` を実行し、`message_contents` と `messages_fts` の乖離を検出できること。通常の `quick_check` / `foreign_key_check` は読み取り専用接続を維持すること | 4.8 / D-13 |

### **2.3 非機能要件・制約**

| # | 指標 | 目標値 | 備考 |
| :--- | :---- | :---- | :---- |
| N-1 | 検索応答（3文字以上） | **300ms 以内** | 5万通・trigram インデックス使用時（開発計画書 5.1） |
| N-2 | 検索応答（2文字以下） | **3秒以内** | LIKE スキャン経路。呼び出し側に警告フラグを返す（同 5.1） |
| N-3 | FTS インデックスサイズ | **本文テキストの3〜5倍以内** | 5万通で約4GB（同 5.2） |

* Phase 2 のコードは **PySide6 に依存しない**（`presentation` 層を作らない）。
* `mypy strict` を通すこと。`# type: ignore` を使う場合は理由をコメントで併記する。
* レイヤーの依存方向を厳守する（`domain` ← `usecases`、`infrastructure` は `domain` にのみ依存）。`domain` に `sqlite3` を import しない。
* `BaseSearchRepository` は**読み取り専用**とする。書き込みメソッドを足さない。`BaseMessageRepository` と統合しない。
* 検索リポジトリは**トランザクションを開かない**（読み取りのみ）。同期中の書き込みをブロックしない。
* `verify` の FTS `integrity-check` は検索リポジトリとは別の保守処理である。StorageLock 保持中に専用の書き込み可能接続を短時間だけ開き、マイグレーションやデータ更新は行わず、検査後すぐ閉じる。
* 検索クエリ文字列・件名をログへ出力する場合は Phase 0 の `MaskingFilter` を通す。本文テキストはログに出力しない。
* SQL は必ずプレースホルダで組み立てる。ユーザー入力を SQL 文字列へ連結しない（キーワード数に応じた `?` の展開は可）。

---

## **3. タスク**

> 依存関係: **A → (B と並行) → C → D → E**。A-5 の判定が済むまで D の SQL 設計を確定させない。F（テスト）は各グループと並行して作成する。

### **3.1 グループA: FTS5 + trigram 実測PoC（*最優先。本フェーズのブロッカー*）**

#### **A-1. 合成コーパス生成**

- [x] `tools/bench_fts.py` を新規作成する（`tools/` は Git 管理下、生成データは管理外）
- [x] `tests/support/eml_builder.py` を再利用してEMLを組み立てる（D-14。重複実装を作らない）
- [x] 日本語本文で現実的な文長分布を再現する（中央値 数KB、裾に数十〜数百KBの長文を混ぜる）
- [x] 件名・差出人（表示名＋アドレス）・添付ファイル名も生成し、`message_contents` の4列すべてに投入する
- [x] 投入は `normalize_for_search()` を通す（**本番と同一経路であることを保証する**）
- [x] **1千 / 5千 / 1万通**の3水準を生成し、線形性を確認できるようにする
- [x] 生成物（DB・EML）を `.gitignore` へ追加する

#### **A-2. 実行環境の前提確認**

- [x] `sqlite3.sqlite_version` が trigram トークナイザ対応（**3.34.0 以上**）であることを確認する
- [x] **Windows と Linux(WSL) の双方**で確認し、バージョン差を記録する
- [x] 未対応環境が存在する場合の扱い（起動時チェックの要否）を判断し、結論を A-5 に記録する

#### **A-3. 計測（*A-1 / A-2 完了後*）**

- [ ] `message_contents` の実サイズに対する `messages_fts` のサイズ倍率を測る（目標 3〜5倍以内）
- [ ] `MATCH` 経路の応答時間 p50 / p95 を測る
  - [ ] キーワード長: 3文字 / 5文字 / 10文字
  - [ ] 語数と結合: 1語 / 2語AND（`INTERSECT`）/ 2語OR（`UNION`）/ 除外あり（`EXCEPT`）
  - [ ] ヒット件数が多いケース（数千件）と少ないケース（数件）の両方
- [ ] 2文字キーワードの `LIKE` 全表スキャン時間を測る（目標 3秒以内 @5万通換算）
- [ ] `message_contents` への INSERT スループットを、FTS トリガー有り／無しで比較する（同期時のオーバーヘッド確認）
- [ ] **ソートとページングのコスト**を測る
  - [ ] `ORDER BY COALESCE(date_sent, internal_date, '') DESC, id DESC LIMIT 200` の応答時間
  - [ ] 深いページ（1万件目以降）での keyset 継続の応答時間
  - [ ] 式インデックスの有無で差が出るかを比較する（**`003` の要否判断に直結**）
- [ ] 構造化フィルタ（アカウント・フォルダ・日付範囲・`local_state`）を併用した場合の応答時間を測る
- [ ] 1千 / 5千 / 1万の3点から**5万通への外挿**を行い、N-1 / N-2 / N-3 の達成見込みを判定する

#### **A-4. 挙動確認（*A-1 完了後。A-3 と並行可*）**

- [ ] 2文字キーワードを `MATCH` に渡すと **0件になる**（エラーにもフォールバックにもならない）ことを確認する
- [ ] FTS5 の構文エラーを誘発する入力を洗い出す（`"` / `*` / `^` / `-` / `(` / `)` / `:` / `NEAR` / `AND` / `OR` / `NOT`）
- [ ] 全キーワードを `"` で囲むエスケープにより、上記すべてが Parse Error にならないことを確認する
- [ ] trigram インデックスが `messages_fts` に対する `LIKE` を高速化するか確認し、2文字経路に流用できるかを判断する
- [ ] `detail=` オプション（`full` / `column` / `none`）がインデックスサイズと検索速度に与える影響を測る
  - [ ] `detail=column` / `detail=none` は性能比較だけに使用し、3文字超の検索とフレーズ検索を満たせないため採用候補にしない

#### **A-5. 判定と設計確定（*A-3 / A-4 完了後。以降のグループの前提*）**

- [ ] N-1 / N-2 / N-3 の達成可否を判定する
- [ ] 未達の場合は代替案を**同一スクリプトで比較**する
  - [ ] `unicode61` + アプリ側での 2-gram 分割（投入時・検索時の両方で分割する）
  - [ ] `trigram` の `detail=column` / `detail=none` は参考値として比較するが採用しない
- [ ] 採用するトークナイザ構成を確定する
- [ ] `trigram` を採用する場合は、3文字超の検索とフレーズ検索を維持するため `detail=full` とする
- [ ] **`003_search_index.sql` の要否と内容を確定する**（式インデックス／フィルタ用索引／FTS 再構築）
- [ ] 索引が不要と判明した場合は `003` を作らず、その旨を本書「7. Phase 3 への引き継ぎ事項」へ記録する（D-12）
- [ ] 計測結果（環境・件数・実測値・外挿値）を本書「7.」へ表形式で記録する

---

### **3.2 グループB: domain 層（*A と並行可*）**

#### **B-0. `domain/normalize.py` — 検索正規化の純粋関数**

- [ ] `infrastructure/parsing/normalize.py` の `normalize_for_search()` を `domain/normalize.py` へ移す
- [ ] 標準ライブラリ以外への依存を持たせない
- [ ] `SqliteMessageRepository` と `usecases/search_query.py` の双方が `domain.normalize` から同じ関数を import する
- [ ] 旧モジュールを残して正規化経路を二重化しない

#### **B-1. `domain/errors.py` — 例外の追加**

- [ ] `MailDockError` 配下に `SearchQueryError` を追加する（クエリ構文が不正で実行できない場合）
- [ ] 「これは利用者の入力誤りであり、システム障害ではない」旨を docstring に明記する
- [ ] 外部依存がゼロであることを維持する

#### **B-2. `domain/search.py` — 検索モデルと読み取り専用ポート**

- [ ] `MessageFilter`（frozen dataclass）を定義する
  - [ ] `account_ids: tuple[str, ...] | None`（None は全アカウント横断＝既定。D-10）
  - [ ] `folder_ids: tuple[int, ...] | None`
  - [ ] `date_from: datetime | None` / `date_to: datetime | None`
  - [ ] `has_attachment: bool | None`
  - [ ] `local_states: frozenset[str]`（既定 `{"active"}`。D-9）
  - [ ] `remote_states: frozenset[str] | None`（既定 None＝問わない。D-9）
  - [ ] `thread_key: str | None`
- [ ] `PageCursor`（frozen dataclass）を定義する: `sort_key: str` / `message_id: int`
  - [ ] 文字列化・復元（CLI の `--after` 用）を純粋関数として持たせる
- [ ] `MessageSummary`（frozen dataclass）を定義する: `id` / `account_id` / `folder_id` / `folder_raw_name` / `folder_display_name` / `subject` / `sender` / `date_sent` / `internal_date` / `size_bytes` / `has_attachment` / `remote_state` / `local_state` / `thread_key`
  - [ ] Phase 3 の一覧表示に必要な列だけを持たせる（本文は含めない）
  - [ ] `folders` を JOIN してフォルダの生名と表示名を取得し、CLI と Phase 3 が追加問い合わせなしで出所を表示できるようにする
- [ ] `MessageDetail`（frozen dataclass）を定義する: `MessageSummary` の項目に `recipient` / `cc` / `message_id` / `in_reply_to` / `references_ids` / `relative_path` / `file_hash` / `imap_flags` を加える
- [ ] `SearchPage`（frozen dataclass）を定義する: `items: tuple[MessageSummary, ...]` / `next_cursor: PageCursor | None` / `exhausted: bool`
- [ ] `SearchPlan`（frozen dataclass）を定義する: `match_terms` / `like_terms` / `exclude_match_terms` / `exclude_like_terms` / `mode: Literal["and", "or"]` / `has_slow_path: bool`
  - [ ] 各 term は**正規化済み**であることを docstring に明記する
- [ ] `BaseSearchRepository(ABC)` を定義する
  - [ ] `search_messages(plan, filters, *, cursor=None, limit=200, cancel=None) -> SearchPage`
  - [ ] `list_messages(filters, *, cursor=None, limit=200) -> SearchPage`
  - [ ] `count_messages(filters, plan=None, *, cancel=None) -> int`
  - [ ] `list_thread(thread_key, filters) -> Sequence[MessageSummary]`
  - [ ] `get_message(message_id) -> MessageDetail | None`
- [ ] モジュール docstring に「**読み取り専用。書き込みは `BaseMessageRepository`。両者を統合しない**（関心とライフサイクルが異なるため）」と明記する
- [ ] 外部依存が標準ライブラリと `domain` のデータ構造だけであることを維持する

---

### **3.3 グループC: usecases（*B 完了後*）**

#### **C-1. `usecases/search_query.py` — クエリ文字列パーサ（*純粋関数*）**

- [ ] `parse_query(text: str, *, mode: Literal["and", "or"] = "and") -> SearchPlan` を実装する
- [ ] **全角スペース（U+3000）と半角スペース**の両方で分割する
- [ ] `"..."` で囲まれた部分を1つのフレーズトークンとして扱う（内部のスペースで分割しない）
- [ ] 先頭 `-` のトークンを除外条件として扱う（`-"..."` にも対応）
- [ ] 各トークンに `normalize_for_search()` を適用する（**F-3。投入時と同一関数を必ず使う**）
- [ ] 正規化後の長さで経路を振り分ける: **3文字以上 → `match_terms`**、**2文字以下 → `like_terms`**
- [ ] 長さ判定は `len(str)` で行う（trigram の単位に合わせる）ことをコメントで残す
- [ ] FTS5 用エスケープ: 全トークンを `"` で囲み、内部の `"` を `""` に置換する（F-5）
- [ ] LIKE 用エスケープ: `\` → `\\`、`%` → `\%`、`_` → `\_` の順で置換する（**`\` を最初に処理する**）
- [ ] `like_terms` または `exclude_like_terms` が非空なら `has_slow_path=True` とする（F-13）
- [ ] 空文字・空白のみ・除外トークンのみ・未閉鎖の引用符・空フレーズ `""`・単独 `-`・正規化後に空になる語は `SearchQueryError` を送出する（F-18）
- [ ] 外部依存を持たず、`sqlite3` を import しない
- [ ] `normalize_for_search()` は `domain.normalize` から import し、infrastructure を import しない

#### **C-2. `usecases/search_messages.py` — 検索・一覧ユースケース**

- [ ] `search_messages(search_repo, *, query, mode="and", filters=None, cursor=None, limit=200, cancel=None) -> SearchPage` を実装する
- [ ] `list_messages(search_repo, *, filters=None, cursor=None, limit=200) -> SearchPage` を実装する（F-12）
- [ ] `list_thread(search_repo, *, thread_key, filters=None) -> Sequence[MessageSummary]` を実装する（F-12）
- [ ] `count_messages(search_repo, *, query=None, mode="and", filters=None, cancel=None) -> int` を実装する（F-12）
- [ ] `get_message(search_repo, *, message_id) -> MessageDetail | None` を実装する（F-12）
- [ ] `filters=None` のとき `MessageFilter()` の既定値（`local_state='active'` のみ・全アカウント横断）を使う（D-9 / D-10）
- [ ] Phase 1 と同じ呼び出し規約に従う（ポートを位置引数、以降は keyword-only）
- [ ] `sqlite3` / `keyring` / infrastructure の具象クラスを import しない（F-16）

---

### **3.4 グループD: infrastructure（*A-5 と B 完了後*）**

#### **D-1. `infrastructure/database/search_repository.py` — `SqliteSearchRepository`**

- [ ] `SqliteSearchRepository(BaseSearchRepository)` を実装する（`sqlite3.Connection` / `ConnectionManager` の両方を受け付ける。Phase 1 の `SqliteMessageRepository` と同じ構成）
- [ ] **MATCH 経路**: 語ごとに `SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?` を発行する
  - [ ] AND は `INTERSECT`、OR は `UNION`、除外は `EXCEPT` で合成する（F-6）
  - [ ] 列を限定せず4列すべてを対象とする（F-8。フィールド指定を採らないため）
- [ ] **LIKE 経路**: `SELECT message_id FROM message_contents WHERE subject_norm LIKE ? ESCAPE '\' OR sender_norm LIKE ? ESCAPE '\' OR body_text LIKE ? ESCAPE '\' OR attachment_names LIKE ? ESCAPE '\'`
- [ ] 両経路の結果を `mode` に従って合成し、`messages` と JOIN する
- [ ] 構造化フィルタを `WHERE` で適用する（アカウント・フォルダ・日付範囲・添付有無・`local_state` / `remote_state`）（F-7）
- [ ] `folders` を JOIN し、`folder_raw_name` / `folder_display_name` を `MessageSummary` に格納する
- [ ] **ソートとページング**（F-9 / F-10）
  - [ ] ソートキーを `COALESCE(date_sent, internal_date, '')` とし、`ORDER BY sort_key DESC, id DESC` で全順序を確定させる
  - [ ] keyset の継続条件を行値比較 `(sort_key, id) < (?, ?)` で表現する
  - [ ] `limit + 1` 件を取得し、余剰行は次ページの存在判定にだけ使って返却対象から除外する
  - [ ] `next_cursor` は**今回返した最後の行**の `(sort_key, id)` から組み立てる（余剰行から作ると次回の `<` 条件で1件欠落するため）
  - [ ] `date_sent` が NULL の行が脱落しないことをコメントで明記する
- [ ] **キャンセル**（F-14 / D-11）
  - [ ] `set_progress_handler` を設定し、`CancelToken` が立っていたら中断させる
  - [ ] `finally` で必ずハンドラを解除する（他のクエリに影響を残さない）
  - [ ] `sqlite3.Error` の一般分類より先に、キャンセル済みトークンに伴う `SQLITE_INTERRUPT` を `OperationCancelledError` へ変換する
- [ ] Phase 1 の `_db_io()` パターンで `sqlite3.Error` を `classify_sqlite_error()` 経由でドメイン例外へラップする（F-17）
- [ ] **トランザクションを開かない**（読み取り専用。同期中の書き込みをブロックしない）ことをモジュール docstring に明記する
- [ ] SQL をプレースホルダで組み立てる（キーワード数に応じた `?` の展開のみ許可し、値を文字列連結しない）
- [ ] 接続をスレッド間で共有しない（`ConnectionManager` を使う）

#### **D-2. `migrations/003_search_index.sql`（*A-5 の判定で必要と決まった場合のみ*）**

- [ ] 横断ソート用の式インデックスを追加する（例: `messages(COALESCE(date_sent, internal_date, '') DESC, id DESC)`）
- [ ] 構造化フィルタ用のインデックスを追加する（A-3 の計測で必要と判明したものだけ）
- [ ] トークナイザ変更が必要になった場合のみ、`messages_fts` を再作成し `INSERT INTO messages_fts(messages_fts) VALUES('rebuild')` で再構築する
- [ ] `001_init.sql` / `002_sync_cursor.sql` を変更しない（D-12）
- [ ] 索引が不要と判明した場合は**本ファイルを作らず**、その判断を本書「7.」へ記録する

#### **D-3. `infrastructure/database/fts_maintenance.py` — FTS 整合性検査**

- [ ] `integrity_check(conn) -> None` を実装する（`INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)`）
- [ ] external-content テーブルでは `rank=1` が `message_contents` との照合に必須であることをテストとdocstringで固定する
- [ ] 失敗時は `DatabaseError` へラップする
- [ ] `rebuild` / `optimize` の**運用導線は Phase 4** である旨をモジュール docstring に明記する（D-13）
- [ ] 検査コマンドは SQL 上 `INSERT` だが、原本データを変更する保守処理ではないことを明記する

---

### **3.5 グループE: CLI（*C / D 完了後*）**

#### **E-1. `__main__.py` — `search` サブコマンド**

- [ ] `mail-dock search QUERY` を追加する
  - [ ] `--account ACCOUNT_ID`（複数指定可）/ `--folder RAW_NAME`（複数指定可）
  - [ ] `--since YYYY-MM-DD` / `--until YYYY-MM-DD`
  - [ ] `--has-attachment` / `--no-attachment`
  - [ ] `--mode and|or`（既定 `and`）
  - [ ] `--limit N`（既定 50）/ `--after CURSOR`（前回出力の `next_cursor` を渡して継続）
  - [ ] `--json`（機械可読出力）
- [ ] 結果を「日付 / アカウント / フォルダ / 差出人 / 件名 / サイズ」で整形表示し、末尾に `next_cursor` を出す
  - [ ] フォルダ欄には `folder_display_name` を表示し、`--json` には `folder_raw_name` / `folder_display_name` の両方を含める
- [ ] `has_slow_path` が立っている場合に「短い語を含むため時間がかかる場合があります」を表示する（F-13）
- [ ] `SIGINT` ハンドラで `CancelToken` をセットする（2回目の Ctrl+C で即時終了。Phase 1 と同じ）
- [ ] `SqliteSearchRepository` をコンポジションルートで注入する（Phase 1 の `_run_application_command` と同じ形）
- [ ] `_exit_code()` に `SearchQueryError` → **7** を追加する
- [ ] `--help` の出力が日本語環境で崩れないことを確認する

#### **E-2. `verify` の拡張**

- [ ] 既存の `PRAGMA quick_check` / `foreign_key_check` に加えて FTS の `integrity-check` を実行する（F-20）
- [ ] `quick_check` / `foreign_key_check` は従来どおり読み取り専用接続で実行する
- [ ] 通常検査の接続を閉じた後、StorageLock を保持したまま FTS 検査専用の書き込み可能接続を開く
- [ ] 専用接続ではマイグレーションを実行せず、`integrity_check()` だけを実行して直ちに閉じる
- [ ] FTS検査専用接続の開始・終了と、例外発生時にも接続が閉じられることをテストする

---

### **3.6 グループF: テスト（*各グループと並行して作成*）**

#### **F-1. 単体テスト（Docker不要）**

- [ ] `test_search_query.py`
  - [ ] 全角スペース／半角スペース／連続スペースでの分割
  - [ ] 正規化が適用されること（全角英数の半角化、大文字小文字、連続空白圧縮）
  - [ ] **3文字境界**: 正規化後3文字は MATCH、2文字は LIKE へ振り分けられること
  - [ ] 正規化によって長さが変わるケース（全角3文字→半角3文字、`Ｔ Ｅ Ｓ Ｔ` のような空白混じり）
  - [ ] フレーズ `"..."` が分割されないこと、内部の `"` が `""` にエスケープされること
  - [ ] 除外 `-kw` / `-"..."` が exclude 側へ振り分けられること
  - [ ] LIKE エスケープの順序（`\` を最初に処理すること）
  - [ ] FTS5 の特殊文字（`*` `^` `:` `NEAR` `AND` `OR` `NOT`）が Parse Error にならないこと
  - [ ] 空クエリ・空白のみ・除外のみ・未閉鎖の引用符・空フレーズ・単独 `-`・正規化後に空になる語で `SearchQueryError` になること
  - [ ] `has_slow_path` の立ち方
- [ ] `test_search_repository.py`（実SQLite・小規模データ）
  - [ ] MATCH 経路のヒット、LIKE 経路のヒット、両経路混在
  - [ ] AND / OR / 除外の合成結果
  - [ ] 構造化フィルタ（アカウント・フォルダ・日付範囲・添付有無）
  - [ ] **既定で `local_state='purged'` / `'trashed'` がヒットしないこと**、`remote_state='deleted'` / `'moved'` はヒットすること（F-11）
  - [ ] 全アカウント横断が既定であること（D-10）
  - [ ] **keyset ページング**: 同一 `date_sent` が複数ある場合の tie-break、`date_sent` NULL 混在、全ページ連結が一括取得と完全一致すること
  - [ ] `limit + 1` 件取得時に `next_cursor` が返却した最後の行を指し、余剰行が次ページの先頭として欠落しないこと
  - [ ] `folder_raw_name` / `folder_display_name` が正しいフォルダから取得されること
  - [ ] `list_messages` / `list_thread` / `count_messages` / `get_message`
  - [ ] `CancelToken` によるクエリ実行中の中断と、progress handler が解除されること
  - [ ] キャンセル済みの `SQLITE_INTERRUPT` が `DatabaseError` ではなく `OperationCancelledError` になること
  - [ ] SQL インジェクション耐性（`'; DROP TABLE messages; --` 等をクエリに渡しても実害がないこと）
- [ ] `test_search_messages.py`（Fake ポートのみ）: usecase が `BaseSearchRepository` だけで動作し、既定フィルタが適用されること
- [ ] `test_fts_triggers.py`（既存を拡張）: `message_contents` の UPDATE / DELETE に FTS が追随すること、`purged` 化で FTS から消えること（F-15）
- [ ] `test_003_search_index.py`（`003` を作る場合のみ）: v2 の実データを保持して適用され、`integrity-check` が通ること
- [ ] `test_ports.py`（既存を拡張）: `usecases/` 配下に `sqlite3` と infrastructure の import が無いこと、投入側と検索側が `domain.normalize` を参照することを静的に確認する（F-3 / F-16）
- [ ] `test_fts_maintenance.py`: 正常DBで `rank=1` の `integrity-check` が通り、external-content と意図的に乖離させたFTSで `DatabaseError` になること
- [ ] `test_main.py`（既存CLIテストを拡張）: 通常検査は読み取り専用接続、FTS検査は専用の書き込み可能接続で実行され、成功時・失敗時とも両接続が閉じられること

#### **F-2. 結合テスト（`docker` マーカー / WSL）**

- [ ] `test_search_flow.py`: 実ストレージルート + 実SQLite で同期 → 検索の end-to-end
  - [ ] 日本語本文がヒットすること
  - [ ] **ひらがなとカタカナを同一視しない**こと（開発計画書 4.5 の明示要件）
  - [ ] 全角英数で入力しても半角の本文にヒットすること（NFKC）
  - [ ] 大文字小文字を区別しないこと（casefold）
  - [ ] CP932 機種依存文字を含む本文がヒットすること
  - [ ] 添付ファイル名で検索できること、**インライン画像の名前ではヒットしない**こと
  - [ ] 2文字キーワードが LIKE 経路で発見できること
- [ ] **正規化一貫性の回帰テスト**: 投入側（`SqliteMessageRepository._normalized_contents`）と検索側（`parse_query`）が同一関数を経由していることを、片方だけを差し替えると落ちる形で固定する（F-3）

#### **F-3. ベンチマーク（手動実行）**

- [ ] `tools/bench_fts.py` を単体で実行でき、A-3 の全項目を再計測できること
- [ ] 通常の `pytest` 実行に含まれないこと（D-5）
- [ ] 実行方法と計測結果の読み方を `README.md` に追記する

#### **F-4. CI**

- [ ] 新規テストが `lint` / `test-windows` / `test-linux` の3ジョブで緑になることを確認する
- [ ] ベンチマークが CI で実行されないことを確認する

---

## **4. 主要成果物**

| パス | 内容 | タスク |
| :---- | :---- | :---- |
| `tools/bench_fts.py` | 合成コーパス生成とFTS性能計測（手動実行） | A-1 / A-3 |
| `src/mail_dock/domain/normalize.py` | 投入・検索で共有する検索正規化の純粋関数 | B-0 |
| `src/mail_dock/domain/errors.py` | `SearchQueryError` の追加 | B-1 |
| `src/mail_dock/domain/search.py` | 検索モデルと `BaseSearchRepository`（読み取り専用ポート） | B-2 |
| `src/mail_dock/usecases/search_query.py` | クエリ文字列パーサ（正規化・経路振り分け・エスケープ） | C-1 |
| `src/mail_dock/usecases/search_messages.py` | 検索・一覧・スレッド・件数のユースケース | C-2 |
| `src/mail_dock/infrastructure/database/search_repository.py` | SQLite検索実装（MATCH/LIKE・keyset・キャンセル） | D-1 |
| `src/mail_dock/migrations/003_search_index.sql` | 索引追加（**A-5 の判定で必要と決まった場合のみ**） | D-2 |
| `src/mail_dock/infrastructure/database/fts_maintenance.py` | FTS `integrity-check` | D-3 |
| `src/mail_dock/__main__.py` | `search` サブコマンド・`verify` 拡張・終了コード追加 | E-1 / E-2 |
| `tests/unit/test_search_query.py` 他 | 単体テスト | F-1 |
| `tests/integration/test_search_flow.py` | 結合テスト | F-2 |

---

## **5. スコープ境界**

### **5.1 含むもの**

セクション3のグループA〜F。ヘッドレスで「PoC による設計確定 → キーワード検索（AND/OR/フレーズ/除外）→ 構造化フィルタ → keyset ページング → スレッド一覧・件数・詳細取得 → CLI からの検証」が通る読み取り基盤一式。

### **5.2 含まないもの（明示的に除外）**

| 除外項目 | 実施フェーズ |
| :---- | :---- |
| 検索結果のスニペット・ハイライト表示 | Phase 3（必要性を UI 設計時に判断。D-4） |
| フィールド指定検索（`from:` / `subject:` / `has:attachment` 構文） | 将来拡張（構造化フィルタで代替。D-2） |
| `bm25()` による関連度ランキング | 将来拡張（D-3） |
| PySide6 の一覧モデル（`QAbstractTableModel` / `canFetchMore`）、検索UI、スレッド画面、HTML表示5層サンドボックス | Phase 3 |
| 一覧のスレッドグルーピング表示（Gmail風） | 将来拡張（`thread_key` 列は保持済み） |
| FTS の `rebuild` / `optimize` の運用導線、再インデックス、整合性チェック全般 | Phase 4（D-13） |
| ローカルゴミ箱・30日purge・墓標化（`message_contents` 削除の**実行側**） | Phase 4 |
| PST由来メッセージの検索統合（ストレージ・DB・検索基盤のみ共有し、データとしては接続しない） | Phase 4.5 |
| 添付ファイル本文（PDF / Office）のテキスト抽出とインデックス化 | 恒久的にスコープ外 |

---

## **6. 検証**

各項目の完了を確認したうえで、対応するタスクのチェックボックスを埋めること。

- [ ] V-1. `uv sync` → `uv run ruff format --check .` → `uv run ruff check .` → `uv run mypy` がすべて成功する
- [ ] V-2. `uv run pytest -m "not docker"` が全緑になり、`domain` + `usecases` のカバレッジが 80% 以上である
- [ ] V-3. PoC の計測結果が本書「7.」へ記録され、N-1（300ms）/ N-2（3秒）/ N-3（3〜5倍）の達成可否と、未達の場合の対応が明記されている
- [ ] V-4. 2文字キーワードが `MATCH` 経路では0件になり、`LIKE` 経路では正しく発見できる。`has_slow_path` が立つ
- [ ] V-5. keyset ページング検証: `next_cursor` は返却した最後の行から生成され、全ページを連結した結果が一括取得と**完全に一致**し、重複・欠損がゼロである。同一 `date_sent` が複数ある場合と `date_sent` が NULL の行が混在する場合の双方で成立する
- [ ] V-6. 正規化一貫性: `normalize_for_search()` が `domain/normalize.py` に置かれ、投入時と検索時が同一関数を参照することがテストで固定され、片方だけを変更すると失敗する
- [ ] V-7. 状態フィルタ: 既定で `local_state='purged'` / `'trashed'` がヒットせず、`remote_state='deleted'` / `'moved'` はヒットする
- [ ] V-8. 層の隔離: `usecases/` 配下に `sqlite3` の import が無いことを静的に確認する。`domain/search.py` の外部依存がゼロである
- [ ] V-9. キャンセル: LIKE スキャン中に `CancelToken` を立てると**クエリ実行中に**中断し、`SQLITE_INTERRUPT` が一般的なDB例外分類より先に `OperationCancelledError` へ変換される。progress handler が解除されている
- [ ] V-10. エスケープと不正入力: `"` / `*` / `^` / `-` / `NEAR` / `%` / `_` / `\` を含む正当なクエリで Parse Error も SQL エラーも発生せず、未閉鎖の引用符・空フレーズ・単独 `-`・正規化後に空になる語は `SearchQueryError` になる
- [ ] V-11. 日本語検索: ひらがなとカタカナが同一視されず、全角英数と半角英数が同一視され、大文字小文字が区別されない
- [ ] V-12. マイグレーション（`003` を作る場合）: v2 の実データを保持して適用され、適用後に `integrity-check` が通る
- [ ] V-13. FTS 追随・検証: `message_contents` の UPDATE / DELETE で `messages_fts` が追随し、`verify` が専用の書き込み可能接続で `rank=1` の `integrity-check` を実行してexternal-contentとの乖離を検出できる。通常検査は読み取り専用接続を維持し、両接続は確実に閉じられる
- [ ] V-14. CLI: `mail-dock search` が Phase 1 で同期した実データに対して期待どおりヒットし、`--after` による継続取得が機能する
- [ ] V-15. CI: プルリクエストで `lint` / `test-windows` / `test-linux` の3ジョブがすべて成功し、ベンチマークが実行されていない

---

## **7. Phase 3 への引き継ぎ事項**

> *PoC（A-5）の結果と、実装中に判明した事項をここへ追記する。*

* A-2 環境確認（2026-07-31）:
  * Linux (WSL2): Python `sqlite3.sqlite_version` **3.46.1**。Pythonから `tokenize='trigram'` の仮想テーブル作成に成功。
  * Windows: Python `sqlite3.sqlite_version` **3.50.4**。SQLite CLI は **3.53.4**。CLIで `tokenize='trigram'` の仮想テーブル作成に成功。
  * いずれも必要条件 **3.34.0以上**を満たし、確認した環境に未対応環境はなかった。
  * `tools/bench_fts.py --check-environment` でバージョン条件と実際のtrigram作成を再確認できる。起動時の追加チェックは設けず、既存の初回マイグレーションがtrigram作成に失敗した場合に `DatabaseError` として扱う。将来未対応環境が確認された場合は、検索機能を提供する前に明示的な起動時チェックを追加する。
* PoC 計測結果（環境・SQLiteバージョン・件数水準・実測値・5万通への外挿）: **A-5 完了後に記録する**
* 採用したトークナイザ構成と、代替案を採らなかった理由: **A-5 完了後に記録する**
* `003_search_index.sql` の要否と、作らなかった場合はその判断根拠: **A-5 完了後に記録する**
* `normalize_for_search()` は Phase 3 の検索ボックスでも**必ず `parse_query()` 経由**で適用すること。UI 側で別の正規化を挟まない。
* `SearchPage.next_cursor` は Phase 3 の `canFetchMore` / `fetchMore` にそのまま使える形にしてある。`limit=200` を既定とし、UI 側でオフセット計算をしない。
* `next_cursor` は返却済みの最後の行を指す。Phase 3 はカーソルを加工せず、そのまま次の `fetchMore` に渡すこと。
* `has_slow_path` が立っている場合、Phase 3 の UI は「短い語を含むため時間がかかる場合があります」を表示し、キャンセルボタンを有効にすること（開発計画書 4.5-3 / 5.4）。
* 検索とスレッド一覧は同じ `MessageFilter` を共有する。Phase 3 の「この会話のN件を表示」は `list_thread()` を呼ぶだけでよい。
* 一覧は**フラット表示が既定**。`thread_key` によるグルーピング表示は将来拡張であり、Phase 3 では実装しない。
* スニペット・ハイライトは Phase 2 で未実装（D-4）。必要と判断した場合、MATCH 経路は FTS5 の `snippet()` が使えるが LIKE 経路では自前実装が必要であり、かつ**正規化済みテキストからの抽出になるため表示原文と一致しない**点に注意すること。
* `BaseSearchRepository` は読み取り専用。Phase 3 の ViewModel から書き込みを行う必要が生じた場合も、このポートに書き込みメソッドを足さず `BaseMessageRepository` 側で扱うこと。
* FTS の `rebuild` / `optimize` は Phase 4 の再インデックス機能で扱う。Phase 2 が提供するのは `rank=1` の `integrity_check()` による検査のみ。この検査は SQL 上 `INSERT` のため専用の書き込み可能接続を使うが、原本データは変更しない。
* PST アーカイブ（Phase 4.5）は検索基盤を共有するが、`messages.uid IS NULL` で区別される。検索フィルタに「由来」の軸が必要になった場合は `MessageFilter` へ追加する。
