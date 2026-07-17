"""tome doctor — the it-is-broken front door. Must run to completion (never
crash) whether the vault is healthy, absent, or broken, and gate its exit
code on FAIL-severity checks only."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(vault, *args):
    return subprocess.run(["git", *args], cwd=str(vault),
                           check=True, capture_output=True, text=True)


def _bootstrap_git_vault(tmp_path, run_tome, name="vault"):
    """Mirrors test_sync.py's helper: a bare 'origin' + a clone scaffolded by
    `tome init`, committed and pushed on main — the shape of a real, healthy
    vault (has a remote, is on main, is clean)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)],
                    check=True, capture_output=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                    cwd=str(origin), check=True, capture_output=True)

    vault = tmp_path / name
    subprocess.run(["git", "clone", str(origin), str(vault)],
                    check=True, capture_output=True)
    _git(vault, "config", "user.email", "test@example.com")
    _git(vault, "config", "user.name", "Test")

    code = run_tome("init", str(vault))
    assert code == 0

    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "initial")
    _git(vault, "push", "-u", "origin", "main")

    return vault, origin


def test_healthy_vault_all_ok(tmp_path, run_tome, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    capsys.readouterr()  # discard `tome init`'s own stdout

    code = run_tome("--vault", str(vault), "doctor")

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    check_lines, summary = lines[:-1], lines[-1]
    assert code == 0
    # every check is ok except quartz (a fresh vault has none bootstrapped, info)
    assert all(line.startswith(("ok", "info")) for line in check_lines)
    assert "0 FAIL" in summary
    assert "0 warn" in summary


def test_no_vault_completes_gracefully(tmp_path, run_tome, monkeypatch, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.delenv("VAULT_ROOT", raising=False)

    code = run_tome("doctor")

    out = capsys.readouterr().out
    check_lines = [line for line in out.splitlines() if line.strip()][:-1]
    assert code == 0
    assert "no vault found" in out
    assert not any(line.startswith("FAIL") for line in check_lines)


def test_invalid_vault_root_env_fails(tmp_path, run_tome, monkeypatch, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    not_a_vault = tmp_path / "not-a-vault"
    not_a_vault.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.setenv("VAULT_ROOT", str(not_a_vault))

    code = run_tome("doctor")

    out = capsys.readouterr().out
    vault_line = next(line for line in out.splitlines() if "vault resolution" in line)
    assert code == 1
    assert vault_line.startswith("FAIL")


def test_broken_link_fails_lint_line(tmp_path, run_tome, capsys, make_page):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    make_page(vault, "scratch/notes/broken.md", type="concept", tags=["project"],
              body="\nSee [[does-not-exist]].\n")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add broken page")
    capsys.readouterr()  # discard `tome init`'s own stdout

    code = run_tome("--vault", str(vault), "doctor")

    out = capsys.readouterr().out
    lint_line = next(line for line in out.splitlines()
                      if line.split(":", 1)[0].split()[-1] == "lint")
    assert code == 1
    assert lint_line.startswith("FAIL")


def test_missing_binaries_warn_without_crashing(tmp_path, run_tome, monkeypatch, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    capsys.readouterr()  # discard `tome init`'s own stdout
    monkeypatch.setattr(shutil, "which", lambda name: None)

    code = run_tome("--vault", str(vault), "doctor")

    out = capsys.readouterr().out
    check_lines = [line for line in out.splitlines() if line.strip()][:-1]
    # every downstream check still handles absent git/node gracefully (warn, not FAIL)
    assert code == 0
    assert not any(line.startswith("FAIL") for line in check_lines)
    assert "warn git: not on PATH" in out
    assert "warn node/npm/npx: missing" in out
    assert "warn git state: git not on PATH" in out


def test_read_capture_profile_skips_node_check(tmp_path, run_tome, monkeypatch, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    capsys.readouterr()  # discard `tome init`'s own stdout
    monkeypatch.setenv("TOME_OPS_PROFILE", "read-capture")

    code = run_tome("--vault", str(vault), "doctor")

    out = capsys.readouterr().out
    assert code == 0
    node_line = next(line for line in out.splitlines() if "node/npm/npx" in line)
    assert node_line.startswith("info")
    assert "read-capture" in node_line
    profile_line = next(line for line in out.splitlines() if "ops profile" in line)
    assert profile_line.startswith("info")
    assert "read-capture" in profile_line


def test_plugin_freshness_no_cache_env_is_info(tmp_path, run_tome, monkeypatch, capsys):
    """Running from this real checkout with $TOME_PLUGIN_ROOT unset (no
    active session to compare against) — the common case for a bare CLI
    invocation outside a Claude Code session."""
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    capsys.readouterr()
    monkeypatch.delenv("TOME_PLUGIN_ROOT", raising=False)

    code = run_tome("--vault", str(vault), "doctor")

    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if "plugin freshness" in line)
    assert code == 0
    assert line.startswith("info")
    assert "TOME_PLUGIN_ROOT unset" in line


def test_plugin_freshness_matching_versions_ok(tmp_path, run_tome, monkeypatch, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    capsys.readouterr()

    from tome_cli import cli as tome
    dev_plugin_json = (Path(tome.__file__).resolve().parent.parent.parent
                        / ".claude-plugin" / "plugin.json")
    dev_version = json.loads(dev_plugin_json.read_text(encoding="utf-8"))["version"]

    cached_root = tmp_path / "cached-plugin"
    (cached_root / ".claude-plugin").mkdir(parents=True)
    (cached_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "tome", "version": dev_version}), encoding="utf-8")
    monkeypatch.setenv("TOME_PLUGIN_ROOT", str(cached_root))

    code = run_tome("--vault", str(vault), "doctor")

    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if "plugin freshness" in line)
    assert code == 0
    assert line.startswith("ok")
    assert dev_version in line


def test_plugin_freshness_stale_cache_warns(tmp_path, run_tome, monkeypatch, capsys):
    """The exact task-57 scenario: a directory-source marketplace's cached
    plugin sat at an old version while the dev checkout had moved on."""
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    capsys.readouterr()

    cached_root = tmp_path / "cached-plugin"
    (cached_root / ".claude-plugin").mkdir(parents=True)
    (cached_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "tome", "version": "0.0.1-stale"}), encoding="utf-8")
    monkeypatch.setenv("TOME_PLUGIN_ROOT", str(cached_root))

    code = run_tome("--vault", str(vault), "doctor")

    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if "plugin freshness" in line)
    assert code == 0  # warn, not FAIL
    assert line.startswith("warn")
    assert "0.0.1-stale" in line
    assert "claude plugin update tome@tome" in line


def test_unknown_ops_profile_fails(tmp_path, run_tome, monkeypatch, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    capsys.readouterr()  # discard `tome init`'s own stdout
    monkeypatch.setenv("TOME_OPS_PROFILE", "not-a-real-profile")

    code = run_tome("--vault", str(vault), "doctor")

    out = capsys.readouterr().out
    profile_line = next(line for line in out.splitlines() if "ops profile" in line)
    assert code == 1
    assert profile_line.startswith("FAIL")
