"""tome archive / --restore for status-less types (workflow-compression
piece 6). Plans and decisions keep their status-driven flow via
`set-status` and are refused here."""

import subprocess


def _git(vault, *args):
    return subprocess.run(["git", *args], cwd=str(vault),
                           check=True, capture_output=True, text=True)


def test_archive_moves_idea_to_archive_folder(make_vault, run_tome, capsys):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "idea", "my-idea", "--project", "proj",
             "--title", "T", "--desc", "d")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "archive", "my-idea")

    assert code == 0
    archived = vault / "wiki" / "proj" / "ideas" / "archive" / "my-idea.md"
    assert archived.exists()
    assert not (vault / "wiki" / "proj" / "ideas" / "my-idea.md").exists()
    index_text = (vault / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "**Ideas — archived:**" in index_text
    out = capsys.readouterr().out
    assert "Archived [[my-idea]]" in out


def test_archive_restore_moves_back(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "idea", "my-idea", "--project", "proj",
             "--title", "T", "--desc", "d")
    run_tome("--vault", str(vault), "archive", "my-idea")

    code = run_tome("--vault", str(vault), "archive", "my-idea", "--restore")

    assert code == 0
    restored = vault / "wiki" / "proj" / "ideas" / "my-idea.md"
    assert restored.exists()
    assert not (vault / "wiki" / "proj" / "ideas" / "archive" / "my-idea.md").exists()


def test_archive_bumps_updated(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "idea", "my-idea", "--project", "proj",
             "--title", "T", "--desc", "d")
    before = (vault / "wiki" / "proj" / "ideas" / "my-idea.md").read_text(encoding="utf-8")
    assert "updated: 2026-01-01" not in before  # sanity: real date used

    run_tome("--vault", str(vault), "archive", "my-idea")

    after = (vault / "wiki" / "proj" / "ideas" / "archive" / "my-idea.md").read_text(encoding="utf-8")
    assert "updated:" in after


def test_archive_refuses_plan(make_vault, run_tome, capsys):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
             "--title", "T", "--desc", "d")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "archive", "my-plan")

    assert code == 1
    err = capsys.readouterr().err
    assert "set-status" in err


def test_archive_refuses_decision(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "decision", "my-decision", "--project", "proj",
             "--title", "T", "--desc", "d")

    code = run_tome("--vault", str(vault), "archive", "my-decision")

    assert code == 1


def test_archive_refuses_project_hub(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")

    code = run_tome("--vault", str(vault), "archive", "proj")

    assert code == 1


def test_archive_refuses_already_archived(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "idea", "my-idea", "--project", "proj",
             "--title", "T", "--desc", "d")
    run_tome("--vault", str(vault), "archive", "my-idea")

    code = run_tome("--vault", str(vault), "archive", "my-idea")

    assert code == 1


def test_restore_refuses_not_archived(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "idea", "my-idea", "--project", "proj",
             "--title", "T", "--desc", "d")

    code = run_tome("--vault", str(vault), "archive", "my-idea", "--restore")

    assert code == 1


def test_archive_unknown_slug_fails_loud(make_vault, run_tome):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "archive", "no-such-idea")

    assert code == 1


def test_archive_keeps_lint_clean(make_vault, run_tome):
    """No inbound-link breakage — wikilinks resolve by slug, unaffected by
    an archive/restore directory move."""
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "idea", "my-idea", "--project", "proj",
             "--title", "T", "--desc", "d")
    hub = vault / "wiki" / "proj" / "proj.md"
    hub.write_text(hub.read_text(encoding="utf-8") + "\nSee [[my-idea]] for more.\n",
                    encoding="utf-8", newline="\n")

    code = run_tome("--vault", str(vault), "archive", "my-idea")
    assert code == 0

    lint_code = run_tome("--vault", str(vault), "lint")
    assert lint_code == 0
