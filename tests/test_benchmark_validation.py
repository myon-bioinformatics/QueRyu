"""Exercise benchmark success and failure in normal and optimized subprocesses."""
import subprocess, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
                    code = """
from contextlib import nullcontext
from pathlib import Path
import runpy
from unittest.mock import patch
ROOT = Path(%r)
g = runpy.run_path(str(ROOT / 'scripts/benchmark_query_retention.py'))['main'].__globals__
with patch.object(g['subprocess'], 'check_output', return_value=%r):
    with %s:
        g['main']()
""" % (str(ROOT), BASELINE, "patch.object(g['queryu'].share, 'query', side_effect=[['first'], ['different']])" if mismatch else 'nullcontext()')
                    result = subprocess.run(
                        [sys.executable, '-S', *(['-O'] if optimized else []), '-c', code],
                        cwd=ROOT, capture_output=True, text=True, timeout=60,
                    )
                    if mismatch:
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertEqual(result.stdout, '')
                        self.assertIn('result mismatch', result.stderr)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertTrue(result.stdout.startswith('{'), result.stdout)

if __name__ == '__main__':
    unittest.main()
