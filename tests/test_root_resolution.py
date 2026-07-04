"""resolve_vault_root: --vault beats walk-up beats VAULT_ROOT."""

import pytest

from tome_cli import cli as tome


def test_explicit_vault_wins(make_vault, tmp_path, monkeypatch):
    vault = make_vault()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    monkeypatch.delenv("VAULT_ROOT", raising=False)

    resolved = tome.resolve_vault_root(str(vault))

    assert resolved == vault.resolve()


def test_explicit_vault_must_have_conventions(tmp_path):
    not_a_vault = tmp_path / "not-a-vault"
    not_a_vault.mkdir()

    with pytest.raises(tome.VaultError, match="conventions.toml"):
        tome.resolve_vault_root(str(not_a_vault))


def test_walk_up_finds_parent_vault(make_vault, monkeypatch):
    vault = make_vault()
    nested = vault / "wiki" / "some" / "deep" / "dir"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.delenv("VAULT_ROOT", raising=False)

    resolved = tome.resolve_vault_root(None)

    assert resolved == vault.resolve()


def test_walk_up_beats_vault_root_env(make_vault, tmp_path, monkeypatch):
    vault = make_vault("real-vault")
    decoy = make_vault("decoy-vault")
    monkeypatch.chdir(vault)
    monkeypatch.setenv("VAULT_ROOT", str(decoy))

    resolved = tome.resolve_vault_root(None)

    assert resolved == vault.resolve()


def test_vault_root_env_used_when_no_walk_up_match(make_vault, tmp_path, monkeypatch):
    vault = make_vault()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("VAULT_ROOT", str(vault))

    resolved = tome.resolve_vault_root(None)

    assert resolved == vault.resolve()


def test_vault_root_env_must_have_conventions(tmp_path, monkeypatch):
    not_a_vault = tmp_path / "not-a-vault"
    not_a_vault.mkdir()
    monkeypatch.chdir(not_a_vault)
    monkeypatch.setenv("VAULT_ROOT", str(not_a_vault))

    with pytest.raises(tome.VaultError, match="conventions.toml"):
        tome.resolve_vault_root(None)


def test_nothing_resolves_fails_loud(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.delenv("VAULT_ROOT", raising=False)

    with pytest.raises(tome.VaultError, match="no vault found"):
        tome.resolve_vault_root(None)
