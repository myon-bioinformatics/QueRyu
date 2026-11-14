"""問い合わせ: 日本語の問いを、決まった型（Query）に直し、判定済みの結果から答える（QueRyu/docs/ask.md）。

    python queryu/ask/ask.py "E25 に阪神 2015 以外の判例はある？" --results <結果フォルダ> --teams <球団表.json>
    python queryu/ask/ask.py --schema          # Query の JSON Schema（llama.cpp や MCP のツールで、型で縛った選択に使う）

役割の分け方（Jev の「型で縛った選択」の考え方。モデルも API キーも使わない）:
  読み取り : 問い → Query。選べる値は登録済みのもの（命題・式の ID、球団、4つの形、問いの種類）だけ
  答え     : Query → 判定済みの結果（propositions.jsonl・sets.jsonl、PythDRagoras が出したもの）を引くだけ。計算し直さない
読めなかった語は黙って捨てず、unread に返す。数（%・年）は正規表現で読む。
"""

from __future__ import annotations

import argparse, json, math, re, sys, unicodedata
from dataclasses import asdict, dataclass, field; from pathlib import Path

FORMS = {"original": "元の命題", "contrapositive": "対偶", "converse": "逆", "inverse": "裏"}
RATE_FORMS = {"original": "元", "contrapositive": "対偶", "converse": "逆", "inverse": "裏"}
ASKS = {
    "has_counterexample": "判例はあるか（はい・いいえ）",
    "counterexamples": "判例の一覧",
    "unique_exception": "判例がちょうど1つのもの",
    "rate": "成立率が条件を満たすもの",
}
# 球団名 → コード。中身は呼び出し側（Aoi など）が set_teams / --teams で渡す。ここには持たない
TEAMS: dict[str, str] = {}
CODES: set[str] = set()
KINDS = {"any": "命題と式", "proposition": "命題", "expression": "式"}


@dataclass
class Query:
    """問い合わせの型。どの値も、選べるものの中から選ぶ。"""
    ask: str = "counterexamples"                     # ASKS のどれか
    target: str | None = None                        # 命題・式の ID（P12・E25）。None ならすべて
    kind: str = "any"                                # KINDS のどれか（「式」なら式だけ、「命題」なら命題だけ）
    form: str = "original"                           # FORMS のどれか
    exclude: list[str] = field(default_factory=list)  # 判例から除く単位（t-2015）
    about: list[str] = field(default_factory=list)    # この単位・球団が判例にいるものだけ（t-2015 または t）
    min_rate: float | None = None                     # 成立率の下限（0〜1）
    max_rate: float | None = None                     # 成立率の上限（0〜1）


def schema() -> dict:
    """Query の JSON Schema。選べる値を enum で縛る（llama.cpp の json_schema・MCP のツールの引数に渡せる）。"""
    unit = {"type": "string", "pattern": r"^[a-z]{1,2}(-\d{4})?$"}
    return {
        "type": "object", "additionalProperties": False, "required": ["ask"],
        "properties": {
            "ask": {"enum": list(ASKS)}, "target": {"type": ["string", "null"], "pattern": r"^[PE]\d+$"},
            "kind": {"enum": list(KINDS)}, "form": {"enum": list(FORMS)}, "exclude": {"type": "array", "items": unit},
            "about": {"type": "array", "items": unit},
            "min_rate": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
            "max_rate": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        },
    }


