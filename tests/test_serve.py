"""Contract tests for `tome serve`'s two generated JSON payloads.

The server internals (routing, static file serving) are the rough,
harden-later part of the Phase 1 foundation slice; the *shapes* of
`build_index` and `build_board` are the deliberate, permanent contract the
frontend and any future static-export path depend on, so those are what's
locked here.
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tome_cli import cli as tome  # noqa: E402
from tome_cli import serve  # noqa: E402


def _conv(vault):
    return tome.load_conventions(vault)


def _git(vault, *args):
    return subprocess.run(["git", *args], cwd=str(vault),
                           check=True, capture_output=True, text=True)


def _bootstrap_git_vault(tmp_path, run_tome, name="vault"):
    """Same helper as test_sync_scoped.py/test_start_done.py, duplicated per
    that convention rather than shared — save_page needs a real origin to
    push against, same as sync_core's scoped-commit tests."""
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


def test_build_index_shape_and_links(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "tome/ideas/alpha.md", type="idea", title="Alpha",
              tags=["tome", "idea"], desc="First page.",
              body="\n# Alpha\n\nLinks to [[beta]] and [[missing]].\n")
    make_page(vault, "tome/ideas/beta.md", type="idea", title="Beta",
              tags=["tome", "idea"], desc="Second page.", body="\n# Beta\n\nn/a\n")

    index = serve.build_index(vault, _conv(vault))
    by_slug = {p["slug"]: p for p in index["pages"]}

    assert "alpha" in by_slug and "beta" in by_slug
    alpha = by_slug["alpha"]
    assert alpha["title"] == "Alpha"
    assert alpha["description"] == "First page."
    assert alpha["type"] == "idea"
    assert alpha["project"] == "tome"
    assert alpha["path"] == "tome/ideas/alpha.md"
    assert alpha["url"] == "/raw/tome/ideas/alpha.md"
    assert alpha["absPath"] == (vault / "wiki" / "tome/ideas/alpha.md").as_posix()
    assert alpha["tags"] == ["tome", "idea"]
    # Outbound wikilink graph is captured verbatim — including targets with no
    # page yet, which is how the frontend knows to render them broken.
    assert alpha["links"] == ["beta", "missing"]


def test_build_index_sorted_by_slug(make_vault, make_page):
    vault = make_vault()
    for slug in ("zeta", "alpha", "mu"):
        make_page(vault, f"tome/ideas/{slug}.md", type="idea", title=slug,
                  tags=["tome", "idea"])
    slugs = [p["slug"] for p in serve.build_index(vault, _conv(vault))["pages"]]
    assert slugs == sorted(slugs)


def test_build_index_exposes_type_enum(make_vault):
    vault = make_vault()
    index = serve.build_index(vault, _conv(vault))
    assert index["typeEnum"] == sorted(_conv(vault)["types"]["enum"])
    assert "plan" in index["typeEnum"] and "project" in index["typeEnum"]


def test_build_board_reads_config_and_tasks(make_vault, make_task):
    vault = make_vault()
    (vault / "backlog").mkdir(exist_ok=True)
    (vault / "backlog" / "config.yml").write_text(
        'default_status: "To Do"\n'
        'statuses: ["Too Soon", "To Do", "In Progress", "Done"]\n',
        encoding="utf-8", newline="\n",
    )
    make_task(vault, 1, "First task", status="In Progress", ordinal=1000,
              labels=["project:tome", "agent:opus"], milestone="m-1",
              refs=["wiki/tome/ideas/alpha.md"])
    make_task(vault, 2, "Second task", status="To Do", ordinal=500,
              labels=["project:artikindle"])

    board = serve.build_board(vault, _conv(vault))

    assert board["statuses"] == ["Too Soon", "To Do", "In Progress", "Done"]
    assert board["defaultStatus"] == "To Do"

    cards = {c["id"]: c for c in board["cards"]}
    assert set(cards) == {"task-1", "task-2"}

    one = cards["task-1"]
    assert one["rawId"] == "TASK-1"
    assert one["title"] == "First task"
    assert one["status"] == "In Progress"
    assert one["project"] == "tome"
    assert one["ordinal"] == 1000 and isinstance(one["ordinal"], int)
    assert one["milestone"] == "m-1"
    assert one["labels"] == ["project:tome", "agent:opus"]
    assert one["references"] == ["wiki/tome/ideas/alpha.md"]
    assert cards["task-2"]["project"] == "artikindle"


