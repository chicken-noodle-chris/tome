"""tome sync — pull/commit/push against a scratch bare-git origin."""

import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(vault, *args):
    return subprocess.run(["git", *args], cwd=str(vault),
                           check=True, capture_output=True, text=True)


def _bootstrap_git_vault(tmp_path, run_tome, name="vault"):
    """A bare 'origin' + a clone scaffolded by `tome init`, with an initial
    commit already pushed — mirrors a vault that already has history, so
    tests exercise steady-state sync rather than the empty-repo edge case
    (an unpushed bare repo has nothing for `git pull` to fetch)."""
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


def test_sync_clean_tree_already_in_sync(tmp_path, run_tome, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    code = run_tome("--vault", str(vault), "sync")

    assert code == 0
    assert "already in sync" in capsys.readouterr().out


def test_sync_dirty_without_message_fails(tmp_path, run_tome, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    (vault / "wiki" / "scratch.md").write_text("scratch\n", encoding="utf-8")

    code = run_tome("--vault", str(vault), "sync")

    assert code == 1
    assert "commit message is required" in capsys.readouterr().err


def test_sync_dirty_with_message_commits_and_pushes(tmp_path, run_tome, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    (vault / "wiki" / "scratch.md").write_text("scratch\n", encoding="utf-8")

    code = run_tome("--vault", str(vault), "sync", "-m", "add scratch")

    assert code == 0
    assert "synced." in capsys.readouterr().out
    log = subprocess.run(["git", "log", "--oneline"], cwd=str(origin),
                          check=True, capture_output=True, text=True)
    assert "add scratch" in log.stdout


def test_sync_refuses_non_main_branch(tmp_path, run_tome, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _git(vault, "checkout", "-b", "feature")

    code = run_tome("--vault", str(vault), "sync")

    assert code == 1
    assert "not main" in capsys.readouterr().err
