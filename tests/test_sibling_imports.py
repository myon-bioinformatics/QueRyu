"""QueRyu は隣のディレクトリ（../blueprobe など）を読まない。兄弟の場所は呼び出し側が入れる。

Aoi では aoi.py（``python aoi.py queryu …``、``import aoi``）が入れ、単独では BlueProbe の最新 main の
checkout を PYTHONPATH で渡す。QueRyu の中に兄弟を探す処理は置かない。
"""
import ast, os, subprocess, sys
from pathlib import Path

import pytest

import queryu

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted(p for p in ROOT.rglob("*.py")
                 if not {"tests", "vendor", ".test-deps", ".venv", "build"} & set(p.relative_to(ROOT).parts))
SIBLINGS = {"blueprobe", "sakanalytics", "pythdragoras", "nagoyaction", "dragowing"}


def _sibling_path_uses(path: Path):
    """``.parent`` / ``.parents[...]`` と兄弟名の文字列を ``/`` でつなぐ式、と sys.path の操作を探す。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            text = ast.unparse(node)
            if ".parent" in text and any(f"'{s}'" in text or f'"{s}"' in text for s in SIBLINGS):
                yield node.lineno, text
        if isinstance(node, ast.Call) and "sys.path" in ast.unparse(node.func):
            text = ast.unparse(node)
            if any(s in text for s in SIBLINGS) or "parents[" in text or ".parent" in text:
                yield node.lineno, text


def test_sources_do_not_reach_siblings_by_relative_path():
    found = {str(p.relative_to(ROOT)): list(_sibling_path_uses(p)) for p in SOURCES}
    assert {k: v for k, v in found.items() if v} == {}


def test_blueprobe_modules_come_from_the_expected_checkout(aoi_root):
    import blueprobe
    import npb_calendar
    import npb_team_annual
    pinned = [Path(p).resolve() for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    for module in (blueprobe, npb_calendar, npb_team_annual):
        origin = Path(module.__file__).resolve()
        if ROOT == aoi_root / "queryu":
            expected = aoi_root / "blueprobe" / (module.__name__ + ".py")
            if module is not blueprobe and not expected.is_file():
                expected = expected.parent / "npb" / expected.name
            assert origin == expected.resolve()
        else:
            assert origin.parent != (ROOT.parent / "blueprobe").resolve(), origin
            assert origin.parent in pinned, (origin, pinned)


def test_cli_entry_point_does_not_add_a_sibling(tmp_path, aoi_root):
    # __main__ の経路（python queryu.py）でも、隣の ../blueprobe を足さずに PYTHONPATH の BlueProbe を使う
    code = (
        "import runpy, sys\n"
        f"sys.argv = [{str(ROOT / 'queryu.py')!r}, '--help']\n"
        "try:\n"
        "    runpy.run_path(sys.argv[0], run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "import blueprobe\n"
        "print('BLUEPROBE=' + blueprobe.__file__)\n"
        "print('\\n'.join(sys.path))\n"
    )
    env = os.environ.copy()
    embedded = ROOT == aoi_root / "queryu"
    if embedded:
        env["PYTHONPATH"] = str(aoi_root / "blueprobe")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=tmp_path, env=env)
    lines = out.stdout.splitlines()
    if embedded:
        assert "BLUEPROBE=" + str(aoi_root / "blueprobe" / "blueprobe.py") in lines
    else:
        assert str(ROOT.parent / "blueprobe") not in lines
    origin = Path(next(l for l in lines if l.startswith("BLUEPROBE="))[len("BLUEPROBE="):]).resolve()
    if not embedded:
        assert origin.parent != (ROOT.parent / "blueprobe").resolve()


def test_import_guard_preserves_standalone_and_embedded_contracts(aoi_root):
    import importlib.util
    spec = importlib.util.spec_from_file_location("queryu_conftest_guard", ROOT / "conftest.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    check = guard._blueprobe_origin_is_allowed
    root = Path("/example/aoi")
    own = root / "blueprobe" / "blueprobe.py"
    external = Path("/example/pinned-blueprobe/blueprobe.py")
    assert check(own, root, root / "queryu")
    assert not check(external, root, root / "queryu")
    assert not check(root / "wrong" / "blueprobe.py", root, root / "queryu")
    assert check(external, root, Path("/example/QueRyu"))
    assert not check(own, root, Path("/example/QueRyu"))


def test_own_module_origin_must_be_this_checkout(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("queryu_layout_guard", ROOT / "conftest.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    check = guard._own_module_origin_is_allowed
    root = tmp_path / "aoi"
    here = root / "queryu"
    assert check("ask", here / "ask" / "ask.py", root, here)
    assert not check("ask", root / "pythdragoras" / "ask.py", root, here)


def test_no_aoi_layout_files_or_default_cycle():
    assert not (ROOT / "aoi_paths.py").exists()
    assert not (ROOT / "ask" / "aoi_layout.py").exists()
    for path in SOURCES:
        if path.name == "conftest.py":  # テストの土台は Aoi の場所を AOI_ROOT で受け取る
            continue
        text = path.read_text(encoding="utf-8")
        assert "c001-chunichi" not in text, path
        assert "aoi_layout" not in text and "aoi_paths" not in text, path
