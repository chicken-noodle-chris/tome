---
name: ingest
description: Compile an external source (paper, article, transcript, notes) into a cited, synthesized wiki page.
when_to_run: When the user points at a source to add to the wiki — "ingest this", "add this paper", "compile these notes".
---

Optional input: a source (a file, a URL, or something already in `raw/`) and optionally which project it belongs to.

Conventions live in `wiki/SCHEMA.md` and `conventions.toml`; `scripts/tome.py` (`tome help`)
scaffolds pages and keeps the index in sync — lean on it rather than hand-authoring frontmatter.
`tome` ships as a plugin at `$CLAUDE_PLUGIN_ROOT`, separate from the vault it operates on;
`tome <cmd>` means `python "$CLAUDE_PLUGIN_ROOT/scripts/tome.py" <cmd>` (it resolves which
vault to act on via `--vault` / walking up from cwd / `VAULT_ROOT`), and bare paths like
`raw/` are relative to the vault root, not the plugin root.

1. **Prime yourself.** Run `tome sync` to pull, then read `wiki/SCHEMA.md` if you haven't this
   session — it overrides anything below. Confirm which project the source belongs to
   (`wiki/<project>/`); ask if unclear.

2. **Place the raw source.** If it isn't already in `raw/`, save it there under a slugified
   filename — that slug becomes the source page's slug. `raw/` is immutable: never hand-edit a
   file there once placed; a correction is a new capture, not an in-place fix.

3. **Read the source.** Short sources: read in full. Long ones (papers, chapters, transcripts):
   chunk-read — map the structure via headers/TOC first, then read sections sequentially,
   summarizing each before moving on. Don't load a source that would eat more than ~25% of your
   context window in one pass. For images, read the surrounding text first and only view figures
   the text treats as load-bearing.

4. **Survey what's touched.** Read `wiki/index.md` (or the project's shard) to find existing
   pages this source overlaps — entities, concepts, prior sources on the same topic. Read
   candidates in full to confirm real overlap; the index summary alone can mislead. This pass is
   what prevents duplicate pages.

5. **Discuss briefly.** Three or four sentences on what struck you, what's surprising, what
   connects to existing pages, what's worth flagging — unless the user is doing a hands-off batch
   ingest, in which case skip the chat but be more conservative with edits and surface surprises
   in the log instead.

6. **Write the source page.** `tome new source <slug> --project <name> --title "T" --desc "..."`
   to scaffold, then write the body: key claims, methodology if relevant, conclusions, open
   questions — a synthesis, not a paraphrase. Cite the raw file (`raw/<file>`) and link touched
   pages with `[[wikilinks]]`. Keep it under the soft cap; split into linked pages if the source is
   too dense for one.

7. **Update touched pages, surgically.** Edit in place, don't rewrite — add a sentence or section
   with a `[[wikilink]]` citation to the new source page. A contradiction gets flagged explicitly
   (both sources cited), never silently overwritten. A page that crosses the size cap during this
   edit gets split now, not deferred to lint.

8. **Create new pages only for real first-class topics.** An entity or concept the source treats
   substantively and future sources will likely build on. Passing mentions go inline on a related
   page instead. Every new page needs at least one inbound `[[wikilink]]` from an existing page —
   an unlinked new page is a bug in the ingest, not a lint finding for later.

9. **Refresh the index, log, and sync.** `tome describe <slug> "..."` for any page whose summary
   changed (new pages get their scaffold description; adjust if the body diverged). `tome log
   ingest "<source title> — <what changed>"`, then `tome sync -m "..."`.

10. **Close the loop.** Tell the user what happened — pages created, pages updated, contradictions
    flagged — in a sentence or two. If the source raised an obvious follow-up, say so.

**Anti-patterns:** loading a large source whole; rewriting a page instead of editing it
surgically; creating an orphaned page; batch-ingesting silently with no summary; trusting a wiki
page's paraphrase over the raw source when merging a correction.
