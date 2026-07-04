"""Lifecycle commands: new, describe, set-status, mv."""

import pytest

import tome


def _new_project(run_tome, vault, name="proj", title="Proj"):
    code = run_tome("--vault", str(vault), "new", "project", name,
                     "--title", title, "--desc", "a project")
    assert code == 0
    return vault / "wiki" / name / f"{name}.md"


# --------------------------------------------------------------------------- #
# new
# --------------------------------------------------------------------------- #

def test_new_project_lands_at_hub_path(make_vault, run_tome):
    vault = make_vault()
    path = _new_project(run_tome, vault, "myproj", "My Proj")
    assert path.is_file()
    assert 'title: "My Proj"' in path.read_text(encoding="utf-8")


def test_new_non_project_lands_at_folders_mapped_path(make_vault, run_tome):
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")

    code = run_tome("--vault", str(vault), "new", "plan", "p1",
                     "--project", "myproj", "--title", "P1", "--desc", "d")

    assert code == 0
    assert (vault / "wiki" / "myproj" / "plans" / "p1.md").is_file()


def test_new_rejects_bad_type(make_vault, run_tome, capsys):
    vault = make_vault()
    code = run_tome("--vault", str(vault), "new", "not-a-type", "x",
                     "--project", "p", "--title", "T", "--desc", "d")
    assert code == 1
    assert "not in" in capsys.readouterr().err


def test_new_rejects_missing_project_for_non_project_type(make_vault, run_tome, capsys):
    vault = make_vault()
    code = run_tome("--vault", str(vault), "new", "plan", "p1",
                     "--title", "T", "--desc", "d")
    assert code == 1
    assert "--project is required" in capsys.readouterr().err


def test_new_rejects_nonexistent_project_dir(make_vault, run_tome, capsys):
    vault = make_vault()
    code = run_tome("--vault", str(vault), "new", "plan", "p1",
                     "--project", "ghost", "--title", "T", "--desc", "d")
    assert code == 1
    assert "no such project" in capsys.readouterr().err


def test_new_rejects_duplicate_slug(make_vault, run_tome, capsys):
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")
    run_tome("--vault", str(vault), "new", "plan", "p1",
             "--project", "myproj", "--title", "T", "--desc", "d")

    code = run_tome("--vault", str(vault), "new", "plan", "p1",
                     "--project", "myproj", "--title", "T2", "--desc", "d2")

    assert code == 1
    assert "already exists" in capsys.readouterr().err


def test_new_rejects_non_kebab_slug(make_vault, run_tome, capsys):
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")
    code = run_tome("--vault", str(vault), "new", "plan", "Not_Kebab",
                     "--project", "myproj", "--title", "T", "--desc", "d")
    assert code == 1
    assert "kebab-case" in capsys.readouterr().err


def test_new_rejects_quote_in_title(make_vault, run_tome, capsys):
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")
    code = run_tome("--vault", str(vault), "new", "plan", "p1",
                     "--project", "myproj", '--title', 'Bad "Title"', "--desc", "d")
    assert code == 1
    assert 'literal "' in capsys.readouterr().err


def test_new_rejects_quote_in_desc(make_vault, run_tome, capsys):
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")
    code = run_tome("--vault", str(vault), "new", "plan", "p1",
                     "--project", "myproj", "--title", "T", "--desc", 'bad "desc"')
    assert code == 1
    assert 'literal "' in capsys.readouterr().err


def test_new_rejects_desc_over_cap(make_vault, run_tome, capsys):
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")
    code = run_tome("--vault", str(vault), "new", "plan", "p1",
                     "--project", "myproj", "--title", "T", "--desc", "x" * 141)
    assert code == 1
    assert "cap 140" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# describe
# --------------------------------------------------------------------------- #

