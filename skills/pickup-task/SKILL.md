---
name: pickup-task
description: Direct agent to execute a tracked task/plan from the vault, updating its status before and after the work.
when_to_run: When the user asks to start, pick up, or execute a task or plan from the wiki.
---

Optional input: which task or plan to pick up (a task ID, a plan name, or a description).

This skill executes work that's already been planned. Three phases: **locate & start**
(steps 1–4), **do the work** and ship it (steps 5–6), and **close out** the tracking
(step 7). The throughline: the vault's task/plan status reflects reality at every step —
mark work *started* before you begin and *done* when it lands. Conventions live in
`wiki/SCHEMA.md`; `scripts/tome.py` (`tome help`) enforces the mechanics — lean on it
rather than hand-executing status moves, links, or git. `tome` ships as a plugin at
`$CLAUDE_PLUGIN_ROOT`, separate from the vault it operates on; `tome <cmd>` throughout
means `python "$CLAUDE_PLUGIN_ROOT/scripts/tome.py" <cmd>` (it resolves which vault to
act on via `--vault` / walking up from cwd / `VAULT_ROOT`), and bare paths like
`wiki/SCHEMA.md` are relative to the vault root, not the plugin root.

Throughout, **the user is always happy to answer questions.** If intent or scope is
unclear — which task, how far to take it, an ambiguity in the plan — ask.

1. **Prime yourself on the vault.** Run `tome sync` to pull, then read the vault's
   `CLAUDE.md`, `wiki/SCHEMA.md`, and `wiki/index.md` (skip any already read this
   session).

2. **Locate the task and its plan.** If the user named one, find it; otherwise check the
   board (`tome task task list --plain`) and the project's live plans (`plans/`, not
   `plans/archive/`) and confirm which one they mean. Read the **plan page** in full and
   follow its `[[wikilinks]]`. Find the matching task with `tome task task <id> --plain`
   (a plan may have none — normal).

3. **Confirm scope before starting.** Tell the user what you found — the plan, the task,
   what executing it entails — and confirm they want you to proceed. Surface anything in
   the plan that looks stale or underspecified.

4. **Mark the work started.** `tome set-status <slug> active`, `tome task task edit <id>
   -s "In Progress" -a @me`, `tome log work-started "..."`.

5. **Do the task work.** Execute the plan in the relevant code repo (not the vault),
   following that repo's `CLAUDE.md`. Stick to the plan's scope; if you hit a fork it
   doesn't resolve, ask rather than guess. Verify per the plan's verification section and
   report results honestly.

6. **Present the work, then commit it on approval.** Show the user what changed and the
   verification results. The code repo is separate from the vault and follows its own
   commit conventions — check its `CLAUDE.md`. Commit/push only after the user approves.

7. **Close out the tracking.** Once the work has landed: `tome set-status <slug> done`
   (or `superseded`/`abandoned`) — this moves the plan to `plans/archive/` and regenerates
   the index itself. Then `tome task task edit <id> -s Done --check-ac ... --final-summary
   "..."` citing the shipping commit and re-pointing the task's `--ref` at the plan's new
   `plans/archive/` path, then `tome task task complete <id>` — close it out immediately, no
   deferred sweep. Move the plan's entry on the project hub to its archived list (that one
   is manual), then `tome log done "..."` and `tome sync -m "..."`.
