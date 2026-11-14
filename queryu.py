"""QueRyu — 再現できる取得が、確かな理解をつくる。

  Cache         : 生のページを gzip で保存し、取得ごとの来歴を manifest.jsonl に追記する
  PoliteFetcher : 取得元への負荷を抑えて取りに行く（キャッシュ優先・条件付きGET・間隔・直列）
  build()       : 取得元（BlueProbe の source）のページを集め、観測データセット(JSONL)を作る

同じファイルの道具（標準ライブラリのみ。Python からは queryu.<tool>.…(…)）:
  📥 python queryu.py --source NAME --years Y [--cache DIR] …     取得と観測データセット
  🗃️ python queryu.py share --cache DIR query|export|verify|import  キャッシュの検索・共有・照合
  😀 python queryu.py emoji --lookup TEXT [--names FILE] …          絵文字の名前（npm の Unicode データ）
  🧹 python queryu.py post -i EXTRACTED.json [--sort …] [--filter …]  抽出結果の並べ替え・絞り込み
キャッシュの最小 API（Cache・CacheIntegrityError）は core/cache_core.py（Aoi の配布物と BlueProbe も使う）。

来歴（manifest.jsonl の1行）:
  key, url, group, status, file, sha256, bytes, fetched_at, last_modified, etag, code_version
  304（更新なし）や 404（ページなし）も1行として残す。「確認した」こと自体が記録になる。

データセットは data/observations/<source>/<entity>.jsonl に置き、隣に .manifest.json を置く。
生のスコアを含むので data/ は git に入れない。
"""

from __future__ import annotations

import argparse, asyncio, base64, copy, datetime as dt, email.message, fnmatch, gzip, hashlib, io, json, math, os, re, subprocess, sys, tarfile, tempfile, threading, time, unicodedata, urllib.error, urllib.parse, urllib.request, zlib
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any

UA = "Aoi-QueRyu/0.1 (+https://github.com/myon-bioinformatics/Aoi; personal research)"


def code_version() -> str | None:
    """このコードの版（git のコミット）。測れなければ None（推測で埋めない）。"""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parent, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


if __name__ == "__main__":
    # runpy does not add the script directory; keep the existing CLI path contract.
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))

_CORE = str(Path(__file__).resolve().parent / "core")  # このプロジェクトの core/cache_core.py（後ろに足すので上書きしない）
if _CORE not in sys.path:
    sys.path.append(_CORE)
from cache_core import Cache as _Cache, CacheIntegrityError


class Cache(_Cache):
    """Original acquisition API; retain checkout provenance and timestamp hooks."""

    def _version(self) -> str | None:
        return self.source_version if self.source_version is not None else code_version()

    def _now(self) -> str:
        return now()


class OfflineMiss(RuntimeError):
    """オフラインモードで、キャッシュにないページを要求した。"""


def wait_seconds(value) -> float:
    wait = float(value)
    if not math.isfinite(wait) or wait < 0:
        raise ValueError("待ち時間は 0 以上の有限の秒数")
    return wait


class HTTPStatusError(RuntimeError):
    """4xx・5xx の応答（404 は「空」として扱うので PoliteFetcher からは出ない）。"""

    def __init__(self, response: "Response"):
        self.response = response
        super().__init__(f"HTTP {response.status_code}: {response.url}")


def _header_map(headers) -> email.message.Message:
    """大文字小文字を区別しないヘッダー（``.get("etag")`` で ``ETag`` が引ける）。"""
    msg = email.message.Message()
    for name, value in (headers.items() if headers is not None else ()):
        msg[name] = value
    return msg


class Request:
    """``HandlerClient`` の handler に渡す要求（url と、大文字小文字を区別しない headers）。"""

    def __init__(self, url: str, headers=None):
        self.url, self.headers = url, _header_map(headers or {})


class Response:
    """取得の結果。``status_code``・``content``（bytes）・``headers``・``raise_for_status()`` を持つ。"""

    def __init__(self, status_code: int, content: bytes = b"", headers=None, *, text: str | None = None, url: str = ""):
        self.status_code = int(status_code)
        self.content = text.encode("utf-8") if text is not None else bytes(content)
        self.headers = _header_map(headers)
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPStatusError(self)


class UrllibClient:
    """標準ライブラリ（urllib）だけの GET。304・404・5xx も例外にせず ``Response`` で返す。"""

    def __init__(self, headers=None, timeout: float = 30, opener=None):
        self.headers, self.timeout = dict(headers or {}), timeout
        # build_opener() は ProxyHandler（環境変数のプロキシ）と HTTPRedirectHandler を含む
        self.opener = opener or urllib.request.build_opener()

    def get(self, url: str, headers=None) -> Response:
        request = urllib.request.Request(url, headers={**self.headers, **dict(headers or {})}, method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as r:
                return Response(r.status, r.read(), r.headers, url=r.geturl())
        except urllib.error.HTTPError as error:
            with error:
                body = error.read() if error.fp is not None else b""
            return Response(error.code, body, error.headers, url=url)


class HandlerClient:
    """ネットワークに出ない client。``handler(Request) -> Response`` で応答を作る（テスト・再生用）。"""

    def __init__(self, handler):
        self.handler = handler

    def get(self, url: str, headers=None) -> Response:
        return self.handler(Request(url, headers))


class PoliteFetcher:
    def __init__(self, cache: Cache | None, wait: float = 3.0, *, offline: bool = False, client=None):
        """cache が None なら、取ったページはその実行の中だけで使い、どこにも書かない。"""
        if cache is None and offline:
            raise ValueError("オフラインで読むにはキャッシュが要る")
        self.cache, self.wait, self.offline = cache, wait_seconds(wait), offline
        self._client = client
        self.requests = 0
        self._latest = cache.latest() if cache is not None else {}

    @property
    def client(self):
        if self._client is None:
            # オフライン実行では作らない。urllib の既定の opener は環境変数のプロキシ設定とリダイレクトに従う
            self._client = UrllibClient(headers={"User-Agent": UA}, timeout=30)
        return self._client

    def get(self, key: str, url: str, group: str, *, live: bool = False) -> str:
        cached = self._latest.get(key)
        if cached and not live:
            return self.cache.body(cached)
        if self.offline:
            if cached:
                return self.cache.body(cached)
            raise OfflineMiss(f"{key} はキャッシュにない（オフライン）")

        headers = {}
        if cached and cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]
        if cached and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if self.requests:
            time.sleep(self.wait)
        self.requests += 1
        r = self.client.get(url, headers=headers)

        if r.status_code == 304 and cached:
            self.cache.record({"key": key, "url": url, "group": group, "status": 304,
                               "fetched_at": now(), "code_version": code_version()})
            return self.cache.body(cached)
        if r.status_code not in (200, 404):
            r.raise_for_status()
        content = r.content if r.status_code == 200 else b""  # 存在しないページも「空」として覚える
        if self.cache is not None:
            self._latest[key] = self.cache.put(key, url, group, r.status_code, content, r.headers)
        return content.decode("utf-8")


