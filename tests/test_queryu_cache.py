"""🗃️ キャッシュ: 元のバイトの照合と不変の履歴、標準ライブラリの urllib クライアント、
share（query・bundle・snapshot・fetch_many・verify）、最新だけを持つ query の保持（計測スクリプトの検査は tests/test_fleet.py）。

ネットワークは 127.0.0.1 の一時サーバーだけ。NumPy も取得元のページも使わない（cache-test: docs/nano-cache-tests.md）。
`python -S -m unittest discover -s tests -p test_queryu_cache.py` では unittest.TestCase のクラスだけが動く。
"""
import asyncio, gzip, hashlib, io, json, os, shutil, subprocess, sys, tarfile, threading, time, unittest, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

try:
    import pytest
except ImportError:  # python -S（site-packages なし）: 下の unittest.TestCase だけが動く
    class _NoPytest:  # pytest 形式のテストは定義だけされ、unittest には集められない
        def __getattr__(self, name): return self
        def __call__(self, *args, **kwargs):
            return args[0] if len(args) == 1 and not kwargs and callable(args[0]) else self
    pytest = _NoPytest()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # python -S -m unittest でもこの checkout の queryu を読む（queryu が core/ を足す）
import queryu
from cache_core import Cache as CoreCache  # share は配布用の最小 Cache（git の版を測らない）で確かめる
from queryu import Cache, CacheIntegrityError, HandlerClient, HTTPStatusError, PoliteFetcher, Response, UrllibClient
from queryu import share as cs


# ──── core: the distributable core works under python -S outside any checkout ─────────────────────────────

