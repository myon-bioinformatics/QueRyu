"""ask/suggest.py が vendor した numpy_min を使い、本物の numpy と同じ結果になることを確かめる。

numpy は dev 依存で、ここで突き合わせるためだけに使う。NUMPY_MIN_PURE=1 で stdlib 経路も確かめる。
"""

import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy
import pytest

import suggest

ROOT = Path(__file__).resolve().parents[1]
SUGGEST = ROOT / "ask" / "suggest.py"
VENDORED = ROOT / "vendor" / "aoi" / "shrink" / "numpy_min.py"

DOCS = [
    ("P46", "中日は得点が5位以下の年に走者ひとりあたりの得点が他より少ないという命題"),
    ("P47", "中日は得点が5位以下の年に走者ひとりあたりの得点が他より少ないという隣の命題"),
    ("P1", "得失点差がプラスなら上位半分である"),
    ("P9", "1点差の試合で勝ち越すと期待勝利数を上回る"),
    ("E25", "本塁打が多い年は得点も多い"),
    ("E24", "盗塁が多い年は失点が少ない"),
    ("N1", "明日の天気とビールの売上"),
    ("N2", "監督の名前と本拠地"),
]
QUESTIONS = ["得失点差がプラスなら上位か", "中日の走者あたりの得点", "1点差で勝つと", "本塁打と得点", "天気", "無関係な問い xyz"]


def _suggest_on_real_numpy():
    """同じ suggest.py を、np だけ本物の numpy に差し替えて読む。"""
    name = "queryu_suggest_real_numpy"
    spec = importlib.util.spec_from_file_location(name, SUGGEST)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module.np = numpy
    return module


def test_suggest_imports_no_numpy():
    tree = ast.parse(SUGGEST.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert "numpy" not in names
    assert Path(suggest.np.__file__).resolve() == VENDORED.resolve()


def test_runtime_does_not_load_numpy():
    code = "import sys; sys.path[:0] = ['ask']; import suggest; assert 'numpy' not in sys.modules, sorted(sys.modules)"
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)


# 中心化した 8 文書の階数は 7。dim >= 8 は零特異値の方向（どの LAPACK でも任意）を含むので比べない
@pytest.mark.parametrize("dim", [2, 4, 7])
@pytest.mark.parametrize("mode", ["float", "binary"])
def test_query_matches_real_numpy(dim, mode):
    real = _suggest_on_real_numpy()
    mine_idx = suggest.Index(DOCS, dim=dim)
    real_idx = real.Index(DOCS, dim=dim)
    assert mine_idx.Zf.shape == real_idx.Zf.shape
    assert mine_idx.sizes() == real_idx.sizes()
    for q in QUESTIONS:
        mine = mine_idx.query(q, mode, len(DOCS))
        want = real_idx.query(q, mode, len(DOCS))
        # 類似度は float32 の丸めの範囲で一致。並びは、ほぼ同点の組を除いて一致する
        assert dict(mine).keys() == dict(want).keys()
        for doc_id, score in want:
            assert dict(mine)[doc_id] == pytest.approx(score, abs=1e-4)
        mine_order = [i for i, _ in mine]
        for a, (ia, sa) in enumerate(want):
            for ib, sb in want[a + 1:]:
                if sa - sb > 1e-4:
                    assert mine_order.index(ia) < mine_order.index(ib)


def test_suggest_matches_real_numpy():
    real = _suggest_on_real_numpy()
    for q in QUESTIONS:
        mine = suggest.suggest(q, DOCS, threshold=0.3, dim=4, k=5)
        want = real.suggest(q, DOCS, threshold=0.3, dim=4, k=5)
        assert mine["status"] == want["status"]
        assert mine["selected"] == want["selected"]


def test_unit_matches_real_numpy():
    real = _suggest_on_real_numpy()
    rows = [[3.0, 4.0, 0.0], [0.0, 0.0, 0.0], [1.0, -2.0, 2.0]]
    got = suggest._unit(suggest.np.array(rows, suggest.np.float32)).tolist()
    want = real._unit(numpy.array(rows, numpy.float32)).tolist()
    assert numpy.allclose(got, want, atol=1e-6)


def test_stdlib_fallback_matches_lapack():
    """LAPACK/BLAS が無い環境（NUMPY_MIN_PURE=1）でも同じ候補になる。"""
    code = (
        "import json, sys; sys.path[:0] = ['ask', 'tests']; import suggest, test_queryu_numpy_min as t;"
        "print(json.dumps([suggest.np.BACKEND, [suggest.Index(t.DOCS, dim=4).query(q, 'float', 8) for q in t.QUESTIONS]]))"
    )
    env = dict(os.environ, NUMPY_MIN_PURE="1")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, check=True, capture_output=True, text=True)
    backend, pure = json.loads(out.stdout)
    assert backend == "stdlib"
    idx = _suggest_on_real_numpy().Index(DOCS, dim=4)
    for q, got in zip(QUESTIONS, pure):
        want = dict(idx.query(q, "float", 8))
        assert {i: pytest.approx(s, abs=1e-4) for i, s in want.items()} == dict(got)


def test_backend_matches_ci_lane():
    """CI の lane（QUERYU_EXPECT_BACKEND）で、想定した経路が選ばれている。"""
    expected = os.environ.get("QUERYU_EXPECT_BACKEND")
    if not expected:
        pytest.skip("QUERYU_EXPECT_BACKEND が無い（ローカル実行）")
    assert suggest.np.BACKEND == expected


def test_vendor_lock_matches_vendored_copy():
    lock = json.loads((ROOT / "vendor.lock.json").read_text(encoding="utf-8"))
    assert lock["schema"] == "vendor-lock/1" and set(lock) == {"schema", "files"}
    (entry,) = lock["files"]
    assert set(entry) == {"repository", "ref", "commit", "source", "destination", "blob_sha", "sha256"}
    assert entry["repository"] == "myon-bioinformatics/Aoi"
    assert entry["ref"] == entry["commit"] and len(entry["commit"]) == 40
    assert entry["source"] == "shrink/numpy_min.py"
    assert entry["destination"] == "vendor/aoi/shrink/numpy_min.py"
    data = (ROOT / entry["destination"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    blob = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
    assert blob == entry["blob_sha"]
    aoi = os.environ.get("AOI_ROOT")
    if aoi and (Path(aoi) / entry["source"]).is_file():
        # Aoi の checkout（CI では最新 main）とずれたら知らせる。更新は lock ごと PR で行う
        upstream = hashlib.sha256((Path(aoi) / entry["source"]).read_bytes()).hexdigest()
        if upstream != entry["sha256"]:
            pytest.xfail(f"Aoi の numpy_min.py が vendor 時から変わった（{upstream}）")
