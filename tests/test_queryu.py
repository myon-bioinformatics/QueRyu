"""取得の礼儀（負荷）と来歴の記録。ネットワークには出ず、HandlerClient で応答を作る。"""

import json, urllib.request
from pathlib import Path

import pytest

import npb_calendar
from queryu import (Cache, HandlerClient, HTTPStatusError, OfflineMiss, PoliteFetcher, Response, UrllibClient,
                    build, main, write_dataset)

import aoi_paths

PAGE = (aoi_paths.resolve_aoi_root(anchor=Path(__file__)) / "blueprobe" / "tests" / "fixtures" / "npb_calendar_sample.html").read_text(encoding="utf-8")


def fetcher(tmp_path, handler, **kw):
    return PoliteFetcher(Cache(tmp_path / "cache"), wait=0, client=HandlerClient(handler), **kw)


def only_april(req):
    if "index_04" in str(req.url):
        return Response(200, text=PAGE, headers={"Last-Modified": "Wed, 01 Oct 2025 00:00:00 GMT", "ETag": '"abc"'})
    return Response(404)


def test_finished_season_hits_network_once(tmp_path):
    calls = []
    f = fetcher(tmp_path, lambda r: calls.append(r) or only_april(r))
    pages = npb_calendar.pages([2024])
    assert len(build(npb_calendar, pages, f)["2024"]["records"]) == 4
    assert len(calls) == 9  # 3〜11月。404 の月も1回だけ
    f2 = fetcher(tmp_path, lambda r: pytest.fail("終了済みシーズンは再取得しない"))
    assert len(build(npb_calendar, pages, f2)["2024"]["records"]) == 4


def test_manifest_records_provenance(tmp_path):
    f = fetcher(tmp_path, only_april)
    build(npb_calendar, npb_calendar.pages([2024]), f)
    entries = [json.loads(line) for line in (tmp_path / "cache" / "manifest.jsonl").read_text().splitlines()]
    assert len(entries) == 9
    april = next(e for e in entries if e["key"] == "2024_04")
    assert april["status"] == 200 and april["url"].endswith("/bis/2024/calendar/index_04.html")
    assert april["last_modified"] and april["etag"] == '"abc"' and len(april["sha256"]) == 64
    assert april["fetched_at"].endswith("+00:00")
    assert {e["status"] for e in entries} == {200, 404}
    assert "code_version" in april  # 測れなければ None。推測で埋めない


def test_live_season_sends_conditional_headers_and_logs_304(tmp_path):
    seen = []

    def handler(req):
        seen.append((req.headers.get("If-Modified-Since"), req.headers.get("If-None-Match")))
        if "index_04" not in str(req.url):
            return Response(404)
        if req.headers.get("If-None-Match"):
            return Response(304)
        return only_april(req)

    f = fetcher(tmp_path, handler)
    pages = [p for p in npb_calendar.pages([2024]) if p[0] == "2024_04"]
    build(npb_calendar, pages, f, live_groups={"2024"})
    got = build(npb_calendar, pages, f, live_groups={"2024"})
    assert len(got["2024"]["records"]) == 4  # 304 → 保存済みの本文を再利用
    assert seen[-1] == ("Wed, 01 Oct 2025 00:00:00 GMT", '"abc"')
    statuses = [json.loads(line)["status"] for line in (tmp_path / "cache" / "manifest.jsonl").read_text().splitlines()]
    assert statuses == [200, 304]  # 「確認した」ことも記録に残る


def test_offline_never_touches_network(tmp_path):
    fetcher(tmp_path, only_april).get("2024_04", npb_calendar.pages([2024])[1][1], "2024")
    off = fetcher(tmp_path, lambda r: pytest.fail("offline"), offline=True)
    assert off.get("2024_04", "u", "2024", live=True)  # live 指定でもキャッシュを使う
    with pytest.raises(OfflineMiss):
        off.get("2024_05", "u", "2024")


def test_server_error_is_not_cached(tmp_path):
    f = fetcher(tmp_path, lambda r: Response(503))
    with pytest.raises(HTTPStatusError):
        f.get("2024_04", "https://npb.jp/x", "2024")
    assert not (tmp_path / "cache" / "manifest.jsonl").exists()


def test_write_dataset_merges_and_writes_manifest(tmp_path):
    path = tmp_path / "obs" / "games.jsonl"
    write_dataset(path, [{"key": "b", "date": "2024-04-02"}, {"key": "a", "date": "2024-04-01"}], {"source": "s"})
    write_dataset(path, [{"key": "b", "date": "2024-04-02", "x": 1}], {"source": "s"})
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows == [{"key": "a", "date": "2024-04-01"}, {"key": "b", "date": "2024-04-02", "x": 1}]
    meta = json.loads(path.with_suffix(".manifest.json").read_text())
    assert meta["records"] == 2 and meta["source"] == "s" and len(meta["sha256"]) == 64


def test_cli_offline_end_to_end(tmp_path, capsys):
    fetcher(tmp_path, only_april).get("2024_04", npb_calendar.pages([2024])[1][1], "2024")
    for key, url, group in npb_calendar.pages([2024]):  # 他の月は 404 として記録済みにする
        if key != "2024_04":
            Cache(tmp_path / "cache").put(key, url, group, 404, b"", {})
    out = tmp_path / "obs" / "games.jsonl"
    assert main(["--source", "npb_calendar", "--years", "2024", "--cache", str(tmp_path / "cache"),
                 "--out", str(out), "--offline"]) == 0
    text = capsys.readouterr().out
    assert "2024: records=4 cancelled=1 non_regular=1 unknown=0" in text and "HTTP requests: 0" in text
    meta = json.loads(out.with_suffix(".manifest.json").read_text())
    assert meta["dropped"] == {"cancelled": 1, "non_regular": 1} and meta["unknown"] == 0
    assert "NFKC" in meta["normalization"]


def test_write_dataset_drops_records_the_rebuilt_scope_no_longer_produces(tmp_path):
    # 解析ルールを直した後、前の規則で採用した試合（例: CS）が残らない。範囲外の年は残す
    path = tmp_path / "obs" / "games.jsonl"
    old = [{"key": "a", "date": "2013-10-12"}, {"key": "cs", "date": "2013-10-12"}, {"key": "z", "date": "2014-04-01"}]
    write_dataset(path, old, {"source": "s"})
    write_dataset(path, [{"key": "a", "date": "2013-10-12"}], {"source": "s"},
                  rebuilt=lambda r: npb_calendar.group(r) == "2013")
    assert [r["key"] for r in map(json.loads, path.read_text().splitlines())] == ["a", "z"]
    assert json.loads(path.with_suffix(".manifest.json").read_text())["removed"] == 1


def test_client_keeps_environment_proxy_settings(tmp_path, monkeypatch):
    # 環境変数のプロキシ設定を捨てない（PR #3 で判明）。urllib の既定の opener は ProxyHandler で環境変数を読む
    monkeypatch.setenv("https_proxy", "http://proxy.example.test:8080")
    client = PoliteFetcher(Cache(tmp_path / "cache"), wait=0).client
    assert isinstance(client, UrllibClient)
    proxies = [h.proxies for h in client.opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert proxies and proxies[0].get("https") == "http://proxy.example.test:8080"
    assert any(isinstance(h, urllib.request.HTTPRedirectHandler) for h in client.opener.handlers)
    assert client.headers["User-Agent"].startswith("Aoi-QueRyu/")