def test_describe_updates_description_and_regenerates_index(make_vault, run_tome):
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")
    run_tome("--vault", str(vault), "new", "plan", "p1",
             "--project", "myproj", "--title", "P1", "--desc", "old desc")

    code = run_tome("--vault", str(vault), "describe", "p1", "new one-liner")

    assert code == 0
    page_text = (vault / "wiki" / "myproj" / "plans" / "p1.md").read_text(encoding="utf-8")
    assert 'description: "new one-liner"' in page_text
    assert f"updated: {tome.today()}" in page_text
    index_text = (vault / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "new one-liner" in index_text


# --------------------------------------------------------------------------- #
# set-status
# --------------------------------------------------------------------------- #

def test_set_status_plan_archives_on_terminal_and_restores_on_live(make_vault, run_tome):
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")
    run_tome("--vault", str(vault), "new", "plan", "p1",
             "--project", "myproj", "--title", "P1", "--desc", "d")
    live_path = vault / "wiki" / "myproj" / "plans" / "p1.md"
    archive_path = vault / "wiki" / "myproj" / "plans" / "archive" / "p1.md"

    code = run_tome("--vault", str(vault), "set-status", "p1", "active")
    assert code == 0
    assert live_path.is_file()
    assert "status: active" in live_path.read_text(encoding="utf-8")

    code = run_tome("--vault", str(vault), "set-status", "p1", "done")
    assert code == 0
    assert archive_path.is_file()
    assert not live_path.exists()
    assert "status: done" in archive_path.read_text(encoding="utf-8")

    code = run_tome("--vault", str(vault), "set-status", "p1", "active")
    assert code == 0
    assert live_path.is_file()
    assert not archive_path.exists()


def test_set_status_decision_accepts_only_proposed_or_current(make_vault, run_tome, capsys):
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")
    run_tome("--vault", str(vault), "new", "decision", "d1",
             "--project", "myproj", "--title", "D1", "--desc", "d")

    code = run_tome("--vault", str(vault), "set-status", "d1", "current")
    assert code == 0
    assert "status: current" in (vault / "wiki" / "myproj" / "decisions" / "d1.md").read_text(encoding="utf-8")

    code = run_tome("--vault", str(vault), "set-status", "d1", "done")
    assert code == 1
    assert "decision status must be one of" in capsys.readouterr().err


def test_set_status_rejects_non_status_type(make_vault, run_tome, capsys):
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")
    run_tome("--vault", str(vault), "new", "idea", "i1",
             "--project", "myproj", "--title", "I1", "--desc", "d")

    code = run_tome("--vault", str(vault), "set-status", "i1", "active")

    assert code == 1
    assert "does not carry a status" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# mv
# --------------------------------------------------------------------------- #

def test_mv_rewrites_bare_and_alias_links(make_vault, run_tome, make_page):
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")
    make_page(vault, "myproj/notes/target.md", type="concept", title="Target")
    make_page(vault, "myproj/notes/linker.md", type="concept", title="Linker",
              body="\nSee [[target]] and [[target|the target]].\n")

    code = run_tome("--vault", str(vault), "mv", "target", "renamed")

    assert code == 0
    assert (vault / "wiki" / "myproj" / "notes" / "renamed.md").is_file()
    linker_text = (vault / "wiki" / "myproj" / "notes" / "linker.md").read_text(encoding="utf-8")
    assert "[[renamed]]" in linker_text
    assert "[[renamed|the target]]" in linker_text
    assert "[[target]]" not in linker_text


def test_mv_does_not_corrupt_slug_that_prefixes_another(make_vault, run_tome, make_page):
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")
    make_page(vault, "myproj/notes/vault.md", type="concept", title="Vault")
    make_page(vault, "myproj/notes/vault-cli.md", type="concept", title="Vault CLI")
    make_page(vault, "myproj/notes/linker.md", type="concept", title="Linker",
              body="\nSee [[vault]] and [[vault-cli]].\n")

    code = run_tome("--vault", str(vault), "mv", "vault", "vaultx")

    assert code == 0
    linker_text = (vault / "wiki" / "myproj" / "notes" / "linker.md").read_text(encoding="utf-8")
    assert "[[vaultx]]" in linker_text
    assert "[[vault-cli]]" in linker_text


def test_mv_leaves_code_spans_untouched(make_vault, run_tome, make_page):
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")
    make_page(vault, "myproj/notes/target.md", type="concept", title="Target")
    make_page(vault, "myproj/notes/linker.md", type="concept", title="Linker",
              body="\nInline `[[target]]` and:\n\n```\n[[target]]\n```\n\n[[target]]\n")

    code = run_tome("--vault", str(vault), "mv", "target", "renamed")

    assert code == 0
    linker_text = (vault / "wiki" / "myproj" / "notes" / "linker.md").read_text(encoding="utf-8")
    assert "`[[target]]`" in linker_text
    assert "```\n[[target]]\n```" in linker_text
    assert "\n[[renamed]]\n" in linker_text


def test_mv_refuses_project_hub(make_vault, run_tome, capsys):
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")

    code = run_tome("--vault", str(vault), "mv", "myproj", "otherproj")

    assert code == 1
    assert "project hub" in capsys.readouterr().err


@pytest.mark.xfail(strict=True, reason="self-link rewrite, fixed in cli-hardening-batch")
def test_mv_rewrites_pages_own_self_link(make_vault, run_tome, make_page):
    """Known bug (scripts/tome.py:552): the rewrite loop skips the page being
    renamed itself, so a self-referential [[old-slug]] in its own body is
    left stale. Correct behavior would rewrite it too."""
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")
    make_page(vault, "myproj/notes/selfy.md", type="concept", title="Selfy",
              body="\nThis page is called [[selfy]].\n")

    code = run_tome("--vault", str(vault), "mv", "selfy", "renamed")

    assert code == 0
    new_text = (vault / "wiki" / "myproj" / "notes" / "renamed.md").read_text(encoding="utf-8")
    assert "[[renamed]]" in new_text
