import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_single_file_help_and_cache_in_isolation(tmp_path):
    output = tmp_path / "queryu.py"
    subprocess.run([sys.executable, str(ROOT / "scripts/build_single_file.py"), str(output)], check=True)
    result = subprocess.run([sys.executable, "-I", str(output), "--help"], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "--source" in result.stdout
    spec = importlib.util.spec_from_file_location("single_queryu", output)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cache = module.Cache(tmp_path / "cache")
    assert cache.latest() == {}


def test_single_file_missing_blueprobe_is_explained(tmp_path):
    output = tmp_path / "queryu.py"
    subprocess.run([sys.executable, str(ROOT / "scripts/build_single_file.py"), str(output)], check=True)
    result = subprocess.run([sys.executable, "-I", str(output), "--source", "example", "--years", "2024",
                             "--cache", str(tmp_path / "cache"), "--plan"], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "BlueProbe" in result.stderr
    assert "Traceback" not in result.stderr


def test_single_file_offline_plan_with_blueprobe(tmp_path):
    import blueprobe, npb_calendar  # BlueProbe の npb/ でも直下でも、読み込めた場所から写す
    output = tmp_path / "queryu.py"
    subprocess.run([sys.executable, str(ROOT / "scripts/build_single_file.py"), str(output)], check=True)
    for module in (blueprobe, npb_calendar):
        shutil.copy2(module.__file__, tmp_path / Path(module.__file__).name)
    result = subprocess.run([sys.executable, "-I", str(output), "--source", "npb_calendar", "--years", "2024",
                             "--cache", str(tmp_path / "cache"), "--plan", "--offline"], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "オフラインで不足するページ: 9" in result.stdout
