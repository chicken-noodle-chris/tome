"""tome task — passthrough to a pinned backlog.md release."""

from tome_cli import lib


def test_task_pins_backlog_version(make_vault, run_tome, monkeypatch):
    vault = make_vault()
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr(lib.subprocess, "run", fake_run)

    code = run_tome("--vault", str(vault), "task", "list", "--plain")

    assert code == 0
    assert captured["cmd"][:3] == ["npx", "--yes", f"backlog.md@{lib.BACKLOG_VERSION}"]
    assert captured["cmd"][3:] == ["list", "--plain"]


# --------------------------------------------------------------------------- #
# The multi-line escape hatch ([[task-editing]]): npx reaches backlog.md
# through a `.cmd` shim on Windows, and a batch shim truncates an argument at
# its first newline — silently, with a zero exit code, dropping every later
# flag too. So an argv carrying a newline runs the resolved cli.js under
# `node` with no shell in the path, and refuses outright if it can't find it.
# --------------------------------------------------------------------------- #

def test_run_backlog_uses_npx_for_single_line_args(make_vault, monkeypatch):
    vault = make_vault()
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["shell"] = kwargs.get("shell")
        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr(lib.subprocess, "run", fake_run)
    lib.run_backlog(vault, ["task", "edit", "1", "-d", "one line"])

    assert captured["cmd"][0] == "npx"


def test_run_backlog_runs_node_directly_for_multiline_args(make_vault, monkeypatch, tmp_path):
    vault = make_vault()
    script = tmp_path / "cli.js"
    script.write_text("//", encoding="utf-8")
    monkeypatch.setattr(lib, "backlog_script", lambda refresh=True: script)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["shell"] = kwargs.get("shell")
        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr(lib.subprocess, "run", fake_run)
    lib.run_backlog(vault, ["task", "edit", "1", "--notes", "one\n\ntwo"])

    assert captured["cmd"] == ["node", str(script), "task", "edit", "1", "--notes", "one\n\ntwo"]
    # No shell anywhere in the path — that's the entire point.
    assert not captured["shell"]


def test_run_backlog_refuses_multiline_when_the_script_is_missing(make_vault, monkeypatch):
    vault = make_vault()
    monkeypatch.setattr(lib, "backlog_script", lambda refresh=True: None)

    def fake_run(cmd, **kwargs):
        raise AssertionError("nothing should be invoked when the script is unresolvable")

    monkeypatch.setattr(lib.subprocess, "run", fake_run)
    proc = lib.run_backlog(vault, ["task", "edit", "1", "--notes", "one\ntwo"], capture=True)

    # A loud failure, not a quietly truncated write.
    assert proc.returncode != 0
    assert "multi-line" in proc.stderr


def test_find_backlog_script_only_accepts_the_pinned_version(monkeypatch, tmp_path):
    cache = tmp_path / "npm-cache"
    for version in (lib.BACKLOG_VERSION, "9.9.9"):
        pkg = cache / "_npx" / version.replace(".", "") / "node_modules" / "backlog.md"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(f'{{"version": "{version}"}}', encoding="utf-8")
        (pkg / "cli.js").write_text("//", encoding="utf-8")

    class Result:
        returncode = 0
        stdout = str(cache)

    monkeypatch.setattr(lib.subprocess, "run", lambda *a, **k: Result())
    found = lib._find_backlog_script()

    assert found is not None
    assert lib.BACKLOG_VERSION.replace(".", "") in found.parts
