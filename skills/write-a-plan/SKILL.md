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
rather than hand-executing mechanics. `tome` ships as a plugin at `$CLAUDE_PLUGIN_ROOT`,
separate from the vault it operates on; `tome <cmd>` throughout means `python
"$CLAUDE_PLUGIN_ROOT/scripts/tome.py" <cmd>` (it resolves which vault to act on via
`--vault` / walking up from cwd / `VAULT_ROOT`), and bare paths like `wiki/SCHEMA.md`
are relative to the vault root, not the plugin root.

1. **Understand the goal.** If the user didn't describe the task, interview them: ask
   what the plan should accomplish, then any clarifying questions on intent and scope.
   Explore the relevant code as needed so the plan is concrete and grounded.

2. **Prime yourself on the vault.** Run `tome sync` to pull, then read the vault's
   `CLAUDE.md`, `wiki/SCHEMA.md`, and `wiki/index.md` (skip any already read this
   session).

3. **Design and author the plan page.** `tome new plan <slug> --project <name> --title
   "T" --desc "..."` to scaffold (the project folder must already exist — `tome new
   project <name> ...` first if not; the index regenerates automatically), link the new
   page from the project hub, then write the body directly — timeless prose describing
   what the work *is*, not its current status. New work is `status: proposed` (or `tome set-status
   <slug> done` if it's *already implemented*). `tome log plan "..."` for the log entry.

4. **Create the Backlog.md task.** Optional — the plan's `status`, not a task, is the
   source of truth. Create one via `tome task task create "<title>" -d "<why>" -l
   project:<name> --priority <high|medium|low> --ref <plan path> --ac "<criterion>"`;
   judge `--priority` and ACs yourself when unspecified. Already-implemented plans get
   `-s Done`, `--final-summary`, and `--check-ac` on every criterion.

5. **Present for approval — the one gate.** Summarize the approach, key decisions, and
   any task fields you judged on the user's behalf. Iterate on the page (and task) in
   place until they approve — no separate draft.

6. **Sync.** `tome sync -m "..."` once approved. No separate commit approval needed —
   step 5's approval covered the plan's content, not its mechanics.
