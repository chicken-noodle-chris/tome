"""Conflict-resolution contract tests ([[conflict-resolution]]).

Two triggers, one three-way model, so two groups of tests:

  * **A — local drift.** A stale `baseHash` no longer returns a bare 409: it
    returns the current text and its provenance, which is what lets the client
    open a resolver instead of telling the user to copy-and-reload. Locked
    here because the frontend's whole merge path reads that payload.
  * **B — git fork.** A `pull --rebase` that genuinely conflicts used to leave
    the tree stopped mid-rebase with "resolve manually". These lock the way
    back out: the three git stages surfaced under the user's own labels, then
    resolve -> continue (or abort).

The B tests build real diverged history through a second clone, so they need
git on PATH like the other write-path tests.
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tome_cli import cli as tome  # noqa: E402
from tome_cli import serve  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _conv(vault):
    return tome.load_conventions(vault)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd),
                          check=True, capture_output=True, text=True)


def _bootstrap_git_vault(tmp_path, run_tome, name="vault"):
    """Same helper as test_serve.py/test_sync_scoped.py, duplicated per that
    convention — the conflict paths need a real origin to diverge from."""
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

    assert run_tome("init", str(vault)) == 0

    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "initial")
    _git(vault, "push", "-u", "origin", "main")
    return vault, origin


def _scaffold_idea(vault, run_tome, slug="alpha"):
    run_tome("--vault", str(vault), "new", "project", "tome", "--title", "Tome", "--desc", "d")
    run_tome("--vault", str(vault), "new", "idea", slug, "--project", "tome",
              "--title", slug.capitalize(), "--desc", "d")
    return vault / "wiki" / "tome" / "ideas" / f"{slug}.md"


def _committed_idea(tmp_path, run_tome):
    """A vault whose `alpha` page is pushed — the starting point both groups
    of tests diverge from."""
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _scaffold_idea(vault, run_tome)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add alpha")
    _git(vault, "push")
    return vault, origin, target


REL = "tome/ideas/alpha.md"
VAULT_REL = "wiki/tome/ideas/alpha.md"


# --------------------------------------------------------------------------- #
# A — local drift: the 409 that opens a resolver.
# --------------------------------------------------------------------------- #

def test_save_page_conflict_returns_their_text_and_provenance(tmp_path, run_tome):
    vault, _origin, target = _committed_idea(tmp_path, run_tome)
    target.write_text(target.read_text(encoding="utf-8") + "\nWritten by VS Code.\n",
                       encoding="utf-8")

    status, result = serve.save_page(vault, _conv(vault), REL,
                                      "\n# Alpha\n\nMy edit.\n", "stale-hash")

    assert status == 409
    conflict = result["conflict"]
    assert conflict["type"] == "local-drift"
    assert conflict["source"] == "disk"
    # The external side, verbatim — the client's `theirs`.
    assert "Written by VS Code." in conflict["theirs"]
    # The *when*, since a local write has no author to name.
    assert conflict["mtime"] == pytest.approx(target.stat().st_mtime)
    # And the hash the merged buffer will save against.
    assert result["currentHash"] == hashlib.sha256(target.read_bytes()).hexdigest()


def test_save_frontmatter_conflict_returns_their_text(tmp_path, run_tome):
    vault, _origin, target = _committed_idea(tmp_path, run_tome)
    tome.run_git(vault, ["checkout", "--", VAULT_REL])
    text = target.read_text(encoding="utf-8").replace('description: "d"',
                                                       'description: "theirs"')
    target.write_text(text, encoding="utf-8")

    status, result = serve.save_frontmatter(vault, _conv(vault), REL,
                                             {"description": "mine"}, "stale-hash")

    assert status == 409
    assert result["conflict"]["type"] == "local-drift"
    assert 'description: "theirs"' in result["conflict"]["theirs"]


def test_rename_conflict_stays_refuse_and_reload(tmp_path, run_tome):
    """A slug is atomic — there is nothing to hunk-merge about two different
    names — so this one 409 deliberately carries no resolver payload."""
    vault, _origin, _target = _committed_idea(tmp_path, run_tome)

    status, result = serve.rename_page(vault, _conv(vault), REL, "beta", "stale-hash")

    assert status == 409
    assert "conflict" not in result
    assert "currentHash" in result


# --------------------------------------------------------------------------- #
# B — git fork: the stopped rebase, and the way out of it.
# --------------------------------------------------------------------------- #

def _diverge(tmp_path, vault, origin, target):
    """Genuinely forked history on one line of `alpha`: a remote commit pushed
    from a second clone, and a local commit editing the same line, unpushed.
    The next `pull --rebase` must conflict."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(origin), str(other)],
                    check=True, capture_output=True)
    _git(other, "config", "user.email", "remote@example.com")
    _git(other, "config", "user.name", "Remote Writer")
    other_page = other / VAULT_REL
    other_page.write_text(other_page.read_text(encoding="utf-8").replace("TBD.", "REMOTE LINE."),
                           encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "remote edit")
    _git(other, "push")

    target.write_text(target.read_text(encoding="utf-8").replace("TBD.", "LOCAL LINE."),
                       encoding="utf-8")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "local edit")


