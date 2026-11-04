# Optional cache test environment

`cache-test` installs the existing locked pytest and its dependencies, without
NumPy. It is an opt-in development profile for these **unchanged** test files:

- `tests/test_cache_core.py`
- `tests/test_cache_snapshots.py`
- `tests/test_queryu_cache_share.py`
- `tests/test_urllib_client.py`
- `tests/test_query_retention.py`

Together they cover the standalone cache, original-byte verification and immutable
history, bundle round trips and rejection of unsafe/tampered input, synchronous
and asynchronous snapshots, parallel fake-client fetching, and urllib behavior
against a temporary loopback HTTP server. They do not fetch live source pages.

The default `dev` group still includes NumPy. Full CI, its LAPACK/pure-Python
matrix, and `tests/test_queryu_numpy_min.py` remain unchanged. Do not run the full
suite in this small environment: the independent NumPy comparison tests require
the full environment and deliberately fail to import when NumPy is missing.
There are no replacement library functions, test deletions, skip additions or
runner changes in this profile.

## Run the focused tests

Use Python 3.11+ and uv. Prepare the same real Aoi and BlueProbe source checkouts
as for the full tests in the main README. These are still required by the
existing conftest and import-origin guards, even for these focused files. The
profile reduces installed Python packages, not the source checkouts.

From the QueRyu root, in a POSIX shell:

```sh
cache_env="$PWD/build/cache-test/.venv"
UV_PROJECT_ENVIRONMENT="$cache_env" uv sync --locked --only-group cache-test

AOI_ROOT="$(cd ../Aoi && pwd)" \
PYTHONPATH="$(cd ../BlueProbe && pwd)" \
UV_PROJECT_ENVIRONMENT="$cache_env" \
  uv run --locked --no-sync python -m pytest -q \
    tests/test_cache_core.py \
    tests/test_cache_snapshots.py \
    tests/test_queryu_cache_share.py \
    tests/test_urllib_client.py \
    tests/test_query_retention.py
```

The separate directory is inside ignored `build/`; it does not replace the
ordinary `.venv`. `--only-group cache-test` selects the small locked group;
`--no-sync` prevents the later test invocation from silently installing the
default `dev` group again. Do not disable conftest or plugin autoloading, or add
ignore/skip flags to make a wider suite pass.

Verify the absence of NumPy, if needed:

```sh
UV_PROJECT_ENVIRONMENT="$cache_env" uv run --locked --no-sync python -c \
  'import importlib.util; assert importlib.util.find_spec("numpy") is None'
```

For the full suite, use the existing README command with no
`UV_PROJECT_ENVIRONMENT` override. Its default environment retains NumPy and all
existing numerical comparisons. Dependency changes should update the normal
lock once, then rerun both profiles; do not maintain a second requirements lock.

## Local comparison on 2026-11-04

Fixed inputs:

- QueRyu main `ac6ceebab09be3b4225d39a4d741544fc9731eea`, plus this profile's
  metadata changes. Product/test/conftest files were identical in both modes.
- Aoi `157c67ce8b6f49008ad1063f1727f433d7ce4ab3`.
- BlueProbe `2ea993aaa1416ff9006eba4ebe2fed6d0b49668e`.
- Linux x86_64, CPython 3.15.1, uv 0.13.5; pytest 9.1.1 in both modes.
- Three pairs, each with a new venv and its own empty uv cache. Execution order:
  full/cache, cache/full, full/cache. No failed installation or test samples.

| Measurement | Default dev | cache-test |
| --- | ---: | ---: |
| Installed distributions | 6 | 5 |
| Venv regular-file bytes before tests | 63,005,411 | 6,616,901 |
| Cache regular-file bytes after install | 62,978,042 | 6,589,084 |
| Install seconds, trials 1/2/3 | 4.600 / 3.300 / 3.561 | 3.674 / 3.497 / 13.105 |
| Install median seconds | 3.561 | 3.674 |
| Test process seconds, trials 1/2/3 | 3.263 / 3.240 / 3.220 | 3.490 / 3.323 / 3.192 |
| Test process median seconds | 3.240 | 3.323 |

