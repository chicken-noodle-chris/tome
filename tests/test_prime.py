"""tome prime — two tiers of session orientation (workflow-compression
piece 4): a terse vault pointer (shared with the SessionStart hook) and a
--full write-protocol tier that replaces a skill's read fan-out."""

from tome_cli import cli as tome


def test_prime_terse_prints_vault_pointer(make_vault, run_tome, capsys):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "prime")

    assert code == 0
    out = capsys.readouterr().out
    assert str(vault) in out
    assert "tome sync" in out
    assert "wiki/index.md" in out


def test_prime_terse_matches_module_function(make_vault, run_tome, capsys):
    vault = make_vault()
    capsys.readouterr()

    run_tome("--vault", str(vault), "prime")

    out = capsys.readouterr().out.rstrip("\n")
    assert out == tome.prime_terse_text(vault)


def test_prime_full_without_project_includes_schema_and_index(make_vault, run_tome, capsys):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "prime", "--full")

    assert code == 0
    out = capsys.readouterr().out
    assert "wiki/SCHEMA.md" in out
    assert "wiki/index.md" in out
    assert "Generated file" in out  # from the index preamble


def test_prime_full_omits_task_snapshot_without_backlog(make_vault, run_tome, capsys):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "prime", "--full")

    assert code == 0
    out = capsys.readouterr().out
    assert "backlog/tasks" not in out


def test_prime_full_includes_open_task_snapshot(make_vault, run_tome, make_task, capsys):
    vault = make_vault()
    make_task(vault, 1, "Do the thing", status="In Progress")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "prime", "--full")

    assert code == 0
    out = capsys.readouterr().out
    assert "backlog/tasks (open)" in out
    assert "TASK-1 [In Progress] Do the thing" in out


def test_prime_terse_never_includes_task_snapshot(make_vault, run_tome, make_task, capsys):
    vault = make_vault()
    make_task(vault, 1, "Do the thing")
    capsys.readouterr()

    run_tome("--vault", str(vault), "prime")

    out = capsys.readouterr().out
    assert "backlog/tasks" not in out
    assert "Do the thing" not in out


def test_prime_full_task_snapshot_scoped_to_project(make_vault, run_tome, make_task, capsys):
    vault = make_vault()
    make_task(vault, 1, "Tome task", labels=["project:tome"])
    make_task(vault, 2, "Other task", labels=["project:other"])
    run_tome("--vault", str(vault), "new", "project", "tome",
             "--title", "Tome", "--desc", "d")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "prime", "tome", "--full")

    assert code == 0
    out = capsys.readouterr().out
    assert "TASK-1 [To Do] Tome task" in out
    assert "TASK-2" not in out


def test_prime_full_groups_open_tasks_by_milestone_with_done_total(
        make_vault, run_tome, make_task, capsys):
    vault = make_vault()
    milestones_dir = vault / "backlog" / "milestones"
    milestones_dir.mkdir(parents=True)
    (milestones_dir / "m-0 - epic.md").write_text(
        '---\nid: m-0\ntitle: "epic"\n---\n\n## Description\n', encoding="utf-8", newline="\n")
    make_task(vault, 1, "Shipped piece", milestone="m-0", completed=True)
    make_task(vault, 2, "Open piece", milestone="m-0")
    make_task(vault, 3, "No milestone task")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "prime", "--full")

    assert code == 0
    out = capsys.readouterr().out
    assert "m-0 — epic (1/2 done):" in out
    assert "TASK-2 [To Do] Open piece" in out
    assert "No milestone:" in out
    assert "TASK-3 [To Do] No milestone task" in out
    # a completed task doesn't itself appear as an open line
    assert "Shipped piece" not in out


def test_prime_full_with_project_includes_hub_and_live_plans(make_vault, run_tome, capsys):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "live-plan", "--project", "proj",
             "--title", "Live", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "done-plan", "--project", "proj",
             "--title", "Done", "--desc", "d")
    run_tome("--vault", str(vault), "set-status", "done-plan", "done")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "prime", "proj", "--full")

    assert code == 0
    out = capsys.readouterr().out
    assert "proj/proj.md" in out
    assert "proj/plans/live-plan.md" in out
    assert "# Live" in out
    assert "proj/plans/archive/done-plan.md" not in out
    assert "log.md" in out


def test_prime_project_without_full_is_rejected(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")

    code = run_tome("--vault", str(vault), "prime", "proj")

    assert code == 1


def test_prime_unknown_project_fails_loud(make_vault, run_tome):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "prime", "no-such-project", "--full")

    assert code == 1


def test_log_tail_keeps_whole_entries_only():
    log_text = (
        "# Wiki Log\n\n---\n"
        "\n## [2026-01-01] init | one\n"
        "\n## [2026-01-02] note | two\n"
        "\n## [2026-01-03] note | three\n"
    )

    tail = tome.log_tail(log_text, n=2)

    assert "one" not in tail
    assert "two" in tail
    assert "three" in tail
    assert tail.count("## [") == 2
