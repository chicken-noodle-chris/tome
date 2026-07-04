"""wiki_search.py smoke tests — light coverage only, it's reworked in
search-and-rm; these just confirm the two basic paths don't quietly break."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import wiki_search  # noqa: E402


def _write_page(wiki_root, rel_path, *, title, body, tags=None):
    path = wiki_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tags_line = f"tags: [{', '.join(tags)}]\n" if tags else ""
    path.write_text(
        f'---\ntype: concept\ntitle: "{title}"\n{tags_line}'
        f'created: 2026-01-01\nupdated: 2026-01-01\n---\n{body}\n',
        encoding="utf-8", newline="\n",
    )
    return path


def test_query_returns_relevant_page_first(tmp_path):
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    _write_page(wiki_root, "proj/notes/relevant.md", title="Diffusion Training",
                body="This page is all about diffusion model training stability "
                     "and diffusion training tricks.")
    _write_page(wiki_root, "proj/notes/other.md", title="Unrelated",
                body="This page discusses gardening and cooking recipes.")

    pages = wiki_search.collect_pages(wiki_root)

    idx = wiki_search.build_bm25(pages)
    query_tokens = wiki_search.tokenize("diffusion training")
    scored = sorted(
        ((wiki_search.bm25_score(query_tokens, i, idx), i) for i in range(len(pages))),
        key=lambda x: -x[0],
    )
    top_page = pages[scored[0][1]]
    assert top_page["slug"] == "relevant"


def test_backlinks_finds_inbound_link(tmp_path):
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    _write_page(wiki_root, "proj/notes/target.md", title="Target", body="Target content.")
    _write_page(wiki_root, "proj/notes/linker.md", title="Linker",
                body="This links to [[target]] explicitly.")

    pages = wiki_search.collect_pages(wiki_root)
    inbound = [p for p in pages if "target" in p["links"]]

    assert len(inbound) == 1
    assert inbound[0]["slug"] == "linker"


def test_load_skip_lists_falls_back_when_no_conventions_file(tmp_path):
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()

    skip_files, skip_dirs = wiki_search.load_skip_lists(wiki_root)

    assert skip_files == wiki_search.DEFAULT_SKIP_FILES
    assert skip_dirs == wiki_search.DEFAULT_SKIP_DIRS


def test_load_skip_lists_reads_conventions_file(tmp_path):
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    (tmp_path / "conventions.toml").write_text(
        '[skip]\nfiles = ["SCHEMA.md", "index.md", "log.md", "README.md"]\n'
        'dirs = ["indexes"]\n',
        encoding="utf-8",
    )

    skip_files, skip_dirs = wiki_search.load_skip_lists(wiki_root)

    assert skip_files == {"SCHEMA.md", "index.md", "log.md", "README.md"}
    assert skip_dirs == {"indexes"}


def test_collect_pages_honors_conventions_skip_list(tmp_path):
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    (tmp_path / "conventions.toml").write_text(
        '[skip]\nfiles = ["README.md"]\ndirs = ["indexes"]\n', encoding="utf-8")
    _write_page(wiki_root, "README.md", title="Readme", body="Not a content page.")
    _write_page(wiki_root, "proj/notes/real.md", title="Real", body="Real content.")

    pages = wiki_search.collect_pages(wiki_root)

    slugs = {p["slug"] for p in pages}
    assert "real" in slugs
    assert "README" not in slugs
