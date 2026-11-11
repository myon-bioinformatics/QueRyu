"""Build a self-contained QueRyu entry with the canonical cache implementation."""
from __future__ import annotations

import argparse
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(out: Path) -> None:
    source = (ROOT / "queryu.py").read_text(encoding="utf-8")
    cache = (ROOT / "cache_core.py").read_text(encoding="utf-8")
    classes = [n for n in ast.parse(cache).body if isinstance(n, ast.ClassDef)]
    functions = [n for n in ast.parse(cache).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if [n.name for n in classes] != ["CacheIntegrityError", "Cache"] or functions:
        raise ValueError("cache_core structure changed; update the standalone builder")
    pieces = [ast.get_source_segment(cache, n) for n in classes]
    pieces[1] = pieces[1].replace("class Cache:", "class _Cache:", 1)
    marker = "from cache_core import Cache as _Cache, CacheIntegrityError"
    if source.count(marker) != 1:
        raise ValueError("queryu cache import changed; update the standalone builder")
    source = source.replace(marker, "# Generated from cache_core.py; do not edit this copy.\n" + "\n\n".join(pieces))
    ast.parse(source)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="destination path, e.g. dist-single/queryu.py")
    build(parser.parse_args().output)