def test_cache_core_runs_alone_and_rejects_tampering(tmp_path):
    source = ROOT / 'core' / 'cache_core.py'
    shutil.copyfile(source, tmp_path / 'cache_core.py')
    code = '''
from pathlib import Path
from cache_core import Cache, CacheIntegrityError
c = Cache(Path('cache'))
e = c.put('k', 'https://example.invalid/k', 'g', 200, b'<p>raw</p>', {})
assert e['code_version'] is None
snapshot = c.snapshot(e)
assert snapshot['html'] == '<p>raw</p>'
assert snapshot['response_sha256'] == e['sha256']
assert Cache(Path('cache'), source_version='a' * 40)._version() == 'a' * 40
p = c.root / e['file']
p.write_bytes(b'corrupt')
try:
    c.snapshot(e)
except CacheIntegrityError:
    pass
else:
    raise AssertionError('tampered body accepted')
'''
    out = subprocess.run([sys.executable, '-I', '-S', '-c',
                          "import sys; sys.path.insert(0, '.');" + code],
                         cwd=tmp_path, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


# ──── snapshots: verified, immutable cache evidence; all fixtures are local and offline ───────────────────

def put(cache, content=b"<p>old</p>", key="page", **kwargs):
    return cache.put(key, kwargs.get("url", "https://example.test/page"), "season", 200,
                     content, {"etag": '"v1"', "last-modified": "yesterday"})


def legacy(cache, content=b"<p>legacy</p>"):
    cache.root.mkdir(parents=True, exist_ok=True)
    entry = {"key": "page", "status": 200, "file": "page.html.gz",
             "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
    (cache.root / entry["file"]).write_bytes(gzip.compress(content))
    cache.record(entry)
    return entry


def test_each_acquisition_keeps_its_own_body_and_manifest(tmp_path):
    cache = Cache(tmp_path)
    first = put(cache)
    second = put(cache, b"<p>new</p>")
    assert first["file"] != second["file"]
    assert cache.body(first) == "<p>old</p>"
    assert cache.body(second) == "<p>new</p>"
    assert cache.latest()["page"] == second
    assert list(map(json.loads, cache.manifest.read_text().splitlines())) == [first, second]


def test_identical_bodies_deduplicate_without_deduplicating_acquisitions(tmp_path):
    cache = Cache(tmp_path)
    first = put(cache, key="../../do-not-use-as-a-path")
    compressed = (tmp_path / first["file"]).read_bytes()
    inode = (tmp_path / first["file"]).stat().st_ino
    second = put(cache, key="/another/key")
    third = put(cache, key="/another/key")
    assert first["file"] == second["file"] == third["file"]
    assert first["file"] == f"blobs/{first['sha256']}.html.gz"
    assert (tmp_path / first["file"]).read_bytes() == compressed
    assert (tmp_path / first["file"]).stat().st_ino == inode
    assert len(list((tmp_path / "blobs").iterdir())) == 1
    assert len(cache.manifest.read_text().splitlines()) == 3


@pytest.mark.parametrize("field,value", [
    ("sha256", None), ("sha256", ""), ("sha256", "A" * 64), ("sha256", "g" * 64),
    ("sha256", "0" * 63), ("sha256", "0" * 65), ("sha256", 123),
    ("bytes", None), ("bytes", -1), ("bytes", True), ("bytes", False),
    ("bytes", 3.0), ("bytes", "3"),
])
def test_malformed_recorded_metadata_is_rejected(tmp_path, field, value):
    cache = Cache(tmp_path)
    entry = put(cache)
    entry[field] = value
    with pytest.raises(CacheIntegrityError, match=field):
        cache.body(entry)


@pytest.mark.parametrize("field", ["sha256", "bytes"])
def test_missing_legacy_metadata_cannot_be_backfilled_from_body(tmp_path, field):
    cache = Cache(tmp_path)
    entry = legacy(cache)
    del entry[field]
    before = (tmp_path / entry["file"]).read_bytes()
    with pytest.raises(CacheIntegrityError, match=field):
        cache.snapshot(entry)
    assert (tmp_path / entry["file"]).read_bytes() == before


@pytest.mark.parametrize("replacement,error", [(b"\xff" * 10, "sha256"), (b"\xff", "bytes")])
def test_raw_integrity_checked_before_utf8_decode(tmp_path, replacement, error):
    cache = Cache(tmp_path)
    entry = put(cache, b"0123456789")
    (tmp_path / entry["file"]).write_bytes(gzip.compress(replacement))
    for reader in (cache.body_bytes, cache.body, cache.snapshot):
        with pytest.raises(CacheIntegrityError, match=error):
            reader(entry)


def test_corrupt_existing_blob_is_never_repaired_or_recorded(tmp_path):
    cache = Cache(tmp_path)
    entry = put(cache)
    path = tmp_path / entry["file"]
    path.write_bytes(b"not gzip")
    manifest = cache.manifest.read_bytes()
    with pytest.raises(CacheIntegrityError, match="gzip"):
        put(cache)
    assert path.read_bytes() == b"not gzip"
    assert cache.manifest.read_bytes() == manifest
    assert not list(path.parent.glob(".blob-*"))


def test_atomic_publish_loser_verifies_winning_blob(tmp_path, monkeypatch):
    cache = Cache(tmp_path)
    real_link = os.link
    observed = []

    def competing_link(source, destination):
        observed.append(gzip.decompress(Path(source).read_bytes()))
        real_link(source, destination)
        raise FileExistsError("another writer already published")

    monkeypatch.setattr(os, "link", competing_link)
    entry = put(cache)
    assert observed == [b"<p>old</p>"]
    assert cache.body(entry) == "<p>old</p>"
    assert len(list((tmp_path / "blobs").iterdir())) == 1


def test_atomic_publish_rejects_corrupt_racing_blob(tmp_path, monkeypatch):
    cache = Cache(tmp_path)

    def competing_link(source, destination):
        Path(destination).write_bytes(gzip.compress(b"unexpected"))
        raise FileExistsError("another writer already published")

    monkeypatch.setattr(os, "link", competing_link)
    with pytest.raises(CacheIntegrityError):
        put(cache)
    assert not cache.manifest.exists()
    assert not list((tmp_path / "blobs").glob(".blob-*"))


def test_failed_publish_cleans_temporary_without_acquisition(tmp_path, monkeypatch):
    cache = Cache(tmp_path)

    def fail_link(*args):
        raise OSError("publication failed")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(OSError, match="publication failed"):
        put(cache)
    assert not cache.manifest.exists()
    assert not list((tmp_path / "blobs").iterdir())


def test_valid_legacy_survives_new_put_without_migration(tmp_path):
    cache = Cache(tmp_path)
    old = legacy(cache)
    original = (tmp_path / old["file"]).read_bytes()
    new = put(cache, b"new")
    assert new["file"].startswith("blobs/")
    assert cache.body(old) == "<p>legacy</p>"
    assert (tmp_path / old["file"]).read_bytes() == original
    assert cache.body(new) == "new"


def test_overwritten_legacy_fails_offline_without_network(tmp_path):
    cache = Cache(tmp_path)
    entry = legacy(cache)
    (tmp_path / entry["file"]).write_bytes(gzip.compress(b"<p>edited</p>"))

    class NoNetwork:
        def get(self, *args, **kwargs):
            pytest.fail("cache integrity failure must not cause a network fallback")

    fetcher = PoliteFetcher(cache, offline=True, client=NoNetwork())
    with pytest.raises(CacheIntegrityError, match="sha256"):
        fetcher.get("page", "https://example.test/page", "season")


@pytest.mark.parametrize("file", ["../outside.gz", "sub/../../outside.gz", "/tmp/outside.gz",
                                  "C:\\outside.gz", "a\\..\\outside.gz", "", ".", None, 4, "a\x00b"])
def test_unsafe_paths_are_rejected(tmp_path, file):
    cache = Cache(tmp_path)
    entry = put(cache)
    entry["file"] = file
    with pytest.raises(CacheIntegrityError, match="file"):
        cache.body_bytes(entry)


def test_symlink_escape_rejected_on_read_and_publication(tmp_path):
    cache = Cache(tmp_path / "cache")
    entry = legacy(cache)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "body.gz").write_bytes(gzip.compress(b"<p>legacy</p>"))
    (cache.root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CacheIntegrityError, match="escapes"):
        cache.body_bytes({**entry, "file": "escape/body.gz"})
    (cache.root / "blobs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CacheIntegrityError, match="escapes"):
        put(cache)
    assert sorted(p.name for p in outside.iterdir()) == ["body.gz"]


def test_snapshot_keeps_unicode_and_complete_detached_provenance(tmp_path):
    cache = Cache(tmp_path)
    html = "<p>日本語 ⚾ e\u0301\r\n</p>"
    entry = put(cache, html.encode("utf-8"))
    entry["extra"] = {"values": [1, 2]}
    snapshot = cache.snapshot(entry)
    assert snapshot["schema"] == "html-snapshot/1"
    assert snapshot["html"] == html
    assert snapshot["url"] == entry["url"]
    assert snapshot["fetched_at"] == entry["fetched_at"]
    assert snapshot["response_sha256"] == snapshot["content_sha256"] == entry["sha256"]
    assert snapshot["encoding"] == "utf-8"
    assert snapshot["cache_entry"] == entry
    snapshot["cache_entry"]["extra"]["values"].append(3)
    assert entry["extra"] == {"values": [1, 2]}


def test_snapshot_unknown_acquisition_metadata_stays_unknown(tmp_path):
    cache = Cache(tmp_path)
    entry = legacy(cache)
    snapshot = cache.snapshot(entry)
    assert snapshot["url"] is None
    assert snapshot["fetched_at"] is None
    assert snapshot["cache_entry"] == entry
    assert "url" not in snapshot["cache_entry"]


def test_verified_non_utf8_body_is_available_only_as_bytes(tmp_path):
    cache = Cache(tmp_path)
    entry = put(cache, b"\xff")
    assert cache.body_bytes(entry) == b"\xff"
    for reader in (cache.body, cache.snapshot):
        with pytest.raises(UnicodeDecodeError):
            reader(entry)


def test_existing_dangling_blob_is_not_repaired(tmp_path):
    cache = Cache(tmp_path)
    content = b"body"
    blob = tmp_path / "blobs" / f"{hashlib.sha256(content).hexdigest()}.html.gz"
    blob.parent.mkdir()
    missing_target = tmp_path / "missing.gz"
    blob.symlink_to(missing_target)
    with pytest.raises(CacheIntegrityError, match="cannot read"):
        put(cache, content)
    assert blob.is_symlink()
    assert not missing_target.exists()
    assert not cache.manifest.exists()


def test_hash_and_length_collision_still_checks_actual_bytes(tmp_path, monkeypatch):
    cache = Cache(tmp_path)

    class ConstantHash:
        def hexdigest(self):
            return "a" * 64

    monkeypatch.setattr(hashlib, "sha256", lambda content: ConstantHash())
    first = put(cache, b"first")
    compressed = (tmp_path / first["file"]).read_bytes()
    with pytest.raises(CacheIntegrityError, match="differs despite"):
        put(cache, b"other")
    assert (tmp_path / first["file"]).read_bytes() == compressed
    assert len(cache.manifest.read_text().splitlines()) == 1


# ──── urllib: 標準ライブラリの取得。127.0.0.1 の一時サーバーだけ ──────────────────────────────────────────────────────────────

BODY = "<html>本文</html>".encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).seen.append((self.path, dict(self.headers)))
        if self.path == "/page":
            if self.headers.get("If-None-Match") == '"v1"':
                self.send_response(304)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("ETag", '"v1"')
            self.send_header("Last-Modified", "Wed, 01 Oct 2025 00:00:00 GMT")
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)
        elif self.path == "/moved":
            self.send_response(301)
            self.send_header("Location", "/page")
            self.end_headers()
        elif self.path == "/down":
            self.send_response(503)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def server():
    _Handler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _client():
    # 環境変数のプロキシを経由させない（127.0.0.1 の一時サーバーに直接つなぐ）
    return UrllibClient(headers={"User-Agent": "queryu-test"}, timeout=5,
                        opener=urllib.request.build_opener(urllib.request.ProxyHandler({})))