def test_build_board_empty_without_backlog(make_vault):
    vault = make_vault()
    board = serve.build_board(vault, _conv(vault))
    assert board == {"statuses": [], "defaultStatus": "", "cards": []}


# --------------------------------------------------------------------------- #
# apply_task_move — the one write `tome serve` accepts, always shelled
# through backlog.md ([[kanban-render-side]]). Tests fake out
# tome.run_backlog rather than shelling out to the real npx CLI, same
# pattern as test_start_done.py's fake_backlog; column state for the
# midpoint math is real on-disk task files via make_task, since
# apply_task_move reads those directly rather than trusting a client ordinal.
# --------------------------------------------------------------------------- #

class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run_backlog(monkeypatch, result=None):
    calls = []

    def _run(vault_root, argv, capture=False):
        calls.append(list(argv))
        return result or _Result()

    monkeypatch.setattr(tome, "run_backlog", _run)
    return calls


def test_apply_task_move_strips_task_prefix_and_appends_to_empty_column(monkeypatch, make_vault):
    vault = make_vault()
    calls = _fake_run_backlog(monkeypatch)

    ok, message = serve.apply_task_move(vault, "TASK-64", "In Progress", None)

    assert (ok, message) == (True, "")
    assert calls == [["task", "edit", "64", "-s", "In Progress", "--ordinal", "10000"]]


def test_apply_task_move_accepts_lowercase_id(monkeypatch, make_vault):
    vault = make_vault()
    calls = _fake_run_backlog(monkeypatch)

    ok, _ = serve.apply_task_move(vault, "task-7", "Done", None)

    assert ok is True
    assert calls == [["task", "edit", "7", "-s", "Done", "--ordinal", "10000"]]


def test_apply_task_move_rejects_non_numeric_id(monkeypatch, make_vault):
    vault = make_vault()
    calls = _fake_run_backlog(monkeypatch)

    ok, message = serve.apply_task_move(vault, "not-a-task", "Done", None)

    assert ok is False
    assert "bad task id" in message
    assert calls == []  # never shells out for an invalid id


def test_apply_task_move_rejects_empty_status(monkeypatch, make_vault):
    vault = make_vault()
    calls = _fake_run_backlog(monkeypatch)

    ok, message = serve.apply_task_move(vault, "task-1", "", None)

    assert ok is False
    assert "status is required" in message
    assert calls == []


def test_apply_task_move_surfaces_backlog_failure(monkeypatch, make_vault):
    vault = make_vault()
    _fake_run_backlog(monkeypatch, _Result(returncode=1, stderr="no such task"))

    ok, message = serve.apply_task_move(vault, "task-1", "Done", None)

    assert ok is False
    assert message == "no such task"


def test_apply_task_move_null_after_id_lands_above_the_current_top(monkeypatch, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "Existing top", status="To Do", ordinal=5000)
    calls = _fake_run_backlog(monkeypatch)

    ok, _ = serve.apply_task_move(vault, "task-2", "To Do", None)

    assert ok is True
    assert calls == [["task", "edit", "2", "-s", "To Do", "--ordinal", "4000"]]


def test_apply_task_move_after_last_card_appends_below_it(monkeypatch, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "Only card", status="To Do", ordinal=5000)
    calls = _fake_run_backlog(monkeypatch)

    ok, _ = serve.apply_task_move(vault, "task-2", "To Do", "task-1")

    assert ok is True
    assert calls == [["task", "edit", "2", "-s", "To Do", "--ordinal", "6000"]]


def test_apply_task_move_between_two_cards_picks_the_midpoint(monkeypatch, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "Top", status="To Do", ordinal=1000)
    make_task(vault, 2, "Bottom", status="To Do", ordinal=2000)
    calls = _fake_run_backlog(monkeypatch)

    ok, _ = serve.apply_task_move(vault, "task-3", "To Do", "task-1")

    assert ok is True
    assert calls == [["task", "edit", "3", "-s", "To Do", "--ordinal", "1500"]]


def test_apply_task_move_accepts_task_prefixed_after_id(monkeypatch, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "Top", status="To Do", ordinal=1000)
    make_task(vault, 2, "Bottom", status="To Do", ordinal=2000)
    calls = _fake_run_backlog(monkeypatch)

    ok, _ = serve.apply_task_move(vault, "task-3", "To Do", "TASK-1")

    assert ok is True
    assert calls == [["task", "edit", "3", "-s", "To Do", "--ordinal", "1500"]]


