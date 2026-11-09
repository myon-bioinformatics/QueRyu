"""抽出が終わったあとの加工。

並べ替え（alphabetical / numeric）と、テキスト条件による絞り込みだけを持つ。
ページの読み取り、DOM パターン表、パターン検出、本文の抽出は BlueProbe が持つ。
パターンが要るときは ``from blueprobe import detect_pattern, get_pattern`` で読む。
blueprobe はパッケージではないので、``from blueprobe import ...`` のままにする。
検出できない、未知のキー、数値にできない値は、ここで握りつぶさずに呼び出し元へ返す。
"""

from __future__ import annotations

import argparse, json, re, sys, unicodedata
from decimal import Decimal, InvalidOperation; from pathlib import Path; from typing import Any

from blueprobe import detect_pattern, get_pattern

SCHEMA = "queryu-pattern-postprocess/1"
SORT_MODES = {"alphabetical", "numeric"}
FILTER_OPS = {"contains", "equals", "startswith", "endswith", "regex", "not_contains"}


def pattern_for_html(html: str, pattern_key: str | None = None) -> dict[str, Any]:
    """BlueProbe にパターンを聞く。一致しない、または未知のキーは例外のまま返す。"""
    if not isinstance(html, str):
        raise TypeError("html は文字列である必要があります")
    if pattern_key not in (None, "", "auto"):
        pattern = get_pattern(pattern_key)
        return {
            "pattern_key": pattern["key"],
            "selector": pattern["selector"],
            "label": pattern["label"],
            "matched_nodes": None,
        }
    matches = detect_pattern(html)
    if not matches:
        raise ValueError("DOM パターンが一致しません")
    top = matches[0]
    pattern = top.get("pattern")
    if not isinstance(pattern, dict) or "key" not in pattern:
        raise ValueError("BlueProbe の検出結果に pattern.key がありません")
    return {
        "pattern_key": pattern["key"],
        "selector": top.get("selector_used") or pattern.get("selector"),
        "label": pattern.get("label", ""),
        "matched_nodes": top.get("matched_nodes"),
    }


def _require_text(value: Any, label: str) -> str:
    if value is None:
        raise ValueError(f"{label} が null です")
    if not isinstance(value, str):
        raise TypeError(f"{label} は文字列である必要があります")
    return value


def _alphabetical_key(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _numeric_key(text: str) -> Decimal:
    normalized = unicodedata.normalize("NFKC", text).strip()
    try:
        number = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"数値として並べられません: {text!r}") from exc
    if not number.is_finite():
        raise ValueError(f"数値として並べられません: {text!r}")
    return number


def sort_text(values: list[Any], mode: str) -> list[str]:
    """文字列を alphabetical（NFKC + casefold）または numeric（有限の数）で安定ソートする。"""
    if mode not in SORT_MODES:
        raise ValueError(f"未知の並べ替えです: {mode!r}")
    texts = [_require_text(value, "text") for value in values]
    if mode == "alphabetical":
        return sorted(texts, key=_alphabetical_key)
    return sorted(texts, key=_numeric_key)


def _condition_holds(text: str, condition: dict[str, Any]) -> bool:
    if not isinstance(condition, dict):
        raise TypeError("絞り込み条件はオブジェクトである必要があります")
    op = condition.get("op")
    if op not in FILTER_OPS:
        raise ValueError(f"未知の絞り込みです: {op!r}")
    expected = _require_text(condition.get("value"), "value")
    folded = bool(condition.get("casefold", False))
    left = _alphabetical_key(text) if folded else text
    right = _alphabetical_key(expected) if folded else expected
    if op == "contains":
        return right in left
    if op == "not_contains":
        return right not in left
    if op == "equals":
        return left == right
    if op == "startswith":
        return left.startswith(right)
    if op == "endswith":
        return left.endswith(right)
    flags = re.IGNORECASE if folded else 0
    try:
        return re.search(expected, text, flags) is not None
    except re.error as exc:
        raise ValueError(f"正規表現が不正です: {expected!r}") from exc


def filter_text(values: list[Any], conditions: list[dict[str, Any]]) -> list[str]:
    """条件をすべて満たす文字列だけ残す。条件が空なら並びを変えずにコピーする。"""
    texts = [_require_text(value, "text") for value in values]
    if not conditions:
        return list(texts)
    return [text for text in texts if all(_condition_holds(text, condition) for condition in conditions)]


def _cell(row: list[Any], column: int) -> str:
    if column < 0 or column >= len(row):
        raise ValueError(f"列 {column} は行の範囲外です（長さ {len(row)}）")
    return _require_text(row[column], f"列 {column}")


def _copy_table(rows: list[Any]) -> list[list[Any]]:
    copied = []
    for row in rows:
        if not isinstance(row, list):
            raise TypeError("行は配列である必要があります")
        copied.append(list(row))
    return copied