def test_status_body_and_case_insensitive_headers(server):
    r = _client().get(server + "/page")
    assert r.status_code == 200 and r.content == BODY
    assert r.headers.get("etag") == '"v1"' and r.headers.get("last-modified") == "Wed, 01 Oct 2025 00:00:00 GMT"
    assert _Handler.seen[-1][1]["User-Agent"] == "queryu-test"


def test_304_404_and_5xx_are_responses_not_exceptions(server):
    c = _client()
    assert c.get(server + "/page", headers={"If-None-Match": '"v1"'}).status_code == 304
    assert c.get(server + "/missing").status_code == 404
    down = c.get(server + "/down")
    assert down.status_code == 503
    with pytest.raises(HTTPStatusError):
        down.raise_for_status()


def test_redirect_is_followed(server):
    r = _client().get(server + "/moved")
    assert r.status_code == 200 and r.url.endswith("/page") and r.content == BODY


def test_polite_fetcher_with_real_urllib_conditional_get_and_cache(server, tmp_path):
    cache = Cache(tmp_path / "cache")
    f = PoliteFetcher(cache, wait=0, client=_client())
    assert f.get("k", server + "/page", "2026").encode("utf-8") == BODY
    assert f.get("k", server + "/page", "2026", live=True).encode("utf-8") == BODY  # 304 → 保存済みの本文
    assert f.get("gone", server + "/missing", "2026") == ""  # 404 は「空」として記録
    with pytest.raises(HTTPStatusError):
        f.get("down", server + "/down", "2026")
    statuses = [json.loads(line)["status"] for line in (tmp_path / "cache" / "manifest.jsonl").read_text().splitlines()]
    assert statuses == [200, 304, 404]  # 5xx は保存しない
    assert _Handler.seen[1][1].get("If-None-Match") == '"v1"'
    assert _Handler.seen[1][1].get("If-Modified-Since") == "Wed, 01 Oct 2025 00:00:00 GMT"


