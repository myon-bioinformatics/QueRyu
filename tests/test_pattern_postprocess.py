"""抽出後の並べ替え・絞り込み。パターン表と検出は BlueProbe に聞く。"""

import ast, json
from pathlib import Path

import pytest

import blueprobe
import queryu as pattern_postprocess
from queryu import (
    filter_table,
    filter_text,
    _post_main as main,
    pattern_for_html,
    _post_process as process,
    sort_table,
    sort_text,
)

_QUERYU = (Path(__file__).resolve().parents[1] / "queryu.py").read_text(encoding="utf-8")
# 抽出後の加工（post）の節だけを見る
SOURCE = _QUERYU[_QUERYU.index("# ──── post "):_QUERYU.index("# ──── tools:")]


def test_module_calls_blueprobe_without_a_local_pattern_table():
    tree = ast.parse(SOURCE)
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.setdefault(node.module, []).extend(alias.name for alias in node.names)
        if isinstance(node, ast.Import):
            assert all(alias.name == "blueprobe" or "." not in alias.name for alias in node.names)
    assert set(imported["blueprobe"]) == {"detect_pattern", "get_pattern"}  # 遅延 import
    assert all("." not in module for module in imported)
    assert "dom_patterns" not in SOURCE
    assert "load_source" not in SOURCE
    assert "DOM_PATTERNS" not in SOURCE
    handlers = [handler.type for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler) for handler in [node]]
    assert all(handler is not None for handler in handlers)
    for handler in handlers:
        name = ast.unparse(handler)
        assert name != "Exception"
        assert not name.startswith("Exception")
    assert not hasattr(pattern_postprocess, "extract_from_har")
    assert not hasattr(pattern_postprocess, "extract_pattern")


def test_sort_text_alphabetical_is_stable_under_casefold():
    assert sort_text(["b", "A", "c"], "alphabetical") == ["A", "b", "c"]
    assert sort_text(["b", "B"], "alphabetical") == ["b", "B"]
    assert sort_text(["B", "b"], "alphabetical") == ["B", "b"]
    assert sort_text(["10", "2", "1"], "alphabetical") == ["1", "10", "2"]


def test_sort_text_numeric_orders_values_and_rejects_non_numbers():
    assert sort_text(["10", "2", "1", "1.5", "-3"], "numeric") == ["-3", "1", "1.5", "2", "10"]
    assert sort_text(["１０", "２"], "numeric") == ["２", "１０"]
    with pytest.raises(ValueError, match="数値として並べられません"):
        sort_text(["R"], "numeric")
    with pytest.raises(ValueError, match="数値として並べられません"):
        sort_text(["NaN"], "numeric")
    with pytest.raises(ValueError, match="未知の並べ替え"):
        sort_text(["1"], "locale")


def test_null_text_is_an_error_not_an_attribute_failure():
    with pytest.raises(ValueError, match="null"):
        sort_text([None], "alphabetical")
    with pytest.raises(ValueError, match="null"):
        sort_table([["ヘッダ"], [None]], column=0, mode="alphabetical")


def test_filter_text_conditions():
    values = ["ドラゴンズ", "タイガース", "ab", "A"]
    assert filter_text(values, [{"op": "contains", "value": "ドラゴン"}]) == ["ドラゴンズ"]
    assert filter_text(["A", "a"], [{"op": "equals", "value": "A"}]) == ["A"]
    assert filter_text(["Readme", "OTHER"], [{"op": "contains", "value": "readme", "casefold": True}]) == ["Readme"]
    assert filter_text(["ab", "a", "b"], [
        {"op": "contains", "value": "a"},
        {"op": "contains", "value": "b"},
    ]) == ["ab"]
    assert filter_text(["1回", "2回", "x"], [{"op": "regex", "value": r"^\d"}]) == ["1回", "2回"]
    assert filter_text(["keep", "drop"], [{"op": "not_contains", "value": "drop"}]) == ["keep"]
    assert filter_text(["ab", "a"], []) == ["ab", "a"]
    with pytest.raises(ValueError, match="正規表現が不正"):
        filter_text(["a"], [{"op": "regex", "value": "(["}])
    with pytest.raises(ValueError, match="未知の絞り込み"):
        filter_text(["a"], [{"op": "glob", "value": "*"}])


def test_sort_and_filter_table_keep_header():
    rows = [["チーム", "得点"], ["ドラゴンズ", "10"], ["タイガース", "2"], ["カープ", "10"]]
    assert sort_table(rows, column=1, mode="numeric") == [
        ["チーム", "得点"],
        ["タイガース", "2"],
        ["ドラゴンズ", "10"],
        ["カープ", "10"],
    ]
    assert filter_table(rows, column=0, conditions=[{"op": "contains", "value": "ドラゴン"}]) == [
        ["チーム", "得点"],
        ["ドラゴンズ", "10"],
    ]
    assert sort_table([["2"], ["10"], ["1"]], column=0, mode="numeric", header=False) == [["1"], ["2"], ["10"]]
    with pytest.raises(ValueError, match="範囲外"):
        sort_table([["のみ"], ["値"]], column=3, mode="alphabetical")
    original = [["2"], ["10"]]
    sort_table(original, column=0, mode="numeric", header=False)
    assert original == [["2"], ["10"]]


