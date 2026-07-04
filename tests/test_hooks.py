"""Subprocess tests for the hook scripts: each is invoked as a real child
process with a controlled cwd/env/stdin, exactly as Claude Code invokes it —
these are stdlib-only scripts outside the tome_cli package, so they can't be
exercised via run_tome()."""
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


def test_session_context_no_vault_anywhere_is_silent(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    env = {"PATH": os.environ.get("PATH", "")}

    result = _run_hook(SESSION_CONTEXT_HOOK, cwd=outside, env=env)

    assert result.returncode == 0
    assert result.stdout.strip() == ""


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
