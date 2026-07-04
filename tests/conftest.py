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