def test_pattern_for_html_uses_blueprobe_and_surfaces_misses():
    found = pattern_for_html('<article class="markdown-body"><h1>Auto README</h1></article>')
    assert found["pattern_key"] == "github_readme"
    assert "markdown-body" in found["selector"]
    explicit = pattern_for_html("<div>unused</div>", pattern_key=" npb_linescore ")
    assert explicit["pattern_key"] == "npb_linescore"
    assert explicit["selector"].startswith("table.score_table")
    with pytest.raises(ValueError, match="一致しません"):
        pattern_for_html("<div><span>no pattern</span></div>")
    with pytest.raises(ValueError, match="unknown pattern key"):
        pattern_for_html("<main>x</main>", pattern_key="not_a_pattern")


def test_detection_errors_propagate(monkeypatch):
    def boom(html):
        raise RuntimeError("detector failed")

    monkeypatch.setattr(blueprobe, "detect_pattern", boom)
    with pytest.raises(RuntimeError, match="detector failed"):
        pattern_for_html("<main>x</main>")


def test_process_filters_then_sorts_without_mutating_input():
    payload = {
        "text": ["b", "a", "c"],
        "tables": [[["名前"], ["b"], ["a"]]],
        "pattern_key": "generic_main",
    }
    result = process(
        payload,
        sort="alphabetical",
        conditions=[{"op": "not_contains", "value": "c"}],
    )
    assert result["schema"] == "queryu-pattern-postprocess/1"
    assert result["text"] == ["a", "b"]
    assert result["tables"] == [[["名前"], ["a"], ["b"]]]
    assert result["pattern"]["pattern_key"] == "generic_main"
    assert payload["text"] == ["b", "a", "c"]


def test_cli_sort_filter_and_pattern(tmp_path):
    source = tmp_path / "in.json"
    out = tmp_path / "nested" / "out.json"
    html = tmp_path / "page.html"
    source.write_text(json.dumps({
        "text": ["10", "2", "11"],
        "tables": [[["得点"], ["10"], ["2"]]],
    }), encoding="utf-8")
    html.write_text('<article class="markdown-body"><h1>CLI</h1></article>', encoding="utf-8")
    code = main([
        "--input", str(source), "--out", str(out), "--sort", "numeric",
        "--filter", "regex=^1", "--html", str(html),
    ])
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["text"] == ["10", "11"]
    assert data["tables"] == [[["得点"], ["10"]]]
    assert data["pattern"]["pattern_key"] == "github_readme"


def test_cli_invalid_json_does_not_succeed(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["--input", str(path), "--sort", "alphabetical"])
    assert exc.value.code == 2


def test_cli_numeric_failure_is_nonzero(tmp_path, capsys):
    path = tmp_path / "in.json"
    path.write_text(json.dumps({"text": ["得点"]}), encoding="utf-8")
    assert main(["--input", str(path), "--sort", "numeric"]) == 1
    assert "数値として並べられません" in capsys.readouterr().err


@pytest.mark.parametrize("key", ["npb_calendar", " npb_linescore "])
@pytest.mark.parametrize("html", [None, '<table class="score_table"><tr><td>1</td></tr></table>'])
def test_explicit_research_key_is_scoped_and_metadata_only(key, html):
    import blueprobe
    before = blueprobe.detect_pattern('<table class="score_table"><tr><td>1</td></tr></table>')
    payload = {"pattern_key": key, "text": ["keep"]}
    if html is not None:
        payload["html"] = html
    result = process(payload)
    assert result["pattern"]["pattern_key"] == key.strip()
    assert result["pattern"]["matched_nodes"] is None
    assert result["text"] == ["keep"]
    assert blueprobe.detect_pattern('<table class="score_table"><tr><td>1</td></tr></table>') == before
    assert pattern_for_html('<article class="markdown-body">generic</article>')["pattern_key"] == "github_readme"


def test_optional_research_metadata_is_copied():
    first = pattern_postprocess._requested_pattern("npb_calendar")
    first["example_sites"].append("mutation")
    assert "mutation" not in pattern_postprocess._requested_pattern("npb_calendar")["example_sites"]


def test_unknown_research_key_stays_an_error():
    with pytest.raises(ValueError):
        process({"pattern_key": "npb_missing"})


def test_generic_lookup_does_not_import_optional_research(monkeypatch):
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == "npb_patterns":
            raise AssertionError("generic path must not import optional research")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    assert process({"pattern_key": "github_readme"})["pattern"]["pattern_key"] == "github_readme"
    assert pattern_for_html('<article class="markdown-body">generic</article>')["pattern_key"] == "github_readme"



@pytest.mark.parametrize('missing', ['npb_patterns', 'nested_dependency'])
def test_optional_import_preserves_original_or_nested_error(monkeypatch, missing):
    import builtins
    original = ValueError('original unknown key')
    failure = ModuleNotFoundError('missing', name=missing)
    def lookup(key):
        raise original
    real_import = builtins.__import__
    def importing(name, *args, **kwargs):
        if name == 'npb_patterns':
            raise failure
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(blueprobe, 'get_pattern', lookup)
    monkeypatch.setattr(builtins, '__import__', importing)
    expected = original if missing == 'npb_patterns' else failure
    with pytest.raises(type(expected)) as caught:
        pattern_postprocess._requested_pattern('npb_missing')
    assert caught.value is expected