def sort_table(rows: list[list[Any]], *, column: int, mode: str, header: bool = True) -> list[list[Any]]:
    """表を並べ替える。header が真のときは先頭行をヘッダとして動かさない。"""
    if mode not in SORT_MODES:
        raise ValueError(f"未知の並べ替えです: {mode!r}")
    table = _copy_table(rows)
    if not table:
        return []
    body_start = 1 if header else 0
    head, body = table[:body_start], table[body_start:]
    if mode == "alphabetical":
        ordered = sorted(body, key=lambda row: _alphabetical_key(_cell(row, column)))
    else:
        ordered = sorted(body, key=lambda row: _numeric_key(_cell(row, column)))
    return [*head, *ordered]


def filter_table(
    rows: list[list[Any]], *, column: int, conditions: list[dict[str, Any]], header: bool = True,
) -> list[list[Any]]:
    """テキスト条件でデータ行を残す。header が真のときは先頭行を残す。"""
    table = _copy_table(rows)
    if not table:
        return []
    body_start = 1 if header else 0
    head, body = table[:body_start], table[body_start:]
    if not conditions:
        return table
    kept = [row for row in body if all(_condition_holds(_cell(row, column), condition) for condition in conditions)]
    return [*head, *kept]


def process(
    payload: dict[str, Any],
    *,
    sort: str | None = None,
    column: int = 0,
    header: bool = True,
    conditions: list[dict[str, Any]] | None = None,
    html: str | None = None,
    pattern_key: str | None = None,
) -> dict[str, Any]:
    """text と tables を絞り込み、並べ替える。html またはパターンキーがあれば BlueProbe に聞く。"""
    if not isinstance(payload, dict):
        raise TypeError("入力はオブジェクトである必要があります")
    conditions = list(conditions or [])
    text = payload.get("text", [])
    tables = payload.get("tables", [])
    if not isinstance(text, list) or not isinstance(tables, list):
        raise TypeError("text と tables は配列である必要があります")
    if conditions:
        text = filter_text(text, conditions)
    else:
        text = list(text)
    if sort is not None:
        text = sort_text(text, sort)

    processed_tables = []
    for table in tables:
        if not isinstance(table, list):
            raise TypeError("tables の各要素は行の配列である必要があります")
        rows = _copy_table(table)
        if conditions:
            rows = filter_table(rows, column=column, conditions=conditions, header=header)
        if sort is not None:
            rows = sort_table(rows, column=column, mode=sort, header=header)
        processed_tables.append(rows)

    html_value = html if html is not None else payload.get("html")
    key = pattern_key if pattern_key is not None else payload.get("pattern_key")
    pattern = None
    if isinstance(html_value, str):
        pattern = pattern_for_html(html_value, key)
    elif html_value is not None:
        raise TypeError("html は文字列である必要があります")
    elif key not in (None, "", "auto"):
        found = get_pattern(key)
        pattern = {
            "pattern_key": found["key"],
            "selector": found["selector"],
            "label": found["label"],
            "matched_nodes": None,
        }

    return {"schema": SCHEMA, "pattern": pattern, "text": text, "tables": processed_tables}


def parse_filter(spec: str) -> dict[str, str]:
    """``op=value`` を条件にする。value 側の ``=`` はそのまま残す。"""
    if not isinstance(spec, str) or "=" not in spec:
        raise ValueError(f"絞り込みは op=value です: {spec!r}")
    op, value = spec.split("=", 1)
    op = op.strip()
    if op not in FILTER_OPS:
        raise ValueError(f"未知の絞り込みです: {op!r}")
    return {"op": op, "value": value}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="抽出結果の並べ替えとテキスト条件による絞り込み")
    ap.add_argument("--input", "-i", type=Path, required=True, help="text と tables を持つ JSON")
    ap.add_argument("--out", "-o", type=Path, help="書き出し先。省略時は標準出力")
    ap.add_argument("--sort", choices=sorted(SORT_MODES), help="alphabetical または numeric")
    ap.add_argument("--column", type=int, default=0, help="表を加工する列（0 始まり）")
    ap.add_argument("--header", action=argparse.BooleanOptionalAction, default=True, help="表の先頭行をヘッダとして残す")
    ap.add_argument("--filter", action="append", default=[], help="op=value。複数指定はすべて満たす行だけ残す")
    ap.add_argument("--casefold", action="store_true", help="絞り込みの比較で大文字小文字を畳む")
    ap.add_argument("--html", type=Path, help="パターン判定だけ BlueProbe に渡す HTML")
    ap.add_argument("--pattern", default=None, help="パターンキー。省略時、HTML があるときだけ自動判定")
    args = ap.parse_args(argv)

    if not args.input.is_file():
        ap.error(f"ファイルが存在しません: {args.input}")
    raw = args.input.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        ap.error(f"JSON として読めません: {exc}")

    html = None
    if args.html is not None:
        if not args.html.is_file():
            ap.error(f"ファイルが存在しません: {args.html}")
        html = args.html.read_text(encoding="utf-8")
    try:
        conditions = [parse_filter(spec) for spec in args.filter]
        if args.casefold:
            conditions = [{**condition, "casefold": True} for condition in conditions]
        result = process(
            payload, sort=args.sort, column=args.column, header=args.header,
            conditions=conditions, html=html, pattern_key=args.pattern,
        )
    except (ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    out_json = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(out_json, encoding="utf-8")
    else:
        print(out_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
