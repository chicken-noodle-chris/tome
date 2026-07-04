"""`tome init` — vault scaffolding."""

import subprocess


def test_init_scaffolds_expected_layout(tmp_path, run_tome):
    target = tmp_path / "vault"
    code = run_tome("init", str(target))
    assert code == 0

    for rel in (
        "conventions.toml", ".gitignore", "CLAUDE.md",
        "quartz.config.yaml", "quartz.lock.json",
        "wiki/SCHEMA.md", "wiki/index.md", "wiki/log.md",
    ):
        assert (target / rel).is_file(), rel

    assert (target / "inbox").is_dir()
    assert (target / "raw" / "assets").is_dir()
    assert (target / ".git").is_dir()


def test_init_defaults_to_cwd(tmp_path, run_tome, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = run_tome("init")
    assert code == 0
    assert (tmp_path / "conventions.toml").is_file()


def test_init_writes_log_header_and_first_entry(make_vault):
    vault = make_vault()
    log_text = (vault / "wiki" / "log.md").read_text(encoding="utf-8")
    assert "# Wiki Log" in log_text
    assert "init | Vault created via `tome init`" in log_text


def test_init_rebuilds_empty_index(make_vault):
    vault = make_vault()
    index_text = (vault / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "# Wiki Index" in index_text


def test_init_reuses_existing_git_repo(tmp_path, run_tome):
    target = tmp_path / "vault"
    target.mkdir()
    subprocess.run(["git", "init"], cwd=str(target), check=True,
                    capture_output=True)
    code = run_tome("init", str(target))
    assert code == 0
    assert (target / ".git").is_dir()


def test_init_refuses_nonempty_target(tmp_path, run_tome, capsys):
    target = tmp_path / "vault"
    target.mkdir()
    (target / "conventions.toml").write_text("existing", encoding="utf-8")

    code = run_tome("init", str(target))

    assert code == 1
    err = capsys.readouterr().err
    assert "refusing to init" in err
    assert "conventions.toml" in err


def test_init_is_idempotent_failure_not_partial_write(tmp_path, run_tome):
    """A refused init must not have scaffolded anything else either."""
    target = tmp_path / "vault"
    target.mkdir()
    (target / "wiki").mkdir()
    (target / "wiki" / "index.md").write_text("existing", encoding="utf-8")

    code = run_tome("init", str(target))

    assert code == 1
    assert not (target / "conventions.toml").exists()
