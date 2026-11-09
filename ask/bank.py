"""Materialize the finite, unfiltered registered questions without an LLM.

Known answers and the inbox cache share one key:

    (cycle, results SHA/hash, answer-code hash, Query)

The directory is chosen by the caller. This module does not assume a sibling
folder named ``known``.
"""
import hashlib, json
from dataclasses import asdict; from pathlib import Path
from ask import ASKS, FORMS, Query, answer, load

RESULT_FILES = ("sets.jsonl", "propositions.jsonl")
CODE_FILES = ("ask.py", "bank.py")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def results_file_hashes(results: Path) -> dict:
    """Hash each result file that is present. A missing file stays null."""
    out = {}
    for name in RESULT_FILES:
        path = Path(results) / name
        out[name] = _sha256(path) if path.is_file() else None
    return out


def answer_code_hash() -> str:
    payload = {name: _sha256(Path(__file__).with_name(name)) for name in CODE_FILES}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _require_cycle(cycle: str) -> str:
    if not isinstance(cycle, str) or not cycle or "/" in cycle or "\\" in cycle or ".." in cycle:
        raise ValueError("cycle が必要")
    return cycle


def key_material(cycle: str, results: Path, result_sha: str, query: Query | None = None) -> dict:
    """The shared identity. ``query`` is omitted only for the known-answer snapshot folder."""
    if not isinstance(result_sha, str):
        raise ValueError("result_sha は文字列")
    body = {
        "cycle": _require_cycle(cycle),
        "results": {"sha": result_sha, "files": results_file_hashes(results)},
        "answer_code": answer_code_hash(),
    }
    if query is not None:
        body["query"] = asdict(query)
    return body


def digest(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def snapshot_id(cycle: str, results: Path, result_sha: str) -> str:
    return digest(key_material(cycle, results, result_sha))


def cache_key(cycle: str, results: Path, query: Query, result_sha: str) -> str:
    return digest(key_material(cycle, results, result_sha, query))


def query_key(query):
    return json.dumps(asdict(query), sort_keys=True, ensure_ascii=False)


def prepare(results: Path, destination: Path, *, cycle: str, result_sha: str):
    version = snapshot_id(cycle, results, result_sha)
    destination = Path(destination)
    manifest = destination / "manifest.json"
    if manifest.exists():
        record = json.loads(manifest.read_text(encoding="utf-8"))
        if record.get("fingerprint") == version and record.get("cycle") == cycle and record.get("result_sha") == result_sha:
            return record
    items = load(results)
    folder = destination / version
    folder.mkdir(parents=True, exist_ok=True)
    count = 0
    for target in sorted({it["id"] for it in items}):
        rows = [it for it in items if it["id"] == target]
        answers = {}
        for form in FORMS:
            for ask in ASKS:
                q = Query(target=target, form=form, ask=ask)
                answers[query_key(q)] = answer(q, rows)
        (folder / f"{target}.json").write_text(json.dumps(answers, ensure_ascii=False) + "\n", encoding="utf-8")
        count += len(answers)
    record = {
        "fingerprint": version, "cycle": cycle, "result_sha": result_sha,
        "targets": len({it["id"] for it in items}), "questions": count,
        "scope": "登録済みID × 4つの形 × 4種類。追加条件のある質問は受付時に同じ判定結果から回答。",
    }
    temp = manifest.with_suffix(".tmp")
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(manifest)
    return record


def lookup(query, results: Path, destination: Path, *, cycle: str, result_sha: str):
    """Return the prepared answer for this cycle and result identity, or None.

    An older snapshot under ``destination`` is not served.
    """
    if not query.target:
        return None
    path = Path(destination) / snapshot_id(cycle, results, result_sha) / f"{query.target}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get(query_key(query))
