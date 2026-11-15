"""🌐 Fleet contracts through the parent's shared checks (vendor/fleet_check.py):
🔒 vendor lock · 📌 parent pins · ⏱️ benchmark guard (-S / -O) · 🧾 CI source receipts.

Aoi は子を subtree で持ち、全部の子のテストを1回の pytest で動かす。最上位の `vendor` パッケージは子どうしで
ぶつかるので、fleet_check はファイルの場所から固有の名前で読み込む。
"""
import hashlib, importlib.util, os, subprocess, unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("queryu_fleet_check", ROOT / "vendor/fleet_check.py")
fleet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fleet)

PARENT_WORKFLOW_PIN = "9e3da7ae1846e9604ebc24bb5178f7de094a4e77"
ACCEPTED = {"vendor/fleet_check.py": "6c29503cf17eda856766ddfdf4d66a5b0965a8b1"}
SOURCES = {  # destination -> (repository, source)
    "vendor/aoi/shrink/numpy_min.py": ("myon-bioinformatics/Aoi", "shrink/numpy_min.py"),
    "vendor/fleet_check.py": (fleet.PARENT, "fleet_check.py"),
}


# ==== 🔒 vendor lock（旧 test_queryu_numpy_min.py の lock 照合。Aoi の checkout とのずれは test_queryu_numpy_min.py に残した）

def test_vendor_lock_matches_vendored_copy():
    rows = fleet.check_lock(ROOT, commits=ACCEPTED)
    assert {where: (row["repository"], row["source"]) for where, row in rows.items()} == SOURCES
    for row in rows.values():
        assert set(row) == {"repository", "ref", "commit", "source", "destination", "blob_sha", "sha256"}


# ==== 📌 parent pins: local rollout contract; adopted vendor locks are deliberately independent

def test_parent_workflow_references_use_reviewed_rollout_commit():
    pins = fleet.parent_refs(ROOT)
    assert pins, "shared parent references must remain observable"
    assert set(pins) == {PARENT_WORKFLOW_PIN}, pins


# ==== ⏱️ benchmark: scripts/benchmark_query_retention.py in normal and optimized subprocesses（旧 test_queryu_cache.py）

# 比較の基準（旧 cache_share.py の代わり）: 今の share.query に、差し替えられる _rows を通す
BASELINE = b"""import queryu
from unittest import mock
def _rows(cache):
    return queryu._share_rows(cache)
def query(cache, **kw):
    with mock.patch.object(queryu, "_share_rows", lambda c: _rows(c)):
        return queryu.share.query(cache, **kw)
"""


class BenchmarkValidationTests(unittest.TestCase):
    def test_parity_in_both_interpreter_modes(self):
        for optimized in (False, True):
            for mismatch in (False, True):
                with self.subTest(optimized=optimized, mismatch=mismatch):
                    patch = f"g['subprocess'].check_output = mock.Mock(return_value={BASELINE!r})\n"
                    if mismatch:
                        patch += "g['queryu'].share.query = mock.Mock(side_effect=[['first'], ['different']])\n"
                    result = fleet.run_main(ROOT, "scripts/benchmark_query_retention.py", patch, optimized=optimized)
                    if mismatch:
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertEqual(result.stdout, '')
                        self.assertIn('result mismatch', result.stderr)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertTrue(result.stdout.startswith('{'), result.stdout)


# ==== 🧾 CI receipts（旧 test_ci_blueprobe_sources.py）: select the pinned layout without relaxing byte identity

def receipt_script():
    job = fleet.workflow_job(ROOT, "test")
    start = job.index('          check_source() {')
    end = job.index('          test -d .test-deps/aoi/cycles', start)
    return '\n'.join(line[10:] for line in job[start:end].splitlines())


@pytest.mark.parametrize('layout', ['', 'npb/'])
@pytest.mark.parametrize('damage', ['none', 'missing', 'changed'])
def test_source_receipts(tmp_path, layout, damage):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    files = ['blueprobe.py', layout + 'npb_calendar.py', layout + 'npb_team_annual.py']
    for name in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# pinned ' + name + '\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', 'fixture'], check=True)
    target = repo / files[-1]
    if damage == 'missing':
        target.unlink()
    elif damage == 'changed':
        target.write_text('# not the pinned blob\n')
    summary = tmp_path / 'summary'
    shell = '''set -euo pipefail
provenance_fail() { echo "$1" >&2; exit 1; }
''' + receipt_script() + '''
check_source "$1" blueprobe.py
check_npb_source "$1" npb_calendar.py
check_npb_source "$1" npb_team_annual.py
'''
    result = subprocess.run(['bash', '-c', shell, 'receipt', str(repo)],
                            env={**os.environ, 'GITHUB_STEP_SUMMARY': str(summary)}, capture_output=True, text=True)
    assert (result.returncode == 0) == (damage == 'none'), result.stderr
    if damage == 'none':
        for name in files:
            path = repo / name
            assert f'{path}: {hashlib.sha256(path.read_bytes()).hexdigest()}' in summary.read_text()
