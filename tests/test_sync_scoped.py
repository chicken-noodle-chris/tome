"""--sync on write commands (scoped commits) and `tome sync <entity>...`
(entity-scoped commits) — the shared sync_core plumbing from workflow-compression
piece 1."""

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


def test_sync_flag_commits_only_touched_files(tmp_path, run_tome, capsys):
    """--sync on `describe` must not sweep in an unrelated finished-but-
    unsynced write from another agent — the whole point of scoped commits."""
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add proj")
    _git(vault, "push")
    capsys.readouterr()

    # Another agent's finished write, left uncommitted (own index rebuild
    # included, so it's lint-clean — only ride-along risk, no gating error).
    run_tome("--vault", str(vault), "new", "idea", "ride-along", "--project", "proj",
             "--title", "Ride along", "--desc", "d")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "describe", "proj", "New one-liner.", "--sync")

    assert code == 0
    out = capsys.readouterr().out
    assert "synced." in out
    assert "left uncommitted:" in out
    status = _git(vault, "status", "--porcelain")
    assert "ideas" in status.stdout  # still dirty, never committed
    log = _git(origin, "log", "--oneline")
    assert "describe: proj" in log.stdout


def test_sync_flag_auto_generates_message(tmp_path, run_tome, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    code = run_tome("--vault", str(vault), "inbox", "a quick note", "--sync")

    assert code == 0
    log = _git(origin, "log", "--oneline")
    assert "inbox:" in log.stdout


def test_sync_flag_honors_explicit_message(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    code = run_tome("--vault", str(vault), "log", "note", "headline",
                     "--sync", "-m", "custom commit message")

    assert code == 0
    log = _git(origin, "log", "--oneline")
    assert "custom commit message" in log.stdout


def test_sync_flag_gates_on_lint_errors(tmp_path, run_tome, capsys, make_page):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    make_page(vault, "scratch/notes/broken.md", type="concept", tags=["project"],
              body="\nSee [[does-not-exist]].\n")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add broken page directly")
    _git(vault, "push")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "log", "note", "headline", "--sync")

    assert code == 1
    assert "refusing to sync" in capsys.readouterr().err
    status = _git(vault, "status", "--porcelain")
    assert status.stdout.strip()  # nothing got committed


def test_sync_entity_scopes_to_page_cluster(tmp_path, run_tome, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
              "--title", "My Plan", "--desc", "d")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "scaffold")
    _git(vault, "push")
    capsys.readouterr()

    # Direct hand edit to the plan body.
    plan_path = vault / "wiki" / "proj" / "plans" / "my-plan.md"
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\nMore detail.\n",
                          encoding="utf-8")
    # An unrelated finished-but-unsynced write elsewhere, left uncommitted.
    run_tome("--vault", str(vault), "new", "idea", "elsewhere", "--project", "proj",
             "--title", "Elsewhere", "--desc", "d")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "sync", "my-plan", "-m", "expand my-plan")

    assert code == 0
    out = capsys.readouterr().out
    assert "synced." in out
    assert "left uncommitted:" in out
    status = _git(vault, "status", "--porcelain")
    assert "ideas" in status.stdout
    log = _git(origin, "log", "--oneline")
    assert "expand my-plan" in log.stdout


def test_sync_entity_unknown_slug_fails_loud(tmp_path, run_tome, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    code = run_tome("--vault", str(vault), "sync", "no-such-slug", "-m", "x")

    assert code == 1
    assert "no page with slug" in capsys.readouterr().err


def test_sync_entity_resolves_by_task_id(tmp_path, run_tome, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
              "--title", "My Plan", "--desc", "d")

    tasks_dir = vault / "backlog" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    task_path = tasks_dir / "task-1 - My-Plan.md"
    task_path.write_text(
        "---\n"
        "id: TASK-1\n"
        "title: My Plan\n"
        "status: To Do\n"
        "references:\n"
        "  - wiki/proj/plans/my-plan.md\n"
        "---\n\n## Description\n\nDo it.\n",
        encoding="utf-8", newline="\n",
    )
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "scaffold + task")
    _git(vault, "push")
    capsys.readouterr()

    plan_path = vault / "wiki" / "proj" / "plans" / "my-plan.md"
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\nMore detail.\n",
                          encoding="utf-8")

    code = run_tome("--vault", str(vault), "sync", "task-1", "-m", "expand via task")

    assert code == 0
    log = _git(origin, "log", "--oneline")
    assert "expand via task" in log.stdout
    show = _git(origin, "show", "--stat", "HEAD")
    assert "my-plan.md" in show.stdout


def test_sync_entity_resolves_completed_task_id(tmp_path, run_tome, capsys):
    """A task already closed and moved to backlog/completed/ (task-57 AC #4)
    must still resolve — e.g. `tome sync` invoked against a task id after
    `tome done` already shipped it, or a task closed by hand."""
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
              "--title", "My Plan", "--desc", "d")

    completed_dir = vault / "backlog" / "completed"
    completed_dir.mkdir(parents=True, exist_ok=True)
    task_path = completed_dir / "task-1 - My-Plan.md"
    task_path.write_text(
        "---\n"
        "id: TASK-1\n"
        "title: My Plan\n"
        "status: Done\n"
        "references:\n"
        "  - wiki/proj/plans/my-plan.md\n"
        "---\n\n## Description\n\nDo it.\n",
        encoding="utf-8", newline="\n",
    )
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "scaffold + completed task")
    _git(vault, "push")
    capsys.readouterr()

    plan_path = vault / "wiki" / "proj" / "plans" / "my-plan.md"
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\nMore detail.\n",
                          encoding="utf-8")

    code = run_tome("--vault", str(vault), "sync", "task-1", "-m", "expand via completed task")

    assert code == 0
    log = _git(origin, "log", "--oneline")
    assert "expand via completed task" in log.stdout
    show = _git(origin, "show", "--stat", "HEAD")
    assert "my-plan.md" in show.stdout


def test_sync_entity_unknown_task_id_fails_loud(tmp_path, run_tome, capsys):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    code = run_tome("--vault", str(vault), "sync", "task-999", "-m", "x")

    assert code == 1
    assert "no backlog task with id" in capsys.readouterr().err


def test_set_status_sync_stages_the_archive_move_fully(tmp_path, run_tome):
    """A regression guard: set-status's --sync must scope in the OLD path
    too when the status change also moves the file (plans/ <-> archive/),
    or the rename's delete-half is left unstaged (found by manually dry-
    running tome done against the real vault, task-47 piece 7)."""
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
             "--title", "T", "--desc", "d")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "scaffold")
    _git(vault, "push")

    code = run_tome("--vault", str(vault), "set-status", "my-plan", "done", "--sync")

    assert code == 0
    status = _git(vault, "status", "--porcelain")
    assert status.stdout.strip() == ""
    show = _git(origin, "show", "--stat", "HEAD")
    assert "my-plan.md" in show.stdout
    assert (vault / "wiki" / "proj" / "plans" / "archive" / "my-plan.md").exists()
    assert not (vault / "wiki" / "proj" / "plans" / "my-plan.md").exists()


def test_mv_sync_stages_the_rename_fully(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "idea", "old-slug", "--project", "proj",
             "--title", "T", "--desc", "d")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "scaffold")
    _git(vault, "push")

    code = run_tome("--vault", str(vault), "mv", "old-slug", "new-slug", "--sync")

    assert code == 0
    status = _git(vault, "status", "--porcelain")
    assert status.stdout.strip() == ""