def test_response_text_and_headers_mapping():
    r = Response(200, text="あ", headers={"ETag": '"x"'})
    assert r.content == "あ".encode("utf-8") and r.headers.get("etag") == '"x"'
    r.raise_for_status()


# ──── share: query the manifest, bundles, snapshots, polite parallel fetch, verify ────────────────────────

def filled(tmp_path):
    cache = CoreCache(tmp_path / "cache")
    rows = [cache.put("cal:2024_03", "https://npb.example/2024/03", "cal", 200, b"<p>v1</p>", {"etag": "a"}),
            cache.put("cal:2024_04", "https://npb.example/2024/04", "cal", 404, b"", {}),
            cache.put("team:2024", "https://team.example/2024", "team", 200, "<p>日本</p>".encode(), {})]
    rows.append(cache.put("cal:2024_03", "https://npb.example/2024/03", "cal", 200, b"<p>v2</p>", {}))
    cache.record({"key": "cal:2024_03", "url": "https://npb.example/2024/03", "group": "cal", "status": 304,
                  "fetched_at": "2099-01-01T00:00:00+00:00"})
    return cache, rows


def test_query_filters_latest_and_history(tmp_path):
    cache, rows = filled(tmp_path)
    assert [r["sha256"] for r in cs.query(cache, key="cal:*")] == [rows[1]["sha256"], rows[3]["sha256"]]
    assert len(cs.query(cache, key="cal:2024_03", history=True)) == 2
    assert [r["key"] for r in cs.query(cache, group="team")] == ["team:2024"]
    assert [r["key"] for r in cs.query(cache, status=404)] == ["cal:2024_04"]
    assert [r["key"] for r in cs.query(cache, url="https://team.example/*")] == ["team:2024"]
    assert cs.query(cache, status=304, with_body=False)[0]["status"] == 304
    assert cs.query(cache, since="2098-01-01") == []
    as_of = cs.query(cache, key="cal:2024_03", until=rows[0]["fetched_at"])
    assert as_of and as_of[-1]["fetched_at"] <= rows[0]["fetched_at"]
    assert cs.query(CoreCache(tmp_path / "empty")) == []


