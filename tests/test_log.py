"""tome log — appends a formatted entry to wiki/log.md."""

import tome


def test_log_appends_formatted_entry(make_vault, run_tome):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "log", "work-started", "Began task-1")

    assert code == 0
    log_text = (vault / "wiki" / "log.md").read_text(encoding="utf-8")
    assert f"## [{tome.today()}] work-started | Began task-1" in log_text


def test_log_appends_body_when_given(make_vault, run_tome):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "log", "note", "Headline",
                     "--body", "Extra detail on its own paragraph.")

    assert code == 0
    log_text = (vault / "wiki" / "log.md").read_text(encoding="utf-8")
    assert "## " in log_text and "note | Headline" in log_text
    assert "Extra detail on its own paragraph." in log_text


def test_log_rejects_op_not_in_vocabulary(make_vault, run_tome, capsys):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "log", "not-a-real-op", "message")

    assert code == 1
    assert "not in" in capsys.readouterr().err


def test_log_rejects_message_over_cap(make_vault, run_tome, capsys):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "log", "note", "x" * 501)

    assert code == 1
    assert "cap 500" in capsys.readouterr().err


def test_log_rejects_multiline_message(make_vault, run_tome, capsys):
    vault = make_vault()

    code = run_tome("--vault", str(vault), "log", "note", "line one\nline two")

    assert code == 1
    assert "single line" in capsys.readouterr().err