def build(source, page_list, fetcher: PoliteFetcher, live_groups=()) -> dict:
    """ページを集めて BlueProbe の source で読み、group ごとの結果を返す。"""
    from blueprobe import merge, new_report

    by_group: dict[str, dict] = {}
    for key, url, group in page_list:
        html = fetcher.get(key, url, group, live=group in live_groups)
        merge(by_group.setdefault(group, new_report()), source.parse(html, url) if html else new_report())
    return by_group


def write_dataset(path: Path, records: list[dict], meta: dict, rebuilt=None) -> None:
    """記録を key でマージして書き出し、隣に manifest を置く（同じ key は新しい方を採用）。

    rebuilt(record) -> bool: 今回すべて作り直した範囲か。既存の記録のうち、この範囲にあって今回の記録にないものは
    消す（解析ルールを直したあと、前の規則で採用した記録が残らないように）。消した件数は manifest の removed に残す。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    merged, new_keys, removed = {}, {r["key"] for r in records}, 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if rebuilt is not None and rebuilt(r) and r["key"] not in new_keys:
                removed += 1
                continue
            merged[r["key"]] = r
    for r in records:
        merged[r["key"]] = r
    rows = sorted(merged.values(), key=lambda r: (r.get("date", ""), r["key"]))
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    body = path.read_bytes()
    meta = {**meta, "records": len(rows), "removed": removed, "sha256": hashlib.sha256(body).hexdigest(),
            "generated_at": now(), "code_version": code_version()}
    path.with_suffix(".manifest.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _years(spec: str) -> list[int]:
    a, _, b = spec.partition("-")
    return list(range(int(a), int(b or a) + 1))


# どの key が「年・月」かは取得元ごとに違うので、呼び出し側が正規表現（名前付きの year・month）で渡す。
# 渡さなければ、どのページも未来の月として分けない。
DEFAULT_FROZEN = Path(__file__).with_name("frozen_seasons.json")


def parse_today(value: str | None) -> dt.date:
    if not value:
        return dt.date.today()
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"日付は YYYY-MM-DD: {value}") from exc


def load_frozen(path: Path) -> set[int]:
    """手動の凍結リスト。ファイルがなければ空（終了した年を推測で足さない）。"""
    path = Path(path)
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    seasons = data.get("seasons") if isinstance(data, dict) else None
    if not isinstance(seasons, list):
        raise ValueError('凍結リストは {"seasons": [年, ...]}（年は 1900〜2100 の整数）')
    validate_frozen(seasons)
    return set(seasons)


def validate_frozen(seasons) -> None:
    if any(type(year) is not int or not 1900 <= year <= 2100 for year in seasons):
        raise ValueError("凍結年は 1900〜2100 の整数")


def save_frozen(path: Path, seasons: set[int]) -> None:
    validate_frozen(seasons)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"seasons": sorted(seasons)}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def live_groups(years: list[int], today: dt.date, frozen: set[int]) -> set[str]:
    """今年以降で、凍結リストに無い年だけを再確認の対象にする。"""
    return {str(year) for year in years if year >= today.year and year not in frozen}


def month_key_pattern(value: str | re.Pattern | None) -> re.Pattern | None:
    """Compile a month-key regex; it must define the named groups ``year`` and ``month``.

    引用符なしで書ける形も受ける: ``YYYY_MM`` のように YYYY（4桁の年）と MM（2桁の月）を置いた見本。
    """
    if value is None or isinstance(value, re.Pattern) and {"year", "month"} <= set(value.groupindex):
        return value
    if "(?P<" not in value and "YYYY" in value and "MM" in value:
        value = re.escape(value).replace("YYYY", r"(?P<year>\d{4})", 1).replace("MM", r"(?P<month>\d{2})", 1)
    pattern = re.compile(value)
    if not {"year", "month"} <= set(pattern.groupindex):
        raise ValueError("月の key の正規表現には (?P<year>...) と (?P<month>...) が要る")
    return pattern


def split_future_months(pages, today: dt.date, month_key: str | re.Pattern | None = None):
    """まだ来ていない月のページを分ける。``month_key`` に当たらないページはそのまま残す。"""
    kept, future = [], []
    pattern = month_key_pattern(month_key)
    for page in pages:
        match = pattern.fullmatch(str(page[0])) if pattern is not None else None
        if match and (int(match["year"]), int(match["month"])) > (today.year, today.month):
            future.append(page)
        else:
            kept.append(page)
    return kept, future


def acquisition_plan(pages, cache: Cache | None, *, live: set[str], wait: float, today: dt.date,
                     frozen: set[int], future, offline: bool = False) -> dict:
    """キャッシュの来歴だけで、取得の内訳を数える。ネットワークには出ない。キャッシュなしなら全部が新規。"""
    wait = wait_seconds(wait)
    latest = cache.latest() if cache is not None else {}
    cached = conditional = unconditional = new = 0
    for key, _url, group in pages:
        if key in latest and (offline or group not in live):
            cached += 1
        elif key in latest and (latest[key].get("etag") or latest[key].get("last_modified")):
            conditional += 1
        elif key in latest:
            unconditional += 1
        else:
            new += 1
    requests = 0 if offline else conditional + unconditional + new
    gaps = max(requests - 1, 0)
    return {
        "today": today.isoformat(),
        "frozen": sorted(frozen),
        "live": [] if offline else sorted(live),
        "cached": cached,
        "conditional": conditional,
        "unconditional": unconditional,
        "new": new,
        "future": len(future),
        "requests": requests,
        "offline": offline,
        "offline_missing": new if offline else 0,
        "wait": wait,
        "gaps": gaps,
        "estimated_wait_seconds": wait * gaps,
    }


def format_plan(plan: dict) -> str:
    frozen = ", ".join(str(year) for year in plan["frozen"]) or "なし"
    return "\n".join([
        "観測:",
        f"  日付: {plan['today']}",
        f"  凍結シーズン: {frozen}",
        f"  再確認の対象年: {', '.join(plan['live']) or 'なし'}",
        f"  キャッシュ済み（再確認しない）: {plan['cached']}",
        f"  条件付きGET: {plan['conditional']}",
        f"  再確認（条件なし）: {plan['unconditional']}",
        f"  新規: {plan['new']}",
        f"  未来の月（取得しない）: {plan['future']}",
        f"  オフラインで不足するページ: {plan['offline_missing']}",
        "解釈:",
        f"  待ち時間の見積もり: {plan['estimated_wait_seconds']:g} 秒"
        f"（間隔 {plan['wait']:g} 秒を {plan['gaps']} 回足した値）",
    ])

# ──── share ───────────────────────────────────────────────────────────────────────────────────────────────
# Query, share and supply a QueRyu cache: stdlib only, next to cache_core.
#
# - ``query(cache, ...)``: select manifest rows by key/url glob, group, status and
#   fetched_at range; latest per key by default, every acquisition with ``history``.
# - ``export_bundle`` / ``import_bundle``: hand verified bodies and their manifest
#   rows to another checkout or person as one ``.tar`` (``queryu-bundle/1``), so
#   they extract without fetching again. Import re-verifies every body and never
#   overwrites an existing blob or row.
# - ``iter_snapshots`` / ``aiter_snapshots``: supply ``html-snapshot/1`` records to an
#   extractor (BlueProbe) synchronously or from asyncio, in query order.
# - ``fetch_many``: polite acquisition across several hosts at once: one request at
#   a time per host with the wait between them, hosts in parallel, every response
#   stored through ``Cache.put`` with provenance.

BUNDLE_SCHEMA = "queryu-bundle/1"


def _share_rows(cache: _Cache) -> list[dict]:
    if not cache.manifest.exists():
        return []
    return [json.loads(line) for line in cache.manifest.read_text(encoding="utf-8").splitlines() if line.strip()]


def _share_time(value):
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        stamp = value
    else:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def _share_query(cache: _Cache, *, key: str | None = None, url: str | None = None, group: str | None = None,
          status=None, since=None, until=None, history: bool = False, with_body: bool = True) -> list[dict]:
    """Manifest rows matching every given condition, in manifest order.

    ``key`` / ``url`` are shell globs (``npb_calendar:2024_*``), ``status`` an int or
    a collection of ints, ``since`` / ``until`` ISO times (inclusive) on
    ``fetched_at``. Without ``history`` only the latest row per key survives
    (after filtering by time, so ``until`` asks "as of then"). ``with_body`` keeps
    only rows that carry a body (status 200/404), as ``Cache.latest`` does.
    """
    statuses = None if status is None else ({status} if isinstance(status, int) else set(status))
    start, end = _share_time(since), _share_time(until)
    out = []
    latest = {}
    for position, row in enumerate(_share_rows(cache)):
        if with_body and row.get("status") not in (200, 404):
            continue
        if key is not None and not fnmatch.fnmatchcase(str(row.get("key", "")), key):
            continue
        if url is not None and not fnmatch.fnmatchcase(str(row.get("url", "")), url):
            continue
        if group is not None and row.get("group") != group:
            continue
        if statuses is not None and row.get("status") not in statuses:
            continue
        if start or end:
            fetched = _share_time(row.get("fetched_at")) if row.get("fetched_at") else None
            if fetched is None or (start and fetched < start) or (end and fetched > end):
                continue
        if history:
            out.append(row)
        else:
            latest[row["key"]] = (position, row)
    if history:
        return out
    return [row for _, row in sorted(latest.values(), key=lambda item: item[0])]


def export_bundle(cache: _Cache, entries: list[dict], path: Path) -> dict:
    """Write verified bodies and their rows to a new ``.tar`` (never overwrites ``path``).

    Each body is checked against its row before it is packed; a corrupt cache
    fails here rather than being handed on.
    """
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"bundle already exists: {path}")
    rows, files = [], {}
    for entry in entries:
        cache.body_bytes(entry)  # verify before handing on
        rows.append(entry)
        files[entry["file"]] = cache._body_path(entry["file"]).read_bytes()  # the stored gzip blob
    manifest = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
    header = json.dumps({"schema": BUNDLE_SCHEMA, "entries": len(rows), "blobs": len(files),
                         "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                         "source_version": cache.source_version}, ensure_ascii=False).encode("utf-8")
    temporary = path.with_name(path.name + ".partial")
    with tarfile.open(temporary, "x") as archive:
        for name, data in (("bundle.json", header), ("manifest.jsonl", manifest),
                           *sorted((f"blobs/{Path(f).name}", c) for f, c in files.items())):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            archive.addfile(info, io.BytesIO(data))
    temporary.rename(path)
    return {"schema": BUNDLE_SCHEMA, "path": str(path), "entries": len(rows), "blobs": len(files),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def import_bundle(cache: _Cache, path: Path) -> dict:
    """Add a bundle's bodies and rows to ``cache``; returns added / already_present counts.

    Every body is re-verified against its row (sha256 and length) before
    anything is written. Blobs are published without overwriting; a row whose
    key, sha256 and fetched_at are already present is skipped. Imported rows get
    ``imported_from`` (the bundle's SHA-256) and keep their original provenance.
    """
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        members = {m.name: m for m in archive.getmembers()}
        if any(not m.isfile() or m.name.startswith("/") or ".." in Path(m.name).parts for m in members.values()):
            raise CacheIntegrityError("bundle members must be plain relative files")
        header = json.loads(archive.extractfile(members["bundle.json"]).read())
        if header.get("schema") != BUNDLE_SCHEMA:
            raise CacheIntegrityError("not a queryu-bundle/1")
        rows = [json.loads(line) for line in archive.extractfile(members["manifest.jsonl"]).read()
                .decode("utf-8").splitlines() if line.strip()]
        bodies = {}
        for row in rows:
            name = f"blobs/{Path(row['file']).name}"
            if name not in members:
                raise CacheIntegrityError(f"bundle is missing {name}")
            blob = archive.extractfile(members[name]).read()
            try:
                content = gzip.decompress(blob)
            except (OSError, EOFError) as exc:
                raise CacheIntegrityError(f"bundle blob is not gzip: {name}") from exc
            if len(content) != row.get("bytes") or hashlib.sha256(content).hexdigest() != row.get("sha256"):
                raise CacheIntegrityError(f"bundle body does not match its row: {row.get('key')!r}")
            bodies[row["file"]] = blob
    seen = {(r.get("key"), r.get("sha256"), r.get("fetched_at")) for r in _share_rows(cache)}
    added = present = 0
    cache.root.mkdir(parents=True, exist_ok=True)
    for row in rows:
        target = cache._body_path(row["file"])
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as stream:
                stream.write(bodies[row["file"]])
        cache.body_bytes(row)  # the published blob must verify in this cache too
        identity = (row.get("key"), row.get("sha256"), row.get("fetched_at"))
        if identity in seen:
            present += 1
            continue
        cache.record({**row, "imported_from": digest})
        seen.add(identity)
        added += 1
    return {"added": added, "already_present": present, "bundle_sha256": digest}


def iter_snapshots(cache: _Cache, entries: list[dict]):
    """``html-snapshot/1`` records in the given order (verified by ``Cache.snapshot``)."""
    for entry in entries:
        yield cache.snapshot(entry)


async def aiter_snapshots(cache: _Cache, entries: list[dict], *, concurrency: int = 4):
    """The same records from asyncio: verification/decompression runs in threads, order kept."""
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    entries = list(entries)
    for start in range(0, len(entries), concurrency):
        batch = entries[start:start + concurrency]
        for snapshot in await asyncio.gather(*(asyncio.to_thread(cache.snapshot, e) for e in batch)):
            yield snapshot


def fetch_many(cache: _Cache, requests: list[tuple[str, str, str]], *, client, wait: float = 3.0,
               max_hosts: int = 4) -> list[dict]:
    """Fetch ``(key, url, group)`` rows: hosts in parallel, one request at a time per host.

    ``client`` is QueRyu's ``UrllibClient`` / ``HandlerClient`` (``get(url, headers)`` ->
    ``Response`` with ``status_code``, ``content``, ``headers``). Per host, requests run in the given order with ``wait``
    seconds between them, the same politeness as ``PoliteFetcher``; different hosts
    do not wait for each other. 200/404 bodies are stored with ``Cache.put``; other
    statuses are recorded without a body. Returns one row per request, in order.
    """
    if not isinstance(wait, (int, float)) or wait < 0 or wait != wait or wait == float("inf"):
        raise ValueError("wait must be a finite number >= 0")
    by_host: dict[str, list[int]] = {}
    for index, (_, url, _) in enumerate(requests):
        host = urllib.parse.urlsplit(url).netloc
        if not host:
            raise ValueError(f"absolute URL required: {url!r}")
        by_host.setdefault(host, []).append(index)
    results: list[dict | None] = [None] * len(requests)
    lock = threading.Lock()  # one manifest writer at a time; network stays outside the lock

    def run_host(indexes):
        for position, index in enumerate(indexes):
            if position:
                time.sleep(wait)
            key, url, group = requests[index]
            try:
                response = client.get(url, {})
                status, body = response.status_code, response.content
                headers = {k: response.headers.get(k) for k in ("last-modified", "etag") if response.headers.get(k)}
            except Exception as error:  # recorded, not hidden; other hosts continue
                results[index] = {"key": key, "url": url, "status": None, "error": type(error).__name__}
                continue
            with lock:
                if status in (200, 404):
                    results[index] = cache.put(key, url, group, status, body, headers)
                else:
                    row = {"key": key, "url": url, "group": group, "status": status,
                           "fetched_at": cache._now(), "code_version": cache._version()}
                    cache.record(row)
                    results[index] = row

    async def _run():
        semaphore = asyncio.Semaphore(max_hosts)

        async def one(indexes):
            async with semaphore:
                await asyncio.to_thread(run_host, indexes)
        await asyncio.gather(*(one(ix) for ix in by_host.values()))

    asyncio.run(_run())
    return results


def verify_cache(cache: _Cache, entries: list[dict] | None = None) -> dict:
    """Verify stored gzip bodies and recorded sha256/bytes for entries in cache.

    Calls ``cache.body_bytes(entry)`` for every entry referencing a body blob.
    Returns a dictionary summarizing total, verified, corrupted counts and error details.
    """
    if entries is None:
        entries = _share_query(cache, history=True)
    total = verified = corrupted = 0
    errors = []
    for entry in entries:
        if not entry.get("file"):
            continue
        total += 1
        try:
            cache.body_bytes(entry)
            verified += 1
        except (CacheIntegrityError, OSError) as error:
            corrupted += 1
            errors.append({
                "key": entry.get("key"),
                "file": entry.get("file"),
                "error": str(error),
            })
    return {
        "total": total,
        "verified": verified,
        "corrupted": corrupted,
        "errors": errors,
    }


def _share_main(argv=None) -> int:
    """``query`` prints rows; ``export`` bundles 200/404 rows; ``import`` adds one; ``verify`` checks integrity."""
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="queryu.py share", description="🗃️ Query, share and supply a QueRyu cache (stdlib only)")
    parser.add_argument("--cache", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("query", "export", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--key")
        command.add_argument("--url")
        command.add_argument("--group")
        command.add_argument("--status", type=int, action="append")
        command.add_argument("--since")
        command.add_argument("--until")
        command.add_argument("--history", action="store_true")
        if name == "export":
            command.add_argument("--out", required=True, type=Path)
    sub.add_parser("import").add_argument("bundle", type=Path)
    args = parser.parse_args(argv)
    cache = _Cache(args.cache)
    if args.command == "import":
        print(json.dumps(import_bundle(cache, args.bundle), ensure_ascii=False))
        return 0
    # query is an inspection path: it also lists rows without a body (304, 5xx, errors);
    # export packs bodies, so it keeps the 200/404 rows only;
    # verify checks body integrity.
    rows = _share_query(cache, key=args.key, url=args.url, group=args.group, status=args.status,
                 since=args.since, until=args.until,
                 history=args.history or args.command == "verify",
                 with_body=args.command in ("export", "verify"))
    if args.command == "query":
        for row in rows:
            sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
        return 0
    if args.command == "verify":
        result = verify_cache(cache, rows)
        print(json.dumps(result, ensure_ascii=False))
        if result["total"] == 0:
            # An empty selection is usually a typo in --key/--group; do not report it as a clean pass.
            print("error: verify selected no cached bodies; check --key/--url/--group/--status/--since/--until",
                  file=sys.stderr)
            return 3
        return 1 if result["corrupted"] > 0 else 0
    print(json.dumps(export_bundle(cache, rows, args.out), ensure_ascii=False))
    return 0


# ──── emoji ───────────────────────────────────────────────────────────────────────────────────────────────
# Emoji names from Unicode data acquired through npm, cached and accumulated for offline reuse.
#
# unicode.org is often unreachable from sandboxes and CI, while the npm registry is
# not. ``unicode-emoji-json`` republishes unicode.org's emoji-test.txt (CLDR short
# names, groups, versions) as JSON. This module:
#
# 1. acquires that tarball once (registry metadata -> tarball, sha512 integrity
#    checked) into a QueRyu ``Cache`` with provenance, or reads it back offline;
# 2. reads names, groups and versions from it (stdlib ``tarfile``/``json``);
# 3. accumulates the names of emoji actually used into a small committed JSON
#    (``emoji_names.json``) that keeps working with no network at all, and only
#    ever grows (entries are added, never dropped).
#
# Lookups prefer the accumulated table, then the cached package. A skin-tone or hair
# variant missing from both is named from its base and components
# (``thumbs up: medium skin tone``), the CLDR pattern.
#
#     python emoji_data.py --cache data/raw/npm --names emoji_names.json --use inventory.json
#     python emoji_data.py --cache data/raw/npm --offline --lookup TEXT
#     python emoji_data.py --names emoji_names.json --lookup TEXT        # no cache, no network

_EMOJI_REGISTRY = "https://registry.npmjs.org"
EMOJI_DEFAULT_PACKAGE = ("unicode-emoji-json", "0.9.0")
_EMOJI_MAX_BYTES = 20 * 1024 * 1024
_EMOJI_COMPONENTS = {"\U0001F3FB": "light skin tone", "\U0001F3FC": "medium-light skin tone",
              "\U0001F3FD": "medium skin tone", "\U0001F3FE": "medium-dark skin tone",
              "\U0001F3FF": "dark skin tone", "\U0001F9B0": "red hair", "\U0001F9B1": "curly hair",
              "\U0001F9B3": "white hair", "\U0001F9B2": "bald"}
_EMOJI_PACKAGE = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*\Z")
_EMOJI_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z")


def emoji_identity(sequence: str) -> str:
    """Comparison form without U+FE0F (as xprobe ``emoji_identity`` and route_trail)."""
    return sequence.replace("\ufe0f", "")


def _emoji_get(url: str, opener, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "queryu-emoji-data/1",
                                                   "Accept": "application/json, application/octet-stream"})
    with opener(request, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url}: larger than {limit} bytes")
    return data


def npm_tarball(cache: _Cache, package: str = EMOJI_DEFAULT_PACKAGE[0], version: str = EMOJI_DEFAULT_PACKAGE[1], *,
                offline: bool = False, opener=urllib.request.urlopen) -> tuple[bytes, dict]:
    """The package tarball and its manifest entry: from the cache, or fetched once.

    The exact version is required (no ``latest``), so a cache key names one
    immutable tarball. Fetching reads the registry's version metadata, accepts only
    a tarball on the registry host, and checks the sha512 ``integrity`` before
    storing. ``offline=True`` never touches the network and raises LookupError
    when the version was never cached.
    """
    if not _EMOJI_PACKAGE.match(package) or not _EMOJI_VERSION.match(version):
        raise ValueError("package must be an npm name and version an exact x.y.z")
    key = f"npm:{package}@{version}"
    entry = cache.latest().get(key)
    if entry is not None:
        return cache.body_bytes(entry), entry
    if offline:
        raise LookupError(f"{key} is not cached; run once with network access")
    meta = json.loads(_emoji_get(f"{_EMOJI_REGISTRY}/{urllib.parse.quote(package, safe='@')}/{version}", opener, 4 * 1024 * 1024))
    dist = meta.get("dist") or {}
    tarball, integrity = dist.get("tarball"), dist.get("integrity") or ""
    if urllib.parse.urlsplit(tarball or "").netloc != urllib.parse.urlsplit(_EMOJI_REGISTRY).netloc:
        raise ValueError(f"{key}: tarball is not on the registry host: {tarball!r}")
    if not integrity.startswith("sha512-"):
        raise ValueError(f"{key}: registry metadata has no sha512 integrity")
    data = _emoji_get(tarball, opener, _EMOJI_MAX_BYTES)
    if base64.b64encode(hashlib.sha512(data).digest()).decode() != integrity[7:]:
        raise ValueError(f"{key}: tarball does not match its sha512 integrity")
    entry = cache.put(key, tarball, "npm", 200, data, {})
    return data, {**entry, "integrity": integrity}


def emoji_records(tarball: bytes) -> dict[str, dict]:
    """identity -> {emoji, name, group, unicode_version, emoji_version, skin_tone_support}."""
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        member = archive.getmember("package/data-by-emoji.json")
        if not member.isfile() or member.size > _EMOJI_MAX_BYTES:
            raise ValueError("package/data-by-emoji.json is missing or too large")
        data = json.loads(archive.extractfile(member).read().decode("utf-8"))
    out = {}
    for emoji, row in data.items():
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ValueError(f"unexpected record for {emoji!r}")
        out[emoji_identity(emoji)] = {"emoji": emoji, "name": row["name"], "group": row.get("group"),
                                "unicode_version": row.get("unicode_version"),
                                "emoji_version": row.get("emoji_version"),
                                "skin_tone_support": bool(row.get("skin_tone_support"))}
    return out


def name_for(sequence: str, names: dict[str, str]) -> str | None:
    """CLDR-style name from ``names`` (identity -> name); variants named from base + components."""
    key = emoji_identity(sequence)
    if key in names:
        return names[key]
    parts = [_EMOJI_COMPONENTS[c] for c in key if c in _EMOJI_COMPONENTS]
    base = "".join(c for c in key if c not in _EMOJI_COMPONENTS).replace("\u200d\u200d", "\u200d").strip("\u200d")
    if parts and base in names:
        return names[base] + ": " + ", ".join(dict.fromkeys(parts))
    return None


def load_names(path: Path) -> dict:
    if not path.exists():
        return {"schema": "queryu.emoji-names/1", "sources": [], "names": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "queryu.emoji-names/1" or not isinstance(data.get("names"), dict):
        raise ValueError(f"{path}: not a queryu.emoji-names/1 table")
    return data


def accumulate(path: Path, identities, records: dict[str, dict], source: dict) -> dict:
    """Add names for ``identities`` to the committed table; never remove or rename entries.

    Returns ``added``, ``kept`` and ``unknown`` (no name in the package, even via
    base + components). The file is rewritten sorted, so diffs stay reviewable.
    """
    table = load_names(path)
    names = table["names"]
    package_names = {key: row["name"] for key, row in records.items()}
    added, kept, unknown = [], [], []
    for item in sorted({emoji_identity(i) for i in identities}):
        if item in names:
            kept.append(item)
            continue
        name = name_for(item, package_names)
        if name is None:
            unknown.append(item)
            continue
        names[item] = name
        added.append(item)
    if added and source not in table["sources"]:
        table["sources"].append(source)
    table["names"] = dict(sorted(names.items()))
    if added or not path.exists():
        path.write_text(json.dumps(table, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"added": added, "kept": kept, "unknown": unknown, "total": len(names)}


def _emoji_identities_from(path: Path) -> list[str]:
    """Identities from an xprobe ``--emoji`` inventory (xprobe.emoji-inventory.v1) or a JSON list."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("schema") == "xprobe.emoji-inventory.v1":
        return [row["identity"] for row in data["emoji"]]
    if isinstance(data, list) and all(isinstance(x, str) for x in data):
        return data
    raise ValueError(f"{path}: expected an xprobe emoji inventory or a JSON list of emoji")


