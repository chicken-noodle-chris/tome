"""tome task — passthrough to a pinned backlog.md release."""

import tome


def test_task_pins_backlog_version(make_vault, run_tome, monkeypatch):
    vault = make_vault()
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr(tome.subprocess, "run", fake_run)

    code = run_tome("--vault", str(vault), "task", "list", "--plain")

    assert code == 0
    assert captured["cmd"][:3] == ["npx", "--yes", f"backlog.md@{tome.BACKLOG_VERSION}"]
    assert captured["cmd"][3:] == ["list", "--plain"]
