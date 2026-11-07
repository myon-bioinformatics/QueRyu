"""単独の QueRyu では、隣のディレクトリ（../blueprobe など）を相対パスで読まない。

BlueProbe は最新 main の checkout を PYTHONPATH で渡す。隣の ``blueprobe/`` を読むのは、親が Aoi の checkout
（pyproject.toml と cycles/ がある）のときだけで、その処理は ``queryu.aoi_sibling_path`` 一か所に限る。
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
    allowed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "aoi_sibling_path":
            allowed |= set(range(node.lineno, node.end_lineno + 1))
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) in allowed:
            continue
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


def test_sibling_is_added_only_inside_an_aoi_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    standalone = tmp_path / "work"
    (standalone / "QueRyu").mkdir(parents=True)
    (standalone / "blueprobe").mkdir()  # 単独の QueRyu の隣に blueprobe/ があっても読まない
    queryu.aoi_sibling_path(standalone / "QueRyu", "queryu_test_missing_module")
    assert str(standalone / "blueprobe") not in sys.path

    aoi = tmp_path / "aoi"
    for d in ("queryu", "blueprobe", "cycles"):
        (aoi / d).mkdir(parents=True)
    (aoi / "pyproject.toml").write_text("", encoding="utf-8")
    queryu.aoi_sibling_path(aoi / "queryu", "queryu_test_missing_module")
    assert sys.path[0] == str(aoi / "blueprobe")


def test_importable_module_is_never_shadowed_by_a_sibling(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    aoi = tmp_path / "aoi"
    for d in ("queryu", "blueprobe", "cycles"):
        (aoi / d).mkdir(parents=True)
    (aoi / "pyproject.toml").write_text("", encoding="utf-8")
    queryu.aoi_sibling_path(aoi / "queryu", "blueprobe")
    assert str(aoi / "blueprobe") not in sys.path


def test_blueprobe_modules_come_from_the_expected_checkout(aoi_root):
    import blueprobe
    import npb_calendar
    import npb_team_annual
    pinned = [Path(p).resolve() for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    for module in (blueprobe, npb_calendar, npb_team_annual):
        origin = Path(module.__file__).resolve()
        if ROOT == aoi_root / "queryu":
            assert origin.parent == aoi_root / "blueprobe"
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