def _emoji_main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="queryu.py emoji", description="😀 Emoji names from Unicode data acquired through npm, cached and accumulated for offline reuse.")
    ap.add_argument("--cache", type=Path, help="QueRyu cache directory for npm tarballs")
    ap.add_argument("--package", default="@".join(EMOJI_DEFAULT_PACKAGE), help="NAME@x.y.z (default: %(default)s)")
    ap.add_argument("--offline", action="store_true", help="use only the cache; never touch the network")
    ap.add_argument("--names", type=Path, help="accumulated table (queryu.emoji-names/1) to read and grow")
    ap.add_argument("--use", type=Path, help="add these emoji to --names: xprobe --emoji JSON or a JSON list")
    ap.add_argument("--all", action="store_true", help="add every emoji in the package to --names")
    ap.add_argument("--lookup", metavar="TEXT", help="print a name for each emoji in TEXT")
    args = ap.parse_args(argv)
    package, _, version = args.package.rpartition("@")
    records, source = {}, None
    try:
        if args.cache is not None:
            data, entry = npm_tarball(_Cache(args.cache), package, version, offline=args.offline)
            records = emoji_records(data)
            source = {"package": package, "version": version, "url": entry.get("url"),
                      "sha256": entry["sha256"], "fetched_at": entry.get("fetched_at")}
        if args.use or args.all:
            if not args.names or not records:
                ap.error("--use/--all need --names and --cache")
            report = accumulate(args.names, list(records) if args.all else _emoji_identities_from(args.use),
                                records, source)
            print(f"emoji names: +{len(report['added'])} kept {len(report['kept'])} "
                  f"unknown {len(report['unknown'])} total {report['total']}")
            for item in report["unknown"]:
                print(f"  unknown {item} " + " ".join("U+%04X" % ord(c) for c in item))
        if args.lookup is not None:
            names = {k: r["name"] for k, r in records.items()}
            if args.names:
                names.update(load_names(args.names)["names"])
            found = 0
            for token in _emoji_sequences(args.lookup):
                name = name_for(token, names)
                found += name is not None
                print(f"{token}\t{name or '(unknown)'}")
            return 0 if found else 1
    except (LookupError, ValueError, OSError) as error:
        print(f"emoji_data: {error}", file=sys.stderr)
        return 2
    return 0