The smaller environment saved 56,388,510 logical bytes (about 89.5%) in this
measurement. **Neither installation nor test execution became faster by the
median.** These are small local samples, not CI timing or a general performance
claim. Only the first install pair favored the smaller environment. The final small
environment took 13.105 seconds to install and was not retried. All samples are retained in [the measurement record](nano-cache-tests.json).

Counts are installed distributions, not the number of lock records. The full
Python 3.15 environment has NumPy 2.5.4 in addition to pytest 9.1.1, iniconfig
2.3.1, packaging 26.3, pluggy 1.7.0 and Pygments 2.21.0. The smaller environment
has exactly those five pytest distributions. All eight existing third-party
lock records, including both NumPy versions, platform markers, sources and wheel
hashes, are unchanged. Only the QueRyu group metadata was added.

The size measure sums ordinary files without following symlinks. It excludes
the shared Python runtime, stdlib, source checkouts and generated test outputs.
Venv/cache can share hardlinks and must not be summed as physical disk savings.
OS/CDN cache and host load were not controlled; an independent 6.48-second full
suite validation overlapped the beginning of the first full installation. Python/source acquisition and
initial lock preparation are outside install timing. Test time includes the uv
and pytest process startup. `PYTHONDONTWRITEBYTECODE=1` was identical in both
measured modes. The profile does not claim container-level security isolation,
CPU/RAM quotas, system-LAPACK removal, or whole-host resource savings.

## Verification

- All 56 selected tests passed in all six environments: 336 executions, zero
  skips. Collection IDs, all setup/call/teardown outcomes, rootdir, configuration
  path and external-plugin lists matched. No external pytest plugins were loaded
  in either mode; autoloading was not disabled. The same read-only observer was
  added to both measurement commands.
- A fixed-time synthetic cache workload produced nine byte-identical files in
  every environment, including the tar bundle, original/imported manifests,
  compressed bodies and JSON snapshots/query/verification results. The workload
  patched only the clock for reproducibility, not production cache logic.
  The complete `output.json` SHA-256 is
  `169963de3062e3850aba6182ecbb8b3e647ea1af6e1ea26c30533182dbc11c92`.
- `uv pip check --offline` passed in all six environments. The installed
  name/version sets were inspected, including NumPy's absence in cache-test.
- The full default suite with `QUERYU_EXPECT_BACKEND=lapack` passed all 201 tests,
  including the real NumPy comparison tests. An earlier local run without that
  variable had 199 passed and the existing backend-lane assertion skipped; no
  test or skip condition was changed to obtain the later result. The final current-main result above includes #33's newly merged regression.
- A deliberate attempt to collect the NumPy comparison file in cache-test failed
  with exit 2 and `ModuleNotFoundError: No module named 'numpy'`, confirming the
  profile's boundary rather than silently skipping the comparison.
- Python 3.12.15 also passed all 56 focused tests in the cache-test venv.
- `uv lock --check --offline` and `git diff --check` passed.

No hosted CI execution, Python 3.11/3.13/3.14 run, other OS run, or forced-pure
full-suite run was performed for this change. Existing CI remains responsible
for the complete supported matrix. The profile does not certify the numerical
workloads or all of QueRyu with NumPy removed.

Initial lock preparation with `uv lock --offline` failed because the cache did
not contain the Python-3.11 NumPy metadata. A normal online `uv lock` succeeded
without changing any existing third-party lock entry. This preparation failure
is not counted as an environment-install sample.

An earlier pass on main `4c59cb00ca2ba898573e4af95bc4a63492aa5eb9` had 55
focused tests and 200 full-suite tests passing. PR #33 merged while that work
was in progress. This record supersedes it: the current-main command includes
`test_query_retention.py`, and all six environments were freshly re-created
and remeasured. No existing test was removed.
