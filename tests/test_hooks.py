"""Subprocess tests for the hook scripts: each is invoked as a real child
process with a controlled cwd/env/stdin, exactly as Claude Code invokes it —
these are stdlib-only scripts outside the tome_cli package, so they can't be
exercised via run_tome()."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_CONTEXT_HOOK = REPO_ROOT / "hooks" / "tome_session_context.py"
SYNC_REMINDER_HOOK = REPO_ROOT / "hooks" / "tome_sync_reminder.py"

_spec = importlib.util.spec_from_file_location("tome_session_context", SESSION_CONTEXT_HOOK)
_session_context = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_session_context)
to_posix = _session_context.to_posix


def _run_hook(script, cwd, env=None, payload=None):
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=str(cwd),
        env=env,
        input=json.dumps(payload or {}),
        capture_output=True, text=True,
    )


def _git(vault, *args):
    return subprocess.run(["git", *args], cwd=str(vault),
                           check=True, capture_output=True, text=True)


def _make_vault(tmp_path, name="vault", dirty=False):
    """A minimal vault: just enough for conventions.toml to resolve and,
    optionally, a git repo with an uncommitted change."""
    vault = tmp_path / name
    (vault / "wiki").mkdir(parents=True)
    (vault / "conventions.toml").write_text("", encoding="utf-8")
    _git(vault, "init")
    _git(vault, "config", "user.email", "test@example.com")
    _git(vault, "config", "user.name", "Test")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "initial")
    if dirty:
        (vault / "wiki" / "index.md").write_text("dirty", encoding="utf-8")
    return vault


# --------------------------------------------------------------------------- #
# SessionStart: tome_session_context.py
# --------------------------------------------------------------------------- #

def test_session_context_inside_vault_emits_pointer(tmp_path):
    vault = _make_vault(tmp_path)

    result = _run_hook(SESSION_CONTEXT_HOOK, cwd=vault)

    assert result.returncode == 0
    out = json.loads(result.stdout)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert str(vault) in context
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_session_context_outside_vault_with_vault_root_emits_pointer(tmp_path):
    vault = _make_vault(tmp_path, name="vault")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    env = {"VAULT_ROOT": str(vault), "PATH": os.environ.get("PATH", "")}

    result = _run_hook(SESSION_CONTEXT_HOOK, cwd=outside, env=env)

    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert str(vault) in out["hookSpecificOutput"]["additionalContext"]


def test_session_context_matches_prime_terse_text(tmp_path):
    """The hook's injected context must be byte-identical to `tome prime`'s
    terse tier — one source of truth, no drift between the two."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from tome_cli.cli import prime_terse_text

    vault = _make_vault(tmp_path)

    result = _run_hook(SESSION_CONTEXT_HOOK, cwd=vault)

    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context == prime_terse_text(vault)


def test_session_context_no_vault_anywhere_is_silent(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    env = {"PATH": os.environ.get("PATH", "")}

    result = _run_hook(SESSION_CONTEXT_HOOK, cwd=outside, env=env)

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_session_context_finds_sibling_vault_when_remote(tmp_path):
    """A cloud (CLAUDE_CODE_REMOTE=true) session standing in a project repo
    with the vault checked out alongside it, not above it, must still
    resolve the vault by scanning siblings."""
    vault = _make_vault(tmp_path, name="knowledge-vault")
    project = tmp_path / "some-project"
    project.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "CLAUDE_CODE_REMOTE": "true"}

    result = _run_hook(SESSION_CONTEXT_HOOK, cwd=project, env=env)

    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert str(vault) in out["hookSpecificOutput"]["additionalContext"]


def test_session_context_sibling_scan_not_used_when_not_remote(tmp_path):
    """The same layout without CLAUDE_CODE_REMOTE=true must not pick up a
    sibling vault — the scan is gated to cloud sessions only."""
    _make_vault(tmp_path, name="knowledge-vault")
    project = tmp_path / "some-project"
    project.mkdir()
    env = {"PATH": os.environ.get("PATH", "")}

    result = _run_hook(SESSION_CONTEXT_HOOK, cwd=project, env=env)

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_session_context_sibling_vault_exports_vault_root(tmp_path):
    """Resolution via the sibling scan must export VAULT_ROOT through
    $CLAUDE_ENV_FILE so `tome` commands run from anywhere in the workspace
    (e.g. a Bash cwd inside the project repo, not the vault) still resolve."""
    vault = _make_vault(tmp_path, name="knowledge-vault")
    project = tmp_path / "some-project"
    project.mkdir()
    env_file = tmp_path / "env.sh"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "CLAUDE_CODE_REMOTE": "true",
        "CLAUDE_ENV_FILE": str(env_file),
    }

    result = _run_hook(SESSION_CONTEXT_HOOK, cwd=project, env=env)

    assert result.returncode == 0
    written = env_file.read_text(encoding="utf-8")
    assert f'export VAULT_ROOT="{to_posix(vault)}"' in written


def test_session_context_walkup_vault_does_not_export_vault_root(tmp_path):
    """A vault found by ordinary cwd walk-up needs no VAULT_ROOT export —
    only the sibling-scan fallback does."""
    vault = _make_vault(tmp_path)
    env_file = tmp_path / "env.sh"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "CLAUDE_CODE_REMOTE": "true",
        "CLAUDE_ENV_FILE": str(env_file),
    }

    result = _run_hook(SESSION_CONTEXT_HOOK, cwd=vault, env=env)

    assert result.returncode == 0
    written = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    assert "VAULT_ROOT" not in written


# --------------------------------------------------------------------------- #
# Stop: tome_sync_reminder.py
# --------------------------------------------------------------------------- #

def test_stop_dirty_vault_by_walkup_blocks(tmp_path):
    vault = _make_vault(tmp_path, dirty=True)

    result = _run_hook(SYNC_REMINDER_HOOK, cwd=vault)

    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block"


def test_stop_dirty_vault_reachable_only_via_vault_root_is_silent(tmp_path):
    vault = _make_vault(tmp_path, dirty=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    env = {"VAULT_ROOT": str(vault), "PATH": os.environ.get("PATH", "")}

    result = _run_hook(SYNC_REMINDER_HOOK, cwd=outside, env=env)

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_stop_clean_vault_is_silent(tmp_path):
    vault = _make_vault(tmp_path, dirty=False)

    result = _run_hook(SYNC_REMINDER_HOOK, cwd=vault)

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_stop_hook_active_reentry_is_silent(tmp_path):
    vault = _make_vault(tmp_path, dirty=True)

    result = _run_hook(SYNC_REMINDER_HOOK, cwd=vault, payload={"stop_hook_active": True})

    assert result.returncode == 0
    assert result.stdout.strip() == ""
