"""ID 候補の提示。ask.py の答えの経路は通さない。"""

import json
from pathlib import Path

import ask
import suggest

import aoi_paths

ROOT = aoi_paths.resolve_aoi_root(anchor=Path(__file__))
RESULTS = aoi_paths.default_cycle_outputs(ROOT)
QUESTIONS = Path(__file__).resolve().parent / "fixtures" / "id_suggest_questions.jsonl"

DOCS = [
    ("P46", "中日は得点が5位以下の年に走者ひとりあたりの得点が他より少ないという命題"),
    ("P47", "中日は得点が5位以下の年に走者ひとりあたりの得点が他より少ないという隣の命題"),
    ("P1", "得失点差がプラスなら上位半分である"),
    ("P9", "1点差の試合で勝ち越すと期待勝利数を上回る"),
    ("N1", "明日の天気とビールの売上"),
    ("N2", "監督の名前と本拠地"),
]


def test_suggest_does_not_call_ask(monkeypatch):
    def fail(*_a, **_k):
        raise AssertionError("ask の答えの経路を呼んだ")

    # 読み取りは、候補モジュールを import しても ID を補完しない
    q, _unread = ask.parse("得失点差がプラスのチームはAクラスになる、という命題の判例は？")
    assert q.target is None
    monkeypatch.setattr(ask, "answer", fail)
    monkeypatch.setattr(ask, "parse", fail)
    monkeypatch.setattr(ask, "main", fail)
    got = suggest.suggest("得失点差がプラスなら上位か", DOCS, threshold=0.0, dim=4, k=5)
    assert got["selected"] is None and got["candidates"]


def test_explicit_id_skips_the_index():
    built = {"n": 0}
    real = suggest.Index

    class Boom(suggest.Index):
        def __init__(self, *a, **k):
            built["n"] += 1
            raise AssertionError("書いてある ID では索引を作らない")

    suggest.Index = Boom
    try:
        got = suggest.suggest("E25 に阪神 2015 以外の判例はある？", DOCS, threshold=0.99)
    finally:
        suggest.Index = real
    assert built["n"] == 0
    assert got["status"] == "explicit" and got["selected"] == "E25"
    assert got["candidates"] == [{"id": "E25", "similarity": None}]
    # E25 は DOCS に無いので、known=False と note で知らせる
    assert got["known"] is False and "見つからない" in got["note"]


def test_handwritten_explicit_questions_keep_the_written_id():
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        q = json.loads(line)
        if q["type"] != "explicit":
            continue
        got = suggest.suggest(q["q"], DOCS, threshold=0.99)
        assert got["status"] == "explicit" and got["selected"] == q["expect"]


def test_below_threshold_is_not_an_answer():
    idx = suggest.Index(DOCS, dim=4)
    text = "明日の先発投手を教えて"
    top = idx.query(text, "float", 5)
    got = suggest.suggest(text, DOCS, threshold=top[0][1] + 1e-6, dim=4, index=idx)
    assert got["status"] == "判定不能" and got["selected"] is None
    assert len(got["candidates"]) == 5
    above = suggest.suggest(text, DOCS, threshold=top[0][1], dim=4, index=idx)
    assert above["status"] == "要確認" and above["selected"] is None


def test_near_duplicate_ids_stay_candidates():
    idx = suggest.Index(DOCS, dim=4)
    got = suggest.suggest("中日は得点が5位以下の年に走者ひとりあたりの得点が他より少ないか", DOCS,
                          threshold=-1.0, dim=4, k=5, index=idx)
    ids = [c["id"] for c in got["candidates"]]
    assert got["status"] == "要確認" and got["selected"] is None
    assert "P46" in ids and "P47" in ids


def test_binary_index_is_smaller_and_still_returns_five():
    idx = suggest.Index(DOCS, dim=4)
    sizes = idx.sizes()
    assert sizes["binary_bytes"] < sizes["float32_bytes"]
    got = suggest.suggest("得失点差がプラスなら上位半分か", DOCS, threshold=-1.0, mode="binary", dim=4, index=idx)
    assert got["status"] == "要確認" and got["selected"] is None and len(got["candidates"]) == 5


def test_real_propositions_refuse_an_unrelated_question():
    docs = suggest.load_docs(RESULTS)
    assert len(docs) >= 50 and any(i == "P46" for i, _ in docs)
    assert (RESULTS / "sets.jsonl").exists() is False or any(i.startswith("E") for i, _ in docs)
    idx = suggest.Index(docs, dim=32)
    text = "明日の先発投手を教えて"
    top = idx.query(text, "float", 5)
    got = suggest.suggest(text, docs, threshold=top[0][1] + 1e-6, mode="float", index=idx)
    assert got["status"] == "判定不能" and got["selected"] is None
    assert len(got["candidates"]) == 5
    assert all(c["id"] in {i for i, _ in docs} for c in got["candidates"])


def test_cli_prints_candidates_and_does_not_answer(tmp_path, capsys):
    results = tmp_path / "out"
    results.mkdir()
    (results / "propositions.jsonl").write_text(
        "\n".join(json.dumps({"id": i, "statement": t, "if": [], "if_any": [], "then": []}, ensure_ascii=False)
                  for i, t in DOCS) + "\n",
        encoding="utf-8")
    # float の類似度は正規化したベクトルの内積なので 1 を超えない。1.1 未満は判定不能になる。
    assert suggest.main(["量子暗号の株価を教えて", "--results", str(results), "--threshold", "1.1", "--dim", "4"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["status"] == "判定不能" and got["selected"] is None


def test_explicit_id_present_in_docs_is_known():
    got = suggest.suggest("P46 の判例は？", DOCS, threshold=0.99)
    assert got["selected"] == "P46" and got["known"] is True and "見つからない" not in got["note"]


def test_ties_are_ordered_by_document_order():
    # 類似度がちょうど同じ文書は、文書の並び（ids の順）で毎回同じ順に出る
    import math

    np = suggest.np  # vendor した numpy_min
    docs = [(f"P{i}", f"得失点差がプラスなら上位半分である {i}") for i in range(20, 0, -1)]
    idx = suggest.Index(docs, dim=4)
    n, d = idx.Zf.shape
    idx.Zf = np.ones((n, d), np.float32) * np.float32(1 / math.sqrt(d))
    idx.B = np.ones(idx.B.shape, np.int8)
    for mode in ("float", "binary"):
        got = idx.query("得失点差がプラスなら上位か", mode, 10)
        assert len({s for _, s in got}) == 1
        assert [i for i, _ in got] == [f"P{i}" for i in range(20, 10, -1)]
