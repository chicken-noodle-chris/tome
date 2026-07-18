---
name: write-a-plan
description: Direct agent to create a plan and store it in the vault with an attached task.
when_to_run: When the user asks to create a plan.
---

Optional input: A description of the task that the plan should accomplish.

The flow: understand the goal, design the plan, author it directly as a vault page, and
present it for approval. **One approval gate: the plan itself.** Iterate on the page *in
place* until the user is happy; don't stop to ask "what do I do next?" between steps.

Conventions (frontmatter, placement, body style, linking, status vocabulary) live in
`wiki/SCHEMA.md` and are enforced by `scripts/tome.py` — run `tome help` and lean on it
rather than hand-executing mechanics. `tome` is on PATH in Bash (the plugin's SessionStart
hook puts it there) — just run `tome <cmd>`; if it's ever not found, fall back to `python
"$TOME_PLUGIN_ROOT/scripts/tome.py" <cmd>`. It resolves which vault to act on via
`--vault` / walking up from cwd / `VAULT_ROOT`, and bare paths like `wiki/SCHEMA.md`
are relative to the vault root, not the plugin root.

1. **Understand the goal.** If the user didn't describe the task, interview them: ask
   what the plan should accomplish, then any clarifying questions on intent and scope.
   Explore the relevant code as needed so the plan is concrete and grounded.

2. **Prime yourself on the vault.** Run `tome sync` to pull, then `tome prime --full`
   (skip if already primed this session).

3. **Design and author the plan page (with its task).** `tome new plan <slug> --project
   <name> --title "T" --desc "..." --with-task "<task title>" [--priority
   high|medium|low] [--ac "<criterion>" ...]` scaffolds both in one command — the task is
   optional (the plan's `status`, not a task, is the source of truth; omit `--with-task`
   for a plan with none), and its hub listing is generated automatically (the project
   folder must already exist — `tome new project <name> ...` first if not). Judge
   `--priority`/ACs yourself when unspecified. If the work demands a stronger executor
   than usual (greenfield architecture, precedent-setting design), add a minimum
   agent-tier label: `-l agent:<haiku|sonnet|opus|fable>` — pickup-task halts a
   lower-tier executor for user direction; omit when any tier can do it. Then write
   the plan body directly —
   timeless prose describing what the work *is*, not its current status. New work is
   `status: proposed` (or `tome set-status <slug> done` if it's *already implemented* —
   close its task separately with `tome done`, since `--with-task` only covers creation).
   `tome log plan "..."` for the log entry.

4. **Present for approval — the one gate.** Summarize the approach, key decisions, and
   any task fields you judged on the user's behalf. Iterate on the page (and task) in
   place until they approve — no separate draft.

5. **Sync.** `tome sync -m "..."` once approved. No separate commit approval needed —
   step 4's approval covered the plan's content, not its mechanics.