def _emoji_sequences(text: str) -> list[str]:
    """Rough split for --lookup: runs joined by ZWJ, VS16, modifiers, tags, keycap and flags."""
    out, current = [], ""
    glue = {"\u200d", "\ufe0f", "\u20e3"} | set(_EMOJI_COMPONENTS)
    for ch in text:
        regional = "\U0001F1E6" <= ch <= "\U0001F1FF"
        tag = "\U000E0020" <= ch <= "\U000E007F"
        if current and (ch in glue or tag or current.endswith("\u200d")
                        or (regional and len(current) == 1 and "\U0001F1E6" <= current <= "\U0001F1FF")):
            current += ch
            continue
        if current:
            out.append(current)
        # (c) and (R) are the only Extended_Pictographic code points below U+2000.
        current = ch if (ord(ch) > 0x2000 or ch in "#*0123456789\u00a9\u00ae") and not ch.isspace() else ""
    if current:
        out.append(current)
    return [s for s in out if not (len(s) == 1 and s in "#*0123456789")]


# ──── post ────────────────────────────────────────────────────────────────────────────────────────────────
# 抽出が終わったあとの加工。
#
# 並べ替え（alphabetical / numeric）と、テキスト条件による絞り込みだけを持つ。
# ページの読み取り、DOM パターン表、パターン検出、本文の抽出は BlueProbe が持つ。
# パターンが要るときは ``from blueprobe import detect_pattern, get_pattern`` で読む。
# blueprobe はパッケージではないので、``from blueprobe import ...`` のままにする。
# 検出できない、未知のキー、数値にできない値は、ここで握りつぶさずに呼び出し元へ返す。

