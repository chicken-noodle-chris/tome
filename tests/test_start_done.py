"""tome start / tome done — the pickup-task skill's bundled start/close
rituals (workflow-compression piece 3). Tests fake out backlog.md itself
(monkeypatching tome.run_backlog) rather than shelling out to the real npx
CLI — slow, network-dependent, and not what these tests are checking."""

import re
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


@pytest.fixture
def fake_backlog(monkeypatch):
    """Monkeypatch tome.run_backlog with a stand-in that mutates the on-disk
    task fixture the way the real backlog.md CLI would for exactly the
    invocation shapes cmd_start/cmd_done issue. Returns the list of argv
    calls made, for assertions."""
    from tome_cli import cli as tome

    calls = []

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _find(tasks_dir, task_id):
        for p in tasks_dir.glob("*.md"):
            if re.search(rf"^id: TASK-{task_id}$", p.read_text(encoding="utf-8"), re.MULTILINE):
                return p
        return None

    def _run(vault_root, argv, capture=False):
        calls.append(list(argv))
        tasks_dir = vault_root / "backlog" / "tasks"
        completed_dir = vault_root / "backlog" / "completed"

        if argv[:2] == ["task", "edit"]:
            task_id = argv[2]
            path = _find(tasks_dir, task_id)
            if path is None:
                return Result(1, stderr=f"no such task {task_id}")
            text = path.read_text(encoding="utf-8")
            rest = argv[3:]
            refs = None
            i = 0
            while i < len(rest):
                tok = rest[i]
                if tok == "-s":
                    text = re.sub(r"^status:.*$", f"status: {rest[i + 1]}",
                                   text, count=1, flags=re.MULTILINE)
                    i += 2
                elif tok == "-a":
                    text = re.sub(r"^assignee:.*$", f"assignee:\n  - '{rest[i + 1]}'",
                                   text, count=1, flags=re.MULTILINE)
                    i += 2
                elif tok == "--check-ac":
                    idx = rest[i + 1]
                    text = re.sub(rf"- \[ \] #{idx}\b", f"- [x] #{idx}", text, count=1)
                    i += 2
                elif tok == "--final-summary":
                    text = text.rstrip("\n") + f"\n\n## Final Summary\n\n{rest[i + 1]}\n"
                    i += 2
                elif tok == "--ref":
                    refs = (refs or []) + [rest[i + 1]]
                    i += 2
                else:
                    i += 1
            if refs is not None:
                new_block = "references:\n" + "\n".join(f"  - {r}" for r in refs)
                text = re.sub(r"references:(\n {2}- .*)*", new_block, text, count=1)
            path.write_text(text, encoding="utf-8", newline="\n")
            return Result()

        if argv[:2] == ["task", "complete"]:
            task_id = argv[2]
            path = _find(tasks_dir, task_id)
            if path is None:
                return Result(1, stderr=f"no such task {task_id}")
            completed_dir.mkdir(parents=True, exist_ok=True)
            path.rename(completed_dir / path.name)
            return Result()

        return Result()  # e.g. ["task", "<id>", "--plain"] — a no-op read in tests

    monkeypatch.setattr(tome, "run_backlog", _run)
    return calls


def _make_plan(vault, run_tome, slug, status="proposed"):
    code = run_tome("--vault", str(vault), "new", "project", "proj",
                     "--title", "Proj", "--desc", "d")
    assert code == 0
    code = run_tome("--vault", str(vault), "new", "plan", slug, "--project", "proj",
                     "--title", "T", "--desc", "d")
    assert code == 0
    if status != "proposed":
        code = run_tome("--vault", str(vault), "set-status", slug, status)
        assert code == 0
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "seed plan")
    _git(vault, "push")
    return vault / "wiki" / "proj" / "plans" / f"{slug}.md"


# --------------------------------------------------------------------------- #
# tome start
# --------------------------------------------------------------------------- #

