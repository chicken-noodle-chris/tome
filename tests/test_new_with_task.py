"""tome new plan --with-task — scaffold a plan and its linked Backlog task
in one command (workflow-compression piece 5). Fakes out backlog.md's
`task create` rather than shelling out to the real npx CLI."""

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
def fake_backlog_create(monkeypatch):
    """Fakes just `task create`, the only backlog.md invocation --with-task
    issues: parses the argv the same way the real CLI would consume it and
    writes a plausible task file, printing the `File: <path>` line cmd_new
    parses back out to scope --sync."""
    from tome_cli import cli as tome

    calls = []
    counter = {"n": 0}

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _run(vault_root, argv, capture=False):
        calls.append(list(argv))
        assert argv[:2] == ["task", "create"], f"unexpected backlog call: {argv}"
        counter["n"] += 1
        task_id = counter["n"]
        title = argv[2]
        rest = argv[3:]
        desc, labels, refs, acs, priority = "", [], [], [], None
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok == "-d":
                desc = rest[i + 1]; i += 2
            elif tok == "-l":
                labels.append(rest[i + 1]); i += 2
            elif tok == "--ref":
                refs.append(rest[i + 1]); i += 2
            elif tok == "--priority":
                priority = rest[i + 1]; i += 2
            elif tok == "--ac":
                acs.append(rest[i + 1]); i += 2
            else:
                i += 1

        tasks_dir = vault_root / "backlog" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        path = tasks_dir / f"task-{task_id} - {title.replace(' ', '-')}.md"
        refs_block = ("references: []" if not refs
                      else "references:\n" + "\n".join(f"  - {r}" for r in refs))
        labels_block = ("labels: []" if not labels
                        else "labels:\n" + "\n".join(f"  - {l}" for l in labels))
        ac_block = ""
        if acs:
            ac_lines = "\n".join(f"- [ ] #{n} {a}" for n, a in enumerate(acs, start=1))
            ac_block = f"\n## Acceptance Criteria\n<!-- AC:BEGIN -->\n{ac_lines}\n<!-- AC:END -->\n"
        text = (
            "---\n"
            f"id: TASK-{task_id}\n"
            f"title: {title}\n"
            "status: To Do\n"
            "assignee: []\n"
            "created_date: '2026-01-01 00:00'\n"
            "updated_date: '2026-01-01 00:00'\n"
            f"{labels_block}\n"
            "dependencies: []\n"
            f"{refs_block}\n"
            f"priority: {priority or 'medium'}\n"
            "ordinal: 1000\n"
            "---\n"
            f"\n## Description\n\n{desc}\n"
            f"{ac_block}"
        )
        path.write_text(text, encoding="utf-8", newline="\n")
        return Result(stdout=f"File: {path}\n")

    monkeypatch.setattr(tome, "run_backlog", _run)
    return calls


def test_new_plan_with_task_creates_linked_task(tmp_path, run_tome, capsys, fake_backlog_create):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
                     "--title", "T", "--desc", "the why",
                     "--with-task", "Do the thing", "--priority", "high",
                     "--ac", "one", "--ac", "two")

    assert code == 0
    task_path = next((vault / "backlog" / "tasks").glob("*.md"))
    text = task_path.read_text(encoding="utf-8")
    assert "title: Do the thing" in text
    assert "the why" in text
    assert "project:proj" in text
    assert "wiki/proj/plans/my-plan.md" in text
    assert "priority: high" in text
    assert "- [ ] #1 one" in text
    assert "- [ ] #2 two" in text
    out = capsys.readouterr().out
    assert "Created backlog task:" in out


def test_new_with_task_scoped_sync_includes_task_file(tmp_path, run_tome, fake_backlog_create):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")

    code = run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
                     "--title", "T", "--desc", "d",
                     "--with-task", "Do the thing", "--sync")

    assert code == 0
    log = _git(origin, "log", "--oneline")
    assert "new: my-plan" in log.stdout
    status = _git(vault, "status", "--porcelain")
    assert status.stdout.strip() == ""  # both plan and task committed, nothing left dirty


def test_new_with_task_requires_plan_type(tmp_path, run_tome, fake_backlog_create):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")

    code = run_tome("--vault", str(vault), "new", "idea", "an-idea", "--project", "proj",
                     "--title", "T", "--desc", "d", "--with-task", "Do the thing")

    assert code == 1
    assert fake_backlog_create == []


def test_new_priority_without_with_task_rejected(tmp_path, run_tome, fake_backlog_create):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")

    code = run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
                     "--title", "T", "--desc", "d", "--priority", "high")

    assert code == 1
    assert fake_backlog_create == []


def test_new_plan_without_with_task_makes_no_backlog_call(tmp_path, run_tome, fake_backlog_create):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")

    code = run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "proj",
                     "--title", "T", "--desc", "d")

    assert code == 0
    assert fake_backlog_create == []
