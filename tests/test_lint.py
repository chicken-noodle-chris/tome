"""tome_lint: one focused test per finding code, plus parse_frontmatter edges."""

from tome_cli import cli as tome
from tome_cli import lint as tome_lint


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


def test_idea_dir_type_idea_outside_ideas_folder(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/notes/stray-idea.md", type="idea", title="Stray")

    pages, findings = _lint(vault)

    assert "IDEA_DIR" in _codes_for(findings, "proj/notes/stray-idea.md")


def test_idea_dir_non_idea_inside_ideas_folder(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/ideas/not-an-idea.md", type="concept", title="Not an idea")

    pages, findings = _lint(vault)

    assert "IDEA_DIR" in _codes_for(findings, "proj/ideas/not-an-idea.md")


def test_idea_dir_silent_when_placement_matches(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/ideas/ok-idea.md", type="idea", title="OK")
    make_page(vault, "proj/ideas/archive/archived-idea.md", type="idea", title="Archived")

    pages, findings = _lint(vault)

    assert "IDEA_DIR" not in _codes_for(findings, "proj/ideas/ok-idea.md")
    assert "IDEA_DIR" not in _codes_for(findings, "proj/ideas/archive/archived-idea.md")


def test_idea_dir_silent_for_cross_cutting_ideas(make_vault, make_page):
    """wiki/ideas/ (no project) is the cross-cutting group, not a project's
    own ideas/ subfolder — parts[0] == 'ideas' must count as in-folder too."""
    vault = make_vault()
    make_page(vault, "ideas/cross-cutting-idea.md", type="idea", title="X")
    make_page(vault, "ideas/archive/archived-cross-cutting-idea.md", type="idea", title="Y")

    pages, findings = _lint(vault)

    assert "IDEA_DIR" not in _codes_for(findings, "ideas/cross-cutting-idea.md")
    assert "IDEA_DIR" not in _codes_for(findings, "ideas/archive/archived-cross-cutting-idea.md")


def test_idea_dir_is_warning_not_error(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/notes/stray-idea.md", type="idea", title="Stray")

    pages, findings = _lint(vault)

    finding = next(f for f in findings if f.code == "IDEA_DIR")
    assert finding.severity == tome_lint.WARNING


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


def test_unparsed_frontmatter_nested_map(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/notes/nested.md", type="concept", title="Nested",
              extra_fm=["meta:", "  key: value"])

    pages, findings = _lint(vault)

    assert "UNPARSED_FRONTMATTER" in _codes_for(findings, "proj/notes/nested.md")


def test_unparsed_frontmatter_multiline_scalar(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/notes/multiline.md", type="concept", title="Multiline",
              extra_fm=["notes: |", "  Some", "  Text"])

    pages, findings = _lint(vault)

    assert "UNPARSED_FRONTMATTER" in _codes_for(findings, "proj/notes/multiline.md")


def test_unparsed_frontmatter_tab_indented_list_item(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/notes/tabbed.md", type="concept", title="Tabbed",
              extra_fm=["aliases:", "\t- one"])

    pages, findings = _lint(vault)

    assert "UNPARSED_FRONTMATTER" in _codes_for(findings, "proj/notes/tabbed.md")


def test_unparsed_frontmatter_comment_line(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/notes/commented.md", type="concept", title="Commented",
              extra_fm=["# a comment"])

    pages, findings = _lint(vault)

    assert "UNPARSED_FRONTMATTER" in _codes_for(findings, "proj/notes/commented.md")


def test_subset_forms_all_parse_clean(make_vault, make_page):
    vault = make_vault()
    make_page(vault, "proj/proj.md", type="project", title="Proj")
    make_page(vault, "proj/notes/clean.md", type="concept", title="Clean",
              extra_fm=["aliases: [a, b]", "authors:", "  - Alice", "  - Bob"])

    pages, findings = _lint(vault)

    assert "UNPARSED_FRONTMATTER" not in _codes_for(findings, "proj/notes/clean.md")


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


def test_parse_frontmatter_quoted_value_keeps_colon_and_comma():
    meta, body, malformed = tome_lint.parse_frontmatter(
        '---\ntitle: "Ratio: 3, 2, 1"\n---\nbody\n')
    assert malformed is False
    assert meta["title"] == "Ratio: 3, 2, 1"


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


# --------------------------------------------------------------------------- #
# write_page round-trip guard
# --------------------------------------------------------------------------- #

def test_write_page_rejects_frontmatter_that_cannot_round_trip(tmp_path):
    path = tmp_path / "bad.md"
    # A bare "---" line would prematurely close the frontmatter block when
    # write_page appends its own closing fence, silently swallowing
    # "other: bad" into the body — write_page must refuse this outright.
    bad_fm_lines = ["title: test", "---", "other: bad"]

    try:
        tome.write_page(path, bad_fm_lines, "\nbody\n")
        assert False, "expected VaultError"
    except tome.VaultError:
        pass
    assert not path.exists()


def test_write_page_accepts_valid_frontmatter(tmp_path):
    path = tmp_path / "good.md"
    fm_lines = ['title: "Good"', "tags: [a, b]"]

    tome.write_page(path, fm_lines, "\nbody\n")

    assert path.is_file()
    assert '"Good"' in path.read_text(encoding="utf-8")
