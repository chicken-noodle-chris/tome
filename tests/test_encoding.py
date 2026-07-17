"""main()'s Windows console UTF-8 guard (task-57 AC #5): stock Windows
consoles default to a legacy code page that can't encode HELP_TEXT's
em-dashes, so main() reconfigures stdout/stderr to UTF-8 before doing
anything else, on Windows only, and only when the stream supports it."""

import io
import sys

import pytest


@pytest.fixture
def run_tome(monkeypatch):
    from tome_cli import cli as tome

    def _run(*args):
        monkeypatch.setattr(sys, "argv", ["tome", *args])
        return tome.main()
    return _run


def test_help_runs_clean_on_non_windows(run_tome, capsys):
    # sys.platform on the test host is never "win32" in this suite (CI runs
    # Linux/macOS) — the guard must be a no-op there, not a crash.
    code = run_tome("help")
    out = capsys.readouterr().out
    assert code == 0
    assert "—" in out  # HELP_TEXT's em-dashes render as real characters


def test_windows_reconfigures_capable_streams(run_tome, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    fake_out = io.StringIO()
    fake_err = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)
    # io.StringIO has no .reconfigure() — the guard must skip it rather
    # than crash on AttributeError, same as any other stream lacking it.
    assert not hasattr(fake_out, "reconfigure")

    code = run_tome("help")

    assert code == 0
    assert "—" in fake_out.getvalue()


def test_windows_reconfigures_real_text_stream(run_tome, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    path = tmp_path / "out.txt"
    stream = open(path, "w", encoding="cp1252", newline="")
    original_reconfigure = stream.reconfigure

    def _tracking_reconfigure(**kwargs):
        calls.append(kwargs)
        return original_reconfigure(**kwargs)

    monkeypatch.setattr(stream, "reconfigure", _tracking_reconfigure)
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    try:
        code = run_tome("help")
    finally:
        stream.close()

    assert code == 0
    assert {"encoding": "utf-8"} in calls
    assert "—".encode("utf-8") in path.read_bytes()
