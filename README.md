# tome

A commonplace book for you and your agents — where your project lore lives.

`tome` is the tooling: a small stdlib CLI, six Claude Code skills, a
SessionStart vault-context hook and a scoped Stop sync-reminder hook, and a
Quartz browse-view bootstrap, all shipped as one Claude Code plugin. A
**vault** is the content: your own private repo of
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
`write-a-plan`, `retrospect`, `ingest`, `query`, `capture`.

CLI (`tome help` for the full list with examples; write commands all take
`--sync` to commit+push just the files they touched):

```
tome prime [project] [--full]     # session orientation; --full adds SCHEMA, index, project context
tome start <slug-or-task-id>      # work-started ritual: statuses, log, sync, prints working context
tome done <plan-slug> [--summary "..."]   # close-out ritual: archive plan, complete task, log, sync
tome new <type> <slug> --project <name> --title "T" --desc "..." [--with-task "T"]
tome set-status <slug> <status>   # plan/decision lifecycle; moves plans to/from plans/archive/
tome archive <slug> [--restore]   # status-less pages (ideas, reports, ...) to/from archive/
tome search "<query>" [--top N]   # BM25 fallback search; also --backlinks, --top-linked
tome rm <slug> [--force]          # delete a page; refuses hubs/linked pages by default
tome inbox "<note>" [--title "T"]   # schema-free capture; retrospect triages it later
tome lint [--strict]
tome sync [<slug-or-task-id>...] [-m "message"]   # pull always; scoped commit when entities given
tome task <args...>       # passthrough to backlog.md
tome doctor               # environment + vault health check, ok/warn/FAIL per line
```

Root resolution for the CLI: `--vault PATH`, else walk up from cwd looking
for `conventions.toml`, else `$VAULT_ROOT` — the vault you're standing in
always beats the global default, and `$VAULT_ROOT` covers sessions in
non-vault directories.

## Headless bootstrap

A container with no human at the keyboard — an agentigrator Cloud Run
instance, a Claude Code cloud session, any headless consumer — clones a
vault, installs tome, and operates it safely with three env vars:

```
uv tool install git+https://github.com/chicken-noodle-chris/tome.git
git clone <vault-remote-url> /path/to/vault   # deploy key or PAT
export VAULT_ROOT=/path/to/vault
export TOME_OPS_PROFILE=read-capture
export TOME_GIT_AUTHOR="tome-remote <tome-remote@invalid>"
tome doctor
```

- **`VAULT_ROOT`** points the CLI at the clone when the process isn't
  standing in it (still overridden by `--vault` or a walk-up match).
- **`TOME_OPS_PROFILE=read-capture`** restricts the command surface to
  `search`, `prime`, `doctor`, `help`, and `inbox` — the reads plus the one
  write that's append-only, schema-free, and conflict-free by design.
  Anything else (including a command added to tome later) is refused with a
  clear "this deployment is read-capture" message; the guard lives at one
  dispatch point, so new commands are guarded by default rather than needing
  to be added to an allowlist. `help`/`doctor` always run, even under an
  unset or misconfigured profile, so the deployment can always self-diagnose.
- **`TOME_GIT_AUTHOR`** (`"Name <email>"`) is applied via `git commit
  --author` on every tome-driven commit, so `git log` on the vault shows
  which surface made each change without global git config on the
  container.
- **`tome doctor`** is the health gate: run it after bootstrap and treat any
  `FAIL` line as a blocker. It's profile-aware — under `read-capture` the
  node/npm/npx check is skipped (`tome task`, the only thing that needs
  them, is guarded off anyway) instead of warning about binaries the
  deployment was never going to use.
- **Sync races**: two writers sharing a vault (a headless remote and a local
  session, say) will eventually collide on `tome sync`'s push. On rejection,
  sync retries once (`pull --rebase` + push); a second rejection fails loud
  with the rebase state left intact rather than guessing further.

## Browse view

`quartz/` is gitignored inside a vault — a derived build tree, not vault
content. Bootstrap it once per vault:

```
python <path-to-this-repo>/scripts/setup_quartz.py
cd quartz && npx quartz build --serve
```

(`tome init` prints the exact next command: `tome-setup-quartz` if you have
the human CLI installed, otherwise the real script path. Agents reach it via
`$CLAUDE_PLUGIN_ROOT`.)

The first command clones [Quartz](https://github.com/jackyzha0/quartz)
(pinned to a known-good commit), wires the vault's `wiki/` in as its content
source, and installs the plugins pinned in the vault's `quartz.lock.json`;
safe to re-run any time. The second serves the site locally.

## Human CLI access (optional)

Agents get `tome` on PATH automatically — the plugin's SessionStart hook
prepends `scripts/` to the session PATH (via `$CLAUDE_ENV_FILE`) and exports
`$TOME_PYTHON`, so every Bash command an agent runs can call `tome <cmd>`
directly, no path wrangling and nothing to re-point when the plugin updates.
That covers agents in Bash; if you also want to run `tome` yourself from a
terminal:

```
uv tool install git+https://github.com/chicken-noodle-chris/tome.git
```

(`pipx install git+https://github.com/chicken-noodle-chris/tome.git` works
too, if you prefer pipx.) This puts `tome` and `tome-setup-quartz` on PATH
with the right interpreter baked in. Working from a local clone instead?
`uv tool install ~/Development/tome` installs from the path directly — that's
also how to pick up local changes before pushing.

## Repo layout

```
tome/
├─ src/tome_cli/      the package: cli.py, lint.py, search.py, quartz_setup.py, templates/
├─ scripts/           thin shims (tome.py, tome_lint.py, wiki_search.py, setup_quartz.py) —
│                     the plugin's actual invocation path via $CLAUDE_PLUGIN_ROOT
├─ skills/            pickup-task, write-a-plan, retrospect, ingest, query, capture
├─ hooks/             SessionStart vault-context + Stop sync-reminder hooks
└─ .claude-plugin/    plugin + marketplace manifest
```

## Development

```
pip install pytest
python -m pytest
```

To exercise the packaged install locally: `pip install .` (or `uv tool
install --force .`), then `tome help`.

## Out of scope (for now)

- A multi-vault registry — the CLI's root-resolution seam supports it, but
  registration itself waits for a real need.
- Non-Windows hook portability: hooks invoke `python`, which is correct on
  Windows; stock macOS may need `python3` there instead.

MIT licensed.
