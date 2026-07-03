# tome

A commonplace book for you and your agents — where your project lore lives.

`tome` is the tooling: a small stdlib CLI, five Claude Code skills, a
sync-reminder hook, and a Quartz browse-view bootstrap, all shipped as one
Claude Code plugin. A **vault** is the content: your own private repo of
`wiki/`, `backlog/`, `raw/`, and `inbox/`, following the conventions this
plugin enforces. One copy of the tooling; as many vaults as you want.

Seeded from Andrej Karpathy's "LLM Wiki" pattern — immutable sources, an
agent-owned wiki, a schema doc — turned into something installable.

## Install

Add this repo as a marketplace and install the plugin:

```
claude plugin marketplace add chicken-noodle-chris/tome
claude plugin install tome@tome
```

(Working from a local clone instead? Point the marketplace at the clone's
path rather than the GitHub slug.)

## Start a vault

In an empty directory (or a fresh repo):

```
tome init
```

This scaffolds `conventions.toml`, `wiki/SCHEMA.md`, an empty `wiki/index.md`,
`wiki/log.md`, `inbox/`, `raw/assets/`, a vault `.gitignore`, a `CLAUDE.md`
primer, and default Quartz config + lockfile. It runs `git init` if the
target isn't already a repo, and fails loudly rather than merging into a
non-empty target.

Next steps it'll print: author a first project page, bootstrap the browse
view (init prints the exact command with the plugin's real path), set up a remote, and
`tome sync`.

## Everyday commands

Skills (triggered by asking, not slash commands): `pickup-task`,
`write-a-plan`, `retrospect`, `ingest`, `query`.

CLI (`tome help` for the full list with examples):

```
tome new <type> <slug> --project <name> --title "T" --desc "..."
tome lint [--strict]
tome sync [-m "message"]
tome set-status <slug> <status>
tome task <args...>       # passthrough to backlog.md
```

Root resolution for the CLI: `--vault PATH`, else walk up from cwd looking
for `conventions.toml`, else `$VAULT_ROOT` — the vault you're standing in
always beats the global default, and `$VAULT_ROOT` covers sessions in
non-vault directories.

## Browse view

`quartz/` is gitignored inside a vault — a derived build tree, not vault
content. Bootstrap it once per vault:

```
python <path-to-this-repo>/scripts/setup_quartz.py
cd quartz && npx quartz build --serve
```

(`tome init` prints the first command with the real path filled in; agents
reach it via `$CLAUDE_PLUGIN_ROOT`, terminal humans via `$TOME` — see below.)

The first command clones [Quartz](https://github.com/jackyzha0/quartz)
(pinned to a known-good commit), wires the vault's `wiki/` in as its content
source, and installs the plugins pinned in the vault's `quartz.lock.json`;
safe to re-run any time. The second serves the site locally.

## Human CLI access (optional)

Agents invoke `tome` via `$CLAUDE_PLUGIN_ROOT` automatically — nothing to set
up. If you also want to run `tome` yourself from a terminal, clone this repo
and point a persistent `TOME` env var at it, e.g. on Windows PowerShell:

```
$env:TOME = "$HOME\Development\tome"
function tome { python "$env:TOME\scripts\tome.py" @args }
```

(add both lines to your `$PROFILE` to persist across sessions). A packaged
install (`uv tool install git+https://github.com/chicken-noodle-chris/tome.git`,
with `pipx` as the works-too fallback) is future scope — not needed today.

## Repo layout

```
tome/
├─ scripts/           tome.py (CLI), tome_lint.py, wiki_search.py, setup_quartz.py
├─ skills/            pickup-task, write-a-plan, retrospect, ingest, query
├─ hooks/             Stop hook: reminds you to sync a dirty vault
├─ templates/         scaffolding sources for `tome init`
└─ .claude-plugin/    plugin + marketplace manifest
```

## Out of scope (for now)

- `pyproject.toml` / pipx packaging (see "Human CLI access" above).
- A multi-vault registry — the CLI's root-resolution seam supports it, but
  registration itself waits for a real need.
- Non-Windows hook portability: hooks invoke `python`, which is correct on
  Windows; stock macOS may need `python3` there instead.

MIT licensed.
