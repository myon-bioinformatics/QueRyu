"""emoji_data: npm acquisition with integrity and provenance, offline reuse, accumulated names."""
import base64, hashlib, io, json, tarfile
from pathlib import Path

import pytest

import queryu
from queryu import emoji as emoji_data
from cache_core import Cache

ROOT = Path(__file__).resolve().parents[1]
MAP, HOUSE, WOMAN_TECH, THUMBS = "\U0001F5FA\ufe0f", "\U0001F3E0", "\U0001F469\u200d\U0001F4BB", "\U0001F44D"
DATA = {MAP: {"name": "world map", "group": "Travel & Places", "unicode_version": "0.7", "emoji_version": "0.7",
              "skin_tone_support": False},
        HOUSE: {"name": "house", "group": "Travel & Places"},
        WOMAN_TECH: {"name": "woman technologist", "skin_tone_support": True},
        THUMBS: {"name": "thumbs up", "skin_tone_support": True}}


def tarball(data=DATA, name="package/data-by-emoji.json"):
    raw = json.dumps(data, ensure_ascii=False).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


class Registry:
    def __init__(self, body, integrity=None, tarball_url="https://registry.npmjs.org/p/-/p-1.0.0.tgz"):
        self.body, self.calls = body, []
        self.meta = {"dist": {"tarball": tarball_url,
                              "integrity": integrity or "sha512-" + base64.b64encode(hashlib.sha512(body).digest()).decode()}}

    def __call__(self, request, timeout):
        self.calls.append(request.full_url)
        payload = self.body if request.full_url.endswith(".tgz") else json.dumps(self.meta).encode()
        return io.BytesIO(payload)


def test_fetch_once_then_offline_from_cache(tmp_path):
    body = tarball()
    registry = Registry(body)
    cache = Cache(tmp_path / "cache")
    data, entry = emoji_data.npm_tarball(cache, "p", "1.0.0", opener=registry)
    assert data == body and entry["key"] == "npm:p@1.0.0" and entry["integrity"].startswith("sha512-")
    assert registry.calls == ["https://registry.npmjs.org/p/1.0.0", "https://registry.npmjs.org/p/-/p-1.0.0.tgz"]
    again, cached = emoji_data.npm_tarball(Cache(tmp_path / "cache"), "p", "1.0.0", offline=True,
                                           opener=lambda *a, **k: pytest.fail("network used offline"))
    assert again == body and cached["sha256"] == hashlib.sha256(body).hexdigest()
    with pytest.raises(LookupError):
        emoji_data.npm_tarball(cache, "p", "2.0.0", offline=True)


@pytest.mark.parametrize("registry, message", [
    (Registry(b"x", integrity="sha512-AAAA"), "integrity"),
    (Registry(b"x", integrity="sha1-abc"), "sha512"),
    (Registry(b"x", tarball_url="https://evil.example/p.tgz"), "registry host"),
])
def test_fetch_rejects_untrusted_tarballs(tmp_path, registry, message):
    with pytest.raises(ValueError, match=message):
        emoji_data.npm_tarball(Cache(tmp_path), "p", "1.0.0", opener=registry)
    assert not (tmp_path / "manifest.jsonl").exists()


@pytest.mark.parametrize("package, version", [("p", "latest"), ("p", "^1.0.0"), ("../p", "1.0.0"), ("P", "1.0.0")])
def test_exact_package_and_version_only(tmp_path, package, version):
    with pytest.raises(ValueError):
        emoji_data.npm_tarball(Cache(tmp_path), package, version)


def test_records_and_names_with_components():
    records = emoji_data.emoji_records(tarball())
    assert records["\U0001F5FA"]["emoji"] == MAP and records["\U0001F5FA"]["group"] == "Travel & Places"
    names = {k: r["name"] for k, r in records.items()}
    assert emoji_data.name_for("\U0001F5FA", names) == emoji_data.name_for(MAP, names) == "world map"
    assert emoji_data.name_for("\U0001F44D\U0001F3FF", names) == "thumbs up: dark skin tone"
    assert emoji_data.name_for("\U0001F469\U0001F3FD\u200d\U0001F4BB", names) == "woman technologist: medium skin tone"
    assert emoji_data.name_for("\U0001F47B", names) is None
    with pytest.raises((KeyError, ValueError)):
        emoji_data.emoji_records(tarball(name="package/other.json"))
    with pytest.raises(ValueError):
        emoji_data.emoji_records(tarball(data={HOUSE: {"group": "no name"}}))