def test_start_by_slug_sets_active_and_task_in_progress(tmp_path, run_tome, capsys,
                                                          fake_backlog, make_task):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _make_plan(vault, run_tome, "my-plan")
    make_task(vault, 1, "My plan task", refs=["wiki/proj/plans/my-plan.md"])
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "seed task")
    _git(vault, "push")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "start", "my-plan")

    assert code == 0
    plan_text = (vault / "wiki" / "proj" / "plans" / "my-plan.md").read_text(encoding="utf-8")
    assert "status: active" in plan_text
    task_text = next((vault / "backlog" / "tasks").glob("*.md")).read_text(encoding="utf-8")
    assert "status: In Progress" in task_text
    assert "'@me'" in task_text
    log_text = (vault / "wiki" / "log.md").read_text(encoding="utf-8")
    assert "work-started | my-plan" in log_text
    log = _git(origin, "log", "--oneline")
    assert "start: my-plan" in log.stdout
    out = capsys.readouterr().out
    assert "status -> active" in out
    assert "In Progress" in out


def test_start_by_task_id_resolves_linked_plan(tmp_path, run_tome, fake_backlog, make_task):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _make_plan(vault, run_tome, "my-plan")
    make_task(vault, 7, "My plan task", refs=["wiki/proj/plans/my-plan.md"])
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "seed task")
    _git(vault, "push")

    code = run_tome("--vault", str(vault), "start", "task-7")

    assert code == 0
    plan_text = (vault / "wiki" / "proj" / "plans" / "my-plan.md").read_text(encoding="utf-8")
    assert "status: active" in plan_text
    task_text = next((vault / "backlog" / "tasks").glob("*.md")).read_text(encoding="utf-8")
    assert "status: In Progress" in task_text


def test_start_plan_without_task_is_normal(tmp_path, run_tome, fake_backlog):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _make_plan(vault, run_tome, "solo-plan")

    code = run_tome("--vault", str(vault), "start", "solo-plan")

    assert code == 0
    plan_text = (vault / "wiki" / "proj" / "plans" / "solo-plan.md").read_text(encoding="utf-8")
    assert "status: active" in plan_text
    assert fake_backlog == []  # no backlog calls at all
    log_text = (vault / "wiki" / "log.md").read_text(encoding="utf-8")
    assert "work-started | solo-plan" in log_text


def test_start_task_without_plan_is_normal(tmp_path, run_tome, fake_backlog, make_task):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    make_task(vault, 9, "Orphan task")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "seed task")
    _git(vault, "push")

    code = run_tome("--vault", str(vault), "start", "task-9")

    assert code == 0
    task_text = next((vault / "backlog" / "tasks").glob("*.md")).read_text(encoding="utf-8")
    assert "status: In Progress" in task_text
    log_text = (vault / "wiki" / "log.md").read_text(encoding="utf-8")
    assert "work-started | task-9" in log_text


def test_start_unknown_entity_fails_loud(tmp_path, run_tome, fake_backlog):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    code = run_tome("--vault", str(vault), "start", "no-such-thing")

    assert code == 1


def test_start_no_sync_leaves_tree_dirty(tmp_path, run_tome, fake_backlog):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _make_plan(vault, run_tome, "my-plan")

    code = run_tome("--vault", str(vault), "start", "my-plan", "--no-sync")

    assert code == 0
    status = _git(vault, "status", "--porcelain")
    assert status.stdout.strip()  # still dirty — never synced
    log = _git(origin, "log", "--oneline")
    assert "start: my-plan" not in log.stdout


def test_start_non_plan_type_rejected(tmp_path, run_tome, fake_backlog):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "idea", "an-idea", "--project", "proj",
             "--title", "T", "--desc", "d")

    code = run_tome("--vault", str(vault), "start", "an-idea")

    assert code == 1


# --------------------------------------------------------------------------- #
# tome done
# --------------------------------------------------------------------------- #

