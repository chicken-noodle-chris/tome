---
name: pickup-task
description: Direct agent to execute a tracked task/plan from the vault, updating its status before and after the work.
when_to_run: When the user asks to start, pick up, or execute a task or plan from the wiki.
---

Optional input: which task or plan to pick up (a task ID, a plan name, or a description).

This skill executes work that's already been planned. Use `scripts/tome.py` (`tome help`)
to manage status changes — don't hand-execute git or status moves. `tome` is on PATH in
Bash; if not found, fall back to `python "$TOME_PLUGIN_ROOT/scripts/tome.py" <cmd>`. It
resolves which vault via `--vault` / walking up from cwd / `VAULT_ROOT`; bare paths like
`wiki/SCHEMA.md` are relative to the vault root, not the plugin root.

**The user is always happy to answer questions.** If intent or scope is unclear — which task,
how far to take it, an ambiguity in the plan — ask.

1. **Prime yourself on the vault.** Run `tome sync` to pull, then `tome prime --full`
   (skip if already primed this session) — prints SCHEMA.md and the index in one shot.

2. **Locate the task and its plan.** If the user named a task or plan, find it. If they
   named a **milestone** instead (an id like `m-0` or its title — the epic layer, see
   `wiki/SCHEMA.md`), resume it rather than picking one task in isolation: run `tome task
   task list --milestone <name> --plain` to list its open children (backlog.md already
   orders each status group by ordinal), then pick the next one — an In Progress child
   first (someone left it mid-flight), else the highest-priority To Do child, ties broken
   by list order. Confirm the pick with the user if it's not clear-cut. Selection data
   comes entirely from that list output; there's no separate tome command for this. With
   no name given at all, check the board (`tome task task list --plain`) and the project's
   live plans (`plans/`, not `plans/archive/`) and confirm which one they mean.

3. **Mark the work started.** `tome start <task-id-or-slug>` — accepts either, resolving
   the other half if linked (a plan without a task, or a task without a plan, is normal).
   Sets the plan `active`, moves the task to In Progress (`-a @me`), logs `work-started`,
   syncs, then prints the task text and the full plan body as your working context — read
   it in full, follow its `[[wikilinks]]`, and if you have concerns stop and discuss with
   the user before continuing.

4. **Do the task work.** Execute the plan in the relevant code repo (not the vault),
   following that repo's `CLAUDE.md`. Stick to the plan's scope; if you hit a fork it
   doesn't resolve, ask rather than guess. Verify per the plan's verification section and
   report results honestly.

5. **Commit, then present the work.** Commit by default. Show the user what changed and the
   verification results. The code repo is separate from the vault and follows its own
   commit conventions — check its `CLAUDE.md`. Push only after the user approves.

6. **Close out the tracking.** Once the work has landed: `tome done <slug> --summary "..."`
   citing the shipping commit (`--as superseded`/`--as abandoned` instead of the default
   `done`, if that's how it landed). This archives the plan (moves it to `plans/archive/`,
   regenerates its project hub and the index), checks the task's acceptance criteria,
   closes and completes it with the summary, re-points its `--ref` at the archived path,
   logs `done`, and syncs — one command, no deferred sweep.