def test_accumulate_only_grows_and_stays_sorted(tmp_path):
    path = tmp_path / "names.json"
    records = emoji_data.emoji_records(tarball())
    source = {"package": "p", "version": "1.0.0"}
    first = emoji_data.accumulate(path, [MAP, "\U0001F44D\U0001F3FB", "\U0001F47B"], records, source)
    assert first["added"] == ["\U0001F44D\U0001F3FB", "\U0001F5FA"] and first["unknown"] == ["\U0001F47B"]
    table = json.loads(path.read_text(encoding="utf-8"))
    assert table["names"] == {"\U0001F44D\U0001F3FB": "thumbs up: light skin tone", "\U0001F5FA": "world map"}
    assert table["sources"] == [source]
    table["names"]["\U0001F5FA"] = "hand-reviewed name"  # a reviewed entry is kept, never overwritten
    path.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
    second = emoji_data.accumulate(path, [MAP, HOUSE], {}, source)
    assert second["kept"] == ["\U0001F5FA"] and second["unknown"] == [HOUSE] and second["added"] == []
    third = emoji_data.accumulate(path, [HOUSE], records, {"package": "p", "version": "1.1.0"})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert third["added"] == [HOUSE] and data["names"]["\U0001F5FA"] == "hand-reviewed name"
    assert list(data["names"]) == sorted(data["names"]) and len(data["sources"]) == 2
    (tmp_path / "bad.json").write_text('{"schema": "other"}', encoding="utf-8")
    with pytest.raises(ValueError):
        emoji_data.load_names(tmp_path / "bad.json")


def test_cli_offline_accumulate_and_lookup(tmp_path, capsys):
    cache = tmp_path / "cache"
    emoji_data.npm_tarball(Cache(cache), "p", "1.0.0", opener=Registry(tarball()))
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"schema": "xprobe.emoji-inventory.v1",
                                     "emoji": [{"identity": "\U0001F5FA"}, {"identity": HOUSE}]}), encoding="utf-8")
    names = tmp_path / "names.json"
    common = ["--cache", str(cache), "--package", "p@1.0.0", "--offline"]
    assert emoji_data.main(common + ["--names", str(names), "--use", str(inventory)]) == 0
    assert "+2 kept 0 unknown 0 total 2" in capsys.readouterr().out
    assert emoji_data.main(common + ["--names", str(names), "--all"]) == 0
    assert "total 4" in capsys.readouterr().out
    # The accumulated table alone answers with no cache and no network.
    assert emoji_data.main(["--names", str(names), "--lookup", f"go {MAP} {THUMBS}\U0001F3FE now"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"{MAP}\tworld map", f"{THUMBS}\U0001F3FE\tthumbs up: medium-dark skin tone"]
    assert emoji_data.main(["--names", str(names), "--lookup", "\U0001F47B"]) == 1
    assert emoji_data.main(["--cache", str(tmp_path / "empty"), "--offline", "--lookup", "x"]) == 2
    (tmp_path / "list.json").write_text('[1]', encoding="utf-8")
    assert emoji_data.main(common + ["--names", str(names), "--use", str(tmp_path / "list.json")]) == 2
    with pytest.raises(SystemExit):
        emoji_data.main(["--names", str(names), "--all"])


def test_sequence_split_for_lookup():
    text = "a \U0001F1EF\U0001F1F5\U0001F1FA\U0001F1F8 #\ufe0f\u20e3 12 \U0001F3F4\u200d\u2620\ufe0f \U0001F3F4\U000E0067\U000E0062\U000E007F"
    assert queryu._emoji_sequences(text) == ["\U0001F1EF\U0001F1F5", "\U0001F1FA\U0001F1F8", "#\ufe0f\u20e3",
                                           "\U0001F3F4\u200d\u2620\ufe0f", "\U0001F3F4\U000E0067\U000E0062\U000E007F"]
    assert queryu._emoji_sequences("\u00a9 2026 \u00ae\ufe0f \u00e9") == ["\u00a9", "\u00ae\ufe0f"]


def test_committed_table_is_valid_and_sourced():
    table = emoji_data.load_names(ROOT / "emoji_names.json")
    assert table["names"] and all("\ufe0f" not in key for key in table["names"])
    assert table["sources"][0]["package"] == "unicode-emoji-json" and len(table["sources"][0]["sha256"]) == 64
    assert table["names"]["\U0001F5FA"] == "world map"
