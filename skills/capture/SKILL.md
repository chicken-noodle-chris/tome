---
name: capture
description: Drop a small, worth-keeping fact, decision, or preference into the vault's inbox in under 15 seconds — no schema, no index ceremony.
when_to_run: When the user says "remember this", "note this down", "add to the vault" about something small — below ingest's weight class.
---

Optional input: the thing to remember, in the user's own words.

Capture exists because memory systems live or die on capture being nearly free. This is
the cheap half of the capture → compile flow in `wiki/SCHEMA.md`: this skill drops a note
in `inbox/`; the `retrospect` skill is the one that later triages it into the wiki. Don't
do triage here — that's the whole point of splitting the two.

1. **Check it's the right weight class.** A full external source (a paper, an article, a
   transcript) is `ingest`'s job, not this one — hand off instead. A small fact, decision
   context, preference, or link is this skill's job.

2. **Distill, don't transcribe.** One to three self-contained sentences — enough context
   that a future triage pass (which won't have this conversation) understands it cold.
   Name the project if there is one.

3. **Capture it.** `tome inbox "<distilled note>"` (fall back to `python
   "$TOME_PLUGIN_ROOT/scripts/tome.py" inbox "..."` if `tome` isn't on PATH). Skip the
   schema-read / index-read ceremony other skills do — that's what keeps this under ~15
   seconds of session time.

4. **Close with one line.** Tell the user it's in the inbox and will be routed at the next
   retrospect triage. If the thing is plainly page-worthy on its own and the user is
   clearly engaged right now, you may offer the direct `tome new` route as an alternative —
   but default to the inbox, not a judgment call.

**Anti-patterns:** running the schema/index-read ceremony (defeats the point — that's what
`ingest` and `write-a-plan` are for); triaging or filing the note into a real page here
(that's retrospect's job); capturing something that's really a full external source.