def test_bundle_round_trip_and_idempotent_import(tmp_path):
    cache, _ = filled(tmp_path)
    picked = cs.query(cache, key="cal:*")
    out = cs.export_bundle(cache, picked, tmp_path / "share.tar")
    assert out["entries"] == 2 and out["blobs"] == 2 and len(out["sha256"]) == 64
    with pytest.raises(FileExistsError):
        cs.export_bundle(cache, picked, tmp_path / "share.tar")
    other = CoreCache(tmp_path / "other")
    first = cs.import_bundle(other, tmp_path / "share.tar")
    assert first["added"] == 2 and first["already_present"] == 0 and first["bundle_sha256"] == out["sha256"]
    assert cs.import_bundle(other, tmp_path / "share.tar")["already_present"] == 2
    snapshots = list(cs.iter_snapshots(other, cs.query(other)))
    assert [s["html"] for s in snapshots] == ["", "<p>v2</p>"]
    assert snapshots[1]["cache_entry"]["imported_from"] == out["sha256"]
    assert snapshots[1]["fetched_at"] == picked[1]["fetched_at"]  # original provenance kept


def test_bundle_tampering_is_rejected(tmp_path):
    cache, _ = filled(tmp_path)
    good = tmp_path / "share.tar"
    cs.export_bundle(cache, cs.query(cache, group="team"), good)
    with tarfile.open(good) as archive:
        members = {m.name: archive.extractfile(m).read() for m in archive.getmembers()}
    manifest = json.loads(members["manifest.jsonl"])
    manifest["sha256"] = "0" * 64
    members["manifest.jsonl"] = (json.dumps(manifest) + "\n").encode()
    bad = tmp_path / "bad.tar"
    with tarfile.open(bad, "x") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    target = CoreCache(tmp_path / "target")
    with pytest.raises(CacheIntegrityError):
        cs.import_bundle(target, bad)
    assert not target.manifest.exists()
    (cache.root / cs.query(cache, group="cal")[-1]["file"]).write_bytes(b"corrupt")
    with pytest.raises(CacheIntegrityError):
        cs.export_bundle(cache, cs.query(cache, group="cal"), tmp_path / "never.tar")


def test_async_snapshots_keep_order(tmp_path):
    cache, _ = filled(tmp_path)
    entries = cs.query(cache, history=True)

    async def collect():
        return [s["response_sha256"] async for s in cs.aiter_snapshots(cache, entries, concurrency=2)]
    assert asyncio.run(collect()) == [e["sha256"] for e in entries]
    with pytest.raises(ValueError):
        asyncio.run(cs.aiter_snapshots(cache, entries, concurrency=0).__anext__())


def test_fetch_many_parallel_hosts_serial_within_host(tmp_path):
    calls, lock = [], threading.Lock()

    def handler(request):
        with lock:
            calls.append((request.url, time.monotonic()))
        time.sleep(0.05)
        if request.url.endswith("/fail"):
            raise OSError("down")
        if request.url.endswith("/missing"):
            return Response(404, b"none")
        if request.url.endswith("/busy"):
            return Response(503, b"")
        return Response(200, f"<p>{request.url}</p>".encode(), {"ETag": "x"})

    cache = CoreCache(tmp_path / "cache")
    requests = [("a1", "https://a.example/1", "g"), ("a2", "https://a.example/2", "g"),
                ("b1", "https://b.example/1", "g"), ("b2", "https://b.example/missing", "g"),
                ("c1", "https://c.example/busy", "g"), ("d1", "https://d.example/fail", "g")]
    started = time.monotonic()
    rows = cs.fetch_many(cache, requests, client=HandlerClient(handler), wait=0.2)
    elapsed = time.monotonic() - started
    assert [r["key"] for r in rows] == ["a1", "a2", "b1", "b2", "c1", "d1"]
    assert [r["status"] for r in rows] == [200, 200, 200, 404, 503, None] and rows[-1]["error"] == "OSError"
    a = [t for u, t in calls if "a.example" in u]
    assert a[1] - a[0] >= 0.2  # politeness within one host
    assert elapsed < 0.2 * 2 + 0.05 * 4  # hosts did not wait for each other
    assert rows[0]["etag"] == "x" and len(cs.query(cache)) == 4
    lines = cache.manifest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5 and all(json.loads(line) for line in lines)  # one writer at a time
    for bad in ({"wait": -1}, {"wait": float("nan")}):
        with pytest.raises(ValueError):
            cs.fetch_many(cache, requests, client=HandlerClient(handler), **bad)
    with pytest.raises(ValueError):
        cs.fetch_many(cache, [("k", "relative/path", "g")], client=HandlerClient(handler))


