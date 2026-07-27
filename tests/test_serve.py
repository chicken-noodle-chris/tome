"""Contract tests for `tome serve`'s two generated JSON payloads.

The server internals (routing, static file serving) are the rough,
harden-later part of the Phase 1 foundation slice; the *shapes* of
`build_index` and `build_board` are the deliberate, permanent contract the
frontend and any future static-export path depend on, so those are what's
locked here.
"""

import functools
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tome_cli import lib  # noqa: E402
from tome_cli import serve  # noqa: E402


def _conv(vault):
    return lib.load_conventions(vault)


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
        'statuses: ["Backlog", "To Do", "In Progress", "Done"]\n',
        encoding="utf-8", newline="\n",
    )
    make_task(vault, 1, "First task", status="In Progress", ordinal=1000,
              labels=["project:tome", "agent:opus"], milestone="m-1",
              refs=["wiki/tome/ideas/alpha.md"], deps=["TASK-63"], assignee=["@me"],
              desc="Full task description.", notes="Shipped in commit abc123.",
              acs=("one", "two"), checked={1})
    make_task(vault, 2, "Second task", status="To Do", ordinal=500,
              labels=["project:artikindle"])

    board = serve.build_board(vault, _conv(vault))

    assert board["statuses"] == ["Backlog", "To Do", "In Progress", "Done"]
    assert board["defaultStatus"] == "To Do"
    assert board["backlogStatus"] == "Backlog"

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
    assert one["agent"] == "opus"
    assert one["references"] == ["wiki/tome/ideas/alpha.md"]
    assert one["dependencies"] == ["task-63"]
    assert one["assignee"] == ["@me"]
    assert one["created"] == "2026-01-01 00:00"
    assert one["updated"] == "2026-01-01 00:00"
    assert one["description"] == "Full task description."
    assert one["acceptanceCriteria"] == [
        {"text": "one", "checked": True}, {"text": "two", "checked": False},
    ]
    assert one["notes"] == "Shipped in commit abc123."
    assert len(one["hash"]) == 64  # sha256 of the task file — the write conflict token
    two = cards["task-2"]
    assert two["project"] == "artikindle"
    assert two["agent"] is None
    assert two["dependencies"] == []
    assert two["assignee"] == []
    assert two["description"] == ""
    assert two["acceptanceCriteria"] == [
        {"text": "one", "checked": False}, {"text": "two", "checked": False},
    ]
    assert two["notes"] == ""


def test_build_board_empty_without_backlog(make_vault):
    vault = make_vault()
    board = serve.build_board(vault, _conv(vault))
    assert board == {
        "statuses": [], "defaultStatus": "", "backlogStatus": "Backlog", "cards": [],
    }


# --------------------------------------------------------------------------- #
# apply_task_move — the one write `tome serve` accepts, always shelled
# through backlog.md ([[kanban-render-side]]). Tests fake out
# lib.run_backlog rather than shelling out to the real npx CLI, same
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

    monkeypatch.setattr(lib, "run_backlog", _run)
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


def test_apply_task_move_rejects_a_completed_task(monkeypatch, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "Shipped task", status="Done", completed=True)
    calls = _fake_run_backlog(monkeypatch)

    ok, message = serve.apply_task_move(vault, "task-1", "To Do", None)

    assert ok is False
    assert "completed" in message
    assert calls == []  # never shells out for a completed task


# --------------------------------------------------------------------------- #
# task_patch_argv / apply_task_edit — the [[task-editing]] write. The
# translation is a pure function (patch in, argv out) precisely so it can be
# asserted without a live server or a real npx, so these use the same
# `_fake_run_backlog` monkeypatch the move tests above do; no test here shells
# out to backlog.md. Like a move, this write never touches git.
# --------------------------------------------------------------------------- #

def _card(vault, task_id="task-1"):
    board = serve.build_board(vault, _conv(vault))
    return next(c for c in board["cards"] if c["id"] == task_id)