def test_save_page_surfaces_a_forked_history_as_a_conflict(tmp_path, run_tome):
    vault, origin, target = _committed_idea(tmp_path, run_tome)
    _diverge(tmp_path, vault, origin, target)

    status, result = serve.save_page(vault, _conv(vault), REL, "\nirrelevant\n", "any")

    assert status == 409
    conflict = result["conflict"]
    assert conflict["type"] == "git-fork"
    assert conflict["rebase"] is True
    (file,) = conflict["files"]
    assert file["path"] == VAULT_REL
    # The stage-to-side mapping, from the user's point of view rather than
    # git's: mid-rebase `:3:` is the commit being replayed (theirs, to git)
    # and `:2:` is the upstream (ours, to git) — inverted from the labels.
    assert "LOCAL LINE." in file["mine"]
    assert "REMOTE LINE." in file["theirs"]
    assert "TBD." in file["base"]
    # Provenance: who/when/which commit, for the side the user must weigh.
    assert conflict["theirsCommit"]["author"] == "Remote Writer"
    assert conflict["theirsCommit"]["subject"] == "remote edit"
    assert conflict["theirsCommit"]["sha"]
    assert conflict["mineCommit"]["subject"] == "local edit"


def test_resolve_then_continue_finishes_the_rebase_and_pushes(tmp_path, run_tome):
    vault, origin, target = _committed_idea(tmp_path, run_tome)
    _diverge(tmp_path, vault, origin, target)
    serve.save_page(vault, _conv(vault), REL, "\nirrelevant\n", "any")  # trips the rebase

    merged = target.read_text(encoding="utf-8").split("TBD")[0] + "MERGED LINE.\n"
    status, result = serve.resolve_conflict_file(vault, VAULT_REL, merged)
    assert status == 200
    assert result["conflict"]["files"] == []  # staged, so no longer unmerged

    status, result = serve.continue_rebase(vault)

    assert status == 200, result
    assert result["done"] is True
    assert not serve.rebase_in_progress(vault)
    assert "MERGED LINE." in target.read_text(encoding="utf-8")
    log = _git(origin, "log", "--oneline").stdout
    assert "local edit" in log and "remote edit" in log


def test_continue_refuses_while_a_file_is_still_unmerged(tmp_path, run_tome):
    vault, origin, target = _committed_idea(tmp_path, run_tome)
    _diverge(tmp_path, vault, origin, target)
    serve.save_page(vault, _conv(vault), REL, "\nirrelevant\n", "any")

    status, result = serve.continue_rebase(vault)

    assert status == 400
    assert "unmerged" in result["error"]
    assert serve.rebase_in_progress(vault)  # left exactly as it was


def test_resolve_refuses_a_path_git_did_not_flag(tmp_path, run_tome):
    """The resolver may only finish a conflict git handed it — it is not a
    general-purpose write endpoint."""
    vault, origin, target = _committed_idea(tmp_path, run_tome)
    _diverge(tmp_path, vault, origin, target)
    serve.save_page(vault, _conv(vault), REL, "\nirrelevant\n", "any")

    status, result = serve.resolve_conflict_file(vault, "wiki/index.md", "pwned")

    assert status == 400
    assert "unmerged" in result["error"]
    assert "pwned" not in (vault / "wiki" / "index.md").read_text(encoding="utf-8")


def test_abort_returns_the_tree_to_a_known_state(tmp_path, run_tome):
    vault, origin, target = _committed_idea(tmp_path, run_tome)
    _diverge(tmp_path, vault, origin, target)
    serve.save_page(vault, _conv(vault), REL, "\nirrelevant\n", "any")

    status, result = serve.abort_rebase(vault)

    assert status == 200
    assert result["aborted"] is True
    assert not serve.rebase_in_progress(vault)
    assert "LOCAL LINE." in target.read_text(encoding="utf-8")  # the local commit survives
    assert _git(vault, "status", "--porcelain").stdout.strip() == ""


def test_conflict_state_is_empty_without_a_rebase(tmp_path, run_tome):
    vault, _origin, _target = _committed_idea(tmp_path, run_tome)

    state = serve.git_conflict_state(vault)

    assert state == {"rebase": False, "files": []}
    assert serve.continue_rebase(vault)[0] == 409
    assert serve.abort_rebase(vault)[0] == 409


def test_conflict_endpoints_are_absent_from_a_static_export(tmp_path, run_tome):
    """AC: the export stays read-only — the resolver's endpoints have no
    server behind them there, and the vendored diff lib ships anyway."""
    vault, _origin, _target = _committed_idea(tmp_path, run_tome)
    out = tmp_path / "static"

    serve.export_static(vault, _conv(vault), out)

    assert not (out / "api").exists()
    assert not list(out.rglob("conflict*"))
    assert (out / "app" / "vendor" / "diff3.mjs").is_file()
    assert (out / "app" / "merge.js").is_file()