POST_SCHEMA = "queryu-pattern-postprocess/1"
_POST_SORT_MODES = {"alphabetical", "numeric"}
_POST_FILTER_OPS = {"contains", "equals", "startswith", "endswith", "regex", "not_contains"}


def pattern_for_html(html: str, pattern_key: str | None = None) -> dict[str, Any]:
    from blueprobe import detect_pattern, get_pattern  # パターン判定だけ BlueProbe に任せる（遅延 import）
    """BlueProbe にパターンを聞く。一致しない、または未知のキーは例外のまま返す。"""
    if not isinstance(html, str):
        raise TypeError("html は文字列である必要があります")
    if pattern_key not in (None, "", "auto"):
        pattern = get_pattern(pattern_key)
        return {
            "pattern_key": pattern["key"],
            "selector": pattern["selector"],
            "label": pattern["label"],
            "matched_nodes": None,
        }
    matches = detect_pattern(html)
    if not matches:
        raise ValueError("DOM パターンが一致しません")
    top = matches[0]
    pattern = top.get("pattern")
    if not isinstance(pattern, dict) or "key" not in pattern:
        raise ValueError("BlueProbe の検出結果に pattern.key がありません")
    return {
        "pattern_key": pattern["key"],
        "selector": top.get("selector_used") or pattern.get("selector"),
        "label": pattern.get("label", ""),
        "matched_nodes": top.get("matched_nodes"),
    }