def test_build_board_card_carries_its_file_hash(make_vault, make_task):
    vault = make_vault()
    path = make_task(vault, 1, "First task")

    card = _card(vault)

    assert card["hash"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_build_board_card_hash_changes_with_the_file(make_vault, make_task):
    # The whole point of a byte-exact token: `updated_date` is stamped at
    # minute granularity, so two edits inside one minute would look identical.
    vault = make_vault()
    path = make_task(vault, 1, "First task")
    before = _card(vault)["hash"]

    path.write_text(path.read_text(encoding="utf-8") + "\ntrailing\n",
                    encoding="utf-8", newline="\n")

    assert _card(vault)["hash"] != before


def test_build_board_includes_completed_cards(make_vault, make_task):
    # A completed task's file lives in backlog/completed/, not backlog/tasks/,
    # but its card must still surface — that's the whole point of
    # [[completed-tasks-viewable]] — flagged rather than dropped, and with no
    # hash since it's no longer a write target.
    vault = make_vault()
    make_task(vault, 1, "Live task", status="To Do")
    make_task(vault, 2, "Shipped task", status="Done", completed=True)

    board = serve.build_board(vault, _conv(vault))
    cards = {c["id"]: c for c in board["cards"]}

    assert set(cards) == {"task-1", "task-2"}
    assert cards["task-1"]["completed"] is False
    assert cards["task-2"]["completed"] is True
    assert cards["task-2"]["hash"] == ""
    assert cards["task-2"]["title"] == "Shipped task"


@pytest.mark.parametrize("patch, expected", [
    ({"title": "New title"}, ["-t", "New title"]),
    ({"description": "Body text."}, ["-d", "Body text."]),
    ({"description": ""}, ["-d", ""]),
    ({"notes": "Shipped in abc123."}, ["--notes", "Shipped in abc123."]),
    ({"priority": "high"}, ["--priority", "high"]),
    ({"milestone": "m-2"}, ["-m", "m-2"]),
    ({"milestone": ""}, ["--clear-milestone"]),
    ({"assignee": "@me"}, ["-a", "@me"]),
    ({"addLabel": "semver:minor"}, ["--add-label", "semver:minor"]),
    ({"removeLabel": "agent:opus"}, ["--remove-label", "agent:opus"]),
    ({"ac": {"index": 1, "checked": True}}, ["--check-ac", "1"]),
    ({"ac": {"index": 2, "checked": False}}, ["--uncheck-ac", "2"]),
])
def test_task_patch_argv_single_fields(make_vault, make_task, patch, expected):
    vault = make_vault()
    make_task(vault, 1, "First task")

    argv = serve.task_patch_argv("1", patch, _card(vault))

    assert argv == ["task", "edit", "1", *expected]


def test_task_patch_argv_folds_several_fields_into_one_invocation(make_vault, make_task):
    # One save, one argv — that's what keeps a multi-field write atomic from
    # the client's side and the npx cost to one process.
    vault = make_vault()
    make_task(vault, 1, "First task")

    argv = serve.task_patch_argv(
        "1", {"title": "T", "priority": "low", "removeLabel": "old", "addLabel": "new"},
        _card(vault))

    assert argv == ["task", "edit", "1", "-t", "T", "--priority", "low",
                    "--remove-label", "old", "--add-label", "new"]


def test_task_patch_argv_rewrites_the_whole_ac_block(make_vault, make_task):
    # backlog.md can add and remove a criterion but not rewrite one in place,
    # so a text edit clears every index and re-adds the list in order, then
    # re-checks by *post*-rewrite index — all inside the one argv.
    vault = make_vault()
    make_task(vault, 1, "First task", acs=("one", "two"), checked={1})

    argv = serve.task_patch_argv("1", {"acs": [
        {"text": "one edited", "checked": True},
        {"text": "two", "checked": False},
        {"text": "three", "checked": True},
    ]}, _card(vault))

    assert argv == [
        "task", "edit", "1",
        "--remove-ac", "1", "--remove-ac", "2",
        "--ac", "one edited", "--ac", "two", "--ac", "three",
        "--check-ac", "1", "--check-ac", "3",
    ]


def test_task_patch_argv_can_clear_every_criterion(make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "First task", acs=("one", "two"))

    argv = serve.task_patch_argv("1", {"acs": []}, _card(vault))

    assert argv == ["task", "edit", "1", "--remove-ac", "1", "--remove-ac", "2"]


@pytest.mark.parametrize("patch, fragment", [
    ({"ordinal": 5}, "unsupported field"),
    ({"references": ["wiki/x.md"]}, "unsupported field"),
    ({"created": "2026-01-01"}, "unsupported field"),
    ({}, "empty"),
    ({"title": "  "}, "must not be empty"),
    ({"title": 7}, "must be a string"),
    ({"priority": "urgent"}, "priority must be one of"),
    ({"assignee": ""}, "must not be empty"),
    ({"ac": {"index": 9, "checked": True}}, "out of range"),
    ({"ac": {"index": "1", "checked": True}}, "index: int"),
    ({"acs": [{"text": "", "checked": False}]}, "must not be empty"),
    ({"acs": "one"}, "must be a list"),
    ({"ac": {"index": 1, "checked": True}, "acs": []}, "mutually exclusive"),
])
def test_task_patch_argv_rejects_bad_patches(make_vault, make_task, patch, fragment):
    vault = make_vault()
    make_task(vault, 1, "First task")

    with pytest.raises(ValueError) as excinfo:
        serve.task_patch_argv("1", patch, _card(vault))

    assert fragment in str(excinfo.value)


def test_apply_task_edit_happy_path_shells_once_and_returns_the_board(monkeypatch, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "First task")
    card = _card(vault)
    calls = _fake_run_backlog(monkeypatch)

    status, payload = serve.apply_task_edit(vault, _conv(vault), "TASK-1",
                                             {"title": "Renamed"}, card["hash"])

    assert status == 200
    assert calls == [["task", "edit", "1", "-t", "Renamed"]]
    assert payload["writable"] is True
    assert [c["id"] for c in payload["cards"]] == ["task-1"]


def test_apply_task_edit_refuses_a_stale_hash_and_returns_the_fresh_card(monkeypatch, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "First task")
    calls = _fake_run_backlog(monkeypatch)

    status, payload = serve.apply_task_edit(vault, _conv(vault), "task-1",
                                             {"title": "Renamed"}, "stale")

    assert status == 409
    assert calls == []  # disk untouched
    # The fresh card is what the client's on-disk pane renders, and its hash
    # is the token that makes an informed retry land.
    assert payload["card"]["title"] == "First task"
    assert payload["card"]["hash"] == _card(vault)["hash"]


def test_apply_task_edit_rejects_an_unknown_task(monkeypatch, make_vault):
    vault = make_vault()
    calls = _fake_run_backlog(monkeypatch)

    status, payload = serve.apply_task_edit(vault, _conv(vault), "task-9", {"title": "x"}, "")

    assert status == 404
    assert calls == []
    assert "task-9" in payload["error"]


def test_apply_task_edit_rejects_a_non_numeric_id(monkeypatch, make_vault):
    vault = make_vault()
    calls = _fake_run_backlog(monkeypatch)

    status, payload = serve.apply_task_edit(vault, _conv(vault), "task-abc", {"title": "x"}, "")

    assert status == 400
    assert calls == []
    assert "bad task id" in payload["error"]


def test_apply_task_edit_rejects_a_malformed_patch_without_shelling_out(monkeypatch, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "First task")
    calls = _fake_run_backlog(monkeypatch)

    status, payload = serve.apply_task_edit(vault, _conv(vault), "task-1",
                                             {"ordinal": 5}, _card(vault)["hash"])

    assert status == 400
    assert calls == []
    assert "unsupported field" in payload["error"]


def test_apply_task_edit_surfaces_a_backlog_failure(monkeypatch, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "First task")
    card = _card(vault)
    _fake_run_backlog(monkeypatch, _Result(returncode=1, stderr="no such status"))

    status, payload = serve.apply_task_edit(vault, _conv(vault), "task-1",
                                             {"title": "Renamed"}, card["hash"])

    assert status == 400
    assert payload["error"] == "no such status"


def test_apply_task_edit_rejects_a_completed_task(monkeypatch, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "Shipped task", status="Done", completed=True)
    calls = _fake_run_backlog(monkeypatch)

    status, payload = serve.apply_task_edit(vault, _conv(vault), "task-1",
                                             {"title": "Renamed"}, "")

    assert status == 400
    assert calls == []
    assert "completed" in payload["error"]


# --------------------------------------------------------------------------- #
# create_task — the [[in-ui-creation]] New Task write: a bare kanban card with
# no wiki page, shelled through backlog.md like apply_task_move/
# apply_task_edit, so it uses the same `_fake_run_backlog`-style monkeypatch.
# Unlike those, success has to parse the real CLI's "File: <path>" stdout line
# and then read that file back for its id, so the happy-path fake points at
# an actual task file on disk rather than just returning an empty result.
# --------------------------------------------------------------------------- #

def test_create_task_happy_path_returns_lowercase_id(monkeypatch, make_vault, make_task):
    vault = make_vault()
    task_path = make_task(vault, 5, "New task", status="To Do")
    calls = []

    def _run(vault_root, argv, capture=False):
        calls.append(list(argv))
        return _Result(returncode=0, stdout=f"File: {task_path}\n")

    monkeypatch.setattr(lib, "run_backlog", _run)

    ok, result = serve.create_task(vault, "New task", "To Do", "tome", "high", "desc")

    assert ok is True
    assert result == "task-5"
    assert calls == [["task", "create", "New task", "-s", "To Do", "--plain",
                       "-d", "desc", "-l", "project:tome", "--priority", "high"]]


def test_create_task_requires_title(monkeypatch, make_vault):
    vault = make_vault()
    calls = _fake_run_backlog(monkeypatch)

    ok, message = serve.create_task(vault, "  ", "To Do", None, None, None)

    assert ok is False
    assert "title is required" in message
    assert calls == []


def test_create_task_requires_status(monkeypatch, make_vault):
    vault = make_vault()
    calls = _fake_run_backlog(monkeypatch)

    ok, message = serve.create_task(vault, "New task", "", None, None, None)

    assert ok is False
    assert "status is required" in message
    assert calls == []


def test_create_task_surfaces_backlog_failure(monkeypatch, make_vault):
    vault = make_vault()
    _fake_run_backlog(monkeypatch, _Result(returncode=1, stderr="no such status"))

    ok, message = serve.create_task(vault, "New task", "To Do", None, None, None)

    assert ok is False
    assert message == "no such status"


def test_create_task_missing_file_line_is_reported(monkeypatch, make_vault):
    vault = make_vault()

    def _run(vault_root, argv, capture=False):
        return _Result(returncode=0, stdout="created ok, no file line\n")

    monkeypatch.setattr(lib, "run_backlog", _run)

    ok, message = serve.create_task(vault, "New task", "To Do", None, None, None)

    assert ok is False
    assert "file path" in message


def test_create_task_missing_id_is_reported(monkeypatch, make_vault):
    vault = make_vault()
    task_path = vault / "backlog" / "tasks" / "task-x.md"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text("---\ntitle: X\n---\nbody\n", encoding="utf-8")

    def _run(vault_root, argv, capture=False):
        return _Result(returncode=0, stdout=f"File: {task_path}\n")

    monkeypatch.setattr(lib, "run_backlog", _run)

    ok, message = serve.create_task(vault, "New task", "To Do", None, None, None)

    assert ok is False
    assert "id could not be read" in message


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
    fm, body = lib.read_page(beta)
    lib.write_page(beta, fm, body + "\nSee [[alpha]] for context.\n")
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
    real = lib.run_all_lint_checks
    calls = {"n": 0}

    def fake(vault_root, conventions):
        pages, findings = real(vault_root, conventions)
        calls["n"] += 1
        if calls["n"] >= 2:
            findings = findings + [lib.Finding(lib.ERROR, "BROKEN_LINK",
                                                "tome/ideas/beta.md", "fabricated")]
        return pages, findings

    monkeypatch.setattr(lib, "run_all_lint_checks", fake)

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
    fm, _body = lib.read_page(vault / "wiki" / "tome" / "plans" / "my-plan.md")
    assert lib.fm_get(fm, "status") == "proposed"


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

    real = lib.run_all_lint_checks

    def fake(vault_root, conventions):
        pages, findings = real(vault_root, conventions)
        findings = findings + [lib.Finding(lib.ERROR, "BROKEN_LINK",
                                            "tome/ideas/my-idea.md", "fabricated")]
        return pages, findings

    monkeypatch.setattr(lib, "run_all_lint_checks", fake)

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


def test_export_static_index_json_has_no_abs_path(tmp_path, make_vault, make_page):
    # absPath is the author's machine leaking into a public artifact
    # ([[export-path-hygiene]]) — the export must carry no absolute
    # filesystem path for any page. Checked two ways: the field is gone
    # outright, and the vault's own absolute path (the concrete value
    # absPath would have held) doesn't appear anywhere in the file, so a
    # future field reintroducing it under another name is still caught.
    # (Not a bare "no leading /" scan — url/path fields are legitimately
    # root-relative, e.g. "/raw/tome/ideas/alpha.md".)
    import json
    import re

    vault = make_vault()
    make_page(vault, "tome/ideas/alpha.md", type="idea", title="Alpha")
    out_dir = tmp_path / "export"
    serve.export_static(vault, _conv(vault), out_dir)

    raw_text = (out_dir / "index.json").read_text(encoding="utf-8")
    index = json.loads(raw_text)
    assert index["pages"], "expected at least one page in the export"
    for page in index["pages"]:
        assert "absPath" not in page

    wiki_root = (vault / "wiki").resolve().as_posix()
    assert wiki_root not in raw_text
    assert not re.search(r"[A-Za-z]:[\\/]", raw_text), \
        "found what looks like a Windows absolute path in the export"


def test_export_static_board_json_is_read_only(tmp_path, make_vault):
    import json

    vault = make_vault()
    out_dir = tmp_path / "export"
    serve.export_static(vault, _conv(vault), out_dir)

    board = json.loads((out_dir / "board.json").read_text(encoding="utf-8"))
    assert board["writable"] is False


def test_export_static_board_json_includes_completed_cards(tmp_path, make_vault, make_task):
    # A static export is just a frozen board.json, so a completed task's
    # visibility there is a for-free consequence of build_board carrying it
    # ([[completed-tasks-viewable]]) — no export-specific handling needed.
    import json

    vault = make_vault()
    make_task(vault, 1, "Shipped task", status="Done", completed=True)
    out_dir = tmp_path / "export"
    serve.export_static(vault, _conv(vault), out_dir)

    board = json.loads((out_dir / "board.json").read_text(encoding="utf-8"))
    cards = {c["id"]: c for c in board["cards"]}
    assert cards["task-1"]["completed"] is True


# --------------------------------------------------------------------------- #
# Live reload ([[live-reload]]) — _tree_token and _ChangeWatcher are pure/
# thread-free by construction, so these exercise the diffing logic directly
# rather than starting a real server and opening a socket to /events.
# --------------------------------------------------------------------------- #

def test_tree_token_changes_on_create_edit_and_delete(tmp_path):
    root = tmp_path / "wiki"
    root.mkdir()
    empty = serve._tree_token(root)

    a = root / "a.md"
    a.write_text("one", encoding="utf-8")
    after_create = serve._tree_token(root)
    assert after_create != empty

    os.utime(a, (a.stat().st_mtime + 5, a.stat().st_mtime + 5))
    after_touch = serve._tree_token(root)
    assert after_touch != after_create

    a.unlink()
    after_delete = serve._tree_token(root)
    assert after_delete == empty


def test_tree_token_ignores_non_markdown_files(tmp_path):
    root = tmp_path / "wiki"
    root.mkdir()
    before = serve._tree_token(root)
    (root / "notes.txt").write_text("not markdown", encoding="utf-8")
    assert serve._tree_token(root) == before


def test_tree_token_missing_dir_is_empty_token(tmp_path):
    assert serve._tree_token(tmp_path / "does-not-exist") == ""


def test_change_watcher_poll_once_detects_wiki_and_board_separately(tmp_path):
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "backlog" / "tasks").mkdir(parents=True)
    watcher = serve._ChangeWatcher(vault)

    assert watcher.poll_once() == []  # nothing moved since __init__'s snapshot

    (vault / "wiki" / "a.md").write_text("x", encoding="utf-8")
    assert watcher.poll_once() == ["index"]
    assert watcher.poll_once() == []  # settled — no further diff on a repeat tick

    (vault / "backlog" / "tasks" / "task-1.md").write_text("x", encoding="utf-8")
    assert watcher.poll_once() == ["board"]


