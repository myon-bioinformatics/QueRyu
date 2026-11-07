"""QueRyu 単独リポジトリ用。サイクルデータ・blueprobe の fixture・nagoyaction/inbox は Aoi に残す（AOI_ROOT）。

BlueProbe（抽出）は最新 main の BlueProbe checkout を PYTHONPATH で渡す。Aoi の blueprobe/ は使わない。
"""
import os, sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
# 取り違えると Aoi 側のコピーを黙ってテストしてしまうので、出どころを最後に確かめる
OWN_MODULES = ("queryu", "ask", "bank", "suggest")


def _root() -> Path:
    # Aoi の場所は AOI_ROOT で受け取る（Aoi の中では Aoi の conftest.py が入れる）。ここでは推定しない
    value = os.environ.get("AOI_ROOT")
    here = Path(__file__).resolve().parent
    if not value and (here.parent / "aoi_paths.py").is_file():
        # Aoi に subtree として埋め込まれ、子のディレクトリだけで pytest を動かしたとき（rootdir が子になる）
        value = str(here.parent)
    if not value:
        raise pytest.UsageError("AOI_ROOT=<Aoi のルート> を渡す（.github/workflows/ci.yml と同じ）")
    root = Path(value).expanduser().resolve()
    # テストのデータの場所（cycles/ の既定のサイクルなど）は Aoi の aoi_paths.py に聞く。QueRyu には置かない
    if str(root) not in sys.path:
        sys.path.append(str(root))
    return root


def _blueprobe_origin_is_allowed(origin: Path, root: Path, here: Path = HERE) -> bool:
    """Embedded subtree uses Aoi/blueprobe; standalone must use an external checkout."""
    if here == root / "queryu":
        return origin.parent == root / "blueprobe"
    return root not in origin.parents


def pytest_configure(config):
    root = _root()
    if not (root / "cycles").is_dir() or not (root / "nagoyaction" / "inbox").is_dir():
        raise pytest.UsageError(
            f"Aoi のデータが見つからない（{root} に cycles/ か nagoyaction/inbox/ がない）。"
            "Aoi を checkout して AOI_ROOT=<Aoi のルート> を渡す（.github/workflows/ci.yml と同じ）"
        )
    # nagoyaction/inbox/inbox.py は Aoi の queryu/ask を sys.path の先頭に足す。
    # 先に QueRyu 側の ask・bank・suggest を読み込んでおき、Aoi 側のコピーが使われないようにする
    for name in ("ask", "bank", "suggest"):
        __import__(name)
    inbox_dir = str(root / "nagoyaction" / "inbox")
    if inbox_dir not in sys.path:
        sys.path.append(inbox_dir)
    try:
        import blueprobe
    except ImportError as error:
        raise pytest.UsageError(
            f"blueprobe を import できない（{error}）。BlueProbe を checkout して PYTHONPATH に渡す"
        ) from None
    origin = Path(blueprobe.__file__).resolve()
    if not _blueprobe_origin_is_allowed(origin, root):
        raise pytest.UsageError(f"blueprobe の出どころが不正（{origin}）。単独では外部 checkout、Aoi 内では Aoi/blueprobe を使う")


def pytest_report_header(config):
    import blueprobe
    return [f"AOI_ROOT={_root()}", f"blueprobe={Path(blueprobe.__file__).resolve()}"]


def _own_module_origin_is_allowed(name: str, origin: Path, root: Path, here: Path = HERE) -> bool:
    """QueRyu modules must come from this checkout."""
    return here.resolve() in origin.resolve().parents


def pytest_sessionfinish(session, exitstatus):
    wrong = {}
    for name in OWN_MODULES:
        module = sys.modules.get(name)
        origin = getattr(module, "__file__", None)
        if origin is not None and not _own_module_origin_is_allowed(name, Path(origin), _root()):
            wrong[name] = origin
    if wrong:
        if exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_sep("=", "QueRyu のモジュールが別の場所から読まれた")
            for name, origin in sorted(wrong.items()):
                terminal.write_line(f"{name}: {origin}")


@pytest.fixture(scope="session")
def aoi_root() -> Path:
    return _root()
