"""Emoji names from Unicode data acquired through npm, cached and accumulated for offline reuse.

unicode.org is often unreachable from sandboxes and CI, while the npm registry is
not. ``unicode-emoji-json`` republishes unicode.org's emoji-test.txt (CLDR short
names, groups, versions) as JSON. This module:

1. acquires that tarball once (registry metadata -> tarball, sha512 integrity
   checked) into a QueRyu ``Cache`` with provenance, or reads it back offline;
2. reads names, groups and versions from it (stdlib ``tarfile``/``json``);
3. accumulates the names of emoji actually used into a small committed JSON
   (``emoji_names.json``) that keeps working with no network at all, and only
   ever grows (entries are added, never dropped).

Lookups prefer the accumulated table, then the cached package. A skin-tone or hair
variant missing from both is named from its base and components
(``thumbs up: medium skin tone``), the CLDR pattern.

    python emoji_data.py --cache data/raw/npm --names emoji_names.json --use inventory.json
    python emoji_data.py --cache data/raw/npm --offline --lookup TEXT
    python emoji_data.py --names emoji_names.json --lookup TEXT        # no cache, no network
"""
from __future__ import annotations

import argparse, base64, hashlib, io, json, re, sys, tarfile, urllib.parse, urllib.request
from pathlib import Path

from cache_core import Cache

__all__ = ["DEFAULT_PACKAGE", "npm_tarball", "emoji_records", "name_for", "accumulate", "load_names"]

REGISTRY = "https://registry.npmjs.org"
DEFAULT_PACKAGE = ("unicode-emoji-json", "0.9.0")
MAX_BYTES = 20 * 1024 * 1024
COMPONENTS = {"\U0001F3FB": "light skin tone", "\U0001F3FC": "medium-light skin tone",
              "\U0001F3FD": "medium skin tone", "\U0001F3FE": "medium-dark skin tone",
              "\U0001F3FF": "dark skin tone", "\U0001F9B0": "red hair", "\U0001F9B1": "curly hair",
              "\U0001F9B3": "white hair", "\U0001F9B2": "bald"}
_PACKAGE = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*\Z")
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z")


def identity(sequence: str) -> str:
    """Comparison form without U+FE0F (as xprobe ``emoji_identity`` and route_trail)."""
    return sequence.replace("\ufe0f", "")


def _get(url: str, opener, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "queryu-emoji-data/1",
                                                   "Accept": "application/json, application/octet-stream"})
    with opener(request, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url}: larger than {limit} bytes")
    return data


def npm_tarball(cache: Cache, package: str = DEFAULT_PACKAGE[0], version: str = DEFAULT_PACKAGE[1], *,
                offline: bool = False, opener=urllib.request.urlopen) -> tuple[bytes, dict]:
    """The package tarball and its manifest entry: from the cache, or fetched once.

    The exact version is required (no ``latest``), so a cache key names one
    immutable tarball. Fetching reads the registry's version metadata, accepts only
    a tarball on the registry host, and checks the sha512 ``integrity`` before
    storing. ``offline=True`` never touches the network and raises LookupError
    when the version was never cached.
    """
    if not _PACKAGE.match(package) or not _VERSION.match(version):
        raise ValueError("package must be an npm name and version an exact x.y.z")
    key = f"npm:{package}@{version}"
    entry = cache.latest().get(key)
    if entry is not None:
        return cache.body_bytes(entry), entry
    if offline:
        raise LookupError(f"{key} is not cached; run once with network access")
    meta = json.loads(_get(f"{REGISTRY}/{urllib.parse.quote(package, safe='@')}/{version}", opener, 4 * 1024 * 1024))
    dist = meta.get("dist") or {}
    tarball, integrity = dist.get("tarball"), dist.get("integrity") or ""
    if urllib.parse.urlsplit(tarball or "").netloc != urllib.parse.urlsplit(REGISTRY).netloc:
        raise ValueError(f"{key}: tarball is not on the registry host: {tarball!r}")
    if not integrity.startswith("sha512-"):
        raise ValueError(f"{key}: registry metadata has no sha512 integrity")
    data = _get(tarball, opener, MAX_BYTES)
    if base64.b64encode(hashlib.sha512(data).digest()).decode() != integrity[7:]:
        raise ValueError(f"{key}: tarball does not match its sha512 integrity")
    entry = cache.put(key, tarball, "npm", 200, data, {})
    return data, {**entry, "integrity": integrity}


def emoji_records(tarball: bytes) -> dict[str, dict]:
    """identity -> {emoji, name, group, unicode_version, emoji_version, skin_tone_support}."""
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        member = archive.getmember("package/data-by-emoji.json")
        if not member.isfile() or member.size > MAX_BYTES:
            raise ValueError("package/data-by-emoji.json is missing or too large")
        data = json.loads(archive.extractfile(member).read().decode("utf-8"))
    out = {}
    for emoji, row in data.items():
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ValueError(f"unexpected record for {emoji!r}")
        out[identity(emoji)] = {"emoji": emoji, "name": row["name"], "group": row.get("group"),
                                "unicode_version": row.get("unicode_version"),
                                "emoji_version": row.get("emoji_version"),
                                "skin_tone_support": bool(row.get("skin_tone_support"))}
    return out


def name_for(sequence: str, names: dict[str, str]) -> str | None:
    """CLDR-style name from ``names`` (identity -> name); variants named from base + components."""
    key = identity(sequence)
    if key in names:
        return names[key]
    parts = [COMPONENTS[c] for c in key if c in COMPONENTS]
    base = "".join(c for c in key if c not in COMPONENTS).replace("\u200d\u200d", "\u200d").strip("\u200d")
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
    for item in sorted({identity(i) for i in identities}):
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


def _identities_from(path: Path) -> list[str]:
    """Identities from an xprobe ``--emoji`` inventory (xprobe.emoji-inventory.v1) or a JSON list."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("schema") == "xprobe.emoji-inventory.v1":
        return [row["identity"] for row in data["emoji"]]
    if isinstance(data, list) and all(isinstance(x, str) for x in data):
        return data
    raise ValueError(f"{path}: expected an xprobe emoji inventory or a JSON list of emoji")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cache", type=Path, help="QueRyu cache directory for npm tarballs")
    ap.add_argument("--package", default="@".join(DEFAULT_PACKAGE), help="NAME@x.y.z (default: %(default)s)")
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
            data, entry = npm_tarball(Cache(args.cache), package, version, offline=args.offline)
            records = emoji_records(data)
            source = {"package": package, "version": version, "url": entry.get("url"),
                      "sha256": entry["sha256"], "fetched_at": entry.get("fetched_at")}
        if args.use or args.all:
            if not args.names or not records:
                ap.error("--use/--all need --names and --cache")
            report = accumulate(args.names, list(records) if args.all else _identities_from(args.use),
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
            for token in _sequences(args.lookup):
                name = name_for(token, names)
                found += name is not None
                print(f"{token}\t{name or '(unknown)'}")
            return 0 if found else 1
    except (LookupError, ValueError, OSError) as error:
        print(f"emoji_data: {error}", file=sys.stderr)
        return 2
    return 0


def _sequences(text: str) -> list[str]:
    """Rough split for --lookup: runs joined by ZWJ, VS16, modifiers, tags, keycap and flags."""
    out, current = [], ""
    glue = {"\u200d", "\ufe0f", "\u20e3"} | set(COMPONENTS)
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


if __name__ == "__main__":
    raise SystemExit(main())
