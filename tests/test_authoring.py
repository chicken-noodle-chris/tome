"""The [[remote-authoring]] surface: one page resolver, and the read/write/
append verbs built on it.

Two halves. `resolve_page` and `append_to_body` are pure enough to test
directly against a scaffolded vault. The write verbs commit and push by
default, so those tests need a real git origin — same bootstrap as
test_serve.py's save_page tests, duplicated per this suite's convention rather
than shared.
"""

import hashlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tome_cli import cli as tome  # noqa: E402

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _conv(vault):
    return tome.load_conventions(vault)


def _git(vault, *args):
    return subprocess.run(["git", *args], cwd=str(vault),
                           check=True, capture_output=True, text=True)


def _bootstrap_git_vault(tmp_path, run_tome):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                    cwd=str(origin), check=True, capture_output=True)

    vault = tmp_path / "vault"
    subprocess.run(["git", "clone", str(origin), str(vault)], check=True, capture_output=True)
    _git(vault, "config", "user.email", "test@example.com")
    _git(vault, "config", "user.name", "Test")

    assert run_tome("init", str(vault)) == 0
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "initial")
    _git(vault, "push", "-u", "origin", "main")
    return vault, origin


def _scaffold_idea(vault, run_tome, slug="alpha"):
    """A real `tome new` page — indexed and lint-clean, so the write path's own
    lint gate isn't tripped by an INDEX_MISSING the fixture caused."""
    run_tome("--vault", str(vault), "new", "project", "tome", "--title", "Tome", "--desc", "d")
    run_tome("--vault", str(vault), "new", "idea", slug, "--project", "tome",
              "--title", slug.capitalize(), "--desc", "d")
    return vault / "wiki" / "tome" / "ideas" / f"{slug}.md"


def _committed(vault, run_tome, slug="alpha"):
    target = _scaffold_idea(vault, run_tome, slug)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", f"add {slug}")
    _git(vault, "push")
    return target


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# resolve_page — the three address spaces collapsed into one
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ident", [
    "alpha",                              # bare slug
    "tome/ideas/alpha.md",                # wiki-relative / search display path
    "tome/ideas/alpha",                   # .md optional
    "wiki/tome/ideas/alpha.md",           # vault-relative
    "wiki\\tome\\ideas\\alpha.md",        # pasted off a Windows shell
    "ALPHA",                              # case-insensitive
    "Tome/Ideas/Alpha.md",
    "[[alpha]]",                          # a whole wikilink
    "[[alpha|Alpha]]",                    # ...with an alias
])
def test_resolver_accepts_every_address_form(make_vault, run_tome, ident):
    vault = make_vault()
    _scaffold_idea(vault, run_tome)
    page = tome.resolve_page(vault, _conv(vault), ident)
    assert page["slug"] == "alpha"


def test_resolver_miss_names_the_vault_and_nearest_slugs(make_vault, run_tome):
    vault = make_vault()
    _scaffold_idea(vault, run_tome)
    with pytest.raises(tome.VaultError) as excinfo:
        tome.resolve_page(vault, _conv(vault), "alpah")
    message = str(excinfo.value)
    assert str(vault) in message
    assert "alpha" in message  # the closest-matching slug, not just a refusal
    assert "wiki-relative" in message


def test_resolver_refuses_traversal_and_non_pages(make_vault, run_tome):
    vault = make_vault()
    _scaffold_idea(vault, run_tome)
    (vault / "wiki" / "tome" / "notes.txt").write_text("not a page", encoding="utf-8")
    for ident in ("../../etc/passwd", "/etc/passwd", "tome/notes.txt", ""):
        with pytest.raises(tome.VaultError):
            tome.resolve_page(vault, _conv(vault), ident)


# --------------------------------------------------------------------------- #
# append_to_body — plain tail append, and section-scoped append
# --------------------------------------------------------------------------- #

SECTIONED = "\n# Title\n\nIntro.\n\n## Log\n\n- first\n\n## Other\n\nTail.\n"


def test_append_lands_at_the_end_by_default():
    assert tome.append_to_body("\n# T\n\nBody.\n", "More.") == "\n# T\n\nBody.\n\nMore.\n"


def test_append_under_lands_inside_the_section():
    out = tome.append_to_body(SECTIONED, "- second", under="## Log")
    assert out.index("- second") < out.index("## Other")
    assert out.index("- first") < out.index("- second")


def test_append_under_matches_heading_text_without_markers():
    assert "- second" in tome.append_to_body(SECTIONED, "- second", under="log")


def test_append_under_unknown_section_lists_the_real_ones():
    with pytest.raises(tome.VaultError) as excinfo:
        tome.append_to_body(SECTIONED, "x", under="## Nope")
    assert "'Log'" in str(excinfo.value) and "'Other'" in str(excinfo.value)


def test_append_ignores_headings_inside_fenced_code():
    body = "\n# T\n\n```bash\n# Log\n```\n\n## Log\n\n- first\n"
    out = tome.append_to_body(body, "- second", under="## Log")
    assert out.endswith("- first\n\n- second\n")


def test_append_refuses_empty_text():
    with pytest.raises(tome.VaultError):
        tome.append_to_body("\n# T\n", "   \n  ")


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #

def test_read_prints_the_whole_page(make_vault, run_tome, capsys):
    vault = make_vault()
    target = _scaffold_idea(vault, run_tome)
    capsys.readouterr()

    assert run_tome("--vault", str(vault), "read", "alpha") == 0
    assert capsys.readouterr().out == target.read_text(encoding="utf-8")