def test_apply_task_move_excludes_the_moving_card_from_its_own_column(monkeypatch, make_vault, make_task):
    # An in-column reorder: task-1's own old ordinal (500) must not be a
    # neighbour candidate for its own new position.
    vault = make_vault()
    make_task(vault, 1, "Moving", status="To Do", ordinal=500)
    make_task(vault, 2, "Top", status="To Do", ordinal=1000)
    make_task(vault, 3, "Bottom", status="To Do", ordinal=2000)
    calls = _fake_run_backlog(monkeypatch)

    ok, _ = serve.apply_task_move(vault, "task-1", "To Do", "task-2")

    assert ok is True
    assert calls == [["task", "edit", "1", "-s", "To Do", "--ordinal", "1500"]]


def test_apply_task_move_unknown_after_id_falls_back_to_bottom(monkeypatch, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "Only card", status="To Do", ordinal=1000)
    calls = _fake_run_backlog(monkeypatch)

    ok, _ = serve.apply_task_move(vault, "task-2", "To Do", "task-99")

    assert ok is True
    assert calls == [["task", "edit", "2", "-s", "To Do", "--ordinal", "2000"]]


def test_apply_task_move_rebalances_when_the_gap_is_exhausted(monkeypatch, make_vault, make_task):
    # Adjacent ordinals (1000, 1001) leave no integer midpoint, so the column
    # is renumbered back to 1000-spacing before the drop position is
    # recomputed against the fresh values.
    vault = make_vault()
    make_task(vault, 1, "Top", status="To Do", ordinal=1000)
    make_task(vault, 2, "Bottom", status="To Do", ordinal=1001)
    calls = _fake_run_backlog(monkeypatch)

    ok, _ = serve.apply_task_move(vault, "task-3", "To Do", "task-1")

    assert ok is True
    assert calls == [
        ["task", "edit", "1", "--ordinal", "10000"],
        ["task", "edit", "2", "--ordinal", "11000"],
        ["task", "edit", "3", "-s", "To Do", "--ordinal", "10500"],
    ]


# --------------------------------------------------------------------------- #
# save_page — the [[page-editing]] write, conflict- and lint-gated, committed
# + pushed scoped to just the one file. Needs a real git origin (unlike
# apply_task_move, which never touches git), so these skip without git on
# PATH, same as test_sync_scoped.py.
# --------------------------------------------------------------------------- #

pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _scaffold_idea(vault, run_tome, slug="alpha"):
    """A real `tome new` page (indexed, lint-clean), not `make_page`'s direct
    file write — save_page's own lint gate would otherwise always fire
    INDEX_MISSING on a page the index doesn't know about."""
    run_tome("--vault", str(vault), "new", "project", "tome", "--title", "Tome", "--desc", "d")
    run_tome("--vault", str(vault), "new", "idea", slug, "--project", "tome",
              "--title", slug.capitalize(), "--desc", "d")
    return vault / "wiki" / "tome" / "ideas" / f"{slug}.md"


