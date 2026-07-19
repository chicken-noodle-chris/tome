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