def test_change_watcher_detects_completed_dir_too(tmp_path):
    # A `tome done` moves a task's file into backlog/completed/ — the board
    # token must cover that half too, or a completion wouldn't push a live
    # reload ([[completed-tasks-viewable]]).
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "backlog" / "tasks").mkdir(parents=True)
    (vault / "backlog" / "completed").mkdir(parents=True)
    watcher = serve._ChangeWatcher(vault)

    assert watcher.poll_once() == []

    (vault / "backlog" / "completed" / "task-1.md").write_text("x", encoding="utf-8")
    assert watcher.poll_once() == ["board"]


def test_change_watcher_client_lifecycle(tmp_path):
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    watcher = serve._ChangeWatcher(vault)
    assert watcher.client_count() == 0

    q1 = watcher.register()
    q2 = watcher.register()
    assert watcher.client_count() == 2

    watcher.broadcast(["board"])
    assert q1.get_nowait() == ["board"]
    assert q2.get_nowait() == ["board"]

    watcher.unregister(q1)
    assert watcher.client_count() == 1
    watcher.broadcast(["index"])
    assert q2.get_nowait() == ["index"]
    assert q1.empty()  # no longer listening


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


# --------------------------------------------------------------------------- #
# HTTP layer ([[serve-http-tests]]) — everything above this line exercises the
# pure functions TomeHandler calls; this section is the layer above them:
# route matching, status codes, header emission, and body/error shaping, over
# a real running server. `start_server` binds TomeHandler to an ephemeral
# port on a real vault (class-level state, same pattern cmd_serve() itself
# uses) and tears every server it started down at the end of the test.
# --------------------------------------------------------------------------- #

