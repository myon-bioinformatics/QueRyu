"""Independent review probes: no NPB traffic and no persistent user state."""
import datetime as dt, json
from types import SimpleNamespace

import pytest

import ask
import queryu
from queryu import Cache, PoliteFetcher, acquisition_plan, load_frozen, main, save_frozen

MONTH_KEY = r"(?P<year>\d{4})_(?P<month>\d{2})"  # npb_calendar の key（年_月）。Aoi の pipeline と同じ



@pytest.mark.parametrize("year", [26, 1899, 2101])
@pytest.mark.parametrize("plan", [False, True])
def test_invalid_freeze_never_changes_state(tmp_path, year, plan):
    frozen = tmp_path / "frozen.json"
    frozen.write_text('{"seasons": [2025]}\n')
    before = frozen.read_bytes()
    argv = ["--source", "npb_calendar", "--month-key", MONTH_KEY, "--years", "2026", "--cache", str(tmp_path / "cache"),
            "--frozen", str(frozen), "--freeze", str(year), "--out", str(tmp_path / "games.jsonl"), "--offline"]
    if plan:
        argv.append("--plan")
    with pytest.raises(SystemExit) as error:
        main(argv)
    assert error.value.code == 2
    assert frozen.read_bytes() == before
    assert not (tmp_path / "cache").exists()
    assert not (tmp_path / "games.jsonl").exists()


@pytest.mark.parametrize("year", [True, 2026.0, 1899, 2101])
def test_save_frozen_rejects_invalid_values_before_writing(tmp_path, year):
    path = tmp_path / "new-directory" / "frozen.json"
    with pytest.raises(ValueError, match="1900"):
        save_frozen(path, {year})
    assert not path.parent.exists()


def test_save_frozen_accepts_both_contract_endpoints(tmp_path):
    path = tmp_path / "frozen.json"
    save_frozen(path, {1900, 2100})
    assert load_frozen(path) == {1900, 2100}


def test_missing_output_does_not_persist_freeze(tmp_path):
    frozen = tmp_path / "frozen.json"
    with pytest.raises(SystemExit) as error:
        main(["--source", "npb_calendar", "--month-key", MONTH_KEY, "--years", "2026", "--cache", str(tmp_path / "cache"),
              "--frozen", str(frozen), "--freeze", "2026"])
    assert error.value.code == 2
    assert not frozen.exists()


def test_reversed_year_range_does_not_persist_freeze(tmp_path):
    frozen = tmp_path / "frozen.json"
    with pytest.raises(SystemExit) as error:
        main(["--source", "npb_calendar", "--month-key", MONTH_KEY, "--years", "2027-2026", "--cache", str(tmp_path / "cache"),
              "--frozen", str(frozen), "--freeze", "2026", "--out", str(tmp_path / "games.jsonl"), "--offline"])
    assert error.value.code == 2
    assert not frozen.exists()


@pytest.mark.parametrize("wait", ["-1", "nan", "inf"])
def test_invalid_wait_rejected_before_plan_or_state_write(tmp_path, wait):
    frozen = tmp_path / "frozen.json"
    with pytest.raises(SystemExit) as error:
        main(["--source", "npb_calendar", "--month-key", MONTH_KEY, "--years", "2026", "--cache", str(tmp_path / "cache"),
              "--frozen", str(frozen), "--freeze", "2026", "--wait", wait, "--plan"])
    assert error.value.code == 2
    assert not frozen.exists()