def test_read_json_closes_the_read_modify_write_loop(make_vault, run_tome, capsys):
    vault = make_vault()
    target = _scaffold_idea(vault, run_tome)
    capsys.readouterr()

    assert run_tome("--vault", str(vault), "read", "tome/ideas/alpha.md", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["slug"] == "alpha"
    assert payload["path"] == "wiki/tome/ideas/alpha.md"
    assert payload["hash"] == _hash(target)  # the token `tome write` demands back
    assert payload["frontmatter"]["type"] == "idea"
    assert payload["body"].startswith("\n# Alpha")
    assert "type: idea" not in payload["body"]  # frontmatter split out, not duplicated


def test_read_unknown_page_exits_1(make_vault, run_tome, capsys):
    vault = make_vault()
    assert run_tome("--vault", str(vault), "read", "nope") == 1
    assert "no page 'nope'" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #

@needs_git
def test_write_replaces_the_body_and_pushes(tmp_path, run_tome, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _committed(vault, run_tome)
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "write", "alpha",
                     "\n# Alpha\n\nRewritten.\n", "--base-hash", _hash(target))

    assert code == 0
    text = target.read_text(encoding="utf-8")
    assert "Rewritten." in text
    assert "type: idea" in text  # frontmatter untouched
    assert "edit: alpha" in _git(origin, "log", "--oneline").stdout
    assert _hash(target) in capsys.readouterr().out  # the next base hash


@needs_git
def test_write_accepts_a_path_identifier_and_stdin(tmp_path, run_tome, monkeypatch):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _committed(vault, run_tome)
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n# Alpha\n\nFrom stdin.\n"))

    code = run_tome("--vault", str(vault), "write", "wiki/tome/ideas/alpha.md",
                     "--base-hash", _hash(target))

    assert code == 0
    assert "From stdin." in target.read_text(encoding="utf-8")


@needs_git
def test_write_body_file(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _committed(vault, run_tome)
    body_file = tmp_path / "body.md"
    body_file.write_text("\n# Alpha\n\nFrom a file.\n", encoding="utf-8")

    code = run_tome("--vault", str(vault), "write", "alpha",
                     "--body-file", str(body_file), "--base-hash", _hash(target))

    assert code == 0
    assert "From a file." in target.read_text(encoding="utf-8")


@needs_git
def test_write_refuses_a_stale_base_hash(tmp_path, run_tome, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _committed(vault, run_tome)
    original = target.read_text(encoding="utf-8")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "write", "alpha", "\n# Alpha\n\nNope.\n",
                     "--base-hash", "stale")

    assert code == 1
    assert target.read_text(encoding="utf-8") == original  # nothing written
    err = capsys.readouterr().err
    assert _hash(target) in err  # the corrective: here's the hash to retry with
    assert "tome read" in err


@needs_git
def test_write_lint_failure_restores_and_explains_the_fix(tmp_path, run_tome, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _committed(vault, run_tome)
    original = target.read_text(encoding="utf-8")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "write", "alpha",
                     "\n# Alpha\n\nSee [[does-not-exist]].\n", "--base-hash", _hash(target))

    assert code == 1
    assert target.read_text(encoding="utf-8") == original  # restored
    err = capsys.readouterr().err
    assert "BROKEN_LINK" in err
    assert "fix:" in err  # the hint, not just the finding
    assert "edit: alpha" not in _git(origin, "log", "--oneline").stdout
    assert _git(vault, "status", "--porcelain").stdout.strip() == ""


@needs_git
def test_write_no_sync_leaves_the_change_uncommitted(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _committed(vault, run_tome)

    code = run_tome("--vault", str(vault), "write", "alpha", "\n# Alpha\n\nBatched.\n",
                     "--base-hash", _hash(target), "--no-sync")

    assert code == 0
    assert "Batched." in target.read_text(encoding="utf-8")
    assert _git(vault, "status", "--porcelain").stdout.strip() != ""  # left for a later commit


def test_write_rejects_two_input_sources(make_vault, run_tome, capsys, tmp_path):
    vault = make_vault()
    _scaffold_idea(vault, run_tome)
    body_file = tmp_path / "body.md"
    body_file.write_text("x", encoding="utf-8")

    code = run_tome("--vault", str(vault), "write", "alpha", "inline",
                     "--body-file", str(body_file), "--base-hash", "irrelevant")

    assert code == 1
    assert "not both" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# append
# --------------------------------------------------------------------------- #

@needs_git
def test_append_adds_to_the_end_without_a_conflict_token(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _committed(vault, run_tome)

    code = run_tome("--vault", str(vault), "append", "alpha", "One more line.")

    assert code == 0
    text = target.read_text(encoding="utf-8")
    assert text.rstrip("\n").endswith("One more line.")
    assert "TBD." in text  # accretion, not replacement
    assert "append: alpha" in _git(origin, "log", "--oneline").stdout


@needs_git
def test_append_under_a_section(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _committed(vault, run_tome)
    fm_lines, _ = tome.read_page(target)
    tome.write_page(target, fm_lines, "\n# Alpha\n\n## Log\n\n- first\n\n## Tail\n\nEnd.\n")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "sections")
    _git(vault, "push")

    code = run_tome("--vault", str(vault), "append", "alpha", "- second", "--under", "## Log")

    assert code == 0
    text = target.read_text(encoding="utf-8")
    assert text.index("- second") < text.index("## Tail")


@needs_git
def test_append_lint_failure_restores(tmp_path, run_tome, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _committed(vault, run_tome)
    original = target.read_text(encoding="utf-8")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "append", "alpha", "See [[does-not-exist]].")

    assert code == 1
    assert target.read_text(encoding="utf-8") == original
    assert "BROKEN_LINK" in capsys.readouterr().err


def test_append_unknown_page_exits_1(make_vault, run_tome, capsys):
    vault = make_vault()
    assert run_tome("--vault", str(vault), "append", "nope", "text") == 1
    assert "no page 'nope'" in capsys.readouterr().err