def validate_query(raw: dict) -> Query:
    """Validate all fields at the execution boundary, including LLM output."""
    if not isinstance(raw, dict) or set(raw) - set(Query.__dataclass_fields__):
        raise ValueError("Query は定義済み項目だけを持つ object")
    q = Query(**raw)
    if any(not isinstance(v, str) or v not in choices for v, choices in ((q.ask, ASKS), (q.form, FORMS), (q.kind, KINDS))):
        raise ValueError("ask / form / kind が選択肢の外")
    if q.target is not None and (not isinstance(q.target, str) or not re.fullmatch(r"[PE]\d+", q.target)):
        raise ValueError("target は P番号 / E番号 / null")
    if q.target and q.kind != "any" and q.target[0] != ("P" if q.kind == "proposition" else "E"):
        raise ValueError("target と kind が一致しない")
    for values in (q.exclude, q.about):
        if not isinstance(values, list) or len(values) > 200 or any(
            not isinstance(v, str) or not re.fullmatch(r"[a-z]{1,2}(-\d{4})?", v)
            or v.split("-")[0] not in CODES for v in values
        ):
            raise ValueError("単位は登録済み球団コード、または 球団コード-年")
    for v in (q.min_rate, q.max_rate):
        if v is not None and (type(v) not in (int, float) or not 0 <= v <= 1 or not math.isfinite(v)):
            raise ValueError("成立率は 0〜1 の数値 / null")
    if q.min_rate is not None and q.max_rate is not None and q.min_rate > q.max_rate:
        raise ValueError("成立率の下限が上限より大きい")
    return q


_ID = re.compile(r"(?<![A-Za-z])([PE])\s?(\d{1,4})(?!\d)")
_YEAR = re.compile(r"(20\d{2})(?:年)?")
_CODE_UNIT = re.compile(r"(?<![A-Za-z])([a-z]{1,2})-(\d{4})(?!\d)")
_PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?(%|パーセント|割)\s?(以上|以下|未満|超)?")
_NEVER = r"(?!)"


def _team_pattern(teams) -> str:
    return "|".join(sorted(map(re.escape, teams), key=len, reverse=True)) or _NEVER


_TEAM = re.compile(_team_pattern(TEAMS))


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


@dataclass(frozen=True)
class UnreadSpan:
    """Half-open Python Unicode code-point offsets into the original input."""
    start: int
    end: int
    raw_text: str


@dataclass(frozen=True)
class ParseDiagnostic:
    code: str
    explanation: str


@dataclass
class ParsedInput:
    raw_text: str
    query: Query
    diagnostics: list[ParseDiagnostic] = field(default_factory=list)
    unread_spans: list[UnreadSpan] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "undetermined" if self.diagnostics or self.unread_spans else "parsed"

    @property
    def unread(self) -> list[str]:
        """Compatibility presentation; never used to recover raw text or offsets."""
        return ([d.explanation for d in self.diagnostics if d.code != "unsupported_text"]
                + [f"未対応の語・条件: {span.raw_text}" for span in self.unread_spans])

    def metadata(self) -> dict:
        return {"raw_text": self.raw_text, "parse_status": self.status,
                "parse_diagnostics": [asdict(d) for d in self.diagnostics],
                "unread_spans": [asdict(span) for span in self.unread_spans]}

    def blocked_answer(self) -> dict:
        """Shared CLI/inbox decision, with stable machine reasons and separate display."""
        return {"answer": "判定不能", "status": "undetermined",
                "reason_codes": [d.code for d in self.diagnostics],
                "error": "解釈を確認してください: " + "; ".join(self.unread), "rows": []}


def _norm_with_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """NFKC/whitespace view plus a map back to untouched source segments.

    Combining marks (including half-width dakuten), compatibility expansions,
    and Hangul composition can change length. Keep each normalization segment
    together instead of treating normalized offsets as source offsets.
    """
    chars, offsets = [], []
    start, segment, normalized = 0, "", ""

    def flush(end):
        chars.extend(normalized)
        offsets.extend([(start, end)] * len(normalized))

    for i, char in enumerate(text):
        part = unicodedata.normalize("NFKC", char)
        joined = unicodedata.normalize("NFKC", segment + char)
        if segment and (unicodedata.combining(part[0]) or joined != normalized + part):
            segment += char
            normalized = joined
        else:
            flush(i)
            start, segment, normalized = i, char, part
    flush(len(text))
    view = "".join(chars)
    words = list(re.finditer(r"\S+", view))
    out, source = [], []
    previous_end = 0
    for word in words:
        if out:
            out.append(" ")
            gap = offsets[previous_end:word.start()]
            source.append((min(s for s, _ in gap), max(e for _, e in gap)))
        out.append(word[0])
        source.extend(offsets[word.start():word.end()])
        previous_end = word.end()
    return "".join(out), source


