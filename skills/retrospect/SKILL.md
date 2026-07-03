---
name: retrospect
description: Periodically review recent vault work — edits, the activity log, and feedback — and propose durable refinements: corrections to promote into SCHEMA.md, knowledge to capture, conventions to add or prune.
when_to_run: When the user asks to run a retrospect/retrospective on the vault, or when it's clearly been a long while since the last one.
---

Optional input: a review window ("since May") or a focus ("just the vaulty work"). Default to the span since the last retrospect.

**Retrospect is the co-evolve-with-the-user step made a ritual.** `wiki/SCHEMA.md` declares itself "co-evolved with the user" — extended when a recurring pattern in your edits or feedback isn't captured yet, pruned when a rule stops fitting. Retrospect is the scheduled pass that delivers on that promise: look back over recent work, decide what the vault's conventions and capture habits should learn from it, and propose those changes for approval.

Hold the line against two neighbours — if a finding belongs to them, hand it off and move on:
- **lint** (`tome lint`) checks page *health*: broken links, missing frontmatter, orphans, drift, size caps. Mechanical correctness of pages that already exist.
- **gap-finding** asks *what knowledge is missing*: topics the wiki should cover but doesn't.
- **retrospect** asks *what the way-we-work should learn*: it refines the conventions and the capture process itself, not the pages. "This link is broken" → lint. "We keep forgetting to record decisions, so SCHEMA should require it" → yours.

Conventions live in **`wiki/SCHEMA.md` — the authority**; `scripts/tome.py` (`tome help`)
enforces the mechanics. `tome` ships as a plugin at `$CLAUDE_PLUGIN_ROOT`, separate from
the vault it operates on; `tome <cmd>` throughout means `python
"$CLAUDE_PLUGIN_ROOT/scripts/tome.py" <cmd>` (it resolves which vault to act on via
`--vault` / `VAULT_ROOT` / walking up from cwd). There is **one gate**: the user approves
the proposed refinements before anything is written.

1. **Prime, and set the window.** Run `tome sync` to pull, then read the vault's
   `CLAUDE.md`, `wiki/SCHEMA.md`, and `wiki/index.md` (skip any already read this
   session). Find the last retrospect log line with `grep -n "^## \[.*\] retrospect "
   wiki/log.md | tail -1` — its date is the window start; none found → default to the
   last ~30 days.

2. **Gather the evidence across the window.** Pull from every source available, then read for *patterns*, not one-offs:
   - **Edits** — the vault repo root, resolved the same way `tome` itself does (`$VAULT_ROOT`
     if set, else walk up from cwd looking for `conventions.toml`). `git -C "<vault root>"
     log --since=<date> --stat` for what changed and `git -C "<vault root>" log
     --since=<date> -p -- wiki/ CLAUDE.md` for how. Churn, reversals, and the same fix made
     by hand twice are the signal.
   - **Activity** — the `wiki/log.md` entries since the window start: the arc of recent work.
   - **Feedback** — your project memory store (its `MEMORY.md` index plus the `feedback`- and `project`-type files): the corrections and preferences you've already been told. Richest source.
   - **Sessions** — if your harness exposes session-history tools (e.g. `list_sessions`, `search_session_transcripts`), mine recent transcripts for corrections the user gave in conversation that never reached SCHEMA or a memory.

3. **Derive the refinements.** Sort what recurs into three kinds; discard one-offs (corrected once is noise, twice is a pattern):
   - **Promote a recurring correction into `wiki/SCHEMA.md`** — the user keeps steering the same way and SCHEMA is silent on it. Draft the convention.
   - **Capture missed knowledge** — something durable surfaced in the work but was never filed. Propose the page (or memory) and where it lands.
   - **Add or prune a convention** — a rule observed practice now contradicts, or one nothing has used. Propose the edit or the deletion.
   Anything that's really lint or gap-finding: route it there, don't fix it here.

4. **Present the proposals — the one gate.** Show each refinement as a concrete change: the exact SCHEMA wording, the page to create, the line to cut — grouped by kind, each with the evidence that earns it. Recommend; don't dump every candidate. Iterate in place until the user approves, and drop what they reject.

5. **Apply, log, and sync.** Make the approved edits. `tome log retrospect "<summary>"`
   naming the window reviewed and what changed — **this entry is the state store**; the
   next run reads its date. `tome sync -m "..."` — no separate commit approval needed;
   step 4's approval already covered the content.
