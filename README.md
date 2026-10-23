# QueRyu

<p align="center">
  <img src="docs/brand/icon.png" alt="QueRyu icon" width="160" />
</p>

[Icon](docs/brand/icon.png) · [Favicon](docs/brand/favicon.png)

> Reproducible fetching builds reliable understanding.
>
> Polite web fetching with conditional-GET caching and provenance for building NPB baseball observation datasets (split out from Aoi/queryu).

![QueRyu](https://github.com/myon-bioinformatics/Aoi/raw/main/docs/images/queryu.webp)

*イメージ図（構想を共有するための図。実際のデータ・HTML構造ではない）*

## Role in Aoi

**Fetching and observation datasets.**

| Challenges | Approach | Outcomes & value |
|---|---|---|
| Choosing data sources is hard | 1. Cache and verify | Reproducible data |
| It is unclear when and where data came from | 2. Record provenance for every fetch | A clear fetch history |
| Large amounts of data are hard to manage | 3. Build observation datasets | Easy comparison and analysis |

Topics: datasets, fetch support, automation

## 概要

取得と、観測データセットの作成を担う。取得元への負荷を抑え、いつ・どこから・どのコードで取ったかを必ず残す。

- **キャッシュと来歴**: 取得したページを保存し、取得ごとに1行の来歴（URL・時刻・SHA-256・ETag・コードの版）を追記する。304 や 404 も「確認した」記録として残す。
- **取得元への配慮**: キャッシュ優先・条件付きGET・直列・既定3秒の間隔。`--plan` で、取得せずに件数と待ち時間を見積もる。
- **観測データセット**: 取得元（[BlueProbe](https://github.com/myon-bioinformatics/BlueProbe) の source）のページから、1記録1行の JSONL と manifest を作る。
- **問い合わせ**（`ask/`）: 日本語の問いを決まった型に直し、PythDRagoras が判定済みの結果から答える（[docs/ask.md](docs/ask.md)）。

[Aoi](https://github.com/myon-bioinformatics/Aoi) のサブプロジェクト [`queryu/`](https://github.com/myon-bioinformatics/Aoi/tree/main/queryu) から独立したリポジトリです（下の「Aoi との関係」）。

## インストールと使い方

Python 3.11 以上と [uv](https://docs.astral.sh/uv/) を使う。実行時の依存は numpy（`ask/suggest.py`）。
抽出（`blueprobe`・`npb_calendar`・`npb_team_annual`）は [BlueProbe](https://github.com/myon-bioinformatics/BlueProbe) を checkout して `PYTHONPATH` に渡す。Aoi の `blueprobe/` は使わない。

```bash
git clone https://github.com/myon-bioinformatics/QueRyu.git
git clone https://github.com/myon-bioinformatics/BlueProbe.git
cd QueRyu
uv sync
PYTHONPATH=../BlueProbe uv run python queryu.py --help
```

`queryu.py` の主な引数は `--source`・`--years`・`--cache`・`--out`・`--wait`・`--offline`・`--plan`・`--freeze`。データセットを作る例は下の「データセット」にある。

## 構成

| ファイル | 中身 |
|---|---|
| `queryu.py` | CLI。`Cache`（保存と来歴）・`PoliteFetcher`（負荷を抑えた取得）・`build()`（観測データセット） |
| `cache_core.py` | 標準ライブラリだけのオフラインのキャッシュ・スナップショット API（下の「Stdlib cache distribution」） |
| `frozen_seasons.json` | 再確認しないシーズンの手動の凍結リスト |
| `ask/` | 問い合わせ（`ask.py`）・確認用の命題 ID の候補（`suggest.py`）・登録済みの問いの一覧（`bank.py`） |
| `docs/ask.md` | 問い合わせの仕様 |
| `tests/` | pytest |

## 取得元への配慮

- 取得済みのページは再取得しない。今年以降で、凍結リストに無い年は更新を確認する。保存済みの `Last-Modified` / `ETag` があれば条件付きGET、無ければ条件なしのGETとなる
- カレンダーの key が `年_月` のページは、実行日より後の月を取らない。`年_リーグ` のような月でない key はそのまま
- シーズンを再確認の対象から外すときは `--freeze 年` で `frozen_seasons.json` に足す。年は1900〜2100の整数。不正な入力や `--out` 不足では凍結リストを書き換えない。終了した年を日付から推測して足すことはしない
- 存在しないページ（404）も「空」として記録する。再確認の対象年では404も再確認し、凍結した年では再取得しない
- 直列で、既定3秒の間隔を空ける。`--wait` は0以上の有限の秒数。5xx は保存しない
- `--offline` ではネットワークに一切出ない（キャッシュにないページは止まる）
- `--plan` はネットワークに出ない。キャッシュの来歴だけを数える

### 観測（`--plan` の件数）

| 表示 | 数えているもの |
|---|---|
| キャッシュ済み（再確認しない） | 来歴があり、その年は再確認の対象ではない |
| 条件付きGET | 再確認対象の来歴に、空でない `ETag` または `Last-Modified` がある |
| 再確認（条件なし） | 再確認対象の来歴に、どちらの更新確認ヘッダーも無い |
| 新規 | 来歴がない |
| 未来の月（取得しない） | `年_月` の key で、実行日より後の月 |
| オフラインで不足するページ | `--offline --plan` のとき、来歴がないページの件数 |

凍結リストの初期値は空である（`queryu/frozen_seasons.json`）。

### 解釈（待ち時間）

オンラインの見積もりは、間隔 × max(0, 条件付きGET + 再確認（条件なし） + 新規 − 1) 秒である。最初のリクエストの前には間隔を空けない、という取得の手順から出している。数えているのは、その間隔を足した待ち時間である。

`--offline --plan` は再確認対象年でも保存済みページを使うので、HTTP件数と待ち時間を0とする。「新規」は不足するページの数であり、オフラインで取得するという意味ではない。本文は検証せず来歴だけを数えるため、計画はキャッシュ本文の正常性を保証しない。

## 来歴

キャッシュの `manifest.jsonl` に、取得ごとに1行を追記する。304（更新なし）も「確認した」記録として残す。

```text
key, url, group, status, file, sha256, bytes, fetched_at, last_modified, etag, code_version
```

`code_version` は git のコミット。測れなければ `null` のまま（推測で埋めない）。

## データセット

`data/observations/<source>/<entity>.jsonl`（1記録1行、`key` で一意）と、隣の `.manifest.json`（取得元・範囲・正規化の内容・採用しなかった件数・未知の件数・SHA-256・コードの版）。
生の観測を含むので `data/` は git に入れない。

```bash
PYTHONPATH=../BlueProbe uv run python queryu.py --source npb_calendar --years 2012-2025 \
  --cache data/raw/npb_calendar --out data/observations/npb_calendar/games.jsonl [--offline]
```

Aoi のルートから動かすときは `uv run python queryu/queryu.py ...`。

## 保存本文の照合と履歴

新しい `Cache.put()` は本文を `blobs/<sha256>.html.gz` に保存する。同じ本文は
共有するが、取得ごとの manifest 行（URL・時刻・ETag など）は毎回残す。
一時ファイルの完成後に既存ファイルを上書きしない形で公開し、その後に来歴を追記する。
同じ key を更新しても、以前の行が指す本文は残る。key はファイル名に使わない。
既存 blob が壊れている場合は再保存で修復せず、エラーで止める。
自動削除・保持期限・既存キャッシュの一括移行は実装していない。
中断で来歴のない blob が残る場合も、勝手に削除しない。

`Cache.body_bytes(entry)` は gzip 展開後の**保存した元バイト列**を、来歴に既にある
`sha256` と `bytes` の両方に照合する。`Cache.body(entry)` はその後で UTF-8 を
厳密に decode する。照合失敗を再取得で隠さず、ネットワークへ取りに行かない。
絶対パス・親ディレクトリ参照・キャッシュ外への symlink は拒否する。
ファイルと来歴を同時に書き換えられる攻撃者に対する署名・真正性の保証ではない。

旧形式の `<key>.html.gz` と manifest は変更しない。保存済みの hash と長さがあり、
現在の本文と一致する行はそのまま読める。hash/長さがない・不正な行は明示的に拒否する。
読み込み時に新しい hash を作って「検証済み」に昇格させない。旧方式で既に上書きされた
過去の本文は復元できず、対応する古い行は照合失敗になる場合がある。
新方式導入後の保存でも旧ファイルを上書きしない。履歴を使うときは、その取得の
manifest 行を選んで `body()` または `snapshot()` に渡す。

`Cache.snapshot(entry)` は元バイト列の照合後に `html-snapshot/1` へ変換する。
`response_sha256` は保存した元バイト列、`content_sha256` は decode 済みHTMLを
UTF-8 にした値の hash。現行の厳密な UTF-8 保存では一致するが、意味は別々に扱う。
元の `url`・`fetched_at` を引き継ぎ、取得時刻が不明なら `null` のままにする。
独自項目を含む元の行全体は `cache_entry` に保存し、補完した来歴と混同しない。
これだけでは URL・取得時刻の真実性や HTTP 応答の真正性は証明しない。

責務は、取得・保存本文・照合・履歴が QueRyu、HTML構造の抽出・観測値の読み取りが
[BlueProbe](https://github.com/myon-bioinformatics/BlueProbe)、集計・指標計算が [SakAnalytics](https://github.com/myon-bioinformatics/SakAnalytics)、
仮説評価が [PythDRagoras](https://github.com/myon-bioinformatics/PythDRagoras)。実行元やソースの identity 確認・
CI runner・証跡の実行管理は [NagoyAction](https://github.com/myon-bioinformatics/NagoyAction) が担う。この変更では新しい runner や
実行証明を追加しない。

## Stdlib cache distribution

`cache_core.py` is the minimal offline API (`Cache`, `CacheIntegrityError`). It
verifies the original body bytes and exports `html-snapshot/1`, without BlueProbe,
NumPy, HTTP clients, Aoi files or an implicit source checkout. Unknown code
provenance remains `None`; callers may pass `source_version` explicitly. The full
`queryu.Cache` subclasses this core and retains its existing Git/timestamp hooks.

The Aoi parent builds `queryu_min.py` with the account parent's `tools/minpy.py`.
The min API is the cache/snapshot subset, not the full planner or question index.
Acquisition uses the existing synchronous urllib client; no asynchronous support
is claimed by the min artifact. Whole-tree subtree synchronization remains the
Aoi/QueRyu development relationship. MIT notices are kept in `LICENSE`.

## キャッシュの照会・受け渡し・並行取得（`cache_share.py`）

`cache_core.Cache` の上に載る stdlib のみのモジュール。取得済みの本文を、取り直さずに
抽出側（BlueProbe など）や他の checkout・他の人へ渡すためのもの。

- `query(cache, key=, url=, group=, status=, since=, until=, history=)`:
  manifest の行を絞り込む。`key`／`url` は glob（`cal:2024_*`）、`since`／`until`
  は `fetched_at` の範囲（両端を含む）。既定ではキーごとに最新の 1 行を返す。`until`
  と組み合わせると「その時点の最新」になり、`history=True` なら全取得履歴を返す。
- `export_bundle(cache, rows, path)` / `import_bundle(cache, path)`:
  照合済みの gzip 本文と行を 1 つの `.tar`（`queryu-bundle/1`）にまとめて受け渡す。
  - 書き出し時には本文を照合する。既存のファイルは上書きしない。
  - 取り込み時には全本文の sha256 とバイト数を再照合する。不一致、パス逸脱、
    通常ファイル以外のメンバーが一つでもあれば、何も書かずに失敗する。
  - 既存の blob と、同じ (key, sha256, fetched_at) の行は追加しない。
  - 取り込んだ行は元の来歴を保ったまま、`imported_from`（バンドルの SHA-256）を持つ。
- `iter_snapshots(cache, rows)` / `aiter_snapshots(cache, rows, concurrency=4)`:
  `html-snapshot/1` を同期ジェネレータまたは async イテレータで供給する。async 版は
  照合と展開をスレッドで行い、順序は入力のまま保つ。
- `fetch_many(cache, [(key, url, group), ...], client=, wait=3.0, max_hosts=4)`:
  複数ホストを並行して取得する。
  - 同じホストへのリクエストは指定順に 1 件ずつ送り、間を `wait` 秒空ける。
    `PoliteFetcher` と同じ配慮。
  - 異なるホスト同士は互いを待たない。
  - 200 と 404 は `Cache.put`（来歴つき）で保存し、それ以外の status は本文なしで記録する。
  - 例外は `status: null` と `error` として返し、他のホストの取得は続ける。
  - manifest への書き込みはロックで 1 つずつ行う。

```bash
python cache_share.py --cache data/cache query --group npb_calendar --since 2026-01-01
python cache_share.py --cache data/cache export --key 'npb_calendar:2024_*' --out share.tar
python cache_share.py --cache other/cache import share.tar
python cache_share.py --cache data/cache verify --group npb_calendar
```

- `verify_cache(cache, entries=None)` / `verify`: 本文のある行（既定では全履歴）について、gzip 本文と
  記録された sha256・バイト数を照合し、`{"total", "verified", "corrupted", "errors"}` を JSON で出す。
  `verify` は query と同じ絞り込みを受け付ける。終了コードは、全件一致で 0、不一致が 1 件でもあれば 1、
  対象が 0 件なら 3（指定ミスに気づけるよう、stderr に注記を出す）。

## テスト

サイクルデータ（`cycles/`）・blueprobe の fixture・`nagoyaction/inbox` は Aoi に残す。テストは Aoi と BlueProbe の最新の main を checkout して渡す。CI は実行時に `git ls-remote` で main の SHA を解決し、解決した SHA をジョブサマリーへ書く。古い実行を再現するときは workflow_dispatch の ref を指定する。

```sh
git clone https://github.com/myon-bioinformatics/Aoi.git ../Aoi
git clone https://github.com/myon-bioinformatics/BlueProbe.git ../BlueProbe
AOI_ROOT=../Aoi PYTHONPATH=../BlueProbe uv run --locked pytest -q
```

CI は Python 3.11–3.15 で同じことをする。

## Aoi との関係

[Aoi](https://github.com/myon-bioinformatics/Aoi) の [`queryu/`](https://github.com/myon-bioinformatics/Aoi/tree/main/queryu) から独立したリポジトリ。直接の親は Aoi で、`queryu/` との差分は Aoi 側で確かめる。
初回の `main`（`35a08024dc0040566068ba513074e3d8326897d4`）は Aoi `85243d6d1285e3bf4667e1447308543c7d648ab7` の `queryu/` を履歴ごと切り出したもの（tree `c995b928f46b297025d9cddaeee3df04b322405b`）。
Aoi の [`child-heads.json`](https://github.com/myon-bioinformatics/Aoi/blob/main/child-heads.json) に登録されている（手順の全体は Aoi の [docs/child-repositories.md](https://github.com/myon-bioinformatics/Aoi/blob/main/docs/child-repositories.md)）。

- サイクルデータ（`cycles/`）・blueprobe の fixture・`nagoyaction/inbox` は Aoi に残す。テストは Aoi を checkout して `AOI_ROOT=<Aoi のルート>` を渡す
- Aoi の配置（`AOI_ROOT`・サイクル名・`pipeline.toml`・結果 ref）は QueRyu では解決しない。Aoi の `aoi_paths.py` が解決して `--results`・`--cycle-dir`・`--teams`・`--month-key` で渡す。球団表と年間ページの範囲（旧 `annual_scope.py`）も Aoi に置く
- Aoi との同期: Aoi 側で一度だけ `git subtree add` をして履歴をつなぎ、そのあとは subtree で同期する。Aoi と QueRyu の間では vendor lock を使わない（Aoi の `queryu/aoi_paths.py` が Aoi 直下の `aoi_paths.py` と同じ blob であることは、Aoi 側で確かめる）
- この README は Aoi の `queryu/README.md` と同じ内容から始まっている。Aoi の中の文書へのリンクは、どちらで読んでも開けるように GitHub の URL で書く

## 関連サブプロジェクト

親の [Aoi](https://github.com/myon-bioinformatics/Aoi) のサブプロジェクトは、それぞれ独立したリポジトリになっています。Aoi の中の場所（subtree）とあわせて載せます。一覧と登録の詳細は Aoi の [docs/child-repositories.md](https://github.com/myon-bioinformatics/Aoi/blob/main/docs/child-repositories.md) です。

| リポジトリ | Aoi の中の場所 | 役割 |
|---|---|---|
| [BlueProbe](https://github.com/myon-bioinformatics/BlueProbe) | [`blueprobe/`](https://github.com/myon-bioinformatics/Aoi/tree/main/blueprobe) | 観測と抽出（保存した HTML/CSS/DOM を読み、何があるかを記録する） |
| **QueRyu**（このリポジトリ） | [`queryu/`](https://github.com/myon-bioinformatics/Aoi/tree/main/queryu) | 取得と観測データセットの作成（来歴つきのキャッシュ） |
| [SakAnalytics](https://github.com/myon-bioinformatics/SakAnalytics) | [`sakanalytics/`](https://github.com/myon-bioinformatics/Aoi/tree/main/sakanalytics) | 観測データからの指標と統計（チーム×シーズンの指標、残り試合の見通し） |
| [PythDRagoras](https://github.com/myon-bioinformatics/PythDRagoras) | [`pythdragoras/`](https://github.com/myon-bioinformatics/Aoi/tree/main/pythdragoras) | 説明の検証（命題・反例・異議あり） |
| [NagoyAction](https://github.com/myon-bioinformatics/NagoyAction) | [`nagoyaction/`](https://github.com/myon-bioinformatics/Aoi/tree/main/nagoyaction) | パイプラインの実行と自動化（同じ定義をローカルと GitHub Actions で） |
| [DRAgoWing](https://github.com/myon-bioinformatics/DRAgoWing) | [`dragowing/`](https://github.com/myon-bioinformatics/Aoi/tree/main/dragowing) | 図とレポート（Plotly.js の HTML、四つの形の表） |

## 絵文字の名前（npm 経由の Unicode データと、オフライン用の集積）

unicode.org はサンドボックスや CI から届かないことが多いが、npm レジストリには届く。
`emoji_data.py`（標準ライブラリのみ）は、unicode.org の emoji-test.txt（CLDR の短い名前・グループ・版）を
JSON にした `unicode-emoji-json` を npm から一度だけ取得し、QueRyu の `Cache` に来歴つきで保存する。

```sh
python emoji_data.py --cache data/raw/npm --names emoji_names.json --use inventory.json   # 取得（初回のみ）＋集積
python emoji_data.py --cache data/raw/npm --offline --lookup "TEXT"                       # キャッシュだけで引く
python emoji_data.py --names emoji_names.json --lookup "TEXT"                             # 集積表だけで引く（ネットワーク・キャッシュ不要）
```

- 版は `名前@x.y.z` で固定（`latest` は使わない）。レジストリの版メタデータから tarball を得て、レジストリのホストにあることと sha512 `integrity` を確かめてから保存する。`--offline` ではネットワークに出ず、未取得なら止まる
- `emoji_names.json`（`queryu.emoji-names/1`）は、実際に使った絵文字の名前を集積してコミットする表。`--use` に xprobe `--emoji` の JSON（`xprobe.emoji-inventory.v1`）か絵文字の配列を渡すと、無い分だけ足す。既存の項目は消さず、書き換えない（手で直した名前も残る）。取得元（パッケージ・版・tarball の SHA-256・取得時刻）を `sources` に残す
- キーは U+FE0F を除いた形（xprobe `emoji_identity`・browser-test-kit `route_trail.py` と同じ）。肌の色・髪の部品つきの変種が表に無ければ、元の絵文字の名前＋部品の名前（`thumbs up: medium skin tone`）にする
- 初期の表は、Flutter・browser-test-kit・web-ui・markdown・gh_identity・親・BlueProbe・Aoi などの既存ファイルに出てくる 287 種類（xprobe の集計 5,934 ファイル）から作った。名前の無い 1 件（♭ U+266D、絵文字ではない記号）は足していない
- 保存本文のファイル名は既存の `Cache.put` の規則（`blobs/<sha256>.html.gz`）のまま。中身は tarball のバイト列で、照合は他の保存本文と同じ

## ライセンス

MIT License（[LICENSE](LICENSE)）。Aoi の `LICENSE` と同じ内容です。

---

# NPB（プロ野球）向けの取得

以下は Aoi の研究サイクル（中日・NPB のデータ）で使っている取得元の内容です。

## 年間チーム成績の追加取得

`--source npb_team_annual --years 2013-2026` で、両リーグの年度別チーム表を取得できる。
2005年以降は詳細な勝敗・打撃・投手・守備の全掲載列も取得し、年度別要約表と区別して保持する。
主対象・合格判定は2013〜2026年。2013年以前は任意の参考資料として通常のデータから分ける。
年間キャッシュの主対象の検証・コピー（旧 `annual_scope.py`）は NPB 固有なので Aoi の `annual_scope.py` に移した。
データセットのmanifestには、解析に用いたsourceモジュール自体の `source_code_sha256` も残す。
範囲・照合・再実行は [c001の取得・架空検証記録](https://github.com/myon-bioinformatics/Aoi/blob/main/cycles/c001-chunichi/annual-history-synthetic.md)。