def test_plan_counts_actual_request_headers_and_sleeps(tmp_path, monkeypatch):
    cache = Cache(tmp_path / "cache")
    pages = [("2025_03", "https://example.test/old", "2025"),
             ("2026_03", "https://example.test/etag", "2026"),
             ("2026_04", "https://example.test/not-found", "2026"),
             ("2026_05", "https://example.test/new", "2026")]
    cache.put(*pages[0], 200, b"old", {})
    cache.put(*pages[1], 200, b"etag", {"etag": '"v1"'})
    cache.put(*pages[2], 404, b"", {})
    plan = acquisition_plan(pages, cache, live={"2026"}, wait=2,
                            today=dt.date(2026, 5, 1), frozen=set(), future=[])
    calls, sleeps = [], []
    # Do not replace time.sleep process-wide: code_version() uses subprocess,
    # whose Python 3.11 polling also sleeps and must not pollute these counts.
    monkeypatch.setattr(queryu, "time", SimpleNamespace(sleep=sleeps.append))

    def handler(request):
        calls.append(request)
        return queryu.Response(304 if request.headers.get("If-None-Match") else 404)

    fetcher = PoliteFetcher(cache, wait=2, client=queryu.HandlerClient(handler))
    for key, url, group in pages:
        fetcher.get(key, url, group, live=group == "2026")
    conditional = sum(bool(r.headers.get("If-None-Match") or r.headers.get("If-Modified-Since")) for r in calls)
    assert plan["cached"] == 1
    assert plan["conditional"] == conditional == 1
    assert plan["unconditional"] == 1
    assert plan["new"] == 1
    assert plan["requests"] == len(calls) == 3
    assert plan["gaps"] == len(sleeps) == 2
    assert plan["estimated_wait_seconds"] == sum(sleeps) == 4


@pytest.mark.parametrize("count", [0, 1, 2])
def test_plan_wait_boundaries(tmp_path, count):
    pages = [(str(i), "https://example.test/", "2026") for i in range(count)]
    plan = acquisition_plan(pages, Cache(tmp_path), live=set(), wait=3,
                            today=dt.date(2026, 1, 1), frozen=set(), future=[])
    assert plan["requests"] == count
    assert plan["estimated_wait_seconds"] == 3 * max(0, count - 1)


def test_offline_plan_has_no_http_or_sleep_and_reports_missing(tmp_path, monkeypatch, capsys):
    cache = Cache(tmp_path / "cache")
    cache.put("2026_03", "https://example.test/march", "2026", 200, b"cached", {"etag": '"v1"'})
    monkeypatch.setattr(queryu, "UrllibClient", lambda **kw: pytest.fail("offline plan created a client"))
    assert main(["--source", "npb_calendar", "--month-key", MONTH_KEY, "--years", "2026", "--cache", str(cache.root),
                 "--frozen", str(tmp_path / "frozen.json"), "--today", "2026-04-01", "--offline", "--plan"]) == 0
    output = capsys.readouterr().out
    assert "キャッシュ済み（再確認しない）: 1" in output
    assert "条件付きGET: 0" in output
    assert "オフラインで不足するページ: 1" in output
    assert "待ち時間の見積もり: 0" in output


@pytest.mark.parametrize("today,kept", [("2026-12-31", ["2026_12"]), ("2027-01-01", ["2026_12", "2027_01"])])
def test_future_filter_at_year_boundary_keeps_non_month_keys(today, kept):
    pages = [(key, "u", key[:4]) for key in ["2026_12", "2027_01", "2027_03", "2027_c"]]
    actual, future = queryu.split_future_months(pages, dt.date.fromisoformat(today), MONTH_KEY)
    assert [p[0] for p in actual] == [*kept, "2027_c"]
    assert len(actual) + len(future) == len(pages)


def test_missing_sets_with_unread_text_never_suggests_propositions(tmp_path, capsys):
    (tmp_path / "propositions.jsonl").write_text(json.dumps({"id": "P1", "statement": "雨の日", "forms": []}))
    assert ask.main(["E25 に雨の日だけの反例はある？", "--results", str(tmp_path), "--json", "--suggest-threshold", "0"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["answer"] == "判定不能"
    assert payload["query"]["target"] == "E25"
    assert payload["suggestions"] == []
    assert payload["suggestion_status"] is None
