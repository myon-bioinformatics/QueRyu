"""Query, share and supply a QueRyu cache: stdlib only, next to cache_core.

- ``query(cache, ...)``: select manifest rows by key/url glob, group, status and
  fetched_at range; latest per key by default, every acquisition with ``history``.
- ``export_bundle`` / ``import_bundle``: hand verified bodies and their manifest
  rows to another checkout or person as one ``.tar`` (``queryu-bundle/1``), so
  they extract without fetching again. Import re-verifies every body and never
  overwrites an existing blob or row.
- ``iter_snapshots`` / ``aiter_snapshots``: supply ``html-snapshot/1`` records to an
  extractor (BlueProbe) synchronously or from asyncio, in query order.
- ``fetch_many``: polite acquisition across several hosts at once: one request at
  a time per host with the wait between them, hosts in parallel, every response
  stored through ``Cache.put`` with provenance.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import fnmatch
import gzip
import hashlib
import io
import json
import tarfile
import threading
import time
import urllib.parse
from pathlib import Path

from cache_core import Cache, CacheIntegrityError

__all__ = ["main", "query", "export_bundle", "import_bundle", "iter_snapshots", "aiter_snapshots", "fetch_many", "verify_cache"]

BUNDLE_SCHEMA = "queryu-bundle/1"


def _rows(cache: Cache) -> list[dict]:
    if not cache.manifest.exists():
        return []
    return [json.loads(line) for line in cache.manifest.read_text(encoding="utf-8").splitlines() if line.strip()]


def _time(value):
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        stamp = value
    else:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def query(cache: Cache, *, key: str | None = None, url: str | None = None, group: str | None = None,
          status=None, since=None, until=None, history: bool = False, with_body: bool = True) -> list[dict]:
    """Manifest rows matching every given condition, in manifest order.

    ``key`` / ``url`` are shell globs (``npb_calendar:2024_*``), ``status`` an int or
    a collection of ints, ``since`` / ``until`` ISO times (inclusive) on
    ``fetched_at``. Without ``history`` only the latest row per key survives
    (after filtering by time, so ``until`` asks "as of then"). ``with_body`` keeps
    only rows that carry a body (status 200/404), as ``Cache.latest`` does.
    """
    statuses = None if status is None else ({status} if isinstance(status, int) else set(status))
    start, end = _time(since), _time(until)
    out = []
    latest = {}
    for position, row in enumerate(_rows(cache)):
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
            fetched = _time(row.get("fetched_at")) if row.get("fetched_at") else None
            if fetched is None or (start and fetched < start) or (end and fetched > end):
                continue
        if history:
            out.append(row)
        else:
            latest[row["key"]] = (position, row)
    if history:
        return out
    return [row for _, row in sorted(latest.values(), key=lambda item: item[0])]


def export_bundle(cache: Cache, entries: list[dict], path: Path) -> dict:
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


def import_bundle(cache: Cache, path: Path) -> dict:
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
    seen = {(r.get("key"), r.get("sha256"), r.get("fetched_at")) for r in _rows(cache)}
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


def iter_snapshots(cache: Cache, entries: list[dict]):
    """``html-snapshot/1`` records in the given order (verified by ``Cache.snapshot``)."""
    for entry in entries:
        yield cache.snapshot(entry)


async def aiter_snapshots(cache: Cache, entries: list[dict], *, concurrency: int = 4):
    """The same records from asyncio: verification/decompression runs in threads, order kept."""
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    entries = list(entries)
    for start in range(0, len(entries), concurrency):
        batch = entries[start:start + concurrency]
        for snapshot in await asyncio.gather(*(asyncio.to_thread(cache.snapshot, e) for e in batch)):
            yield snapshot


def fetch_many(cache: Cache, requests: list[tuple[str, str, str]], *, client, wait: float = 3.0,
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

    async def main():
        semaphore = asyncio.Semaphore(max_hosts)

        async def one(indexes):
            async with semaphore:
                await asyncio.to_thread(run_host, indexes)
        await asyncio.gather(*(one(ix) for ix in by_host.values()))

    asyncio.run(main())
    return results


def verify_cache(cache: Cache, entries: list[dict] | None = None) -> dict:
    """Verify stored gzip bodies and recorded sha256/bytes for entries in cache.

    Calls ``cache.body_bytes(entry)`` for every entry referencing a body blob.
    Returns a dictionary summarizing total, verified, corrupted counts and error details.
    """
    if entries is None:
        entries = query(cache, history=True)
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


def main(argv=None) -> int:
    """``query`` prints rows; ``export`` bundles 200/404 rows; ``import`` adds one; ``verify`` checks integrity."""
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="cache_share.py", description=__doc__.splitlines()[0])
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
    cache = Cache(args.cache)
    if args.command == "import":
        print(json.dumps(import_bundle(cache, args.bundle), ensure_ascii=False))
        return 0
    # query is an inspection path: it also lists rows without a body (304, 5xx, errors);
    # export packs bodies, so it keeps the 200/404 rows only;
    # verify checks body integrity.
    rows = query(cache, key=args.key, url=args.url, group=args.group, status=args.status,
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


if __name__ == "__main__":
    raise SystemExit(main())
