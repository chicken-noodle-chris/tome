"""generate_index — golden-string coverage over a hand-built page set."""

from tome_cli import cli as tome


def _conventions(vault):
    return tome.load_conventions(vault)


def _pages(vault):
    conventions = _conventions(vault)
    wiki_root, pages = tome.collect(vault, conventions)
    return wiki_root, pages, conventions


def test_hub_line_uses_title_as_alias(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proja/proja.md", type="project", title="Proj A",
              desc="Project A hub")
    wiki_root, pages, conventions = _pages(vault)

    text = tome.generate_index(pages, conventions, wiki_root)

    assert "## Proja" in text
    assert "- [[proja|Proj A]] — Project A hub" in text


def test_plans_live_vs_archived_grouping(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proja/proja.md", type="project", title="Proj A", desc="hub")
    make_page(vault, "proja/plans/live1.md", type="plan", status="active",
              desc="Live plan desc")
    make_page(vault, "proja/plans/archive/old1.md", type="plan", status="done",
              desc="Archived plan desc")
    wiki_root, pages, conventions = _pages(vault)

    text = tome.generate_index(pages, conventions, wiki_root)

    assert "**Plans — live:**\n- [[live1]] — Live plan desc" in text
    assert "**Plans — archived:**\n- [[old1]] — Archived plan desc" in text


def test_ideas_live_vs_archived_grouping(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proja/proja.md", type="project", title="Proj A", desc="hub")
    make_page(vault, "proja/ideas/idea1.md", type="idea", desc="Idea desc")
    make_page(vault, "proja/ideas/archive/idea2.md", type="idea",
              desc="Archived idea desc")
    wiki_root, pages, conventions = _pages(vault)

    text = tome.generate_index(pages, conventions, wiki_root)

    assert "**Ideas:**\n- [[idea1]] — Idea desc" in text
    assert "**Ideas — archived:**\n- [[idea2]] — Archived idea desc" in text


def test_cross_cutting_ideas_and_general_sections(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proja/proja.md", type="project", title="Proj A", desc="hub")
    make_page(vault, "ideas/cross1.md", type="idea", desc="Cross idea desc")
    make_page(vault, "general/note1.md", type="concept", desc="General note desc")
    wiki_root, pages, conventions = _pages(vault)

    text = tome.generate_index(pages, conventions, wiki_root)

    assert "## Ideas (cross-cutting)" in text
    assert "- [[cross1]] — Cross idea desc" in text
    assert "## General" in text
    assert "- [[note1]] — General note desc" in text


def test_no_description_fallback(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proja/proja.md", type="project", title="Proj A", desc="hub")
    make_page(vault, "proja/reports/report1.md", type="report", desc="")
    wiki_root, pages, conventions = _pages(vault)

    text = tome.generate_index(pages, conventions, wiki_root)

    assert "- [[report1]] — (no description)" in text


def test_stable_sort_order_projects_and_pages(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "projb/projb.md", type="project", title="Proj B", desc="hub b")
    make_page(vault, "proja/proja.md", type="project", title="Proj A", desc="hub a")
    make_page(vault, "proja/ideas/zeta.md", type="idea", desc="zeta desc")
    make_page(vault, "proja/ideas/alpha.md", type="idea", desc="alpha desc")
    wiki_root, pages, conventions = _pages(vault)

    text = tome.generate_index(pages, conventions, wiki_root)

    assert text.index("## Proja") < text.index("## Projb")
    assert text.index("[[alpha]]") < text.index("[[zeta]]")


def test_single_trailing_newline(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proja/proja.md", type="project", title="Proj A", desc="hub")
    make_page(vault, "proja/ideas/idea1.md", type="idea", desc="Idea desc")
    wiki_root, pages, conventions = _pages(vault)

    text = tome.generate_index(pages, conventions, wiki_root)

    assert text.endswith("\n")
    assert not text.endswith("\n\n")


def test_empty_wiki_still_has_cross_cutting_and_general_headers(make_vault):
    vault = make_vault()
    wiki_root, pages, conventions = _pages(vault)

    text = tome.generate_index(pages, conventions, wiki_root)

    assert "## Ideas (cross-cutting)" in text
    assert "## General" in text
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
