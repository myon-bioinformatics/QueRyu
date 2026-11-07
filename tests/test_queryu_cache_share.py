"""cache_share: query the manifest, hand caches over as bundles, supply snapshots, fetch politely in parallel."""
import asyncio, io, json, tarfile, threading, time

import pytest

import cache_share as cs
from cache_core import Cache, CacheIntegrityError
from queryu import HandlerClient, Response


def filled(tmp_path):
    cache = Cache(tmp_path / "cache")
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
    assert cs.query(Cache(tmp_path / "empty")) == []


def test_bundle_round_trip_and_idempotent_import(tmp_path):
    cache, _ = filled(tmp_path)
    picked = cs.query(cache, key="cal:*")
    out = cs.export_bundle(cache, picked, tmp_path / "share.tar")
    assert out["entries"] == 2 and out["blobs"] == 2 and len(out["sha256"]) == 64
    with pytest.raises(FileExistsError):
        cs.export_bundle(cache, picked, tmp_path / "share.tar")
    other = Cache(tmp_path / "other")
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
    target = Cache(tmp_path / "target")
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

    cache = Cache(tmp_path / "cache")
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
            cs.import_bundle(Cache(tmp_path / "t"), bad)
    linked = tmp_path / "link.tar"
    with tarfile.open(linked, "x") as archive:
        info = tarfile.TarInfo("blobs/x")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        archive.addfile(info)
    with pytest.raises(CacheIntegrityError):
        cs.import_bundle(Cache(tmp_path / "t"), linked)


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
