"""Local rollout contract; adopted vendor locks are deliberately independent."""
import re
from pathlib import Path

PARENT = "myon-bioinformatics/myon-bioinformatics"
PARENT_WORKFLOW_PIN = "2192ff22acafdd09e80ac722df91ee937aaedd5e"
ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".github/workflows").is_dir())


def test_parent_workflow_references_use_reviewed_rollout_commit():
    pins = []
    for path in sorted((ROOT / ".github/workflows").glob("*")):
        if path.suffix not in (".yml", ".yaml"):
            continue
        text = path.read_text(encoding="utf-8")
        # Capture mutable and shortened references too, rather than ignoring them.
        pins.extend(re.findall(r"uses:\s*['\"]?" + re.escape(PARENT) + r"/[^\s@'\"]+@([^\s'\"]+)", text))
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if re.fullmatch(r"\s+repository:\s*['\"]?" + re.escape(PARENT) + r"['\"]?\s*", line):
                refs = [re.fullmatch(r"\s+ref:\s*['\"]?([^\s'\"]+)['\"]?\s*", item) for item in lines[i + 1:i + 12]]
                pins.append(next((m.group(1) for m in refs if m), None))
    assert pins, "shared parent references must remain observable"
    assert set(pins) == {PARENT_WORKFLOW_PIN}, pins


def test_vendor_dispatch_verifies_lock_on_latest_parent_main():
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    job = text.split("\n  vendor-dispatch:\n", 1)[1].split("\n  failure-identity:\n", 1)[0]
    assert "pilot/vendor_dispatch.py --manifest vendor.lock.json" in job
    assert "--record" not in job
    assert "python -S" not in job
    assert "git ls-remote" in job
    assert "638801160" in job
    assert "repository id mismatch" in job
    assert 'node-version: "24"' in job
    assert 'python-version: "3.12"' in job
    assert "actions/checkout@v7" in job
    assert "actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0" in job
    assert "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v7.1.0" in job
    assert "astral-sh/setup-uv@07a047e608db84b3d8c06cd9a7c7dcc9c355c18e # v10.3.2" in job
    assert "uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" not in text
    assert "uv run --locked --python" in text
    assert "pytest -q --junitxml=reports/junit.xml" in text
