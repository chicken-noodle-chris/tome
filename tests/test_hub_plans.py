"""Generated hub plan lists — <!-- tome:plans --> marker regeneration, drift
lint, and the commands that hang off it (new plan, set-status, mv, rm,
describe, index rebuild). workflow-compression piece 2."""

from tome_cli import cli as tome


def _hub_text(vault, project):
    return (vault / "wiki" / project / f"{project}.md").read_text(encoding="utf-8")


def test_new_project_scaffolds_hub_with_markers(make_vault, run_tome):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "new", "project", "proj",
                     "--title", "Proj", "--desc", "d")

    assert code == 0
    text = _hub_text(vault, "proj")
    assert tome.HUB_MARKER_START in text
    assert tome.HUB_MARKER_END in text


def test_new_plan_appears_in_hub_live_list(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")

    code = run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
                     "--title", "My Plan", "--desc", "Does a thing.")

    assert code == 0
    text = _hub_text(vault, "proj")
    assert "**Plans — live:**" in text
    assert "[[my-plan]] — Does a thing." in text
    assert "**Plans — archived:**" not in text


def test_set_status_moves_plan_between_hub_sections(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
             "--title", "My Plan", "--desc", "Does a thing.")

    code = run_tome("--vault", str(vault), "set-status", "my-plan", "done")

    assert code == 0
    text = _hub_text(vault, "proj")
    assert "**Plans — live:**" not in text
    assert "**Plans — archived:**" in text
    assert "[[my-plan]] — Does a thing." in text


def test_describe_updates_hub_entry_text(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
             "--title", "My Plan", "--desc", "Old description.")

    code = run_tome("--vault", str(vault), "describe", "my-plan", "New description.")

    assert code == 0
    text = _hub_text(vault, "proj")
    assert "[[my-plan]] — New description." in text
    assert "Old description." not in text


def test_mv_renames_plan_in_hub(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "old-slug", "--project", "proj",
             "--title", "My Plan", "--desc", "Does a thing.")

    code = run_tome("--vault", str(vault), "mv", "old-slug", "new-slug")

    assert code == 0
    text = _hub_text(vault, "proj")
    assert "[[new-slug]]" in text
    assert "[[old-slug]]" not in text


def test_rm_removes_plan_from_hub(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
             "--title", "My Plan", "--desc", "Does a thing.")

    code = run_tome("--vault", str(vault), "rm", "my-plan")

    assert code == 0
    text = _hub_text(vault, "proj")
    assert "my-plan" not in text
    assert "**Plans" not in text  # no live or archived plans left


def test_hub_prose_outside_markers_is_preserved(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    hub_path = vault / "wiki" / "proj" / "proj.md"
    hub_path.write_text(
        hub_path.read_text(encoding="utf-8") + "\nSome hand-authored prose that must survive.\n",
        encoding="utf-8",
    )

    code = run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
                     "--title", "My Plan", "--desc", "Does a thing.")

    assert code == 0
    text = _hub_text(vault, "proj")
    assert "Some hand-authored prose that must survive." in text
    assert "[[my-plan]]" in text


def test_hub_without_markers_is_never_touched(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    hub_path = vault / "wiki" / "proj" / "proj.md"
    # Strip the markers out — an opted-out hub.
    hub_path.write_text(
        hub_path.read_text(encoding="utf-8").replace(tome.HUB_MARKER_START, "")
        .replace(tome.HUB_MARKER_END, ""),
        encoding="utf-8",
    )
    before = hub_path.read_text(encoding="utf-8")

    code = run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
                     "--title", "My Plan", "--desc", "Does a thing.")

    assert code == 0
    assert hub_path.read_text(encoding="utf-8") == before
    code = run_tome("--vault", str(vault), "lint")
    assert code == 0  # opted-out hub never gates on HUB_DRIFT


def test_hub_regeneration_is_idempotent(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "plan-a", "--project", "proj",
             "--title", "Plan A", "--desc", "A.")
    run_tome("--vault", str(vault), "new", "plan", "plan-b", "--project", "proj",
             "--title", "Plan B", "--desc", "B.")
    run_tome("--vault", str(vault), "set-status", "plan-a", "done")

    text1 = _hub_text(vault, "proj")
    code = run_tome("--vault", str(vault), "index", "rebuild")
    text2 = _hub_text(vault, "proj")

    assert code == 0
    assert text1 == text2


def test_lint_detects_hub_drift(make_vault, run_tome, capsys):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
             "--title", "My Plan", "--desc", "Does a thing.")
    capsys.readouterr()

    hub_path = vault / "wiki" / "proj" / "proj.md"
    hub_path.write_text(
        hub_path.read_text(encoding="utf-8").replace("Does a thing.", "Stale text."),
        encoding="utf-8",
    )

    code = run_tome("--vault", str(vault), "lint")

    assert code == 1
    assert "HUB_DRIFT" in capsys.readouterr().out


def test_index_rebuild_regenerates_all_hubs(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
             "--title", "My Plan", "--desc", "Does a thing.")
    hub_path = vault / "wiki" / "proj" / "proj.md"
    hub_path.write_text(
        hub_path.read_text(encoding="utf-8").replace("Does a thing.", "Stale text."),
        encoding="utf-8",
    )

    code = run_tome("--vault", str(vault), "index", "rebuild")

    assert code == 0
    text = _hub_text(vault, "proj")
    assert "Does a thing." in text
    assert "Stale text." not in text
