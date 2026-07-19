"""scripts/*.py shims — the plugin invokes these paths directly
(`python "$CLAUDE_PLUGIN_ROOT/scripts/<name>.py" ...`), so they must keep
working as thin wrappers even though the real code now lives under
src/tome_cli/."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


@pytest.mark.parametrize("script,arg", [
    ("tome.py", "help"),
    ("tome_lint.py", "--help"),
    ("wiki_search.py", "--help"),
])
def test_shim_runs(script, arg):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), arg],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
