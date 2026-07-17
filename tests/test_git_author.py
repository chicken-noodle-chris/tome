"""TOME_GIT_AUTHOR — per-commit attribution for headless remote writers, so
a vault's git log shows which surface made each change without needing any
git config on the container. Supplies the author via `git commit --author`
and the committer via derived GIT_COMMITTER_* env — git refuses to commit
(and to rebase) without a committer identity, which a bare container lacks."""

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(vault, *args):
    return subprocess.run(["git", *args], cwd=str(vault),
                           check=True, capture_output=True, text=True)


def _bootstrap_git_vault(tmp_path, run_tome, name="vault"):
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


def test_sync_commit_uses_default_git_identity_without_env(tmp_path, run_tome, monkeypatch):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    monkeypatch.delenv("TOME_GIT_AUTHOR", raising=False)
    # ambient GIT_AUTHOR_*/GIT_COMMITTER_* would otherwise outrank the vault's
    # own repo-local git config in this assertion
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    with (vault / "wiki" / "log.md").open("a", encoding="utf-8") as fh:
        fh.write("\nscratch note\n")

    code = run_tome("--vault", str(vault), "sync", "-m", "add scratch")

    assert code == 0
    log = _git(vault, "log", "-1", "--pretty=%an <%ae>")
    assert log.stdout.strip() == "Test <test@example.com>"


def test_sync_commit_applies_tome_git_author(tmp_path, run_tome, monkeypatch):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    monkeypatch.setenv("TOME_GIT_AUTHOR", "tome-remote <tome-remote@invalid>")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    with (vault / "wiki" / "log.md").open("a", encoding="utf-8") as fh:
        fh.write("\nscratch note\n")

    code = run_tome("--vault", str(vault), "sync", "-m", "add scratch")

    assert code == 0
    log = _git(vault, "log", "-1", "--pretty=%an <%ae>")
    assert log.stdout.strip() == "tome-remote <tome-remote@invalid>"
    # committer follows TOME_GIT_AUTHOR too (derived GIT_COMMITTER_* env
    # outranks the repo-local config): the surface that ran the command is
    # the committer, and a bare container has no other identity to offer
    committer = _git(vault, "log", "-1", "--pretty=%cn <%ce>")
    assert committer.stdout.strip() == "tome-remote <tome-remote@invalid>"


def test_sync_commits_on_container_with_no_git_identity(tmp_path, run_tome, monkeypatch):
    """The headless-bootstrap case TOME_GIT_AUTHOR exists for: no repo-local
    user config, no global/system config. Without the derived committer env
    the commit dies with 'Committer identity unknown'."""
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _git(vault, "config", "--unset", "user.name")
    _git(vault, "config", "--unset", "user.email")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("TOME_GIT_AUTHOR", "tome-remote <tome-remote@invalid>")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    with (vault / "wiki" / "log.md").open("a", encoding="utf-8") as fh:
        fh.write("\nscratch note\n")

    code = run_tome("--vault", str(vault), "sync", "-m", "add scratch")

    assert code == 0
    log = _git(vault, "log", "-1", "--pretty=%an <%ae> %cn <%ce>")
    assert log.stdout.strip() == ("tome-remote <tome-remote@invalid> "
                                  "tome-remote <tome-remote@invalid>")


def test_explicit_committer_env_outranks_derived_identity(tmp_path, run_tome, monkeypatch):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    monkeypatch.setenv("TOME_GIT_AUTHOR", "tome-remote <tome-remote@invalid>")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Explicit")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "explicit@example.com")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    with (vault / "wiki" / "log.md").open("a", encoding="utf-8") as fh:
        fh.write("\nscratch note\n")

    code = run_tome("--vault", str(vault), "sync", "-m", "add scratch")

    assert code == 0
    log = _git(vault, "log", "-1", "--pretty=%an <%ae> %cn <%ce>")
    assert log.stdout.strip() == ("tome-remote <tome-remote@invalid> "
                                  "Explicit <explicit@example.com>")
