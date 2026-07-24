# CLAUDE.md

This repo **is** the `tome` plugin + CLI. It's installed as a Claude Code plugin from a local *Directory* marketplace pointing at this checkout, so "the tool an agent runs" is a version-keyed cache copy, not your working tree.

## Releasing a tool change (required — easy to miss)

Plugin updates are **version-gated** and the cache is keyed by version directory. A code change without a version bump reaches no one: `claude plugin update` sees the same version, reports "already up to date," and the installed cache keeps running the old code.

So whenever you change the tool itself (the CLI in `src/tome_cli/`, the skills, or the hooks), finishing the work means:

1. Bump `version` in `.claude-plugin/plugin.json` (e.g. `1.2.25` → `1.2.26`).
   If working from a tracked task, its `semver:patch|minor|major` label sizes
   the bump. No label, or no active task: default to `patch`, or ask if unsure.
2. Commit + push to `main`.
3. Install it locally: `claude plugin update tome@tome` — this refreshes the
   version-keyed cache to your new code.

The push alone changes nothing an agent invokes; the **bump + local install** is what makes the change real. Docs-only edits like this file don't touch plugin runtime, so they need no bump.

## Running your in-progress changes

The `tome` on a session's PATH is the plugin-cache copy pinned at session start — not your working tree — and `claude plugin update` only takes effect on the **next** session ("restart to apply"). To exercise uncommitted or just-pushed changes in the current session, run the working tree directly:

    PYTHONPATH=src python -m tome_cli.cli <args>
