"""TOME_OPS_PROFILE — the single dispatch-point guard restricting a headless
remote deployment's command surface. The enumeration test is the load-bearing
one: it walks argparse's live subcommand table rather than a hand-copied
list, so a command added later without touching OPS_PROFILES is proven
guarded by default instead of trusted to be."""

from tome_cli import cli as tome

READ_CAPTURE_ALLOWED = {"search", "prime", "doctor", "help", "inbox"}


def test_read_capture_allowlist_matches_plan():
    assert tome.OPS_PROFILES["read-capture"] == frozenset(READ_CAPTURE_ALLOWED)


def test_every_registered_command_is_accounted_for():
    all_commands = tome.all_registered_commands()
    assert all_commands  # sanity: introspection actually found the subcommands
    assert READ_CAPTURE_ALLOWED <= all_commands


def test_no_profile_allows_everything(monkeypatch):
    monkeypatch.delenv("TOME_OPS_PROFILE", raising=False)
    for command in tome.all_registered_commands():
        assert tome.enforce_ops_profile(command) is None


def test_read_capture_blocks_every_non_allowlisted_command(monkeypatch):
    monkeypatch.setenv("TOME_OPS_PROFILE", "read-capture")
    for command in tome.all_registered_commands():
        result = tome.enforce_ops_profile(command)
        if command in READ_CAPTURE_ALLOWED or command in tome.ALWAYS_ALLOWED_COMMANDS:
            assert result is None, command
        else:
            assert result == 1, command


def test_help_and_doctor_always_allowed_even_under_unknown_profile(monkeypatch):
    monkeypatch.setenv("TOME_OPS_PROFILE", "not-a-real-profile")
    assert tome.enforce_ops_profile("help") is None
    assert tome.enforce_ops_profile("doctor") is None


def test_unknown_profile_blocks_everything_else(monkeypatch, capsys):
    monkeypatch.setenv("TOME_OPS_PROFILE", "not-a-real-profile")
    assert tome.enforce_ops_profile("sync") == 1
    assert "unknown TOME_OPS_PROFILE" in capsys.readouterr().err


def test_read_capture_wired_into_main_blocks_new(run_tome, make_vault, monkeypatch, capsys):
    vault = make_vault()
    monkeypatch.setenv("TOME_OPS_PROFILE", "read-capture")

    code = run_tome("--vault", str(vault), "new", "idea", "x",
                     "--title", "T", "--desc", "d")

    assert code == 1
    assert "read-capture" in capsys.readouterr().err


def test_read_capture_wired_into_main_allows_inbox(run_tome, make_vault, monkeypatch, capsys):
    vault = make_vault()
    monkeypatch.setenv("TOME_OPS_PROFILE", "read-capture")

    code = run_tome("--vault", str(vault), "inbox", "a capture note")

    assert code == 0
    assert list((vault / "inbox").glob("*.md"))