def test_bundle_rejects_unsafe_members(tmp_path):
    for name in ("../escape", "/abs"):
        bad = tmp_path / f"bad{len(name)}.tar"
        with tarfile.open(bad, "x") as archive:
            info = tarfile.TarInfo(name)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        with pytest.raises(CacheIntegrityError):
            cs.import_bundle(CoreCache(tmp_path / "t"), bad)
    linked = tmp_path / "link.tar"
    with tarfile.open(linked, "x") as archive:
        info = tarfile.TarInfo("blobs/x")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        archive.addfile(info)
    with pytest.raises(CacheIntegrityError):
        cs.import_bundle(CoreCache(tmp_path / "t"), linked)


def test_cli_query_export_import(tmp_path, capsys):
    cache, _ = filled(tmp_path)
    assert cs.main(["--cache", str(cache.root), "query", "--group", "cal", "--status", "200"]) == 0
    assert [json.loads(line)["key"] for line in capsys.readouterr().out.splitlines()] == ["cal:2024_03"]
    assert cs.main(["--cache", str(cache.root), "query", "--status", "304"]) == 0
    assert [json.loads(line)["status"] for line in capsys.readouterr().out.splitlines()] == [304]
    out = tmp_path / "b.tar"
    assert cs.main(["--cache", str(cache.root), "export", "--key", "*", "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["entries"] == 3
    assert cs.main(["--cache", str(tmp_path / "copy"), "import", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == 3


def test_verify_cache_clean_and_corrupted(tmp_path, capsys):
    cache, rows = filled(tmp_path)
    res = cs.verify_cache(cache)
    assert res["total"] == 4 and res["verified"] == 4 and res["corrupted"] == 0 and res["errors"] == []

    # Test CLI verify clean
    assert cs.main(["--cache", str(cache.root), "verify"]) == 0
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out["verified"] == 4 and cli_out["corrupted"] == 0

    # Corrupt one body blob
    blob_path = cache.root / rows[0]["file"]
    blob_path.write_bytes(b"corrupt_gzip")

    res_bad = cs.verify_cache(cache)
    assert res_bad["total"] == 4 and res_bad["verified"] == 3 and res_bad["corrupted"] == 1
    assert len(res_bad["errors"]) == 1 and res_bad["errors"][0]["file"] == rows[0]["file"]

    # Test CLI verify with corrupted blob (should exit 1)
    assert cs.main(["--cache", str(cache.root), "verify"]) == 1
    cli_bad_out = json.loads(capsys.readouterr().out)
    assert cli_bad_out["corrupted"] == 1

    # Filtered CLI verify on non-corrupted entry
    assert cs.main(["--cache", str(cache.root), "verify", "--group", "team"]) == 0
    cli_filtered_out = json.loads(capsys.readouterr().out)
    assert cli_filtered_out["total"] == 1 and cli_filtered_out["corrupted"] == 0

    # An empty selection is not a clean pass: exit 3 with a note on stderr
    assert cs.main(["--cache", str(cache.root), "verify", "--group", "no-such-group"]) == 3
    captured = capsys.readouterr()
    assert json.loads(captured.out)["total"] == 0
    assert "selected no cached bodies" in captured.err


# ──── query retention: latest-only query (docs/query-retention.md) ────────────────────────────────────────

class QueryRetentionTests(unittest.TestCase):
    def test_latest_order_filters_and_history(self):
        rows=[{'key':str(i%3),'status':200,'url':'https://example.test','sequence':i} for i in range(100)]
        with patch.object(queryu,'_share_rows',return_value=rows):
            self.assertEqual(cs.query(None),rows[-3:])
            self.assertEqual(cs.query(None,history=True),rows)
            self.assertEqual(cs.query(None,key='1'),[rows[97]])
            self.assertEqual(cs.query(None,status=404),[])
        rows.append({'key':'0','status':500})
        with patch.object(queryu,'_share_rows',return_value=rows):
            self.assertEqual(cs.query(None)[-1]['sequence'],99)
            self.assertEqual(cs.query(None,with_body=False)[-1]['status'],500)


if __name__ == "__main__":
    unittest.main()