@pytestmark_git
def test_save_page_happy_path_commits_and_pushes(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _scaffold_idea(vault, run_tome)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add alpha")
    _git(vault, "push")

    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    status, result = serve.save_page(vault, _conv(vault), "tome/ideas/alpha.md",
                                      "\n# Alpha\n\nEdited body.\n", base_hash)

    assert status == 200
    assert result["hash"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert "Edited body." in target.read_text(encoding="utf-8")
    assert "type: idea" in target.read_text(encoding="utf-8")  # frontmatter preserved
    log = _git(origin, "log", "--oneline")
    assert "edit: alpha" in log.stdout


@pytestmark_git
def test_save_page_conflict_on_stale_hash(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _scaffold_idea(vault, run_tome)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add alpha")
    _git(vault, "push")

    original_text = target.read_text(encoding="utf-8")

    status, result = serve.save_page(vault, _conv(vault), "tome/ideas/alpha.md",
                                      "\n# Alpha\n\nEdited body.\n", "stale-hash")

    assert status == 409
    assert "currentHash" in result
    assert target.read_text(encoding="utf-8") == original_text  # untouched
    status_out = _git(vault, "status", "--porcelain")
    assert status_out.stdout.strip() == ""  # nothing written, nothing to commit


@pytestmark_git
def test_save_page_lint_failure_restores_file(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _scaffold_idea(vault, run_tome)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add alpha")
    _git(vault, "push")

    original_text = target.read_text(encoding="utf-8")
    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    status, result = serve.save_page(vault, _conv(vault), "tome/ideas/alpha.md",
                                      "\nSee [[does-not-exist]].\n", base_hash)

    assert status == 422
    codes = {f["code"] for f in result["findings"]}
    assert "BROKEN_LINK" in codes
    assert target.read_text(encoding="utf-8") == original_text  # restored
    status_out = _git(vault, "status", "--porcelain")
    assert status_out.stdout.strip() == ""  # nothing left dirty
    log = _git(origin, "log", "--oneline")
    assert "edit: alpha" not in log.stdout  # never committed


@pytestmark_git
def test_save_page_rejects_path_traversal(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    status, result = serve.save_page(vault, _conv(vault), "../../etc/passwd",
                                      "pwned", "irrelevant")

    assert status == 404
    assert "error" in result


@pytestmark_git
def test_save_page_rejects_missing_page(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    status, result = serve.save_page(vault, _conv(vault), "tome/ideas/no-such-page.md",
                                      "body", "irrelevant")

    assert status == 404
    assert "error" in result


@pytestmark_git
def test_save_page_rejects_non_markdown_path(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    (vault / "wiki" / "tome").mkdir(parents=True, exist_ok=True)
    (vault / "wiki" / "tome" / "notes.txt").write_text("not markdown", encoding="utf-8")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add stray file")
    _git(vault, "push")

    status, result = serve.save_page(vault, _conv(vault), "tome/notes.txt",
                                      "body", "irrelevant")

    assert status == 404
    assert "error" in result


# --------------------------------------------------------------------------- #
# save_frontmatter — the [[frontmatter-editing]] write: title/tags/description
# through fm_set, conflict- and lint-gated like save_page, plus an index (and,
# for a plan, hub) regeneration step save_page never needs.
# --------------------------------------------------------------------------- #

@pytestmark_git
def test_save_frontmatter_happy_path_commits_and_pushes(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _scaffold_idea(vault, run_tome)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add alpha")
    _git(vault, "push")

    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    fields = {"title": "Alpha Renamed", "tags": ["tome", "personal"], "description": "New desc."}

    status, result = serve.save_frontmatter(vault, _conv(vault), "tome/ideas/alpha.md",
                                             fields, base_hash)

    assert status == 200
    assert result["hash"] == hashlib.sha256(target.read_bytes()).hexdigest()
    text = target.read_text(encoding="utf-8")
    assert 'title: "Alpha Renamed"' in text
    assert "tags: [tome, personal]" in text
    assert 'description: "New desc."' in text
    index_text = (vault / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "New desc." in index_text
    log = _git(origin, "log", "--oneline")
    assert "edit frontmatter: alpha" in log.stdout


@pytestmark_git
def test_save_frontmatter_noop_when_nothing_changed(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _scaffold_idea(vault, run_tome)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add alpha")
    _git(vault, "push")

    original_text = target.read_text(encoding="utf-8")
    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    status, result = serve.save_frontmatter(vault, _conv(vault), "tome/ideas/alpha.md",
                                             {"title": "Alpha", "description": "d"}, base_hash)

    assert status == 200
    assert result["hash"] == base_hash
    assert target.read_text(encoding="utf-8") == original_text  # untouched, no `updated` bump
    log_before = _git(origin, "log", "--oneline").stdout
    assert "edit frontmatter" not in log_before  # nothing committed


@pytestmark_git
def test_save_frontmatter_conflict_on_stale_hash(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _scaffold_idea(vault, run_tome)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add alpha")
    _git(vault, "push")

    original_text = target.read_text(encoding="utf-8")

    status, result = serve.save_frontmatter(vault, _conv(vault), "tome/ideas/alpha.md",
                                             {"title": "New Title"}, "stale-hash")

    assert status == 409
    assert "currentHash" in result
    assert target.read_text(encoding="utf-8") == original_text
    status_out = _git(vault, "status", "--porcelain")
    assert status_out.stdout.strip() == ""


@pytestmark_git
def test_save_frontmatter_lint_failure_restores_file_and_index(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _scaffold_idea(vault, run_tome)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add alpha")
    _git(vault, "push")

    original_text = target.read_text(encoding="utf-8")
    original_index = (vault / "wiki" / "index.md").read_text(encoding="utf-8")
    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    status, result = serve.save_frontmatter(vault, _conv(vault), "tome/ideas/alpha.md",
                                             {"tags": ["not-a-real-tag"]}, base_hash)

    assert status == 422
    codes = {f["code"] for f in result["findings"]}
    assert "BAD_TAG" in codes
    assert target.read_text(encoding="utf-8") == original_text  # restored
    assert (vault / "wiki" / "index.md").read_text(encoding="utf-8") == original_index  # index restored too
    status_out = _git(vault, "status", "--porcelain")
    assert status_out.stdout.strip() == ""
    log = _git(origin, "log", "--oneline")
    assert "edit frontmatter: alpha" not in log.stdout


@pytestmark_git
def test_save_frontmatter_regenerates_hub_for_plan(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "tome", "--title", "Tome", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "tome",
              "--title", "My Plan", "--desc", "old desc")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add plan")
    _git(vault, "push")

    target = vault / "wiki" / "tome" / "plans" / "my-plan.md"
    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    status, result = serve.save_frontmatter(vault, _conv(vault), "tome/plans/my-plan.md",
                                             {"description": "new desc"}, base_hash)

    assert status == 200
    hub_text = (vault / "wiki" / "tome" / "tome.md").read_text(encoding="utf-8")
    assert "new desc" in hub_text
    log = _git(origin, "log", "--oneline")
    assert "edit frontmatter: my-plan" in log.stdout


@pytestmark_git
def test_save_frontmatter_rejects_unknown_field(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _scaffold_idea(vault, run_tome)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add alpha")
    _git(vault, "push")

    status, result = serve.save_frontmatter(vault, _conv(vault), "tome/ideas/alpha.md",
                                             {"status": "done"}, "irrelevant")

    assert status == 400
    assert "status" in result["error"]


@pytestmark_git
def test_save_frontmatter_rejects_quote_in_title(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _scaffold_idea(vault, run_tome)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add alpha")
    _git(vault, "push")

    original_text = target.read_text(encoding="utf-8")
    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    status, result = serve.save_frontmatter(vault, _conv(vault), "tome/ideas/alpha.md",
                                             {"title": 'Bad "Title"'}, base_hash)

    assert status == 400
    assert target.read_text(encoding="utf-8") == original_text  # untouched


@pytestmark_git
def test_save_frontmatter_rejects_tag_with_comma(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _scaffold_idea(vault, run_tome)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add alpha")
    _git(vault, "push")

    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    status, result = serve.save_frontmatter(vault, _conv(vault), "tome/ideas/alpha.md",
                                             {"tags": ["a,b"]}, base_hash)

    assert status == 400
    assert "tag" in result["error"]


@pytestmark_git
def test_save_frontmatter_rejects_path_traversal(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    status, result = serve.save_frontmatter(vault, _conv(vault), "../../etc/passwd",
                                             {"title": "pwned"}, "irrelevant")

    assert status == 404
    assert "error" in result


@pytestmark_git
def test_save_frontmatter_rejects_missing_page(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    status, result = serve.save_frontmatter(vault, _conv(vault), "tome/ideas/no-such-page.md",
                                             {"title": "x"}, "irrelevant")

    assert status == 404
    assert "error" in result


# --------------------------------------------------------------------------- #
# rename_page — the [[slug-rename]] write: a slug rename through cli.move_page
# (the tome mv core), conflict-gated like the others, plus a wiki-wide inbound-
# link rewrite and a new-errors-only lint gate save_page/save_frontmatter don't
# need. Returns the new slug's in-app URL for the client to redirect to.
# --------------------------------------------------------------------------- #

def _scaffold_two_ideas(vault, run_tome):
    """Project tome + ideas alpha & beta, with beta's body linking [[alpha]],
    all committed + pushed clean — the fixture for exercising the inbound-link
    rewrite a rename performs."""
    run_tome("--vault", str(vault), "new", "project", "tome", "--title", "Tome", "--desc", "d")
    run_tome("--vault", str(vault), "new", "idea", "alpha", "--project", "tome",
              "--title", "Alpha", "--desc", "d")
    run_tome("--vault", str(vault), "new", "idea", "beta", "--project", "tome",
              "--title", "Beta", "--desc", "d")
    beta = vault / "wiki" / "tome" / "ideas" / "beta.md"
    fm, body = tome.read_page(beta)
    tome.write_page(beta, fm, body + "\nSee [[alpha]] for context.\n")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "scaffold ideas")
    _git(vault, "push")
    return vault / "wiki" / "tome" / "ideas" / "alpha.md", beta


@pytestmark_git
def test_rename_page_happy_path_moves_and_rewrites_links(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    alpha, beta = _scaffold_two_ideas(vault, run_tome)
    base_hash = hashlib.sha256(alpha.read_bytes()).hexdigest()

    status, result = serve.rename_page(vault, _conv(vault), "tome/ideas/alpha.md",
                                        "gamma", base_hash)

    assert status == 200
    assert result["slug"] == "gamma"
    assert result["url"] == "?page=gamma"
    gamma = vault / "wiki" / "tome" / "ideas" / "gamma.md"
    assert gamma.is_file()
    assert not alpha.exists()
    beta_text = beta.read_text(encoding="utf-8")
    assert "[[gamma]]" in beta_text and "[[alpha]]" not in beta_text
    assert "[[gamma]]" in (vault / "wiki" / "index.md").read_text(encoding="utf-8")
    log = _git(origin, "log", "--oneline")
    assert "mv: alpha -> gamma" in log.stdout


@pytestmark_git
def test_rename_page_conflict_on_stale_hash(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    alpha, _beta = _scaffold_two_ideas(vault, run_tome)
    original_text = alpha.read_text(encoding="utf-8")

    status, result = serve.rename_page(vault, _conv(vault), "tome/ideas/alpha.md",
                                        "gamma", "stale-hash")

    assert status == 409
    assert "currentHash" in result
    assert alpha.read_text(encoding="utf-8") == original_text  # untouched
    assert not (vault / "wiki" / "tome" / "ideas" / "gamma.md").exists()
    assert _git(vault, "status", "--porcelain").stdout.strip() == ""


@pytestmark_git
def test_rename_page_noop_when_same_slug(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    alpha, _beta = _scaffold_two_ideas(vault, run_tome)
    base_hash = hashlib.sha256(alpha.read_bytes()).hexdigest()

    status, result = serve.rename_page(vault, _conv(vault), "tome/ideas/alpha.md",
                                        "alpha", base_hash)

    assert status == 200
    assert result["slug"] == "alpha"
    assert _git(vault, "status", "--porcelain").stdout.strip() == ""
    assert "mv: alpha" not in _git(origin, "log", "--oneline").stdout


@pytestmark_git
def test_rename_page_rejects_invalid_slug(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    alpha, _beta = _scaffold_two_ideas(vault, run_tome)
    base_hash = hashlib.sha256(alpha.read_bytes()).hexdigest()

    status, result = serve.rename_page(vault, _conv(vault), "tome/ideas/alpha.md",
                                        "Not A Slug", base_hash)

    assert status == 400
    assert "slug" in result["error"]
    assert alpha.exists()  # nothing moved


@pytestmark_git
def test_rename_page_rejects_taken_slug(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    alpha, _beta = _scaffold_two_ideas(vault, run_tome)
    base_hash = hashlib.sha256(alpha.read_bytes()).hexdigest()

    status, result = serve.rename_page(vault, _conv(vault), "tome/ideas/alpha.md",
                                        "beta", base_hash)

    assert status == 400
    assert "beta" in result["error"]
    assert alpha.exists()
    assert _git(vault, "status", "--porcelain").stdout.strip() == ""


@pytestmark_git
def test_rename_page_rejects_project_hub(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _scaffold_two_ideas(vault, run_tome)
    hub = vault / "wiki" / "tome" / "tome.md"
    base_hash = hashlib.sha256(hub.read_bytes()).hexdigest()

    status, result = serve.rename_page(vault, _conv(vault), "tome/tome.md",
                                        "grimoire", base_hash)

    assert status == 400
    assert "hub" in result["error"]
    assert hub.exists()


@pytestmark_git
def test_rename_page_regenerates_hub_for_plan(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "tome", "--title", "Tome", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "my-plan", "--project", "tome",
              "--title", "My Plan", "--desc", "a plan")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add plan")
    _git(vault, "push")

    target = vault / "wiki" / "tome" / "plans" / "my-plan.md"
    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    status, result = serve.rename_page(vault, _conv(vault), "tome/plans/my-plan.md",
                                        "the-plan", base_hash)

    assert status == 200
    assert (vault / "wiki" / "tome" / "plans" / "the-plan.md").is_file()
    hub_text = (vault / "wiki" / "tome" / "tome.md").read_text(encoding="utf-8")
    assert "[[the-plan]]" in hub_text and "[[my-plan]]" not in hub_text
    assert "mv: my-plan -> the-plan" in _git(origin, "log", "--oneline").stdout


@pytestmark_git
def test_rename_page_lint_failure_resets_move(tmp_path, run_tome, monkeypatch):
    """A rewrite that leaves a dangling link is caught by the new-errors-only
    gate even on a page outside the touched set; the whole move is then rolled
    back from HEAD (no single buffer to restore, unlike the field editors)."""
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    alpha, beta = _scaffold_two_ideas(vault, run_tome)
    base_hash = hashlib.sha256(alpha.read_bytes()).hexdigest()
    original_beta = beta.read_text(encoding="utf-8")

    # Force a fabricated *new* error on the post-move lint pass only, so the
    # gate fires and _reset_move runs — the pre-move pass stays clean.
    real = tome.run_all_lint_checks
    calls = {"n": 0}

    def fake(vault_root, conventions):
        pages, findings = real(vault_root, conventions)
        calls["n"] += 1
        if calls["n"] >= 2:
            findings = findings + [tome.Finding(tome.ERROR, "BROKEN_LINK",
                                                "tome/ideas/beta.md", "fabricated")]
        return pages, findings

    monkeypatch.setattr(tome, "run_all_lint_checks", fake)

    status, result = serve.rename_page(vault, _conv(vault), "tome/ideas/alpha.md",
                                        "gamma", base_hash)

    assert status == 422
    assert {f["code"] for f in result["findings"]} == {"BROKEN_LINK"}
    assert alpha.is_file()  # move rolled back
    assert not (vault / "wiki" / "tome" / "ideas" / "gamma.md").exists()
    assert beta.read_text(encoding="utf-8") == original_beta  # rewrite reverted
    assert _git(vault, "status", "--porcelain").stdout.strip() == ""  # tree clean
    assert "mv: alpha -> gamma" not in _git(origin, "log", "--oneline").stdout


@pytestmark_git
def test_rename_page_rejects_path_traversal(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    status, result = serve.rename_page(vault, _conv(vault), "../../etc/passwd",
                                        "pwned", "irrelevant")

    assert status == 404
    assert "error" in result


@pytestmark_git
def test_rename_page_rejects_missing_page(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    status, result = serve.rename_page(vault, _conv(vault), "tome/ideas/no-such-page.md",
                                        "gamma", "irrelevant")

    assert status == 404
    assert "error" in result


# --------------------------------------------------------------------------- #
# create_page — the [[page-creation]] write: scaffolding a brand-new page
# through cli.new_page (the tome new core). No baseHash — the guard is slug
# uniqueness, re-checked after a pull — and on rejection there's no single
# buffer to restore, so a rejected create rolls the whole scaffold back via
# _reset_create instead.
# --------------------------------------------------------------------------- #

@pytestmark_git
def test_create_page_happy_path_commits_and_pushes(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "tome", "--title", "Tome", "--desc", "d")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add project")
    _git(vault, "push")

    status, result = serve.create_page(vault, _conv(vault), "idea", "tome", "my-idea",
                                        "My Idea", "a fresh idea")

    assert status == 200
    assert result["slug"] == "my-idea"
    assert result["url"] == "?page=my-idea"
    created = vault / "wiki" / "tome" / "ideas" / "my-idea.md"
    assert created.is_file()
    assert "[[my-idea]]" in (vault / "wiki" / "index.md").read_text(encoding="utf-8")
    log = _git(origin, "log", "--oneline")
    assert "new: my-idea" in log.stdout


@pytestmark_git
def test_create_page_project_type_creates_hub(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    status, result = serve.create_page(vault, _conv(vault), "project", None, "artikindle",
                                        "Artikindle", "a read-it-later tool")

    assert status == 200
    assert result["slug"] == "artikindle"
    hub = vault / "wiki" / "artikindle" / "artikindle.md"
    assert hub.is_file()
    assert "tome:plans" in hub.read_text(encoding="utf-8")
    assert "new: artikindle" in _git(origin, "log", "--oneline").stdout


@pytestmark_git
def test_create_page_regenerates_hub_for_plan(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "tome", "--title", "Tome", "--desc", "d")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add project")
    _git(vault, "push")

    status, result = serve.create_page(vault, _conv(vault), "plan", "tome", "my-plan",
                                        "My Plan", "a plan")

    assert status == 200
    hub_text = (vault / "wiki" / "tome" / "tome.md").read_text(encoding="utf-8")
    assert "[[my-plan]]" in hub_text
    fm, _body = tome.read_page(vault / "wiki" / "tome" / "plans" / "my-plan.md")
    assert tome.fm_get(fm, "status") == "proposed"


@pytestmark_git
def test_create_page_rejects_taken_slug(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _scaffold_two_ideas(vault, run_tome)

    status, result = serve.create_page(vault, _conv(vault), "idea", "tome", "alpha",
                                        "Alpha Again", "d")

    assert status == 422
    assert "alpha" in result["error"]
    assert _git(vault, "status", "--porcelain").stdout.strip() == ""


@pytestmark_git
def test_create_page_rejects_missing_project(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    status, result = serve.create_page(vault, _conv(vault), "idea", None, "orphan-idea",
                                        "Orphan", "d")

    assert status == 422
    assert "project" in result["error"]


@pytestmark_git
def test_create_page_rejects_unknown_project_dir(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    status, result = serve.create_page(vault, _conv(vault), "idea", "ghost", "an-idea",
                                        "Idea", "d")

    assert status == 422
    assert "ghost" in result["error"]


@pytestmark_git
def test_create_page_rejects_bad_type(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)

    status, result = serve.create_page(vault, _conv(vault), "not-a-type", "tome",
                                        "an-idea", "Idea", "d")

    assert status == 422
    assert "not-a-type" in result["error"]


@pytestmark_git
def test_create_page_rejects_quote_in_title(tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "tome", "--title", "Tome", "--desc", "d")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add project")
    _git(vault, "push")

    status, result = serve.create_page(vault, _conv(vault), "idea", "tome", "my-idea",
                                        'Bad "Title"', "d")

    assert status == 422
    assert 'literal "' in result["error"]
    assert not (vault / "wiki" / "tome" / "ideas" / "my-idea.md").exists()


@pytestmark_git
def test_create_page_lint_failure_removes_scaffolded_file(tmp_path, run_tome, monkeypatch):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "tome", "--title", "Tome", "--desc", "d")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add project")
    _git(vault, "push")

    real = tome.run_all_lint_checks

    def fake(vault_root, conventions):
        pages, findings = real(vault_root, conventions)
        findings = findings + [tome.Finding(tome.ERROR, "BROKEN_LINK",
                                            "tome/ideas/my-idea.md", "fabricated")]
        return pages, findings

    monkeypatch.setattr(tome, "run_all_lint_checks", fake)

    status, result = serve.create_page(vault, _conv(vault), "idea", "tome", "my-idea",
                                        "My Idea", "d")

    assert status == 422
    assert {f["code"] for f in result["findings"]} == {"BROKEN_LINK"}
    assert not (vault / "wiki" / "tome" / "ideas" / "my-idea.md").exists()
    assert _git(vault, "status", "--porcelain").stdout.strip() == ""
    assert "new: my-idea" not in _git(origin, "log", "--oneline").stdout


# --------------------------------------------------------------------------- #
# writable flag — live serve vs. static export, layered onto build_board()
# without changing its own pure-function contract (tested above).
# --------------------------------------------------------------------------- #

def test_board_with_writable_true_for_live_serve(make_vault):
    vault = make_vault()
    board = serve._board_with_writable(vault, _conv(vault), True)
    assert board["writable"] is True
    assert board["statuses"] == []  # build_board's own shape still comes through


def test_export_static_board_json_is_read_only(tmp_path, make_vault):
    import json

    vault = make_vault()
    out_dir = tmp_path / "export"
    serve.export_static(vault, _conv(vault), out_dir)

    board = json.loads((out_dir / "board.json").read_text(encoding="utf-8"))
    assert board["writable"] is False


# --------------------------------------------------------------------------- #
# launch_gui — the pythonw/gui-scripts desktop launcher. Tests the wiring
# (vault resolution, args passed to cmd_serve) without starting a real
# server or opening a browser.
# --------------------------------------------------------------------------- #

def test_launch_gui_resolves_vault_and_opens_with_idle_timeout(monkeypatch, make_vault):
    vault = make_vault()
    monkeypatch.chdir(vault)

    captured = {}

    def fake_cmd_serve(vault_root, conventions, args):
        captured["vault_root"] = vault_root
        captured["args"] = args
        return 0

    monkeypatch.setattr(serve, "cmd_serve", fake_cmd_serve)

    code = serve.launch_gui()

    assert code == 0
    assert captured["vault_root"] == vault
    assert captured["args"].open is True
    assert captured["args"].idle_timeout == 30
    assert captured["args"].export is None


def test_launch_gui_reports_failure_without_crashing(monkeypatch, tmp_path):
    # No conventions.toml anywhere up from here and no VAULT_ROOT set.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VAULT_ROOT", raising=False)

    code = serve.launch_gui()

    assert code == 1
