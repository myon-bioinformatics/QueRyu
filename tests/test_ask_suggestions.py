"""判定不能の返信に、答えではない候補 ID を最大 5 件付ける。"""
import json
from pathlib import Path

import ask
import suggest
from ask import Query, answer_results, main, suggestion_for

DOCS = [
    ("P46", "中日は得点が5位以下の年に走者ひとりあたりの得点が他より少ない"),
    ("P47", "中日は得点が5位以下の年に走者ひとりあたりの得点が他より少ない隣"),
    ("P1", "得失点差がプラスなら上位半分である"),
    ("P9", "1点差の試合で勝ち越すと期待勝利数を上回る"),
    ("P2", "失点順位が上位なら勝率も上位"),
    ("P3", "得点順位が下位ならBクラス"),
    ("P4", "本拠地の観客が多い年は順位が上"),
    ("P5", "救援の失点が少ない年は上位"),
]


def _write(results: Path, rows):
    results.mkdir(parents=True, exist_ok=True)
    (results / "propositions.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _prop(pid, statement):
    return {
        "id": pid, "statement": statement,
        "if": [{"col": "rd", "op": ">", "value": 0}],
        "if_any": [], "then": [{"col": "upper_half", "op": "==", "value": True}],
        "forms": [{"form": "original", "n": 4, "hold": 3, "rate": 0.75,
                   "counterexamples": [{"unit": "t-2015"}]}],
    }


def test_similar_candidates_never_select_an_id():
    got = suggest.similar_candidates("P46 の隣の番号だったかもしれない", DOCS, threshold=0.0, dim=4, k=5)
    assert got["selected"] is None
    assert got["written_id"] == "P46"
    assert 1 <= len(got["candidates"]) <= 5
    assert "答えではない" in got["note"]


def test_unread_appends_candidates_and_does_not_answer_them(tmp_path, capsys):
    results = tmp_path / "out"
    _write(results, [_prop(i, t) for i, t in DOCS])
    (results / "sets.jsonl").write_text("", encoding="utf-8")
    text = "中日は得点が5位以下の年に走者ひとりあたりの得点が他より少ない、雨の日だけ"
    code = main([text, "--results", str(results), "--json", "--suggest-threshold", "0"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["answer"] == "判定不能"
    assert payload["query"]["target"] is None
    assert payload["suggestion_status"] in ("要確認", "判定不能")
    assert 1 <= len(payload["suggestions"]) <= 5
    assert payload.get("selected") is None
    rendered = main([text, "--results", str(results), "--suggest-threshold", "0"])
    shown = capsys.readouterr().out
    assert rendered == 1
    assert "もしかして: " in shown
    assert "（候補。答えではない）" in shown
    assert shown.split("もしかして: ", 1)[1].count(" / ") <= 4
    assert "［" not in shown.split("もしかして: ", 1)[1]


def test_unknown_id_is_not_auto_answered(tmp_path, capsys):
    results = tmp_path / "out"
    _write(results, [_prop(i, t) for i, t in DOCS])
    text = "P999 の判例は？"
    assert main([text, "--results", str(results), "--json", "--suggest-threshold", "-1"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["answer"] == "判定不能"
    assert "結果にない" in payload["error"]
    assert "結果が読めない" not in payload["error"]
    assert payload["query"]["target"] == "P999"
    ids = [c["id"] for c in payload["suggestions"]]
    assert ids and "P999" not in ids and len(ids) <= 5


def test_missing_sets_is_unreadable_and_has_no_candidates(tmp_path, capsys):
    results = tmp_path / "out"
    _write(results, [_prop("P1", "得失点差がプラスなら上位")])
    res = answer_results(Query(target="E25"), results)
    assert res["answer"] == "判定不能"
    assert res["error"] == "結果が読めない: sets.jsonl"
    assert "結果にない" not in res["error"]
    hinted = suggestion_for("E25 の判例は？", results, [], res, threshold=0.0)
    assert hinted["line"] == "" and hinted["candidates"] == []
    assert main(["E25 の判例は？", "--results", str(results), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["suggestions"] == []
    assert "もしかして" not in json.dumps(payload, ensure_ascii=False)


def test_present_file_without_the_id_stays_not_in_results(tmp_path):
    results = tmp_path / "out"
    results.mkdir()
    (results / "sets.jsonl").write_text(json.dumps(_prop("E1", "式")) + "\n", encoding="utf-8")
    res = answer_results(Query(target="E25"), results)
    assert "結果にない" in res["error"]
    assert "結果が読めない" not in res["error"]


def test_answered_question_has_no_moshikashite(tmp_path, capsys):
    results = tmp_path / "out"
    _write(results, [_prop("P1", "得失点差がプラスなら上位")])
    assert main(["P1 の判例は？", "--results", str(results), "--json", "--suggest-threshold", "0"]) == 0
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    assert payload["answer"] == 1
    assert payload["suggestions"] == []
    assert "もしかして" not in raw


def test_threshold_comes_from_cycle_config_and_missing_means_unconfirmed(tmp_path):
    cycle = tmp_path / "cycles" / "c9"
    cycle.mkdir(parents=True)
    (cycle / "ask.toml").write_text("suggest_threshold = 0.2\n", encoding="utf-8")
    assert ask.load_suggest_threshold(cycle) == 0.2
    bare = tmp_path / "cycles" / "empty"
    bare.mkdir()
    (bare / "ask.toml").write_text("# 未設定\n", encoding="utf-8")
    assert ask.load_suggest_threshold(bare) is None
    results = tmp_path / "out"
    _write(results, [_prop(i, t) for i, t in DOCS])
    (results / "sets.jsonl").write_text("", encoding="utf-8")
    hinted = suggestion_for("雨の日だけ教えて", results, ["未対応の語・条件: 雨日だけ教えて"], {"answer": "判定不能", "error": "x", "rows": []}, None)
    assert hinted["status"] == "判定不能"
    assert hinted["selected"] is None
    assert hinted["line"].endswith("（候補。答えではない）")


def test_repo_without_sets_does_not_call_e25_missing():
    import aoi_paths
    results = aoi_paths.default_cycle_outputs(aoi_paths.resolve_aoi_root(anchor=Path(__file__)))
    if (results / "sets.jsonl").is_file():
        return
    res = answer_results(Query(target="E25"), results)
    assert res["error"].startswith("結果が読めない")
    assert "結果にない" not in res["error"]


class _Queue:
    """パイプ・FIFO の代わり。端末ではない標準入力。"""

    def __init__(self, text):
        import io
        self._io = io.StringIO(text)

    def __iter__(self):
        return iter(self._io)

    def read(self):
        return self._io.read()

    def isatty(self):
        return False


def test_question_words_need_no_quotes_and_may_sit_between_options(tmp_path, capsys):
    results = tmp_path / "out"
    _write(results, [_prop("P1", "得失点差がプラスなら上位")])
    assert main(["P1", "--results", str(results), "の判例は？", "--json", "--suggest-threshold", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["answer"] == 1


def test_questions_from_a_queue_are_answered_in_order(tmp_path, capsys, monkeypatch):
    results = tmp_path / "out"
    _write(results, [_prop("P1", "得失点差がプラスなら上位"), _prop("P2", "失点が少なければ上位")])
    monkeypatch.setattr("sys.stdin", _Queue("P1 の判例は？\n\nP2 の判例は？\n"))
    assert main(["--results", str(results), "--json", "--suggest-threshold", "0"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["query"]["target"] for line in lines] == ["P1", "P2"]


def test_query_json_can_come_from_stdin(tmp_path, capsys, monkeypatch):
    results = tmp_path / "out"
    _write(results, [_prop("P1", "得失点差がプラスなら上位")])
    monkeypatch.setattr("sys.stdin", _Queue(json.dumps({"target": "P1"})))
    assert main(["--query", "-", "--results", str(results), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["query"]["target"] == "P1"
