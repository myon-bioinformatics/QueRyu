"""CI receipts select the pinned layout without relaxing byte identity."""
import hashlib
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def receipt_script():
    text = (ROOT / '.github/workflows/ci.yml').read_text()
    start = text.index('          check_source() {')
    end = text.index('          test -d .test-deps/aoi/cycles', start)
    return '\n'.join(line[10:] for line in text[start:end].splitlines())


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
    import os
    result = subprocess.run(['bash', '-c', shell, 'receipt', str(repo)],
                            env={**os.environ, 'GITHUB_STEP_SUMMARY': str(summary)}, capture_output=True, text=True)
    assert (result.returncode == 0) == (damage == 'none'), result.stderr
    if damage == 'none':
        for name in files:
            path = repo / name
            assert f'{path}: {hashlib.sha256(path.read_bytes()).hexdigest()}' in summary.read_text()
