"""tome inbox — schema-free capture notes in inbox/, never scanned by lint."""

from tome_cli import cli as tome


def test_inbox_writes_expected_name_and_content(make_vault, run_tome):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "inbox", "Remember: X does Y because Z")

    assert code == 0
    expected = vault / "inbox" / f"{tome.today()}-remember-x-does-y-because-z.md"
    assert expected.is_file()
    text = expected.read_text(encoding="utf-8")
    assert text.startswith(f"# {tome.today()} capture\n\n")
    assert "Remember: X does Y because Z" in text


def test_inbox_collision_suffixes(make_vault, run_tome):
    vault = make_vault()

    run_tome("--vault", str(vault), "inbox", "same note")
    run_tome("--vault", str(vault), "inbox", "same note")
    run_tome("--vault", str(vault), "inbox", "same note")

    base = vault / "inbox" / f"{tome.today()}-same-note.md"
    dup2 = vault / "inbox" / f"{tome.today()}-same-note-2.md"
    dup3 = vault / "inbox" / f"{tome.today()}-same-note-3.md"
    assert base.is_file() and dup2.is_file() and dup3.is_file()


def test_inbox_title_overrides_derived_slug(make_vault, run_tome):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "inbox", "the note body itself",
                     "--title", "Custom Title Here")

    assert code == 0
    expected = vault / "inbox" / f"{tome.today()}-custom-title-here.md"
    assert expected.is_file()
    assert "the note body itself" in expected.read_text(encoding="utf-8")


def test_inbox_multiline_note_preserved(make_vault, run_tome):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "inbox", "line one\nline two\nline three")

    assert code == 0
    matches = list((vault / "inbox").glob("*.md"))
    assert len(matches) == 1
    text = matches[0].read_text(encoding="utf-8")
    assert "line one\nline two\nline three" in text


def test_inbox_slug_handles_punctuation_and_unicode_without_crashing(make_vault, run_tome):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "inbox", "!!! éèê 中文 ??? -- ...")

    assert code == 0
    matches = list((vault / "inbox").glob("*.md"))
    assert len(matches) == 1
    # No ASCII alnum survives punctuation/unicode-only input: falls back cleanly.
    assert matches[0].name == f"{tome.today()}-capture.md"


def test_inbox_slug_truncated_to_max_chars(make_vault, run_tome):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "inbox",
                     "one two three four five six seven eight nine ten eleven")

    assert code == 0
    matches = list((vault / "inbox").glob("*.md"))
    assert len(matches) == 1
    slug = matches[0].stem.removeprefix(f"{tome.today()}-")
    assert len(slug) <= tome.INBOX_SLUG_MAX_CHARS


def test_lint_ignores_inbox_items(make_vault, run_tome, capsys):
    """inbox/ is a vault-root sibling of wiki/, not nested under it, so
    tome_lint's wiki_root.rglob("*.md") walk never reaches it — pinned here
    since capture-inbox-loop depends on that staying true."""
    vault = make_vault()
    run_tome("--vault", str(vault), "new", "project", "proj",
             "--title", "Proj", "--desc", "d")
    run_tome("--vault", str(vault), "inbox", "a stray capture with no frontmatter at all")
    capsys.readouterr()

    code = run_tome("--vault", str(vault), "lint")

    assert code == 0
    assert "inbox" not in capsys.readouterr().out
