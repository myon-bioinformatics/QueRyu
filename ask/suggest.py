"""自然言語の問いから、確認用の命題・式 ID を最大5件出す。

答えは出さない。ask.answer も ask.parse も呼ばない。類似度が閾値未満なら status は「判定不能」で、
候補を答えとして確定しない。閾値は呼び出し側が渡す。試作で範囲外の問いに合わせた値は、ここには置かない。

文書は文字 1〜3-gram の TF-IDF を SVD で落としたもの。mode="binary" は符号と次元スケールで圧縮した索引。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

import importlib.util


def _load_numpy_min():
    """vendor/aoi の numpy_min を読む。numpy 本体は import しない（lock は vendor.lock.json）。"""
    name = "queryu_numpy_min"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parents[1] / "vendor" / "aoi" / "shrink" / "numpy_min.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


np = _load_numpy_min()

# 試作（手書き 35 問、2026-10-12）の観測。このモジュールの既定値にはしない。
# 範囲外 7 問の最高類似度のすぐ上に閾値を置くと、範囲外は 7/7 が判定不能になった。
# そのとき 200 次元 float の閾値は 0.4976 で、閾値を超えた言い換え 22 問のうち 4 問は別 ID だった。
# 外れの多くは隣の番号（P46 と P47、E25 と E24、E26 と E22）。試験集合が小さく、閾値は過学習の恐れがある。
PROVISIONAL_FIT_NOTE = (
    "試作の閾値は手書き 35 問の範囲外 7 問に合わせたもので、過学習の恐れがある。"
    "200 次元 float では 0.4976。閾値を超えても兄弟 ID（P46/P47、E25/E24）を取り違える。"
)

STOP = re.compile(r"[、。・（）()「」『』:：,，\s?？!！]+")
_ID = re.compile(r"(?<![A-Za-z])([PE])\s?(\d{1,4})(?!\d)")


def norm(text: str) -> str:
    return STOP.sub(" ", unicodedata.normalize("NFKC", text)).strip().lower()


def cond_text(conds) -> str:
    return " ".join(f"{c['col']} {c['op']} {c['value']}" for c in conds or [])


def load_docs(results: Path) -> list[tuple[str, str]]:
    """propositions.jsonl と、あれば sets.jsonl。ID と、文・条件の文字列。"""
    docs = []
    found = False
    for name in ("propositions.jsonl", "sets.jsonl"):
        path = Path(results) / name
        if not path.exists():
            continue
        found = True
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            groups = " ".join(cond_text(g) for g in d.get("if_any", []))
            body = f"{d.get('statement', '')} {cond_text(d.get('if'))} {groups} {cond_text(d.get('then'))}"
            docs.append((str(d["id"]), body))
    if not found:
        raise FileNotFoundError(f"propositions.jsonl も sets.jsonl もない: {results}")
    return docs


def ngrams(text: str, ns=(1, 2, 3)) -> list[str]:
    t = norm(text)
    return [t[i:i + n] for n in ns for i in range(len(t) - n + 1) if " " not in t[i:i + n]]


def _unit(x: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    return np.divide(x, n, out=np.zeros_like(x), where=n > 0)


class Index:
    def __init__(self, docs: list[tuple[str, str]], dim: int = 128):
        if not docs:
            raise ValueError("文書がない")
        if dim < 1:
            raise ValueError("dim は 1 以上")
        self.ids = [i for i, _ in docs]
        vocab: dict[str, int] = {}
        rows = []
        for _, txt in docs:
            counts: dict[int, int] = {}
            for g in ngrams(txt):
                j = vocab.setdefault(g, len(vocab))
                counts[j] = counts.get(j, 0) + 1
            rows.append(counts)
        self.vocab = vocab
        X = np.zeros((len(docs), max(len(vocab), 1)), np.float32)
        for r, counts in enumerate(rows):
            for j, v in counts.items():
                X[r, j] = 1 + np.log(v)
        if not vocab:
            X[:] = 0
        df = (X > 0).sum(0)
        self.idf = np.log((1 + len(docs)) / (1 + df)) + 1
        X *= self.idf
        X = _unit(X)
        self.mu = X.mean(0)
        centered = X - self.mu
        if vocab and centered.shape[1] > 0 and np.any(centered):
            _U, _S, Vt = np.linalg.svd(centered, full_matrices=False)
            rank = min(dim, Vt.shape[0])
            self.P = Vt[:rank].T.astype(np.float32)
        else:
            self.P = np.zeros((X.shape[1], 1), np.float32)
        Z = self.embed_raw(X)
        self.Zf = _unit(Z)
        signs = np.sign(self.Zf)
        signs[signs == 0] = 1
        self.l = np.abs(self.Zf).mean(0)
        self.bits = np.packbits(signs > 0, axis=1)
        self.B = signs.astype(np.int8)

    def vec(self, text: str) -> np.ndarray:
        x = np.zeros(len(self.vocab) or self.P.shape[0], np.float32)
        if not self.vocab:
            return x
        for g in ngrams(text):
            j = self.vocab.get(g)
            if j is not None:
                x[j] += 1
        nz = x > 0
        x[nz] = 1 + np.log(x[nz])
        x *= self.idf
        n = np.linalg.norm(x)
        return x / n if n else x

    def embed_raw(self, X: np.ndarray) -> np.ndarray:
        return (X - self.mu) @ self.P

    def query(self, text: str, mode: str = "float", k: int = 5) -> list[tuple[str, float]]:
        if mode not in ("float", "binary"):
            raise ValueError(f"mode は float / binary: {mode}")
        if k < 1:
            raise ValueError("k は 1 以上")
        z = self.embed_raw(self.vec(text)[None])[0]
        n = np.linalg.norm(z)
        z = z / n if n else z
        if mode == "float":
            sim = self.Zf @ z
        else:
            # 文書は符号 × 次元スケール。問いは float のまま（1本なので軽い）
            scale = float(np.linalg.norm(self.l) or 1.0)
            sim = ((self.B * self.l) @ z) / scale
        sim = np.nan_to_num(sim, nan=-np.inf, posinf=np.inf, neginf=-np.inf)
        k = min(k, sim.size)
        # 同じ類似度の候補は文書の並び（ids の順）で決める。既定の quicksort は同点の並びが決まらない
        order = np.argsort(-sim, kind="stable")[:k]
        return [(self.ids[int(i)], float(sim[int(i)])) for i in order]

    def sizes(self) -> dict[str, int]:
        n, d = self.Zf.shape
        return {"float32_bytes": int(n * d * 4), "binary_bytes": int(self.bits.nbytes + d * 4 + n * 4)}


def written_id(text: str) -> str | None:
    m = _ID.search(unicodedata.normalize("NFKC", text))
    if not m:
        return None
    return f"{m[1]}{int(m[2])}"


def suggest(text: str, docs: list[tuple[str, str]], *, threshold: float, mode: str = "float",
            dim: int = 128, k: int = 5, index: Index | None = None) -> dict:
    """候補を返す。selected に入るのは、問いに書いてある ID だけ。類似度では選ばない。

    status:
      explicit … 問いに ID が書いてある。索引は使わない。書いてあるものと対象が違うことがある。
                 書いてある ID が docs に無いときは known=False にし、note で知らせる（selected はそのまま）
      要確認   … 1位の類似度が閾値以上。候補であり、答えではない
      判定不能 … 1位の類似度が閾値未満。候補リストは残すが、確定しない
    """
    if not math_isfinite(threshold):
        raise ValueError("threshold は有限の数")
    named = written_id(text)
    if named:
        known = any(i == named for i, _ in docs)
        note = "問いに書いてある ID。類似度では選んでいない。答えではない。"
        if not known:
            note += f" 注意: {named} は読み込んだ命題・式に見つからない（番号の書き間違いか、別の結果の ID かもしれない）。"
        return {"status": "explicit", "selected": named, "known": known,
                "candidates": [{"id": named, "similarity": None}], "note": note}
    idx = index if index is not None else Index(docs, dim)
    top = idx.query(text, mode, k)
    candidates = [{"id": i, "similarity": s} for i, s in top]
    if not top or top[0][1] < threshold:
        return {"status": "判定不能", "selected": None, "candidates": candidates,
                "note": "類似度が閾値未満。候補は確定していない。"}
    return {"status": "要確認", "selected": None, "candidates": candidates,
            "note": "確認用の候補。1位を ask.py に渡さない。"}


def math_isfinite(v: float) -> bool:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return False
    return x == x and x not in (float("inf"), float("-inf"))


def similar_candidates(text: str, docs: list[tuple[str, str]], *, threshold: float | None, mode: str = "float",
                       dim: int = 128, k: int = 5, index: Index | None = None) -> dict:
    """類似する ID を最大 k 件返す。selected は常に null。答えにはしない。

    問いに ID が書いてあっても索引を使う（番号の書き間違いを、書いてある ID の繰り返しにしない）。
    閾値が None のときは、どの候補も閾値を超えた扱いにしない。
    """
    named = written_id(text)
    body = _ID.sub(" ", text).strip()
    if written_id(body):
        body = _ID.sub(" ", body).strip()
    if not body:
        body = text
    # None は「設定が無い」。有限の閾値を置いたことにはせず、候補だけ返す。
    got = suggest(body, docs, threshold=-1.0 if threshold is None else threshold,
                  mode=mode, dim=dim, k=k, index=index)
    explicit = got.get("status") == "explicit"
    got["selected"] = None
    got["written_id"] = named
    got["note"] = "候補。答えではない。"
    if explicit:
        got["status"] = "判定不能"
        got["candidates"] = []
    elif threshold is None:
        got["status"] = "判定不能"
    return got


def suggest_results(text: str, results: Path, **kw) -> dict:
    return suggest(text, load_docs(results), **kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="問いから命題・式 ID の候補を出す（答えは出さない）")
    ap.add_argument("question")
    ap.add_argument("--results", type=Path, required=True, help="判定済みの結果フォルダ")
    ap.add_argument("--threshold", type=float, required=True, help="1位の類似度がこれ未満なら判定不能。試験集合に合わせた既定値はない")
    ap.add_argument("--mode", choices=("float", "binary"), default="float")
    ap.add_argument("--dim", type=int, default=128)
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args(argv)
    results = args.results
    try:
        got = suggest_results(args.question, results, threshold=args.threshold, mode=args.mode,
                              dim=args.dim, k=args.top)
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(json.dumps(got, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