def _post_require_text(value: Any, label: str) -> str:
    if value is None:
        raise ValueError(f"{label} が null です")
    if not isinstance(value, str):
        raise TypeError(f"{label} は文字列である必要があります")
    return value


def _alphabetical_key(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _numeric_key(text: str) -> Decimal:
    normalized = unicodedata.normalize("NFKC", text).strip()
    try:
        number = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"数値として並べられません: {text!r}") from exc
    if not number.is_finite():
        raise ValueError(f"数値として並べられません: {text!r}")
    return number


def sort_text(values: list[Any], mode: str) -> list[str]:
    """文字列を alphabetical（NFKC + casefold）または numeric（有限の数）で安定ソートする。"""
    if mode not in _POST_SORT_MODES:
        raise ValueError(f"未知の並べ替えです: {mode!r}")
    texts = [_post_require_text(value, "text") for value in values]
    if mode == "alphabetical":
        return sorted(texts, key=_alphabetical_key)
    return sorted(texts, key=_numeric_key)


def _condition_holds(text: str, condition: dict[str, Any]) -> bool:
    if not isinstance(condition, dict):
        raise TypeError("絞り込み条件はオブジェクトである必要があります")
    op = condition.get("op")
    if op not in _POST_FILTER_OPS:
        raise ValueError(f"未知の絞り込みです: {op!r}")
    expected = _post_require_text(condition.get("value"), "value")
    folded = bool(condition.get("casefold", False))
    left = _alphabetical_key(text) if folded else text
    right = _alphabetical_key(expected) if folded else expected
    if op == "contains":
        return right in left
    if op == "not_contains":
        return right not in left
    if op == "equals":
        return left == right
    if op == "startswith":
        return left.startswith(right)
    if op == "endswith":
        return left.endswith(right)
    flags = re.IGNORECASE if folded else 0
    try:
        return re.search(expected, text, flags) is not None
    except re.error as exc:
        raise ValueError(f"正規表現が不正です: {expected!r}") from exc


