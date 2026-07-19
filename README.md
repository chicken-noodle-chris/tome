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

## Cloud session priming

A cloud session (Claude Code on the web) starts with no human at the
keyboard to run the install ritual above, so the plugin is designed to
travel with the repos themselves instead — see the vault's
`cloud-session-priming` plan page for the full design.

**Vault repo.** `tome init` scaffolds `.claude/settings.json` into every new
vault, declaring the marketplace and enabling the plugin:

```json
{
  "extraKnownMarketplaces": {
    "tome": { "source": { "source": "github", "repo": "chicken-noodle-chris/tome" } }
  },
  "enabledPlugins": { "tome@tome": true }
}
```

Any cloud session that opens the vault repo installs the plugin, runs the
SessionStart hook, and gets the prime text — no first prompt spent on setup.
An existing vault predating this stanza just needs the same file committed
by hand.

**Enrolling a project repo.** A cloud session whose primary repo is a
*project*, with the vault pulled in as a source alongside it, needs the same
plugin available — commit the identical `.claude/settings.json` stanza above
to that project repo too. There's no separate `tome enroll` command; copying
the file is the whole procedure.

**Nearby-checkout vault discovery.** The SessionStart hook resolves the
vault by walking up from cwd, same as `tome` itself. A multi-repo cloud
workspace shows up in two shapes depending on whether a "primary" repo was
designated: with one, cwd starts inside that repo and the vault sits
alongside it as a *sibling* checkout; with none, cwd starts at the workspace
root itself, one level *above* every checkout, so the vault is a *child* of
cwd instead. Walk-up alone misses both. When `CLAUDE_CODE_REMOTE=true`, the
hook additionally scans cwd's children and cwd's siblings for a directory
containing `conventions.toml` and exports `VAULT_ROOT` (via the same
`$CLAUDE_ENV_FILE` mechanism used for `PATH`), so every `tome` command for
the rest of the session resolves regardless of which repo a Bash command's
cwd happens to be in. This scan is gated to remote sessions only — an
arbitrary local multi-repo checkout on someone's laptop isn't a safe place
to assume any nearby `conventions.toml` is *the* vault.

**Verified cloud environment setup-script recipe.** The most reliable way
to prime a cloud environment, confirmed working end-to-end: add the plugin
install to the environment's setup script (it runs before the session
starts, at user scope, so it applies no matter which repo ends up primary
or whether the repos-committed `.claude/settings.json` stanza above even
gets read — that part turned out to be unreliable with no primary repo
designated) —

```bash
claude plugin marketplace add chicken-noodle-chris/tome
claude plugin install tome@tome
npm install backlog.md -g   # tome task passthrough depends on it
```

— **and** include the vault repo itself as one of the session's attached
repos, alongside whatever project repo you're actually there to work on. With
both of those true, the plugin is installed before the session starts, the
vault lands near cwd for the nearby-checkout scan above to find, and the
session is primed with zero first-prompt setup — verified against a real
Claude Code web session with `ai-toolkit` as the working repo and
`knowledge-vault` attached alongside it.

Tried and *not* yet working: having the setup script itself `git clone` the
vault, so a session only needs the project repo attached and the vault
comes along for free. The per-session git-auth proxy this environment uses
for attached repos doesn't appear to be reachable at setup-script time, so
an authenticated clone from there failed. Revisit with a PAT/deploy-key
clone (the pattern the headless bootstrap section below already documents)
if this seam matters enough to unblock.

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
tome prime [project] [--full]     # session orientation; --full adds SCHEMA, index, open-task snapshot, project context
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
tome serve [--open]       # local no-build browse frontend (pages + read-only board); read-only
```

Root resolution for the CLI: `--vault PATH`, else walk up from cwd looking
for `conventions.toml`, else `$VAULT_ROOT` — the vault you're standing in
always beats the global default, and `$VAULT_ROOT` covers sessions in
non-vault directories.

## Scheduled retrospect

`retrospect` (see the skill) is designed as a periodic ritual, but a ritual
nobody schedules only runs when someone remembers — so point whatever
recurring-job mechanism your environment offers at it, roughly weekly. The
two constraints that make an unattended firing safe:

- **The prompt is `/tome:retrospect`, nothing more.** The skill already
  contains an "unattended" branch (no live user to approve): it still
  gathers evidence and derives proposals, but instead of the live approval
  gate it drops every proposal as a `tome inbox` capture and stops — no
  SCHEMA edit, page rewrite, or convention change ever happens without a
  human present. A scheduled run is a gather-and-propose pass, not an
  autonomous-edit pass.
- **The next live retrospect (or a `capture` triage) picks the captures
  up.** They land in `inbox/` exactly like any other capture; nothing about
  them is special beyond the "(unattended — proposals filed to inbox)" note
  in the log entry the run leaves behind.

What fires the prompt is deliberately not tome's concern — "the schedule
itself is per-machine." A few options, pick whichever your environment
already has:

- **Claude Code on the web / Cowork Routines** — create a weekly-cron
  Routine (`create_trigger` with a `cron_expression` like `0 9 * * 1` and
  `prompt: "/tome:retrospect"`) bound to a session that already has the
  vault primed. Cheapest option if you're already running sessions there.
- **A headless container's system cron**, calling a non-interactive `claude
  -p "/tome:retrospect"` (or your harness's equivalent) against the vault —
  reuse the [Headless bootstrap](#headless-bootstrap) section's `VAULT_ROOT`
  and `TOME_GIT_AUTHOR`, but *not* `TOME_OPS_PROFILE=read-capture`: even the
  unattended branch still needs `tome inbox`, `tome log`, and `tome sync`
  (step 6), and read-capture only allows the first of those.
- **Any other trigger your agent harness exposes** — the only requirement
  is that it lands a single `/tome:retrospect` turn on a schedule.

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
- **`TOME_GIT_AUTHOR`** (`"Name <email>"`) is applied as the author (via
  `git commit --author`) and — unless `GIT_COMMITTER_*` is set explicitly —
  as the committer identity on every tome-driven git call, so `git log` on
  the vault shows which surface made each change and commits succeed with
  no git config on the container at all (git refuses to commit without a
  committer identity; `--author` alone doesn't provide one).
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

## Shipping a release

`.claude-plugin/plugin.json`'s `version` is the sole version authority —
the marketplace entry in `.claude-plugin/marketplace.json` carries no
`version` field of its own, so there's nothing else to keep in sync. Ship
ritual: bump `plugin.json`'s version, commit and push, then run `claude
plugin update tome@tome` to pick it up locally. A directory-source
marketplace (a local clone, not a GitHub slug) doesn't auto-refresh on repo
changes — anyone else pointed at it needs that same command after pulling,
or `tome doctor`'s "plugin freshness" check will flag the drift (a
resolvable dev checkout's `plugin.json` vs. the cached, currently-active
plugin found via `$TOME_PLUGIN_ROOT`).

## Out of scope (for now)

- A multi-vault registry — the CLI's root-resolution seam supports it, but
  registration itself waits for a real need.
- Non-Windows hook portability: hooks invoke `python`, which is correct on
  Windows; stock macOS may need `python3` there instead.

MIT licensed.
