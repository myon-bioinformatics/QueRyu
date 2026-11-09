"""Verified, immutable cache evidence; all fixtures are local and offline."""

import gzip, hashlib, json, os
from pathlib import Path

import pytest

from queryu import Cache, CacheIntegrityError, PoliteFetcher


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