def filter_text(values: list[Any], conditions: list[dict[str, Any]]) -> list[str]:
    """条件をすべて満たす文字列だけ残す。条件が空なら並びを変えずにコピーする。"""
    texts = [_post_require_text(value, "text") for value in values]
    if not conditions:
        return list(texts)
    return [text for text in texts if all(_condition_holds(text, condition) for condition in conditions)]


def _post_cell(row: list[Any], column: int) -> str:
    if column < 0 or column >= len(row):
        raise ValueError(f"列 {column} は行の範囲外です（長さ {len(row)}）")
    return _post_require_text(row[column], f"列 {column}")


def _post_copy_table(rows: list[Any]) -> list[list[Any]]:
    copied = []
    for row in rows:
        if not isinstance(row, list):
            raise TypeError("行は配列である必要があります")
        copied.append(list(row))
    return copied


def sort_table(rows: list[list[Any]], *, column: int, mode: str, header: bool = True) -> list[list[Any]]:
    """表を並べ替える。header が真のときは先頭行をヘッダとして動かさない。"""
    if mode not in _POST_SORT_MODES:
        raise ValueError(f"未知の並べ替えです: {mode!r}")
    table = _post_copy_table(rows)
    if not table:
        return []
    body_start = 1 if header else 0
    head, body = table[:body_start], table[body_start:]
    if mode == "alphabetical":
        ordered = sorted(body, key=lambda row: _alphabetical_key(_post_cell(row, column)))
    else:
        ordered = sorted(body, key=lambda row: _numeric_key(_post_cell(row, column)))
    return [*head, *ordered]


def filter_table(
    rows: list[list[Any]], *, column: int, conditions: list[dict[str, Any]], header: bool = True,
) -> list[list[Any]]:
    """テキスト条件でデータ行を残す。header が真のときは先頭行を残す。"""
    table = _post_copy_table(rows)
    if not table:
        return []
    body_start = 1 if header else 0
    head, body = table[:body_start], table[body_start:]
    if not conditions:
        return table
    kept = [row for row in body if all(_condition_holds(_post_cell(row, column), condition) for condition in conditions)]
    return [*head, *kept]


def _post_process(
    payload: dict[str, Any],
    *,
    sort: str | None = None,
    column: int = 0,
    header: bool = True,
    conditions: list[dict[str, Any]] | None = None,
    html: str | None = None,
    pattern_key: str | None = None,
) -> dict[str, Any]:
    """text と tables を絞り込み、並べ替える。html またはパターンキーがあれば BlueProbe に聞く。"""
    if not isinstance(payload, dict):
        raise TypeError("入力はオブジェクトである必要があります")
    conditions = list(conditions or [])
    text = payload.get("text", [])
    tables = payload.get("tables", [])
    if not isinstance(text, list) or not isinstance(tables, list):
        raise TypeError("text と tables は配列である必要があります")
    if conditions:
        text = filter_text(text, conditions)
    else:
        text = list(text)
    if sort is not None:
        text = sort_text(text, sort)

    processed_tables = []
    for table in tables:
        if not isinstance(table, list):
            raise TypeError("tables の各要素は行の配列である必要があります")
        rows = _post_copy_table(table)
        if conditions:
            rows = filter_table(rows, column=column, conditions=conditions, header=header)
        if sort is not None:
            rows = sort_table(rows, column=column, mode=sort, header=header)
        processed_tables.append(rows)

    html_value = html if html is not None else payload.get("html")
    key = pattern_key if pattern_key is not None else payload.get("pattern_key")
    pattern = None
    if isinstance(html_value, str):
        pattern = pattern_for_html(html_value, key)
    elif html_value is not None:
        raise TypeError("html は文字列である必要があります")
    elif key not in (None, "", "auto"):
        from blueprobe import get_pattern
        found = get_pattern(key)
        pattern = {
            "pattern_key": found["key"],
            "selector": found["selector"],
            "label": found["label"],
            "matched_nodes": None,
        }

    return {"schema": POST_SCHEMA, "pattern": pattern, "text": text, "tables": processed_tables}


def parse_filter(spec: str) -> dict[str, str]:
    """``op=value`` を条件にする。value 側の ``=`` はそのまま残す。"""
    if not isinstance(spec, str) or "=" not in spec:
        raise ValueError(f"絞り込みは op=value です: {spec!r}")
    op, value = spec.split("=", 1)
    op = op.strip()
    if op not in _POST_FILTER_OPS:
        raise ValueError(f"未知の絞り込みです: {op!r}")
    return {"op": op, "value": value}