_WORDS = ("判例", "反例", "例外", "成立率", "命題", "式", "対偶", "逆に", "逆", "裏", "元の", "original",
          "一覧", "唯一", "ただ1つ", "1つだけ", "ひとつだけ", "1個だけ", "以外", "除いて", "除く", "抜いて", "抜き",
          "ありますか", "あるの", "ある", "ない", "有無", "はい", "いいえ", "教えてください", "教えて", "ください")
_WORD = re.compile("|".join(map(re.escape, sorted(_WORDS, key=len, reverse=True))))
_TEAM_UNIT = re.compile(r"(?:" + _TEAM.pattern + r")\s*(?:20\d{2}年?)?")


def set_teams(teams: dict[str, str]) -> None:
    """Install the team-name table (name → code) used by the reader. Empty means no team names."""
    global _TEAM, _TEAM_UNIT, CODES
    if not isinstance(teams, dict) or not all(isinstance(k, str) and k and isinstance(v, str) and v
                                              for k, v in teams.items()):
        raise ValueError("球団表は 空でない文字列 → 文字列 の対応")
    TEAMS.clear()
    TEAMS.update(teams)
    CODES = set(TEAMS.values())
    _TEAM = re.compile(_team_pattern(TEAMS))
    _TEAM_UNIT = re.compile(r"(?:" + _TEAM.pattern + r")\s*(?:20\d{2}年?)?")


