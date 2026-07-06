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
