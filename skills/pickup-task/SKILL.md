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
`wiki/SCHEMA.md` are relative to the vault root, not the plugin root. Run `tome` from
wherever you already are — it finds the vault itself. Don't `cd` into the vault or `find`
your way to task/plan files; let the `tome` commands surface them.

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
   syncs, then prints the task text (labels included) and the full plan body as your
   working context. Run it directly — it's pure bookkeeping that dumps everything the next
   steps need, so there's no separate task-lookup step. Read the plan in full and follow
   its `[[wikilinks]]`; if you have concerns, stop and discuss with the user before going
   further.

4. **Check the agent-tier label (gate).** The task text `tome start` just printed carries
   an `agent:<tier>` label (tier: `haiku` < `sonnet` < `opus` < `fable` — the suggested
   executor for the work). Compare it against the model you are running as. If the tier
   *differs in either direction*, or there's a label and you can't tell, **stop before
   doing the work**: tell the user the suggested tier and your own, and wait. Running below
   the tier risks the work's quality; running above it wastes capability and money — both
   are the user's call, they'll switch the model or tell you to continue. No label, or an
   exact match: proceed. (The task is already started; switching model now only changes who
   does the work — the started state stays accurate.)

5. **Do the task work.** Execute the plan in the relevant code repo (not the vault),
   following that repo's `CLAUDE.md`. Stick to the plan's scope; if you hit a fork it
   doesn't resolve, ask rather than guess. Verify per the plan's verification section and
   report results honestly.

6. **Commit. Present. No push.** Commit the work. Check repo `CLAUDE.md` for commit rules first.
   Show user the diff and verify results. Do not push. Wait for user OK. Push only after OK.

7. **Close out the tracking.** Once the work has landed: `tome done <slug> --summary "..."`
   citing the shipping commit (`--as superseded`/`--as abandoned` instead of the default
   `done`, if that's how it landed). This archives the plan (moves it to `plans/archive/`,
   regenerates its project hub and the index), checks the task's acceptance criteria,
   closes and completes it with the summary, re-points its `--ref` at the archived path,
   logs `done`, and syncs — one command, no deferred sweep. **Umbrella plans:** if the
   task is one phase of a milestone plan shared by sibling phase tasks, `tome done
   <task-id>` closes only that task and leaves the plan active (it prints how many open
   siblings kept it alive); the plan archives automatically when the last sibling closes.
   Don't try to close the shared plan slug while phases remain open — it's refused unless
   `--force`.
