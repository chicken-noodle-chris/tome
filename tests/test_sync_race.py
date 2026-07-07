"""tome sync's push-rejection retry: between sync_core's own pull and its
push, another writer can land on the remote — guaranteed eventually once a
headless remote deployment and a local session share a vault. On rejection,
sync_core pulls --rebase once and retries the push exactly once."""

import shutil
import subprocess

import pytest

from tome_cli import cli as tome

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(vault, *args):
    return subprocess.run(["git", *args], cwd=str(vault),
                           check=True, capture_output=True, text=True)


def _bootstrap_two_clones(tmp_path, run_tome):
    """A bare origin, cloned twice: `vault` (scaffolded by `tome init`, the
    clone under test) and `other` (a plain second writer that races it)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)],
                    check=True, capture_output=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                    cwd=str(origin), check=True, capture_output=True)

    vault = tmp_path / "vault"
    subprocess.run(["git", "clone", str(origin), str(vault)],
                    check=True, capture_output=True)
    _git(vault, "config", "user.email", "test@example.com")
    _git(vault, "config", "user.name", "Test")
    code = run_tome("init", str(vault))
    assert code == 0
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "initial")
    _git(vault, "push", "-u", "origin", "main")

    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(origin), str(other)],
                    check=True, capture_output=True)
    _git(other, "config", "user.email", "other@example.com")
    _git(other, "config", "user.name", "Other")

    return vault, other, origin


def test_sync_retries_once_after_push_rejection(tmp_path, run_tome, monkeypatch, capsys):
    vault, other, origin = _bootstrap_two_clones(tmp_path, run_tome)
    with (vault / "wiki" / "log.md").open("a", encoding="utf-8") as fh:
        fh.write("\nour scratch note\n")

    real_run_git = tome.run_git
    raced = {"done": False}

    def racing_run_git(vault_root, args):
        if args and args[0] == "push" and not raced["done"]:
            raced["done"] = True
            (other / "wiki" / "race.md").write_text("race\n", encoding="utf-8")
            _git(other, "add", "-A")
            _git(other, "commit", "-m", "race commit from another writer")
            _git(other, "push")
        return real_run_git(vault_root, args)

    monkeypatch.setattr(tome, "run_git", racing_run_git)

    code = run_tome("--vault", str(vault), "sync", "-m", "our commit")

    assert raced["done"], "the race wasn't actually injected"
    assert code == 0
    assert "synced." in capsys.readouterr().out
    log = subprocess.run(["git", "log", "--oneline"], cwd=str(origin),
                          check=True, capture_output=True, text=True)
    assert "race commit from another writer" in log.stdout
    assert "our commit" in log.stdout


def test_sync_fails_loud_when_retry_rebase_conflicts(tmp_path, run_tome, monkeypatch, capsys):
    vault, other, origin = _bootstrap_two_clones(tmp_path, run_tome)
    # both writers touch the exact same line of the exact same file -> a
    # rebase conflict the retry cannot resolve on its own.
    log_path = vault / "wiki" / "log.md"
    original = log_path.read_text(encoding="utf-8")
    log_path.write_text(original + "\nOUR LINE\n", encoding="utf-8")

    real_run_git = tome.run_git
    raced = {"done": False}

    def racing_run_git(vault_root, args):
        if args and args[0] == "push" and not raced["done"]:
            raced["done"] = True
            other_log = other / "wiki" / "log.md"
            other_log.write_text(original + "\nTHEIR LINE\n", encoding="utf-8")
            _git(other, "add", "-A")
            _git(other, "commit", "-m", "conflicting race commit")
            _git(other, "push")
        return real_run_git(vault_root, args)

    monkeypatch.setattr(tome, "run_git", racing_run_git)

    code = run_tome("--vault", str(vault), "sync", "-m", "our commit")

    assert raced["done"], "the race wasn't actually injected"
    assert code == 1
    err = capsys.readouterr().err
    assert "resolve manually" in err
    # the rebase state is left intact for a human, not silently aborted
    assert (vault / ".git" / "rebase-merge").exists() or (vault / ".git" / "rebase-apply").exists()
