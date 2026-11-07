"""QueRyu — 再現できる取得が、確かな理解をつくる。

  Cache         : 生のページを gzip で保存し、取得ごとの来歴を manifest.jsonl に追記する
  PoliteFetcher : 取得元への負荷を抑えて取りに行く（キャッシュ優先・条件付きGET・間隔・直列）
  build()       : 取得元（BlueProbe の source）のページを集め、観測データセット(JSONL)を作る

来歴（manifest.jsonl の1行）:
  key, url, group, status, file, sha256, bytes, fetched_at, last_modified, etag, code_version
  304（更新なし）や 404（ページなし）も1行として残す。「確認した」こと自体が記録になる。

データセットは data/observations/<source>/<entity>.jsonl に置き、隣に .manifest.json を置く。
生のスコアを含むので data/ は git に入れない。
"""

from __future__ import annotations

import argparse, copy, datetime as dt, email.message, gzip, hashlib, importlib.util, json, math, os, re, subprocess, sys, tempfile, time, urllib.error, urllib.request, zlib
from pathlib import Path

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


def aoi_sibling_path(here: Path, module: str, sibling: str = "blueprobe") -> None:
    """``module`` が import できないときだけ、Aoi の checkout の中にある隣の ``sibling/`` を ``sys.path`` に足す。

    単独の QueRyu では足さない（BlueProbe は最新 main の checkout を PYTHONPATH で渡す）。
    ``../blueprobe`` のような相対の隣を読むのは、親が Aoi（pyproject.toml と cycles/ がある）のときに限る。
    """
    if importlib.util.find_spec(module) is not None:
        return
    root = here.resolve().parent
    candidate = root / sibling
    if (root / "pyproject.toml").is_file() and (root / "cycles").is_dir() and candidate.is_dir():
        sys.path.insert(0, str(candidate))


class PoliteFetcher:
    def __init__(self, cache: Cache, wait: float = 3.0, *, offline: bool = False, client=None):
        self.cache, self.wait, self.offline = cache, wait_seconds(wait), offline
        self._client = client
        self.requests = 0
        self._latest = cache.latest()

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
    """Compile a month-key regex; it must define the named groups ``year`` and ``month``."""
    if value is None or isinstance(value, re.Pattern) and {"year", "month"} <= set(value.groupindex):
        return value
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


def acquisition_plan(pages, cache: Cache, *, live: set[str], wait: float, today: dt.date,
                     frozen: set[int], future, offline: bool = False) -> dict:
    """キャッシュの来歴だけで、取得の内訳を数える。ネットワークには出ない。"""
    wait = wait_seconds(wait)
    latest = cache.latest()
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


def main(argv=None) -> int:
    from blueprobe import NORMALIZATION, load_source

    ap = argparse.ArgumentParser(description="取得元からページを集め、観測データセットを作る")
    ap.add_argument("--source", required=True)
    ap.add_argument("--years", required=True, help="例: 2012-2025")
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None, help="観測データセット。--plan では使わない")
    ap.add_argument("--wait", type=wait_seconds, default=3.0, help="リクエスト間隔（0以上の有限の秒数）")
    ap.add_argument("--offline", action="store_true", help="キャッシュだけで作る。ネットワークに出ない")
    ap.add_argument("--today", default=None, help="未来の月の判定に使う日付（YYYY-MM-DD）。省略すると実行日")
    ap.add_argument("--month-key", default=None,
                    help="月のページの key の正規表現（名前付きの year・month）。省略時は未来の月を分けない")
    ap.add_argument("--frozen", type=Path, default=None, help="手動の凍結リスト JSON。省略時は queryu/frozen_seasons.json")
    ap.add_argument("--freeze", action="append", default=[], type=int, help="この年を凍結リストに足す。--plan では書き出さない")
    ap.add_argument("--plan", action="store_true", help="取得せず、キャッシュ済み・条件付きGET・新規の件数と待ち時間の見積もりを出す")
    args = ap.parse_args(argv)
    if not args.plan and args.out is None:
        ap.error("--out が要る")

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
        print(format_plan(acquisition_plan(pages, Cache(args.cache), live=live, wait=args.wait,
                                           today=today, frozen=frozen, future=future, offline=args.offline)))
        return 0
    fetcher = PoliteFetcher(Cache(args.cache), args.wait, offline=args.offline)
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
        "cache_manifest": str(args.cache / "manifest.jsonl"),
    }, rebuilt=rebuilt)
    return 0


if __name__ == "__main__":
    aoi_sibling_path(here, "blueprobe")
    raise SystemExit(main())
