"""未来の月、手動の凍結、--plan。NPB には出ない。"""
import datetime as dt

import pytest

import npb_calendar
import queryu
from queryu import (
    HandlerClient,
    Response,
    Cache,
    acquisition_plan,
    format_plan,
    live_groups,
    load_frozen,
    main,
    split_future_months,
)

import aoi_paths
from pathlib import Path

MONTH_KEY = r"(?P<year>\d{4})_(?P<month>\d{2})"  # npb_calendar の key（年_月）。Aoi の pipeline と同じ

PAGE = (
    aoi_paths.resolve_aoi_root(anchor=Path(__file__))
    / "blueprobe" / "tests" / "fixtures" / "npb_calendar_sample.html"
).read_text(encoding="utf-8")


def mock_client(monkeypatch, handler):
    monkeypatch.setattr(queryu, "UrllibClient", lambda **_kw: HandlerClient(handler))


def test_future_months_are_split_and_non_month_keys_stay():
    pages = [("2026_03", "u", "2026"), ("2026_04", "u", "2026"), ("2026_11", "u", "2026"), ("2026_c", "u", "2026")]
    kept, future = split_future_months(pages, dt.date(2026, 4, 1), MONTH_KEY)
    assert [p[0] for p in kept] == ["2026_03", "2026_04", "2026_c"]
    assert [p[0] for p in future] == ["2026_11"]
    assert live_groups([2024, 2026], dt.date(2026, 10, 16), set()) == {"2026"}
    assert live_groups([2024, 2026], dt.date(2026, 10, 16), {2026}) == set()


def test_plan_counts_cache_conditional_and_new_without_a_client(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(queryu, "UrllibClient", lambda **kw: pytest.fail("network"))
    cache = Cache(tmp_path / "cache")
    pages = npb_calendar.pages([2026])
    for key, url, group in pages:
        if key.endswith(("_03", "_04")):
            cache.put(key, url, group, 200, b"<html></html>", {"etag": '"v1"'})
    today = dt.date(2026, 5, 1)
    kept, future = split_future_months(pages, today, MONTH_KEY)
    plan = acquisition_plan(
        kept, cache, live=live_groups([2026], today, set()), wait=3,
        today=today, frozen=set(), future=future,
    )
    assert plan["future"] == 6
    assert plan["conditional"] == 2
    assert plan["new"] == 1
    assert plan["cached"] == 0
    assert plan["estimated_wait_seconds"] == 6
    text = format_plan(plan)
    assert text.split("解釈:")[0].count("条件付きGET: 2") == 1
    assert "待ち時間の見積もり: 6 秒" in text.split("解釈:")[1]
    frozen = tmp_path / "frozen.json"
    assert main(["--source", "npb_calendar", "--month-key", MONTH_KEY, "--years", "2026", "--cache", str(tmp_path / "cache"),
                 "--plan", "--today", "2026-05-01", "--frozen", str(frozen), "--wait", "3",
                 "--freeze", "2026"]) == 0
    shown = capsys.readouterr().out
    assert "キャッシュ済み（再確認しない）: 2" in shown
    assert "条件付きGET: 0" in shown
    assert "新規: 1" in shown
    assert "未来の月（取得しない）: 6" in shown
    assert not frozen.exists()


def test_freeze_is_manual_and_a_frozen_season_is_not_rechecked(tmp_path, monkeypatch, capsys):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if "index_04" in str(req.url):
            return Response(200, text=PAGE, headers={"Last-Modified": "Wed, 01 Oct 2025 00:00:00 GMT", "ETag": '"abc"'})
        return Response(404)

    mock_client(monkeypatch, handler)
    frozen = tmp_path / "frozen.json"
    out = tmp_path / "games.jsonl"
    args = ["--source", "npb_calendar", "--month-key", MONTH_KEY, "--years", "2026", "--cache", str(tmp_path / "cache"),
            "--out", str(out), "--wait", "0", "--today", "2026-10-16", "--frozen", str(frozen),
            "--freeze", "2026"]
    assert main(args) == 0
    assert load_frozen(frozen) == {2026}
    first = len(calls)
    assert first == 8  # 3〜10月。11月はまだ来ていない
    assert all("index_11" not in url for url in calls)
    assert main(args) == 0
    assert len(calls) == first
    assert "HTTP requests: 0" in capsys.readouterr().out


def test_unfrozen_current_season_is_rechecked(tmp_path, monkeypatch):
    calls = []

    def handler(req):
        calls.append(req.headers.get("If-None-Match"))
        return Response(404)

    mock_client(monkeypatch, handler)
    frozen = tmp_path / "frozen.json"
    out = tmp_path / "games.jsonl"
    args = ["--source", "npb_calendar", "--month-key", MONTH_KEY, "--years", "2026", "--cache", str(tmp_path / "cache"),
            "--out", str(out), "--wait", "0", "--today", "2026-04-02", "--frozen", str(frozen)]
    assert main(args) == 0
    assert len(calls) == 2  # 3月と4月
    assert main(args) == 0
    assert len(calls) == 4


def test_month_key_needs_year_and_month_groups_and_none_keeps_every_page():
    with pytest.raises(ValueError):
        queryu.month_key_pattern(r"(?P<year>\d{4})")
    pages = [("2026_11", "u", "2026")]
    assert split_future_months(pages, dt.date(2026, 4, 1)) == (pages, [])
