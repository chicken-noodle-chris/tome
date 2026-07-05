"""tome search / tome rm subcommands: vault-resolved BM25 search, and
delete-with-inbound-link-reporting."""

from tome_cli import cli as tome


def _new_project(run_tome, vault, name="proj", title="Proj"):
    code = run_tome("--vault", str(vault), "new", "project", name,
                     "--title", title, "--desc", "a project")
    assert code == 0
    return vault / "wiki" / name / f"{name}.md"


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #

def test_search_resolves_vault_by_walk_up(make_vault, run_tome, make_page, monkeypatch, capsys):
    vault = make_vault()
    _new_project(run_tome, vault)
    make_page(vault, "proj/notes/target.md", type="concept", title="Target",
              body="\nThis page is all about diffusion model training stability.\n")
    nested = vault / "wiki" / "proj" / "notes"
    monkeypatch.chdir(nested)
    monkeypatch.delenv("VAULT_ROOT", raising=False)

    code = run_tome("search", "diffusion training", "--top", "5")

    assert code == 0
    out = capsys.readouterr().out
    assert "Target" in out


def test_search_type_and_tag_filters(make_vault, run_tome, make_page, capsys):
    vault = make_vault()
    _new_project(run_tome, vault)
    make_page(vault, "proj/notes/keep.md", type="concept", title="Keep",
              tags=["proj", "reference"], body="\nabout widgets and gadgets.\n")
    make_page(vault, "proj/notes/drop.md", type="idea", title="Drop",
              tags=["proj", "idea"], body="\nalso about widgets and gadgets.\n")

    code = run_tome("--vault", str(vault), "search", "widgets", "--type", "concept")

    assert code == 0
    out = capsys.readouterr().out
    assert "Keep" in out
    assert "Drop" not in out


def test_search_backlinks_through_subcommand(make_vault, run_tome, make_page, capsys):
    vault = make_vault()
    _new_project(run_tome, vault)
    make_page(vault, "proj/notes/target.md", type="concept", title="Target")
    make_page(vault, "proj/notes/linker.md", type="concept", title="Linker",
              body="\nSee [[target]].\n")

    code = run_tome("--vault", str(vault), "search", "", "--backlinks", "target")

    assert code == 0
    out = capsys.readouterr().out
    assert "Linker" in out


def test_search_no_pages_gives_sensible_message(make_vault, run_tome, capsys):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "search", "anything")

    assert code == 0
    assert "No wiki pages found" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# rm
# --------------------------------------------------------------------------- #

def test_rm_happy_path_deletes_and_rebuilds_index(make_vault, run_tome, make_page):
    vault = make_vault()
    _new_project(run_tome, vault)
    path = make_page(vault, "proj/notes/scratch.md", type="concept", title="Scratch")

    code = run_tome("--vault", str(vault), "rm", "scratch")

    assert code == 0
    assert not path.exists()
    index_text = (vault / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "scratch" not in index_text


def test_rm_refuses_with_inbound_links(make_vault, run_tome, make_page, capsys):
    vault = make_vault()
    _new_project(run_tome, vault)
    path = make_page(vault, "proj/notes/target.md", type="concept", title="Target")
    make_page(vault, "proj/notes/linker.md", type="concept", title="Linker",
              body="\nSee [[target]] for detail.\n")

    code = run_tome("--vault", str(vault), "rm", "target")

    assert code == 1
    assert path.exists()
    err = capsys.readouterr().err
    assert "linker.md" in err
    assert "refusing to delete" in err


def test_rm_force_deletes_and_reports_broken_links(make_vault, run_tome, make_page, capsys):
    vault = make_vault()
    _new_project(run_tome, vault)
    path = make_page(vault, "proj/notes/target.md", type="concept", title="Target")
    make_page(vault, "proj/notes/linker.md", type="concept", title="Linker",
              body="\nSee [[target]] for detail.\n")

    code = run_tome("--vault", str(vault), "rm", "target", "--force")

    assert code == 0
    assert not path.exists()
    err = capsys.readouterr().err
    assert "linker.md" in err


def test_rm_refuses_project_hub(make_vault, run_tome, capsys):
    vault = make_vault()
    _new_project(run_tome, vault, "myproj")

    code = run_tome("--vault", str(vault), "rm", "myproj")

    assert code == 1
    assert "project hub" in capsys.readouterr().err


def test_rm_unknown_slug_fails_loud(make_vault, run_tome, capsys):
    vault = make_vault()
    _new_project(run_tome, vault)

    code = run_tome("--vault", str(vault), "rm", "ghost")

    assert code == 1
    assert "no page with slug" in capsys.readouterr().err
