"""Shared fixtures for the tome_cli test suite.

Imports the modules under test by inserting src/ into sys.path — the same
layout the installed package resolves from, exercised here straight out of
the checkout.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from tome_cli import cli as tome  # noqa: E402
from tome_cli import lint as tome_lint  # noqa: E402


@pytest.fixture
def run_tome(monkeypatch):
    """Drive tome.main() through argparse with a monkeypatched argv, so the
    CLI wiring itself is exercised rather than calling cmd_* directly."""
    def _run(*args):
        monkeypatch.setattr(sys, "argv", ["tome", *args])
        return tome.main()
    return _run


@pytest.fixture
def make_vault(tmp_path, run_tome):
    """Scaffold a fresh vault via the real `tome init` at tmp_path/<name>."""
    def _make(name="vault"):
        target = tmp_path / name
        code = run_tome("init", str(target))
        assert code == 0, f"tome init failed for {target}"
        return target
    return _make


@pytest.fixture
def make_task():
    """Write a backlog task file directly at backlog/tasks/task-<n> - <slug>.md,
    close enough to real backlog.md output (block-list `assignee:`/
    `references:`/`dependencies:`, `<!-- SECTION:DESCRIPTION:BEGIN/END -->`
    and `<!-- AC:BEGIN/END -->` body blocks) for tome's own task-reading
    helpers to work against it. Tests never shell out to the real backlog.md
    CLI — see the fake `run_backlog` pattern in test_start_done.py for how
    mutations are simulated. `checked` is a set of 1-based AC numbers to mark
    `- [x]`; `desc`/`notes`, left None, omit their sections entirely
    (`cli.task_description`/`cli.task_notes` return "" either way)."""
    def _make(vault_root, task_num, title, *, status="To Do", assignee=None,
              refs=None, deps=None, acs=("one", "two"), checked=(), ordinal=1000,
              labels=None, milestone=None, completed=False, desc=None, notes=None):
        tasks_dir = vault_root / "backlog" / ("completed" if completed else "tasks")
        tasks_dir.mkdir(parents=True, exist_ok=True)
        path = tasks_dir / f"task-{task_num} - {title.replace(' ', '-')}.md"
        assignee_block = ("assignee: []" if not assignee
                           else "assignee:\n" + "\n".join(f"  - '{a}'" for a in assignee))
        refs = refs or []
        refs_block = ("references: []" if not refs
                      else "references:\n" + "\n".join(f"  - {r}" for r in refs))
        deps = deps or []
        deps_block = ("dependencies: []" if not deps
                      else "dependencies:\n" + "\n".join(f"  - {d}" for d in deps))
        labels_block = ("labels: []" if not labels
                        else "labels:\n" + "\n".join(f"  - '{l}'" for l in labels))
        milestone_line = f"milestone: {milestone}\n" if milestone else ""
        ac_lines = "\n".join(
            f"- [{'x' if i in checked else ' '}] #{i} {text}"
            for i, text in enumerate(acs, start=1)
        )
        description_section = (
            "\n## Description\n\n<!-- SECTION:DESCRIPTION:BEGIN -->\n"
            f"{desc}\n<!-- SECTION:DESCRIPTION:END -->\n"
        ) if desc is not None else ""
        notes_section = (
            "\n## Implementation Notes\n\n<!-- SECTION:NOTES:BEGIN -->\n"
            f"{notes}\n<!-- SECTION:NOTES:END -->\n"
        ) if notes is not None else ""
        text = (
            "---\n"
            f"id: TASK-{task_num}\n"
            f"title: {title}\n"
            f"status: {status}\n"
            f"{assignee_block}\n"
            "created_date: '2026-01-01 00:00'\n"
            "updated_date: '2026-01-01 00:00'\n"
            f"{labels_block}\n"
            f"{milestone_line}"
            f"{deps_block}\n"
            f"{refs_block}\n"
            f"ordinal: {ordinal}\n"
            "---\n"
            f"{description_section}"
            "\n## Acceptance Criteria\n"
            "<!-- AC:BEGIN -->\n"
            f"{ac_lines}\n"
            "<!-- AC:END -->\n"
            f"{notes_section}"
        )
        path.write_text(text, encoding="utf-8", newline="\n")
        return path
    return _make


@pytest.fixture
def make_page():
    """Write a page file with given frontmatter directly, bypassing `tome new`
    so lint/index tests can construct fixtures the CLI itself would reject."""
    def _make(vault_root, rel_path, *, type="plan", title="T", tags=None,
              desc="d", created="2026-01-01", updated="2026-01-01",
              status=None, extra_fm=None, body="\n# T\n\nTBD.\n"):
        path = vault_root / "wiki" / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tags = tags if tags is not None else [type]
        fm = [
            f"type: {type}",
            f'title: "{title}"',
            f"tags: [{', '.join(tags)}]",
            f'description: "{desc}"',
            f"created: {created}",
            f"updated: {updated}",
        ]
        if status:
            fm.append(f"status: {status}")
        if extra_fm:
            fm.extend(extra_fm)
        text = "---\n" + "\n".join(fm) + "\n---\n" + body
        path.write_text(text, encoding="utf-8", newline="\n")
        return path
    return _make