def load_teams(path: Path) -> dict[str, str]:
    """Read a team-name table from a JSON object file (name → code)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"球団表は JSON のオブジェクト: {path}")
    return data
_SEPARATORS = " \t\r\n?？、。,:："
_CONNECTORS = {"の", "に", "は", "が", "を", "で", "と", "か", "には", "のは", "とは", "では"}


def _unread_spans(raw: str, view: str, offsets: list[tuple[int, int]]) -> list[UnreadSpan]:
    # Mask consumed ranges without changing their positions or joining words.
    rest = list(view)
    consumed = [False] * len(view)
    for pattern in (_ID, _PCT, _TEAM_UNIT, _CODE_UNIT, _WORD):
        for match in pattern.finditer("".join(rest)):
            if pattern is _CODE_UNIT and match[1] not in CODES:
                continue
            rest[match.start():match.end()] = ["\0"] * len(match[0])
            consumed[match.start():match.end()] = [True] * len(match[0])
    spans = []
    # A literal NUL in user input is unknown text, not our internal mask.
    for match in re.finditer(r"0+", "".join("1" if flag else "0" for flag in consumed)):
        start, end = match.span()
        while start < end and view[start] in _SEPARATORS:
            start += 1
        while end > start and view[end - 1] in _SEPARATORS:
            end -= 1
        if start == end or view[start:end] in _CONNECTORS:
            continue
        # A standalone leading particle is grammar, not part of the next word.
        # Do not remove particles from inside an unknown word or condition.
        leading = re.match(r"(?:には|のは|とは|では|[のにはがをでと])\s+", view[start:end])
        if leading:
            start += leading.end()
        source_start = min(s for s, _ in offsets[start:end])
        source_end = max(e for _, e in offsets[start:end])
        if spans and source_start <= spans[-1].end:
            source_start = spans.pop().start
        spans.append(UnreadSpan(source_start, source_end, raw[source_start:source_end]))
    return spans


def parse_input(text: str) -> ParsedInput:
    """Read a normalized view, retaining source evidence and machine diagnostics."""
    t, offsets = _norm_with_spans(text)
    q = Query()
    parsed = ParsedInput(text, q)

    def report(code, explanation):
        parsed.diagnostics.append(ParseDiagnostic(code, explanation))

    ids = [f"{a}{int(b)}" for a, b in _ID.findall(t)]
    if len(ids) > 1:
        report("multiple_targets", f"命題・式が2つ以上: {', '.join(ids)}（最初の {ids[0]} だけ使う）")
    q.target = ids[0] if ids else None
    has_e, has_p = "式" in t, "命題" in t
    if not q.target and has_e != has_p:
        q.kind = "expression" if has_e else "proposition"
    t_wo_ids = _ID.sub(" ", t)

    # 形: 対偶 → 裏 → 逆 の順に見る（「逆に」は形ではない）
    if "対偶" in t:
        q.form = "contrapositive"
    elif "裏" in t:
        q.form = "inverse"
    elif re.search(r"逆(?!に)", t):
        q.form = "converse"

    # 単位（球団 + 年、または t-2015）と、球団だけ
    units: list[tuple[int, str]] = [(m.start(), f"{m[1]}-{m[2]}") for m in _CODE_UNIT.finditer(t_wo_ids) if m[1] in CODES]
    for m in _TEAM.finditer(t_wo_ids):
        y = _YEAR.match(t_wo_ids[m.end():].lstrip())
        units.append((m.start(), f"{TEAMS[m[0]]}-{y[1]}" if y else TEAMS[m[0]]))
    for pos, u in sorted(units):
        rest = t_wo_ids[pos:]
        (q.exclude if re.match(r"\S+(?:\s?\d{4}年?)?\s?(を)?(以外|除い|除く|抜い|抜き)", rest) else q.about).append(u)

    # 成立率
    for num, unit_, rel in _PCT.findall(t):
        v = float(num) / (10 if unit_ == "割" else 100)
        if rel in ("以下", "未満"):
            q.max_rate = v
        else:
            q.min_rate = v
            if not rel:
                report("missing_rate_direction", f"{num}{unit_} の向き（以上・以下）が書かれていない（以上として読む）")

    # 問いの種類
    if re.search(r"唯一|ただ1つ|1つだけ|ひとつだけ|1個だけ", t):
        q.ask = "unique_exception"
    elif re.search(r"判例|反例|例外", t) and re.search(r"ある[?？か]|ありますか|あるの|有無|はい|いいえ|ない[?？か]", t):
        q.ask = "has_counterexample"
    elif re.search(r"判例|反例|例外", t):
        q.ask = "counterexamples"
    elif q.min_rate is not None or q.max_rate is not None or "成立率" in t:
        q.ask = "rate"
    else:
        report("unrecognized_ask", "問いの種類（判例・唯一の例外・成立率）が読めない（判例の一覧として読む）")
    if q.ask == "rate" and q.min_rate is None and q.max_rate is None:
        report("missing_rate_value", "成立率の条件の数が読めない")
    if any(rel in ("未満", "超") for _, _, rel in _PCT.findall(t)):
        report("unsupported_rate_comparison", "未満・超は未対応。以上・以下とは区別して問い合わせてください")
    if sum(word in t for word in ("対偶", "裏")) + bool(re.search(r"逆(?!に)", t)) > 1:
        report("multiple_forms", "形が2つ以上ある")
    parsed.unread_spans = _unread_spans(text, t, offsets)
    if parsed.unread_spans:
        report("unsupported_text", "未対応の語・条件が残っています")
    try:
        validate_query(asdict(q))
    except (ValueError, TypeError) as e:
        report("invalid_query", str(e))
    return parsed


def parse(text: str) -> tuple[Query, list[str]]:
    """Compatibility API. Structured evidence is available from ``parse_input``."""
    parsed = parse_input(text)
    return parsed.query, parsed.unread


RESULT_FILES = ("propositions.jsonl", "sets.jsonl")


def load(results: Path) -> list[dict]:
    """判定済みの命題と式（PythDRagoras の出力）。どちらも forms に4つの形の判定を持つ。"""
    return load_info(results)[0]


def load_info(results: Path) -> tuple[list[dict], dict[str, bool]]:
    """Read the result files that exist. A missing file is recorded, not skipped silently."""
    present, out = {}, []
    for name in RESULT_FILES:
        path = Path(results) / name
        if path.is_file():
            present[name] = True
            out += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        else:
            present[name] = False
    return out, present


def required_files(q: Query) -> list[str]:
    """Files the query has to read. Kind ``any`` without an ID needs both."""
    need = []
    if q.target:
        need.append("sets.jsonl" if q.target.startswith("E") else "propositions.jsonl")
    if q.kind == "expression":
        need.append("sets.jsonl")
    elif q.kind == "proposition":
        need.append("propositions.jsonl")
    elif not q.target:
        need.extend(RESULT_FILES)
    return list(dict.fromkeys(need))


def answer_results(q: Query, results: Path) -> dict:
    """Answer from one configured results directory.

    A missing file the query needs is ``結果が読めない``. That is separate from an ID
    that was looked up in a file and was not there.
    """
    items, present = load_info(results)
    missing = [name for name in required_files(q) if not present.get(name)]
    if missing:
        return {"answer": "判定不能", "error": "結果が読めない: " + "、".join(missing), "rows": []}
    return answer(q, items)


def load_suggest_threshold(cycle_dir: Path) -> float | None:
    """Read ``suggest_threshold`` from ``<cycle>/ask.toml``. Missing file or key → None."""
    path = Path(cycle_dir) / "ask.toml"
    if not path.is_file():
        return None
    import tomllib
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    if "suggest_threshold" not in data or data["suggest_threshold"] is None:
        return None
    value = data["suggest_threshold"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("suggest_threshold は有限の数")
    return float(value)


def resolve_threshold(results: Path, override: float | None, cycle_dir: Path | None = None) -> float | None:
    """Explicit override, else ``<cycle_dir>/ask.toml``, else ``ask.toml`` next to the results folder."""
    if override is not None:
        return override
    if cycle_dir is not None and (Path(cycle_dir) / "ask.toml").is_file():
        return load_suggest_threshold(cycle_dir)
    return load_suggest_threshold(Path(results).parent)


def suggestion_for(text: str, results: Path, unread: list[str], res: dict, threshold: float | None,
                   *, query: Query | None = None) -> dict:
    """Up to five candidate IDs when the reply is 判定不能 because of unread text or an unknown ID.

    Candidates are not written into ``answer`` or ``Query.target``.
    """
    error = res.get("error") or ""
    q = query if query is not None else parse(text)[0]
    if "結果が読めない" in error or any(not (Path(results) / name).is_file() for name in required_files(q)):
        return {"candidates": [], "line": "", "status": None, "selected": None}
    unknown_id = "結果にない" in error and "結果が読めない" not in error
    if not unread and not unknown_id:
        return {"candidates": [], "line": "", "status": None, "selected": None}
    try:
        import suggest
        docs = suggest.load_docs(results)
        got = suggest.similar_candidates(text, docs, threshold=threshold, k=5)
    except (FileNotFoundError, ValueError):
        return {"candidates": [], "line": "", "status": None, "selected": None}
    candidates = list(got.get("candidates") or [])[:5]
    ids = [c["id"] for c in candidates if c.get("id")]
    line = ("もしかして: " + " / ".join(ids) + "（候補。答えではない）") if ids else ""
    return {"candidates": candidates, "line": line, "status": got.get("status"), "selected": None}


def _id_key(i: str) -> tuple:
    return (i[0], int(i[1:])) if re.fullmatch(r"[PE]\d+", i) else (i, 0)


def registration_status(item: dict) -> str:
    """Conservative provenance label; posthoc false/missing is not preregistration."""
    if item.get("posthoc") is True:
        return "事後構成"
    if item.get("preregistered") is True or item.get("preregistered_at") or item.get("preregistration"):
        return "事前登録"
    return "不明"


def answer(q: Query, items: list[dict]) -> dict:
    """Query に、判定済みの結果だけで答える（計算し直さない）。"""
    if q.target and not any(it["id"] == q.target for it in items):
        return {"answer": "判定不能", "error": f"{q.target} は判定済みの結果にない", "rows": []}
    rows, uncertain, evaluated = [], False, 0
    for it in sorted(items, key=lambda it: _id_key(it["id"])):
        if q.target and it["id"] != q.target:
            continue
        if q.kind != "any" and it["id"][0] != ("E" if q.kind == "expression" else "P"):
            continue
        f = next((f for f in it.get("forms", []) if f.get("form") == q.form), None)
        if f is None or not f.get("n"):
            uncertain = True
            continue
        evaluated += 1
        listed = [c["unit"] for c in f.get("counterexamples", [])]
        # PythDRagoras counts n AFTER removing unknown rows. Do not subtract
        # undetermined again: that used to hide real counterexamples.
        n_cx = f["n"] - f["hold"]
        cx = [u for u in listed if not any(u == a or u.split("-")[0] == a for a in q.exclude)]
        complete = len(listed) == n_cx
        left = n_cx - (len(listed) - len(cx))
        count_known = complete or not q.exclude
        uncertain |= bool(f.get("undetermined", 0)) or not count_known
        if q.about and not any(u == a or u.split("-")[0] == a for u in cx for a in q.about):
            uncertain |= not complete
            continue
        if q.ask == "unique_exception" and (not count_known or left != 1):
            continue
        if q.min_rate is not None and f["rate"] < q.min_rate:
            continue
        if q.max_rate is not None and f["rate"] > q.max_rate:
            continue
        rows.append({"id": it["id"], "form": q.form, "n": f["n"], "hold": f["hold"], "rate": round(f["rate"], 3),
                     "counterexamples_left": left if count_known else None,
                     "has_counterexample": bool(cx) or (count_known and left > 0),
                     "undetermined": f.get("undetermined", 0), "counterexamples": cx, "listed_all": complete,
                     "statement": it.get("statement", ""), "registration": registration_status(it)})
    if q.ask == "has_counterexample":
        ans = ("はい" if any(r["has_counterexample"] for r in rows)
               else "判定不能" if uncertain or not evaluated else "いいえ")
    else:
        ans = len(rows)
    return {"answer": ans, "rows": rows, "incomplete": uncertain or not evaluated}


def render(text: str, q: Query, unread: list[str], res: dict, suggestion_line: str = "") -> str:
    lines = [f"問い: {text}", "読み取り: " + json.dumps(asdict(q), ensure_ascii=False)]
    lines += [f"読めなかった: {u}" for u in unread]
    if res.get("error"):
        lines.append(f"答え: 判定不能（{res['error']}）")
        if suggestion_line:
            lines.append(suggestion_line)
        return "\n".join(lines)
    head = f"{ASKS[q.ask]}・{FORMS[q.form]}・{KINDS[q.kind]}" + ("・除いた後の判例" if q.exclude else "")
    lines.append(f"答え（{head}）: {res['answer']}" + ("" if q.ask == "has_counterexample" else " 件"))
    for r in res["rows"][:50]:
        cx = ", ".join(r["counterexamples"]) or "なし"
        more = "" if r["listed_all"] else "（一覧は一部）"
        count = r["counterexamples_left"] if r["counterexamples_left"] is not None else "不明"
        label = "除外後の判例" if q.exclude else "判例"
        excluded = "（除外 " + ", ".join(q.exclude) + "）" if q.exclude else ""
        lines.append(f"- {r['id']}［{r['registration']}］ {RATE_FORMS[q.form]}の成立率 {r['hold']}/{r['n']}（{r['rate']:.3f}）／{label} {count}{excluded}: {cx}{more}")
    if len(res["rows"]) > 50:
        lines.append(f"- ほか {len(res['rows']) - 50} 件")
    if suggestion_line:
        lines.append(suggestion_line)
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("question", nargs="*", help="日本語の問い。空白で区切った語のまま渡してよい（引用符は要らない）。"
                    "省略して標準入力（パイプ・FIFO）から渡すと、1行1問ずつ順に答える")
    ap.add_argument("--results", type=Path, default=None, help="判定済みの結果のフォルダ（--schema 以外では必須）")
    ap.add_argument("--cycle-dir", type=Path, default=None,
                    help="ask.toml を読むフォルダ。省略時は結果フォルダの親")
    ap.add_argument("--teams", type=Path, default=None, help="球団名 → コードの JSON。省略時は球団名を読まない")
    ap.add_argument("--suggest-threshold", type=float, default=None,
                    help="候補の類似度。省略時は ask.toml。未設定なら候補は確定しない")
    ap.add_argument("--query", help="読み取りを飛ばし、Query を JSON で直接渡す（llama.cpp・MCP が選んだもの）。"
                    "- なら標準入力から読む（引用符は要らない）")
    ap.add_argument("--schema", action="store_true", help="Query の JSON Schema を出す")
    ap.add_argument("--json", action="store_true", help="答えを JSON で出す")
    ap.add_argument("--log", type=Path, help="問いと読み取りを1行ずつ足す（JSONL）。同じ Query なら答えは同じ")
    args = ap.parse_intermixed_args(argv)  # 語と --オプションが混ざっていてよい

    if args.schema:
        print(json.dumps(schema(), ensure_ascii=False, indent=2))
        return 0
    if args.results is None:
        ap.error("--results が要る")
    results = args.results
    try:
        if args.teams is not None:
            set_teams(load_teams(args.teams))
        threshold = resolve_threshold(results, args.suggest_threshold, args.cycle_dir)
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1
    if args.query:
        text = sys.stdin.read() if args.query == "-" else args.query
        try:
            q = validate_query(json.loads(text))
        except (ValueError, TypeError) as e:
            print(f"Query の値が型の外: {e}", file=sys.stderr)
            return 2
        return _answer(text, ParsedInput(text, q), results, threshold, args)
    if args.question:
        text = " ".join(args.question)
        return _answer(text, parse_input(text), results, threshold, args)
    if sys.stdin is None or sys.stdin.isatty():
        ap.error("問いか --query か --schema が要る（標準入力から1行1問でもよい）")
    # 🔁 キュー: 1行1問、届いた順に答える（パイプ・FIFO）。--json なら1行1件の JSON
    codes = [_answer(line.strip(), parse_input(line.strip()), results, threshold, args, stream=True)
             for line in sys.stdin if line.strip()]
    return max(codes, default=0)


def _answer(text: str, parsed, results: Path, threshold, args, *, stream: bool = False) -> int:
    """1つの問いに答えて表示する。答えに誤りがあれば 1。"""
    q = parsed.query
    unread = parsed.unread
    res = parsed.blocked_answer() if parsed.status == "undetermined" else answer_results(q, results)
    hinted = suggestion_for(text, results, unread, res, threshold, query=q)
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        with args.log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"question": text, "query": asdict(q), "unread": unread,
                                 **parsed.metadata()}, ensure_ascii=False) + "\n")
    payload = {"query": asdict(q), "unread": unread, **parsed.metadata(), **res,
               "suggestions": hinted["candidates"], "suggestion_status": hinted["status"]}
    print(json.dumps(payload, ensure_ascii=False, indent=None if stream else 2) if args.json
          else render(text, q, unread, res, hinted["line"]), flush=stream)
    return 1 if res.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
