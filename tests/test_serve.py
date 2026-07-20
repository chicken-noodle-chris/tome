"""Contract tests for `tome serve`'s two generated JSON payloads.

The server internals (routing, static file serving) are the rough,
harden-later part of the Phase 1 foundation slice; the *shapes* of
`build_index` and `build_board` are the deliberate, permanent contract the
frontend and any future static-export path depend on, so those are what's
locked here.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tome_cli import cli as tome  # noqa: E402
from tome_cli import serve  # noqa: E402


def _conv(vault):
    return tome.load_conventions(vault)


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
# apply_task_status — the one write `tome serve` accepts, always shelled
# through backlog.md ([[kanban-render-side]]). Tests fake out
# tome.run_backlog rather than shelling out to the real npx CLI, same
# pattern as test_start_done.py's fake_backlog.
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


def test_apply_task_status_strips_task_prefix(monkeypatch, make_vault):
    vault = make_vault()
    calls = _fake_run_backlog(monkeypatch)

    ok, message = serve.apply_task_status(vault, "TASK-64", "In Progress")

    assert (ok, message) == (True, "")
    assert calls == [["task", "edit", "64", "-s", "In Progress"]]


def test_apply_task_status_accepts_lowercase_id(monkeypatch, make_vault):
    vault = make_vault()
    calls = _fake_run_backlog(monkeypatch)

    ok, _ = serve.apply_task_status(vault, "task-7", "Done")

    assert ok is True
    assert calls == [["task", "edit", "7", "-s", "Done"]]


def test_apply_task_status_rejects_non_numeric_id(monkeypatch, make_vault):
    vault = make_vault()
    calls = _fake_run_backlog(monkeypatch)

    ok, message = serve.apply_task_status(vault, "not-a-task", "Done")

    assert ok is False
    assert "bad task id" in message
    assert calls == []  # never shells out for an invalid id


def test_apply_task_status_rejects_empty_status(monkeypatch, make_vault):
    vault = make_vault()
    calls = _fake_run_backlog(monkeypatch)

    ok, message = serve.apply_task_status(vault, "task-1", "")

    assert ok is False
    assert "status is required" in message
    assert calls == []


def test_apply_task_status_surfaces_backlog_failure(monkeypatch, make_vault):
    vault = make_vault()
    _fake_run_backlog(monkeypatch, _Result(returncode=1, stderr="no such task"))

    ok, message = serve.apply_task_status(vault, "task-1", "Done")

    assert ok is False
    assert message == "no such task"


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
