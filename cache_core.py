"""QueRyu's offline cache/snapshot API: stdlib only, no implicit checkout or data paths.

A caller may supply source_version; unknown provenance stays None.
No HTTP client, acquisition planner, frozen-season file or Aoi layout is required.
"""
from __future__ import annotations

import copy, datetime as dt, gzip, hashlib, json, os, re, tempfile, zlib
from pathlib import Path

__all__ = ["Cache", "CacheIntegrityError"]


class CacheIntegrityError(ValueError):
    """A cached body cannot be verified against its recorded acquisition."""


class Cache:
    def __init__(self, root: Path, *, source_version: str | None = None):
        self.source_version = source_version
        self.root = Path(root)
        self.manifest = self.root / "manifest.jsonl"

    def _version(self) -> str | None:
        return self.source_version

    def _now(self) -> str:
        return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

    def latest(self) -> dict[str, dict]:
        """ページごとの最新の来歴。本文を持つ取得（200/404）だけを対象にする。"""
        out = {}
        if self.manifest.exists():
            for line in self.manifest.read_text(encoding="utf-8").splitlines():
                e = json.loads(line)
                if e["status"] in (200, 404):
                    out[e["key"]] = e
        return out

    def _body_path(self, file: str) -> Path:
        """Resolve only relative paths that stay inside this cache root."""
        if not isinstance(file, str) or not file or "\\" in file or "\x00" in file:
            raise CacheIntegrityError("cache file must be a safe relative path")
        relative = Path(file)
        if relative.is_absolute() or ".." in relative.parts or ":" in file:
            raise CacheIntegrityError("cache file must be a safe relative path")
        try:
            root = self.root.resolve()
            path = (root / relative).resolve()
            path.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise CacheIntegrityError("cache file escapes the cache root or cannot be resolved") from exc
        if path == root:
            raise CacheIntegrityError("cache file must name a body inside the cache root")
        # Preserve the final directory entry for no-overwrite publication: a
        # dangling symlink must count as an existing (invalid) blob, not be repaired.
        return root / relative

    def body_bytes(self, entry: dict) -> bytes:
        """Verify recorded metadata against decompressed bytes before decoding."""
        digest = entry.get("sha256")
        size = entry.get("bytes")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise CacheIntegrityError("cache entry requires sha256 as 64 lowercase hexadecimal characters")
        if type(size) is not int or size < 0:
            raise CacheIntegrityError("cache entry requires bytes as a nonnegative integer")
        path = self._body_path(entry.get("file"))
        try:
            content = gzip.decompress(path.read_bytes())
        except (OSError, EOFError, zlib.error) as exc:
            raise CacheIntegrityError(f"cannot read cached gzip body: {entry.get('file')!r}") from exc
        if len(content) != size:
            raise CacheIntegrityError(f"cache body bytes mismatch: recorded {size}, found {len(content)}")
        if hashlib.sha256(content).hexdigest() != digest:
            raise CacheIntegrityError("cache body sha256 mismatch")
        return content

    def body(self, entry: dict) -> str:
        return self.body_bytes(entry).decode("utf-8", errors="strict")

    def snapshot(self, entry: dict) -> dict:
        """Export verified evidence, without guessing missing acquisition metadata."""
        provenance = copy.deepcopy(entry)
        content = self.body_bytes(provenance)
        html = content.decode("utf-8", errors="strict")
        return {
            "schema": "html-snapshot/1", "html": html,
            "url": provenance.get("url"), "fetched_at": provenance.get("fetched_at"),
            "response_sha256": hashlib.sha256(content).hexdigest(),
            "content_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
            "encoding": "utf-8", "cache_entry": provenance,
        }

    def record(self, entry: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.manifest.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def put(self, key: str, url: str, group: str, status: int, content: bytes, headers) -> dict:
        digest = hashlib.sha256(content).hexdigest()
        file = f"blobs/{digest}.html.gz"
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._body_path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Publish a fully written gzip atomically, never replacing an existing blob.
        # If another acquisition wins the race, verify its bytes instead of repairing it.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".blob-", delete=False) as f:
                temporary = Path(f.name)
                f.write(gzip.compress(content, mtime=0))
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            verified = self.body_bytes({"file": file, "sha256": digest, "bytes": len(content)})
            if verified != content:
                raise CacheIntegrityError("cache blob content differs despite matching hash and length")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        entry = {
            "key": key, "url": url, "group": group, "status": status, "file": file,
            "sha256": digest, "bytes": len(content), "fetched_at": self._now(),
            "last_modified": headers.get("last-modified"), "etag": headers.get("etag"),
            "code_version": self._version(),
        }
        self.record(entry)
        return entry


