"""The distributable core works under python -S outside any checkout."""
import json, shutil, subprocess, sys
from pathlib import Path


def test_cache_core_runs_alone_and_rejects_tampering(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'cache_core.py'
    shutil.copyfile(source, tmp_path / 'cache_core.py')
    code = '''
from pathlib import Path
from cache_core import Cache, CacheIntegrityError
c = Cache(Path('cache'))
e = c.put('k', 'https://example.invalid/k', 'g', 200, b'<p>raw</p>', {})
assert e['code_version'] is None
snapshot = c.snapshot(e)
assert snapshot['html'] == '<p>raw</p>'
assert snapshot['response_sha256'] == e['sha256']
assert Cache(Path('cache'), source_version='a' * 40)._version() == 'a' * 40
p = c.root / e['file']
p.write_bytes(b'corrupt')
try:
    c.snapshot(e)
except CacheIntegrityError:
    pass
else:
    raise AssertionError('tampered body accepted')
'''
    out = subprocess.run([sys.executable, '-I', '-S', '-c',
                          "import sys; sys.path.insert(0, '.');" + code],
                         cwd=tmp_path, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