def test_done_closes_task_and_archives_plan(tmp_path, run_tome, capsys, fake_backlog, make_task):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _make_plan(vault, run_tome, "my-plan", status="active")
    make_task(vault, 1, "My plan task", status="In Progress", assignee=["@me"],
              refs=["wiki/proj/plans/my-plan.md"])
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "seed task")
    _git(vault, "push")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "done", "my-plan", "--summary", "Shipped it.")

    assert code == 0
    archived = vault / "wiki" / "proj" / "plans" / "archive" / "my-plan.md"
    assert archived.exists()
    assert "status: done" in archived.read_text(encoding="utf-8")
    assert not (vault / "backlog" / "tasks").exists() or \
        not list((vault / "backlog" / "tasks").glob("*.md"))
    completed = next((vault / "backlog" / "completed").glob("*.md"))
    completed_text = completed.read_text(encoding="utf-8")
    assert "status: Done" in completed_text
    assert "- [x] #1 one" in completed_text
    assert "- [x] #2 two" in completed_text
    assert "Shipped it." in completed_text
    assert "wiki/proj/plans/archive/my-plan.md" in completed_text
    log_text = (vault / "wiki" / "log.md").read_text(encoding="utf-8")
    assert "done | my-plan: Shipped it." in log_text
    log = _git(origin, "log", "--oneline")
    assert "done: my-plan" in log.stdout
    out = capsys.readouterr().out
    assert "status -> done" in out
    assert "Completed TASK-1" in out
    # Regression guard (task-47 piece 7): the archive move's old-path half
    # must be staged too, or this is left dirty.
    status = _git(vault, "status", "--porcelain")
    assert status.stdout.strip() == ""


def test_done_as_superseded(tmp_path, run_tome, fake_backlog):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _make_plan(vault, run_tome, "old-plan", status="active")

    code = run_tome("--vault", str(vault), "done", "old-plan", "--as", "superseded")

    assert code == 0
    archived = vault / "wiki" / "proj" / "plans" / "archive" / "old-plan.md"
    assert archived.exists()
    assert "status: superseded" in archived.read_text(encoding="utf-8")


def test_done_no_check_ac_leaves_criteria_unchecked(tmp_path, run_tome, fake_backlog, make_task):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _make_plan(vault, run_tome, "my-plan", status="active")
    make_task(vault, 1, "My plan task", refs=["wiki/proj/plans/my-plan.md"])
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "seed task")
    _git(vault, "push")

    code = run_tome("--vault", str(vault), "done", "my-plan", "--no-check-ac")

    assert code == 0
    completed = next((vault / "backlog" / "completed").glob("*.md"))
    completed_text = completed.read_text(encoding="utf-8")
    assert "status: Done" in completed_text
    assert "- [ ] #1 one" in completed_text  # left unchecked


def test_done_plan_without_task(tmp_path, run_tome, fake_backlog):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _make_plan(vault, run_tome, "solo-plan", status="active")

    code = run_tome("--vault", str(vault), "done", "solo-plan")

    assert code == 0
    archived = vault / "wiki" / "proj" / "plans" / "archive" / "solo-plan.md"
    assert archived.exists()
    assert fake_backlog == []
    log_text = (vault / "wiki" / "log.md").read_text(encoding="utf-8")
    assert "done | solo-plan" in log_text


def test_done_non_plan_type_rejected(tmp_path, run_tome, fake_backlog):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "idea", "an-idea", "--project", "proj",
             "--title", "T", "--desc", "d")

    code = run_tome("--vault", str(vault), "done", "an-idea")

    assert code == 1


def test_done_invalid_as_status_rejected(tmp_path, run_tome, fake_backlog):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _make_plan(vault, run_tome, "my-plan", status="active")

    code = run_tome("--vault", str(vault), "done", "my-plan", "--as", "not-a-status")

    assert code == 1
