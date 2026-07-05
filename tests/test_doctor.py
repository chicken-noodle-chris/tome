"""tome doctor — the it-is-broken front door. Must run to completion (never
crash) whether the vault is healthy, absent, or broken, and gate its exit
code on FAIL-severity checks only."""

import shutil
import subprocess

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