def _post_main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="queryu.py post", description="🧹 抽出結果の並べ替えとテキスト条件による絞り込み")
    ap.add_argument("--input", "-i", type=Path, required=True, help="text と tables を持つ JSON")
    ap.add_argument("--out", "-o", type=Path, help="書き出し先。省略時は標準出力")
    ap.add_argument("--sort", choices=sorted(_POST_SORT_MODES), help="alphabetical または numeric")
    ap.add_argument("--column", type=int, default=0, help="表を加工する列（0 始まり）")
    ap.add_argument("--header", action=argparse.BooleanOptionalAction, default=True, help="表の先頭行をヘッダとして残す")
    ap.add_argument("--filter", action="append", default=[], help="op=value。複数指定はすべて満たす行だけ残す")
    ap.add_argument("--casefold", action="store_true", help="絞り込みの比較で大文字小文字を畳む")
    ap.add_argument("--html", type=Path, help="パターン判定だけ BlueProbe に渡す HTML")
    ap.add_argument("--pattern", default=None, help="パターンキー。省略時、HTML があるときだけ自動判定")
    args = ap.parse_args(argv)

    if not args.input.is_file():
        ap.error(f"ファイルが存在しません: {args.input}")
    raw = args.input.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        ap.error(f"JSON として読めません: {exc}")

    html = None
    if args.html is not None:
        if not args.html.is_file():
            ap.error(f"ファイルが存在しません: {args.html}")
        html = args.html.read_text(encoding="utf-8")
    try:
        conditions = [parse_filter(spec) for spec in args.filter]
        if args.casefold:
            conditions = [{**condition, "casefold": True} for condition in conditions]
        result = _post_process(
            payload, sort=args.sort, column=args.column, header=args.header,
            conditions=conditions, html=html, pattern_key=args.pattern,
        )
    except (ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    out_json = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(out_json, encoding="utf-8")
    else:
        print(out_json)
    return 0


# ──── tools: python queryu.py <tool> … / queryu.<tool>.…(…) ──────────────────────────────
share = SimpleNamespace(query=_share_query, export_bundle=export_bundle, import_bundle=import_bundle, iter_snapshots=iter_snapshots, aiter_snapshots=aiter_snapshots, fetch_many=fetch_many, verify_cache=verify_cache, main=_share_main)
emoji = SimpleNamespace(identity=emoji_identity, npm_tarball=npm_tarball, emoji_records=emoji_records, name_for=name_for, load_names=load_names, accumulate=accumulate, main=_emoji_main)
post = SimpleNamespace(process=_post_process, pattern_for_html=pattern_for_html, sort_text=sort_text, filter_text=filter_text, sort_table=sort_table, filter_table=filter_table, parse_filter=parse_filter, main=_post_main)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    tool = globals().get(argv[0]) if argv else None
    if isinstance(tool, SimpleNamespace):  # share / emoji / post
        return tool.main(argv[1:])
    ap = argparse.ArgumentParser(description="📥 取得元からページを集め、観測データセットを作る",
                                 epilog="🧰 tools: 🗃️ share · 😀 emoji · 🧹 post（python queryu.py <tool> --help）")
    ap.add_argument("--source", required=True)
    ap.add_argument("--years", required=True, help="例: 2012-2025")
    ap.add_argument("--cache", type=Path, default=None,
                    help="生ページのキャッシュ。省略時は取ったページをこの実行の中だけで使い、何も書かない")
    ap.add_argument("--out", type=Path, default=None, help="観測データセット。--plan では使わない")
    ap.add_argument("--wait", type=wait_seconds, default=3.0, help="リクエスト間隔（0以上の有限の秒数）")
    ap.add_argument("--offline", action="store_true", help="キャッシュだけで作る。ネットワークに出ない")
    ap.add_argument("--today", default=None, help="未来の月の判定に使う日付（YYYY-MM-DD）。省略すると実行日")
    ap.add_argument("--month-key", default=None,
                    help="月のページの key。YYYY_MM のような見本（引用符なしで書ける）か、名前付きの year・month を持つ正規表現。"
                         "省略時は未来の月を分けない")
    ap.add_argument("--frozen", type=Path, default=None, help="手動の凍結リスト JSON。省略時は queryu/frozen_seasons.json")
    ap.add_argument("--freeze", action="append", default=[], type=int, help="この年を凍結リストに足す。--plan では書き出さない")
    ap.add_argument("--plan", action="store_true", help="取得せず、キャッシュ済み・条件付きGET・新規の件数と待ち時間の見積もりを出す")
    args = ap.parse_args(argv)
    if args.offline and args.cache is None:
        ap.error("--offline は --cache と使う（読むキャッシュがない）")
    if not args.plan and args.out is None:
        ap.error("--out が要る")

    try:
        from blueprobe import NORMALIZATION, load_source
    except ModuleNotFoundError as exc:
        if exc.name != "blueprobe":
            raise
        ap.exit(2, "BlueProbe が必要です。Aoi では python aoi.py queryu …（aoi.py が場所を入れる）、単独では BlueProbe の checkout を PYTHONPATH に渡してください。\n")

    source = load_source(args.source)
    years = _years(args.years)
    if not years:
        ap.error("--years の終了年は開始年以上にする")
    today = parse_today(args.today)
    try:
        month_key = month_key_pattern(args.month_key)
    except (re.error, ValueError) as exc:
        ap.error(f"--month-key: {exc}")
    frozen_path = args.frozen or DEFAULT_FROZEN
    try:
        validate_frozen(args.freeze)
        frozen = load_frozen(frozen_path)
    except ValueError as exc:
        ap.error(str(exc))
    frozen |= set(args.freeze)
    if args.freeze and not args.plan:
        save_frozen(frozen_path, frozen)
    live = live_groups(years, today, frozen)
    pages, future = split_future_months(source.pages(years), today, month_key)
    if args.plan:
        cache = Cache(args.cache) if args.cache is not None else None
        print(format_plan(acquisition_plan(pages, cache, live=live, wait=args.wait,
                                           today=today, frozen=frozen, future=future, offline=args.offline)))
        return 0
    fetcher = PoliteFetcher(Cache(args.cache) if args.cache is not None else None, args.wait, offline=args.offline)
    groups = build(source, pages, fetcher, live)

    records, counts, unknown = [], {}, 0
    for g, r in sorted(groups.items()):
        parts = [f"records={len(r['records'])}", *(f"{k}={v}" for k, v in sorted(r["counts"].items())),
                 f"unknown={len(r['unknown'])}"]
        print(f"{g}: " + " ".join(parts))
        records += r["records"]
        for k, v in r["counts"].items():
            counts[k] = counts.get(k, 0) + v
        unknown += len(r["unknown"])
    print(f"HTTP requests: {fetcher.requests}")
    if hasattr(source, "check"):
        for row in source.check(records):
            print(row["text"])

    # 指定した年はキャッシュからすべて作り直している。範囲を記録から言えない取得元なら、ファイル全体を作り直す
    scope = {str(y) for y in years}
    rebuilt = (lambda r: source.group(r) in scope) if hasattr(source, "group") else (lambda r: True)
    write_dataset(args.out, records, {
        "source": args.source, "scope": {"years": [years[0], years[-1]]},
        "source_code_sha256": hashlib.sha256(Path(source.__file__).read_bytes()).hexdigest(),
        "normalization": NORMALIZATION, "dropped": counts, "unknown": unknown,
        "cache_manifest": str(args.cache / "manifest.jsonl") if args.cache is not None else None,
    }, rebuilt=rebuilt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
