# QueRyu

<p align="center">
  <img src="docs/brand/icon.png" alt="QueRyu icon" width="160" />
</p>

[Icon](docs/brand/icon.png) · [Favicon](docs/brand/favicon.png)

> Reproducible fetching builds reliable understanding.
>
> Polite web fetching with conditional-GET caching and provenance for building NPB baseball observation datasets (split out from Aoi/queryu).

![QueRyu](https://github.com/myon-bioinformatics/Aoi/raw/main/docs/images/queryu.webp)

*Concept image (a figure for sharing the idea; not the actual data or HTML structure)*

## Role in Aoi

**Fetching and observation datasets.**

| Challenges | Approach | Outcomes & value |
|---|---|---|
| Choosing data sources is hard | 1. Cache and verify | Reproducible data |
| It is unclear when and where data came from | 2. Record provenance for every fetch | A clear fetch history |
| Large amounts of data are hard to manage | 3. Build observation datasets | Easy comparison and analysis |

Topics: datasets, fetch support, automation

## Overview

Handles fetching and building observation datasets. It keeps the load on sources low and always records when, from where, and with which code each page was fetched.

- **Cache and provenance**: saves fetched pages and appends one provenance line per fetch (URL, time, SHA-256, ETag, code version). 304s and 404s are also kept as "checked" records.
- **Care for sources**: cache first, conditional GETs, serial requests, and a default 3-second interval. `--plan` estimates counts and waiting time without fetching.
- **Observation datasets**: builds one-record-per-line JSONL and a manifest from a source's pages (a [BlueProbe](https://github.com/myon-bioinformatics/BlueProbe) source).
- **Queries** (`ask/`): turns Japanese questions into a fixed type and answers from results PythDRagoras has already judged ([docs/ask.md](docs/ask.md)).

This repository was split out from the [Aoi](https://github.com/myon-bioinformatics/Aoi) subproject [`queryu/`](https://github.com/myon-bioinformatics/Aoi/tree/main/queryu) (see "Relationship with Aoi" below).

## Installation and usage

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). There are no runtime dependencies: `ask/suggest.py` uses `vendor/aoi/shrink/numpy_min.py`, vendored from Aoi and pinned in `vendor.lock.json` (it uses the system LAPACK/BLAS through ctypes when present and falls back to the standard library). numpy is a dev dependency, used only to compare results in tests.
Extraction (`blueprobe`, `npb_calendar`, `npb_team_annual`) checks out [BlueProbe](https://github.com/myon-bioinformatics/BlueProbe) and passes it on `PYTHONPATH`. Aoi's `blueprobe/` is not used.

```bash
git clone https://github.com/myon-bioinformatics/QueRyu.git
git clone https://github.com/myon-bioinformatics/BlueProbe.git
cd QueRyu
uv sync
PYTHONPATH=../BlueProbe uv run python queryu.py --help
```

The main arguments of `queryu.py` are `--source`, `--years`, `--cache`, `--out`, `--wait`, `--offline`, `--plan`, and `--freeze`. An example of building a dataset is under "Datasets" below.

## Layout

| File | Contents |
|---|---|
| `queryu.py` | CLI. `Cache` (storage and provenance), `PoliteFetcher` (low-load fetching), `build()` (observation datasets) |
| `cache_core.py` | Standard-library-only offline cache/snapshot API ("Stdlib cache distribution" below) |
| `frozen_seasons.json` | Manual freeze list of seasons that are not rechecked |
| `ask/` | Queries (`ask.py`), proposition ID candidates for confirmation (`suggest.py`), list of registered questions (`bank.py`) |
| `docs/ask.md` | Query spec |
| `tests/` | pytest |

## Care for sources

- Pages already fetched are not refetched. For this year onward, years not on the freeze list are checked for updates. A conditional GET is used if a saved `Last-Modified` / `ETag` exists, otherwise an unconditional GET
- For calendar pages whose key is `year_month`, months after the run date are not fetched. Non-month keys such as `year_league` are used as-is
- To remove a season from rechecking, add it to `frozen_seasons.json` with `--freeze YEAR`. The year is an integer from 1900 to 2100. Invalid input or a missing `--out` does not rewrite the freeze list. Finished years are not added by guessing from the date
- Nonexistent pages (404) are also recorded as "empty". In years subject to rechecking, 404s are rechecked too; frozen years are not refetched
- Serial, with a default 3-second interval. `--wait` is a finite number of seconds, 0 or more. 5xx responses are not saved
- `--offline` never goes to the network (it stops at pages not in the cache)
- `--plan` never goes to the network. It only counts cache provenance

### Observation (`--plan` counts)

| Label | What it counts |
|---|---|
| キャッシュ済み（再確認しない） (cached, not rechecked) | Has provenance, and that year is not subject to rechecking |
| 条件付きGET (conditional GET) | Provenance subject to rechecking has a non-empty `ETag` or `Last-Modified` |
| 再確認（条件なし） (recheck, unconditional) | Provenance subject to rechecking has neither update-check header |
| 新規 (new) | No provenance |
| 未来の月（取得しない） (future month, not fetched) | A `year_month` key for a month after the run date |
| オフラインで不足するページ (pages missing offline) | With `--offline --plan`, the number of pages without provenance |

The freeze list starts empty (`queryu/frozen_seasons.json`).

### Interpretation (waiting time)

The online estimate is interval × max(0, conditional GET + recheck (unconditional) + new − 1) seconds. This comes from the fetch procedure, which does not wait before the first request. What is counted is the waiting time from adding those intervals.

`--offline --plan` uses saved pages even for years subject to rechecking, so the HTTP count and waiting time are 0. "New" is the number of missing pages, not a claim that they will be fetched offline. Since it counts only provenance without validating bodies, the plan does not guarantee the cached bodies are sound.

## Provenance

One line per fetch is appended to the cache's `manifest.jsonl`. 304 (not modified) is also kept as a "checked" record.

```text
key, url, group, status, file, sha256, bytes, fetched_at, last_modified, etag, code_version
```

`code_version` is the git commit. If it can't be measured it stays `null` (not filled in by guessing).

## Datasets

`data/observations/<source>/<entity>.jsonl` (one record per line, unique by `key`) and the adjacent `.manifest.json` (source, range, normalization details, count not adopted, count unknown, SHA-256, code version).
Because it contains raw observations, `data/` is not committed to git.

```bash
PYTHONPATH=../BlueProbe uv run python queryu.py --source npb_calendar --years 2012-2025 \
  --cache data/raw/npb_calendar --out data/observations/npb_calendar/games.jsonl [--offline]
```

When running from Aoi's root, use `uv run python queryu/queryu.py ...`.

## Verifying saved bodies and history

The new `Cache.put()` saves bodies to `blobs/<sha256>.html.gz`. Identical bodies are
shared, but the per-fetch manifest line (URL, time, ETag, etc.) is kept every time.
After a temporary file is complete, it is published without overwriting an existing file, and then provenance is appended.
Updating the same key keeps the bodies earlier lines point to. Keys are not used as file names.
If an existing blob is corrupt, it is not repaired by re-saving; it stops with an error.
Automatic deletion, retention periods, and bulk migration of existing caches are not implemented.
Blobs left without provenance by an interruption are not deleted automatically either.

`Cache.body_bytes(entry)` checks the gzip-decompressed **original saved bytes** against both the
`sha256` and `bytes` already in the provenance. `Cache.body(entry)` then strictly decodes
UTF-8. Verification failures are not hidden by refetching, and it never goes to the network.
Absolute paths, parent-directory references, and symlinks outside the cache are rejected.
This is not a signature or authenticity guarantee against an attacker who can rewrite both files and provenance.

Old-format `<key>.html.gz` files and manifests are unchanged. Lines with a saved hash and length
that match the current body can be read as-is. Lines without, or with invalid, hash/length are explicitly rejected.
No new hash is created at read time to promote a line to "verified". Past bodies already overwritten under the
old scheme cannot be restored, and the corresponding old lines may fail verification.
Saves after the new scheme was introduced do not overwrite old files either. To use history, choose that fetch's
manifest line and pass it to `body()` or `snapshot()`.

`Cache.snapshot(entry)` converts to `html-snapshot/1` after verifying the original bytes.
`response_sha256` is the hash of the saved original bytes, and `content_sha256` the hash of the decoded HTML
re-encoded as UTF-8. With the current strict UTF-8 storage they match, but their meanings are treated separately.
The original `url` and `fetched_at` are carried over; if the fetch time is unknown it stays `null`.
The whole original line, including custom fields, is saved in `cache_entry` and not confused with filled-in provenance.
This alone does not prove the truth of the URL or fetch time, or the authenticity of the HTTP response.

Responsibilities: fetching, saved bodies, verification, and history are QueRyu's; extracting HTML structure and reading observed values are
[BlueProbe](https://github.com/myon-bioinformatics/BlueProbe)'s; aggregation and metric calculation are [SakAnalytics](https://github.com/myon-bioinformatics/SakAnalytics)'s;
hypothesis evaluation is [PythDRagoras](https://github.com/myon-bioinformatics/PythDRagoras)'s. Identity checks of the executor and source,
CI runners, and execution management of evidence are handled by [NagoyAction](https://github.com/myon-bioinformatics/NagoyAction). This change adds no new runner or
execution attestation.

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

## Querying, handing over, and concurrently fetching the cache (`cache_share.py`)

A stdlib-only module on top of `cache_core.Cache`. It hands fetched bodies, without refetching, to
the extraction side (BlueProbe, etc.), other checkouts, or other people.

- `query(cache, key=, url=, group=, status=, since=, until=, history=)`:
  filters manifest lines. `key` / `url` are globs (`cal:2024_*`), and `since` / `until`
  are a `fetched_at` range (inclusive at both ends). By default it returns the latest line per key. Combined with `until`
  it gives "the latest as of that time", and `history=True` returns the full fetch history.
- `export_bundle(cache, rows, path)` / `import_bundle(cache, path)`:
  packs verified gzip bodies and lines into a single `.tar` (`queryu-bundle/1`) for handover.
  - Bodies are verified on export. Existing files are not overwritten.
  - On import, every body's sha256 and byte count are re-verified. If there is any mismatch, path escape,
    or non-regular-file member, it fails without writing anything.
  - Existing blobs and lines with the same (key, sha256, fetched_at) are not added.
  - Imported lines keep their original provenance and carry `imported_from` (the bundle's SHA-256).
- `iter_snapshots(cache, rows)` / `aiter_snapshots(cache, rows, concurrency=4)`:
  supplies `html-snapshot/1` as a sync generator or an async iterator. The async version
  does verification and decompression in threads and keeps the input order.
- `fetch_many(cache, [(key, url, group), ...], client=, wait=3.0, max_hosts=4)`:
  fetches from several hosts concurrently.
  - Requests to the same host are sent one at a time in the given order, `wait` seconds apart,
    with the same care as `PoliteFetcher`.
  - Different hosts do not wait for each other.
  - 200 and 404 are saved with `Cache.put` (with provenance); other statuses are recorded without a body.
  - Exceptions are returned as `status: null` and `error`, and fetching from other hosts continues.
  - Writes to the manifest are done one at a time under a lock.

```bash
python cache_share.py --cache data/cache query --group npb_calendar --since 2026-01-01
python cache_share.py --cache data/cache export --key 'npb_calendar:2024_*' --out share.tar
python cache_share.py --cache other/cache import share.tar
python cache_share.py --cache data/cache verify --group npb_calendar
```

- `verify_cache(cache, entries=None)` / `verify`: for lines with a body (the full history by default), checks the gzip body against
  the recorded sha256 and byte count, and outputs `{"total", "verified", "corrupted", "errors"}` as JSON.
  `verify` accepts the same filters as query. The exit code is 0 if everything matches, 1 if there is even one mismatch,
  and 3 if there are no targets (with a note on stderr so a mistaken filter is noticed).

## Tests

Cycle data (`cycles/`), blueprobe fixtures, and `nagoyaction/inbox` stay in Aoi. Tests check out the latest main of Aoi and BlueProbe and pass them in. CI resolves main's SHA with `git ls-remote` at run time and writes the resolved SHA to the job summary. To reproduce an old run, specify the ref in workflow_dispatch.

```sh
git clone https://github.com/myon-bioinformatics/Aoi.git ../Aoi
git clone https://github.com/myon-bioinformatics/BlueProbe.git ../BlueProbe
AOI_ROOT=../Aoi PYTHONPATH=../BlueProbe uv run --locked pytest -q
```

CI does the same on Python 3.11–3.15.

## Relationship with Aoi

A repository split out from [Aoi](https://github.com/myon-bioinformatics/Aoi)'s [`queryu/`](https://github.com/myon-bioinformatics/Aoi/tree/main/queryu). Its direct parent is Aoi, and differences from `queryu/` are checked on the Aoi side.
The first `main` (`35a08024dc0040566068ba513074e3d8326897d4`) is `queryu/` from Aoi `85243d6d1285e3bf4667e1447308543c7d648ab7`, split out with its history (tree `c995b928f46b297025d9cddaeee3df04b322405b`).
It is registered in Aoi's [`child-heads.json`](https://github.com/myon-bioinformatics/Aoi/blob/main/child-heads.json) (the full procedure is in Aoi's [docs/child-repositories.md](https://github.com/myon-bioinformatics/Aoi/blob/main/docs/child-repositories.md)).

- Cycle data (`cycles/`), blueprobe fixtures, and `nagoyaction/inbox` stay in Aoi. Tests check out Aoi and pass `AOI_ROOT=<Aoi root>`
- Aoi's layout (`AOI_ROOT`, cycle names, `pipeline.toml`, results refs) is not resolved in QueRyu. Aoi's `aoi_paths.py` resolves it and passes `--results`, `--cycle-dir`, `--teams`, and `--month-key`. The team table and the range of annual pages (formerly `annual_scope.py`) also live in Aoi
- Syncing with Aoi: Aoi does `git subtree add` once to connect the histories, and after that syncs via subtree. No vendor lock is used between Aoi and QueRyu (that Aoi's `queryu/aoi_paths.py` is the same blob as the top-level `aoi_paths.py` is checked on the Aoi side)
- This README starts from the same contents as Aoi's `queryu/README.md`. Links to documents inside Aoi are written as GitHub URLs so they open from either place

## Related subprojects

Each subproject of the parent [Aoi](https://github.com/myon-bioinformatics/Aoi) is its own repository. They are listed with their location in Aoi (subtree). The list and registration details are in Aoi's [docs/child-repositories.md](https://github.com/myon-bioinformatics/Aoi/blob/main/docs/child-repositories.md).

| Repository | Location in Aoi | Role |
|---|---|---|
| [BlueProbe](https://github.com/myon-bioinformatics/BlueProbe) | [`blueprobe/`](https://github.com/myon-bioinformatics/Aoi/tree/main/blueprobe) | Observation and extraction (reads saved HTML/CSS/DOM and records what is there) |
| **QueRyu** (this repository) | [`queryu/`](https://github.com/myon-bioinformatics/Aoi/tree/main/queryu) | Fetching and building observation datasets (caches with provenance) |
| [SakAnalytics](https://github.com/myon-bioinformatics/SakAnalytics) | [`sakanalytics/`](https://github.com/myon-bioinformatics/Aoi/tree/main/sakanalytics) | Metrics and statistics from observation data (team × season metrics, outlook for remaining games) |
| [PythDRagoras](https://github.com/myon-bioinformatics/PythDRagoras) | [`pythdragoras/`](https://github.com/myon-bioinformatics/Aoi/tree/main/pythdragoras) | Verifying explanations (propositions, counterexamples, objections) |
| [NagoyAction](https://github.com/myon-bioinformatics/NagoyAction) | [`nagoyaction/`](https://github.com/myon-bioinformatics/Aoi/tree/main/nagoyaction) | Pipeline execution and automation (the same definition locally and on GitHub Actions) |
| [DRAgoWing](https://github.com/myon-bioinformatics/DRAgoWing) | [`dragowing/`](https://github.com/myon-bioinformatics/Aoi/tree/main/dragowing) | Figures and reports (Plotly.js HTML, the four-forms table) |

## Emoji names (Unicode data via npm, and an offline collection)

unicode.org is often unreachable from sandboxes and CI, but the npm registry is reachable.
`emoji_data.py` (standard library only) fetches `unicode-emoji-json`, which is unicode.org's emoji-test.txt (CLDR short names, groups, versions)
as JSON, from npm just once, and saves it in QueRyu's `Cache` with provenance.

```sh
python emoji_data.py --cache data/raw/npm --names emoji_names.json --use inventory.json   # fetch (first time only) + collect
python emoji_data.py --cache data/raw/npm --offline --lookup "TEXT"                       # look up from the cache only
python emoji_data.py --names emoji_names.json --lookup "TEXT"                             # look up from the collected table only (no network or cache needed)
```

- The version is fixed as `name@x.y.z` (`latest` is not used). It gets the tarball from the registry's version metadata, confirms it is on the registry host and matches the sha512 `integrity`, and then saves it. `--offline` never goes to the network and stops if not yet fetched
- `emoji_names.json` (`queryu.emoji-names/1`) is a committed table collecting the names of emoji actually used. Passing xprobe `--emoji` JSON (`xprobe.emoji-inventory.v1`) or an array of emoji to `--use` adds only what is missing. Existing entries are never removed or rewritten (hand-corrected names stay). The source (package, version, tarball SHA-256, fetch time) is kept in `sources`
- Keys are the form without U+FE0F (same as xprobe `emoji_identity` and browser-test-kit `route_trail.py`). If a variant with skin-tone or hair components is not in the table, it becomes the base emoji's name plus the component names (`thumbs up: medium skin tone`)
- The initial table was built from 287 kinds that appear in existing files of Flutter, browser-test-kit, web-ui, markdown, gh_identity, the parent, BlueProbe, Aoi, and others (xprobe's tally of 5,934 files). The 1 item without a name (♭ U+266D, a symbol, not an emoji) was not added
- Saved body file names follow the existing `Cache.put` rule (`blobs/<sha256>.html.gz`). The contents are the tarball bytes, verified the same way as other saved bodies

## License

MIT License ([LICENSE](LICENSE)). Same contents as Aoi's `LICENSE`.

---

# Fetching for NPB (Japanese professional baseball)

The following describes the sources used in Aoi's research cycle (Chunichi and NPB data).

## Additional fetching of annual team stats

`--source npb_team_annual --years 2013-2026` fetches both leagues' per-year team tables.
For 2005 onward it also fetches all listed columns of detailed win-loss, batting, pitching, and fielding, kept separate from the per-year summary tables.
The main target and pass criteria are 2013–2026. Years before 2013 are optional reference material, kept apart from regular data.
Verification and copying of the annual cache's main target (formerly `annual_scope.py`) is NPB-specific, so it moved to Aoi's `annual_scope.py`.
The dataset manifest also records `source_code_sha256` of the source module itself used for parsing.
Range, verification, and re-runs are in [c001's fetch and synthetic verification record](https://github.com/myon-bioinformatics/Aoi/blob/main/cycles/c001-chunichi/annual-history-synthetic.md).
