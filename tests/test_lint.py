"""tome_lint: one focused test per finding code, plus parse_frontmatter edges."""

import tome
import tome_lint


def _write_raw(vault, rel_path, text):
    path = vault / "wiki" / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _lint(vault):
    conventions = tome.load_conventions(vault)
    wiki_root = vault / "wiki"
    index_path = wiki_root / conventions["index"]["file"]
    return tome_lint.run(wiki_root, conventions, index_path)


def _codes_for(findings, rel_path=None):
    if rel_path is None:
        return {f.code for f in findings}
    return {f.code for f in findings if f.path == rel_path}


def test_read_error(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    bad = vault / "wiki" / "proj" / "notes" / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"---\ntitle: \xff\xfe bad bytes\n---\nbody\n")

    pages, findings = _lint(vault)

    assert "READ_ERROR" in _codes_for(findings, "proj/notes/bad.md")


def test_duplicate_slug(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/notes/dup.md", type="concept", title="Dup 1")
    make_page(vault, "proj/plans/dup.md", type="plan", title="Dup 2")

    pages, findings = _lint(vault)

    dupes = [f for f in findings if f.code == "DUPLICATE_SLUG"]
    assert len(dupes) == 1
    assert "dup" in dupes[0].message


def test_broken_link(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/notes/linker.md", type="concept", title="Linker",
              body="\nSee [[does-not-exist]].\n")

    pages, findings = _lint(vault)

    assert "BROKEN_LINK" in _codes_for(findings, "proj/notes/linker.md")


def test_orphan(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/notes/lonely.md", type="concept", title="Lonely")

    pages, findings = _lint(vault)

    assert "ORPHAN" in _codes_for(findings, "proj/notes/lonely.md")


def test_missing_frontmatter(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    _write_raw(vault, "proj/notes/incomplete.md",
               '---\ntype: concept\ntitle: "Incomplete"\ntags: [proj]\n'
               'created: 2026-01-01\nupdated: 2026-01-01\n---\nbody\n')

    pages, findings = _lint(vault)

    missing = [f for f in findings
               if f.code == "MISSING_FRONTMATTER" and f.path == "proj/notes/incomplete.md"]
    assert missing
    assert "description" in missing[0].message


def test_malformed_frontmatter(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    _write_raw(vault, "proj/notes/broken.md", "---\ntype: concept\ntitle: no closing fence\nbody\n")

    pages, findings = _lint(vault)

    assert "MALFORMED_FRONTMATTER" in _codes_for(findings, "proj/notes/broken.md")


def test_oversize_soft(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/notes/big.md", type="concept", title="Big",
              body="\n" + "line\n" * 450)

    pages, findings = _lint(vault)

    assert "OVERSIZE_SOFT" in _codes_for(findings, "proj/notes/big.md")


def test_oversize_hard(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/notes/huge.md", type="concept", title="Huge",
              body="\n" + "line\n" * 850)

    pages, findings = _lint(vault)

    assert "OVERSIZE_HARD" in _codes_for(findings, "proj/notes/huge.md")


def test_bad_type(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/notes/weird.md", type="not-a-real-type", title="Weird")

    pages, findings = _lint(vault)

    assert "BAD_TYPE" in _codes_for(findings, "proj/notes/weird.md")


def test_bad_tag_and_project_name_tag_allowed(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/notes/ok.md", type="concept", title="Ok",
              tags=["proj"])
    make_page(vault, "proj/notes/bad.md", type="concept", title="Bad",
              tags=["not-a-real-tag"])

    pages, findings = _lint(vault)

    assert "BAD_TAG" not in _codes_for(findings, "proj/notes/ok.md")
    assert "BAD_TAG" in _codes_for(findings, "proj/notes/bad.md")


def test_plan_dir_terminal_status_outside_archive(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/plans/p1.md", type="plan", title="P1", status="done")

    pages, findings = _lint(vault)

    assert "PLAN_DIR" in _codes_for(findings, "proj/plans/p1.md")


def test_plan_dir_live_status_inside_archive(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/plans/archive/p1.md", type="plan", title="P1", status="active")

    pages, findings = _lint(vault)

    assert "PLAN_DIR" in _codes_for(findings, "proj/plans/archive/p1.md")


def test_index_missing(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "new", "plan", "p1",
             "--project", "proj", "--title", "P1", "--desc", "d")
    index_path = vault / "wiki" / "index.md"
    index_text = index_path.read_text(encoding="utf-8")
    stripped = "\n".join(line for line in index_text.splitlines() if "[[p1]]" not in line) + "\n"
    index_path.write_text(stripped, encoding="utf-8", newline="\n")

    pages, findings = _lint(vault)

    assert "INDEX_MISSING" in _codes_for(findings, "proj/plans/p1.md")


def test_index_broken(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    index_path = vault / "wiki" / "index.md"
    with index_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n- [[ghost-page]] — stray entry\n")

    pages, findings = _lint(vault)

    broken = [f for f in findings if f.code == "INDEX_BROKEN"]
    assert broken
    assert "ghost-page" in broken[0].message


def test_index_drift(make_vault, run_tome, capsys):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    index_path = vault / "wiki" / "index.md"
    with index_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("stray hand-edit\n")

    code = run_tome("--vault", str(vault), "lint", "--strict")

    assert code == 1
    assert "INDEX_DRIFT" in capsys.readouterr().out


def test_desc_too_long(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/notes/longdesc.md", type="concept", title="Long",
              desc="x" * 150)
    conventions = tome.load_conventions(vault)
    _, pages = tome.collect(vault, conventions)

    findings = tome.check_description_cap(pages, conventions)

    assert any(f.code == "DESC_TOO_LONG" and f.path == "proj/notes/longdesc.md"
               for f in findings)


def test_index_oversize_warns_past_soft_cap(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    conventions = tome.load_conventions(vault)
    conventions["index"]["soft_cap_lines"] = 5
    index_path = vault / "wiki" / "index.md"

    findings = tome.check_index_oversize(conventions, index_path)

    assert any(f.code == "INDEX_OVERSIZE" for f in findings)
    assert findings[0].severity == tome.WARNING


def test_index_oversize_missing_key_defaults(make_vault, run_tome):
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    conventions = tome.load_conventions(vault)
    del conventions["index"]["soft_cap_lines"]
    index_path = vault / "wiki" / "index.md"
    with index_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("x\n" * 500)

    findings = tome.check_index_oversize(conventions, index_path)

    assert any(f.code == "INDEX_OVERSIZE" and "cap 400" in f.message
               for f in findings)


# --------------------------------------------------------------------------- #
# parse_frontmatter edge cases
# --------------------------------------------------------------------------- #

def test_parse_frontmatter_inline_list():
    meta, body, malformed = tome_lint.parse_frontmatter(
        '---\ntags: [a, b, c]\n---\nbody\n')
    assert malformed is False
    assert meta["tags"] == ["a", "b", "c"]


def test_parse_frontmatter_block_list():
    meta, body, malformed = tome_lint.parse_frontmatter(
        '---\ntags:\n  - a\n  - b\n---\nbody\n')
    assert malformed is False
    assert meta["tags"] == ["a", "b"]


def test_parse_frontmatter_quoted_values():
    meta, body, malformed = tome_lint.parse_frontmatter(
        '---\ntitle: "Hello World"\n---\nbody\n')
    assert malformed is False
    assert meta["title"] == "Hello World"


def test_parse_frontmatter_empty_value():
    meta, body, malformed = tome_lint.parse_frontmatter(
        '---\ndescription:\n---\nbody\n')
    assert malformed is False
    assert meta["description"] == []


def test_parse_frontmatter_unparseable_block():
    meta, body, malformed = tome_lint.parse_frontmatter(
        '---\nno closing fence here\nmore text\n')
    assert malformed is True
    assert meta == {}


def test_parse_frontmatter_no_frontmatter_at_all():
    meta, body, malformed = tome_lint.parse_frontmatter("just a plain body\nno frontmatter\n")
    assert malformed is False
    assert meta == {}
    assert body == "just a plain body\nno frontmatter\n"
