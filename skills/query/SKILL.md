---
name: query
description: Answer a question from the wiki's accumulated knowledge, with citations, and offer to file the answer back.
when_to_run: When the user asks what the wiki knows about something, or a question that should be answered from accumulated notes rather than fresh research.
---

Optional input: the question, and optionally a project to scope it to.

Conventions live in `wiki/SCHEMA.md`; `scripts/tome.py` (`tome help`) scaffolds any page this
produces. `tome` is on PATH in Bash (the plugin's SessionStart hook puts it there) — just
run `tome <cmd>`; if it's ever not found, fall back to `python
"$TOME_PLUGIN_ROOT/scripts/tome.py" <cmd>`. It resolves which vault to act on via `--vault`
/ walking up from cwd / `VAULT_ROOT`, and bare paths like `wiki/index.md` are relative to
the vault root, not the plugin root.

1. **Prime yourself.** Read `wiki/SCHEMA.md` if you haven't this session — some wikis declare
   query-specific conventions that override the default flow below.

2. **Read the index first.** Start at `wiki/index.md` (or the relevant shard under
   `wiki/indexes/` if sharded). Build a short candidate list from the one-line summaries — be
   selective; reading dozens of pages to answer one question means the index isn't doing its job.

3. **Fall back to search only if the index doesn't surface good candidates.** `tome search
   "query terms" --top 10` (BM25 over frontmatter + body; `--type`, `--tag`, `--since` filters;
   `--backlinks <slug>` for inbound-link lookups). Fallback, not the default — index-first is
   cheaper and more interpretable when it works.

4. **Read the candidates in full.** Note `[[wikilinks]]` to other pages as you go — pre-curated
   leads — but don't recursively chase every link. If a page cites a source page and the answer
   hinges on exactly what that source said, read the source page too; only go back to `raw/`
   itself if the wiki's summary is clearly insufficient.

5. **Synthesize the answer, cited.** Write it in your own words with `[[wikilink]]` citations to
   every page you drew on. If the wiki holds contradicting claims, surface the contradiction
   rather than silently picking one. If the wiki has nothing relevant, say so plainly — don't
   confabulate — and suggest what to ingest to fill the gap.

6. **Offer to file the answer back.** Default to offering when the answer represents real
   connection-making (a comparison, a synthesis across pages, an answer to a recurring question) —
   not for a trivial one-line lookup. On approval: `tome new synthesis <slug> --project <name>
   --title "T" --desc "..."`, body = the answer, cite the pages consulted.

7. **Log it.** `tome log query "<question summary>"`, and `tome sync -m "..."` if anything
   was filed back (a read-only query still logs; the log line rides along with the next sync).

**Anti-patterns:** reading the whole wiki "to be safe" instead of trusting (or fixing) the index;
citing the wiki without chaining back to a raw source for a claim the user might need to verify;
filing back answers that don't earn a permanent page; inventing an answer when the wiki is silent.