@pytest.fixture
def start_server():
    servers = []

    def _start(vault, conventions=None):
        conv = conventions if conventions is not None else _conv(vault)
        serve.TomeHandler.vault_root = vault
        serve.TomeHandler.conventions = conv
        serve.TomeHandler.last_activity = time.monotonic()
        serve.TomeHandler.watcher = serve._ChangeWatcher(vault)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.TomeHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def _request(base_url, method, path, body=None):
    """A bare HTTP round-trip via http.client rather than urllib.request,
    which silently collapses `..` segments out of the path before the
    request ever leaves the process — exactly the thing the traversal tests
    below need to reach the handler."""
    parts = urlsplit(base_url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read(), resp.headers
    finally:
        conn.close()


def _get(base_url, path):
    return _request(base_url, "GET", path)


def _post_bytes(base_url, path, data):
    status, body, _headers = _request(base_url, "POST", path, body=data)
    return status, body


def _post(base_url, path, obj):
    return _post_bytes(base_url, path, json.dumps(obj).encode("utf-8"))


# -- GET ---------------------------------------------------------------- #

def test_get_index_json(start_server, make_vault, make_page):
    vault = make_vault()
    make_page(vault, "tome/ideas/alpha.md", type="idea", title="Alpha", desc="d")
    base = start_server(vault)

    status, body, headers = _get(base, "/index.json")

    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    data = json.loads(body)
    assert any(p["slug"] == "alpha" for p in data["pages"])


def test_get_board_json(start_server, make_vault, make_task):
    vault = make_vault()
    (vault / "backlog").mkdir(exist_ok=True)
    (vault / "backlog" / "config.yml").write_text(
        'default_status: "To Do"\nstatuses: ["To Do", "Done"]\n', encoding="utf-8")
    make_task(vault, 1, "First task")
    base = start_server(vault)

    status, body, headers = _get(base, "/board.json")

    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    data = json.loads(body)
    assert data["writable"] is True
    assert [c["id"] for c in data["cards"]] == ["task-1"]


def test_get_raw_page_returns_etag(start_server, make_vault, make_page):
    vault = make_vault()
    make_page(vault, "tome/ideas/alpha.md", type="idea", title="Alpha",
              body="\n# Alpha\n\nBody text.\n")
    base = start_server(vault)

    status, body, headers = _get(base, "/raw/tome/ideas/alpha.md")

    assert status == 200
    assert headers["Content-Type"] == "text/markdown; charset=utf-8"
    assert "Body text." in body.decode("utf-8")
    assert headers["ETag"] == hashlib.sha256(body).hexdigest()


def test_get_raw_page_missing_is_404(start_server, make_vault):
    vault = make_vault()
    base = start_server(vault)

    status, _body, _headers = _get(base, "/raw/tome/ideas/no-such.md")

    assert status == 404


def test_get_raw_rejects_path_traversal(start_server, make_vault):
    vault = make_vault()
    base = start_server(vault)

    status, _body, _headers = _get(base, "/raw/../../etc/passwd")

    assert status == 400


def test_get_app_statics(start_server, make_vault):
    vault = make_vault()
    base = start_server(vault)

    status, body, headers = _get(base, "/app/app.js")
    assert status == 200
    assert headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert len(body) > 0

    status, _body, headers = _get(base, "/app/styles.css")
    assert status == 200
    assert headers["Content-Type"] == "text/css; charset=utf-8"


def test_get_app_rejects_path_traversal(start_server, make_vault):
    vault = make_vault()
    base = start_server(vault)

    status, _body, _headers = _get(base, "/app/../../etc/passwd")

    assert status == 400


def test_get_root_serves_frontend_index(start_server, make_vault):
    vault = make_vault()
    base = start_server(vault)

    status, body, headers = _get(base, "/")

    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert b"<html" in body.lower()


def test_get_unknown_path_is_404(start_server, make_vault):
    vault = make_vault()
    base = start_server(vault)

    status, _body, _headers = _get(base, "/nope")

    assert status == 404


# -- POST: task move / edit / create ------------------------------------- #

def test_post_task_move_success(monkeypatch, start_server, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "First task", status="To Do")
    calls = _fake_run_backlog(monkeypatch)
    base = start_server(vault)

    status, body = _post(base, "/api/task/task-1/move", {"status": "Done", "afterId": None})

    assert status == 200
    data = json.loads(body)
    assert data["writable"] is True
    assert calls == [["task", "edit", "1", "-s", "Done", "--ordinal", "10000"]]


def test_post_task_move_missing_status_is_400(monkeypatch, start_server, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "First task")
    _fake_run_backlog(monkeypatch)
    base = start_server(vault)

    status, body = _post(base, "/api/task/task-1/move", {"status": ""})

    assert status == 400
    assert "error" in json.loads(body)


def test_post_malformed_json_body_is_400(start_server, make_vault):
    vault = make_vault()
    base = start_server(vault)

    status, body = _post_bytes(base, "/api/task/task-1/move", b"{not valid json")

    assert status == 400
    assert "error" in json.loads(body)


def test_post_task_edit_success(monkeypatch, start_server, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "First task")
    card = _card(vault)
    _fake_run_backlog(monkeypatch)
    base = start_server(vault)

    status, body = _post(base, "/api/task/task-1/edit",
                          {"patch": {"title": "Renamed"}, "baseHash": card["hash"]})

    assert status == 200
    assert json.loads(body)["writable"] is True


def test_post_task_edit_stale_hash_is_409(monkeypatch, start_server, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "First task")
    _fake_run_backlog(monkeypatch)
    base = start_server(vault)

    status, body = _post(base, "/api/task/task-1/edit",
                          {"patch": {"title": "Renamed"}, "baseHash": "stale"})

    data = json.loads(body)
    assert status == 409
    assert data["card"]["title"] == "First task"


def test_post_task_edit_missing_patch_is_400(monkeypatch, start_server, make_vault, make_task):
    vault = make_vault()
    make_task(vault, 1, "First task")
    _fake_run_backlog(monkeypatch)
    base = start_server(vault)

    status, body = _post(base, "/api/task/task-1/edit", {"baseHash": "irrelevant"})

    assert status == 400
    assert "error" in json.loads(body)


def test_post_task_create_success(monkeypatch, start_server, make_vault, make_task):
    vault = make_vault()
    task_path = make_task(vault, 9, "New task", status="To Do")

    def _run(vault_root, argv, capture=False):
        return _Result(returncode=0, stdout=f"File: {task_path}\n")

    monkeypatch.setattr(lib, "run_backlog", _run)
    base = start_server(vault)

    status, body = _post(base, "/api/task",
                          {"title": "New task", "status": "To Do", "project": "tome",
                           "priority": "high", "description": "d"})

    assert status == 200
    data = json.loads(body)
    assert data["taskId"] == "task-9"
    assert data["writable"] is True


def test_post_task_create_missing_title_is_400(start_server, make_vault):
    vault = make_vault()
    base = start_server(vault)

    status, body = _post(base, "/api/task", {"status": "To Do"})

    assert status == 400
    assert "error" in json.loads(body)


# -- POST: page / frontmatter / rename / new (needs a real git vault) ---- #

@pytestmark_git
def test_post_page_success(start_server, tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _scaffold_idea(vault, run_tome)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add alpha")
    _git(vault, "push")
    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    base = start_server(vault)

    status, body = _post(base, "/api/page", {"path": "tome/ideas/alpha.md",
                                              "body": "\n# Alpha\n\nEdited via HTTP.\n",
                                              "baseHash": base_hash})

    assert status == 200
    data = json.loads(body)
    assert "hash" in data
    assert "Edited via HTTP." in target.read_text(encoding="utf-8")


@pytestmark_git
def test_post_page_missing_fields_is_400(start_server, tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    base = start_server(vault)

    status, body = _post(base, "/api/page", {"path": "tome/ideas/alpha.md"})

    assert status == 400
    assert "error" in json.loads(body)


@pytestmark_git
def test_post_page_stale_hash_is_409(start_server, tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _scaffold_idea(vault, run_tome)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add alpha")
    _git(vault, "push")
    base = start_server(vault)

    status, body = _post(base, "/api/page", {"path": "tome/ideas/alpha.md",
                                              "body": "x", "baseHash": "stale"})

    data = json.loads(body)
    assert status == 409
    assert "currentHash" in data


@pytestmark_git
def test_post_frontmatter_success(start_server, tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    target = _scaffold_idea(vault, run_tome)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add alpha")
    _git(vault, "push")
    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    base = start_server(vault)

    status, body = _post(base, "/api/frontmatter",
                          {"path": "tome/ideas/alpha.md",
                           "fields": {"title": "Alpha Renamed"},
                           "baseHash": base_hash})

    assert status == 200
    data = json.loads(body)
    assert "hash" in data
    assert 'title: "Alpha Renamed"' in target.read_text(encoding="utf-8")


@pytestmark_git
def test_post_frontmatter_missing_fields_is_400(start_server, tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    base = start_server(vault)

    status, body = _post(base, "/api/frontmatter", {"path": "tome/ideas/alpha.md"})

    assert status == 400
    assert "error" in json.loads(body)


@pytestmark_git
def test_post_frontmatter_stale_hash_is_409(start_server, tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _scaffold_idea(vault, run_tome)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add alpha")
    _git(vault, "push")
    base = start_server(vault)

    status, body = _post(base, "/api/frontmatter",
                          {"path": "tome/ideas/alpha.md", "fields": {"title": "X"},
                           "baseHash": "stale"})

    data = json.loads(body)
    assert status == 409
    assert "currentHash" in data


@pytestmark_git
def test_post_rename_success(start_server, tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    alpha, _beta = _scaffold_two_ideas(vault, run_tome)
    base_hash = hashlib.sha256(alpha.read_bytes()).hexdigest()
    base = start_server(vault)

    status, body = _post(base, "/api/rename",
                          {"path": "tome/ideas/alpha.md", "newSlug": "gamma", "baseHash": base_hash})

    assert status == 200
    data = json.loads(body)
    assert data["slug"] == "gamma"
    assert data["url"] == "?page=gamma"


@pytestmark_git
def test_post_rename_missing_fields_is_400(start_server, tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    base = start_server(vault)

    status, body = _post(base, "/api/rename", {"path": "tome/ideas/alpha.md"})

    assert status == 400
    assert "error" in json.loads(body)


@pytestmark_git
def test_post_rename_stale_hash_is_409(start_server, tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    _scaffold_two_ideas(vault, run_tome)
    base = start_server(vault)

    status, body = _post(base, "/api/rename",
                          {"path": "tome/ideas/alpha.md", "newSlug": "gamma", "baseHash": "stale"})

    data = json.loads(body)
    assert status == 409
    assert "currentHash" in data


@pytestmark_git
def test_post_new_success(start_server, tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    run_tome("--vault", str(vault), "new", "project", "tome", "--title", "Tome", "--desc", "d")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "add project")
    _git(vault, "push")
    base = start_server(vault)

    status, body = _post(base, "/api/new",
                          {"type": "idea", "project": "tome", "slug": "my-idea",
                           "title": "My Idea", "description": "a fresh idea"})

    assert status == 200
    data = json.loads(body)
    assert data["slug"] == "my-idea"
    assert (vault / "wiki" / "tome" / "ideas" / "my-idea.md").is_file()


@pytestmark_git
def test_post_new_missing_fields_is_400(start_server, tmp_path, run_tome):
    vault, origin = _bootstrap_git_vault(tmp_path, run_tome)
    base = start_server(vault)

    status, body = _post(base, "/api/new", {"type": "idea"})

    assert status == 400
    assert "error" in json.loads(body)


# -- static export, served over HTTP by an ordinary static host ---------- #

def test_export_over_http_reports_writable_false_and_lacks_write_routes(tmp_path, make_vault):
    vault = make_vault()
    out_dir = tmp_path / "export"
    serve.export_static(vault, _conv(vault), out_dir)

    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(out_dir))
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        status, body, _headers = _get(base, "/board.json")
        assert status == 200
        assert json.loads(body)["writable"] is False

        # No server-side route handling behind a static host at all — a
        # write POST isn't refused by application logic, it's unsupported
        # by the file server itself.
        status, _body = _post(base, "/api/task/task-1/move", {"status": "Done"})
        assert status in (404, 501)
    finally:
        httpd.shutdown()
        httpd.server_close()
