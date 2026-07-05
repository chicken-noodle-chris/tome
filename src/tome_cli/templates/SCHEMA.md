# Wiki Schema

This file is the configuration for this wiki: conventions, taste, and
workflow customizations that a `tome help` command surface can't express.
The LLM reads this first when entering the wiki; it's authoritative over any
skill's baked-in defaults. Data-shaped rules (frontmatter fields, the
type enum, tag taxonomy, size caps, plan-status vocabulary) live in
`conventions.toml`, not here.

This file is **co-evolved with the user**: when the LLM notices a recurring
pattern in your edits or feedback that isn't here, it proposes adding it;
when something here stops fitting, prune it.

## Mechanics

`tome` (installed as a Claude Code plugin) owns scaffolding, the generated
index, status/archive moves, renames, the log, and git sync — run `tome
help` for the full command surface, `<command> -h` for one command's detail.
Reads stay native file tools; there is no read/query surface here. Start and
end vault work with `tome sync`. `tome lint` must pass error-free as the
last step of any wiki-touching task.

Frontmatter is a hand-rolled subset, not full YAML: `key: value`, inline
lists (`key: [a, b, c]`), and block lists (`key:` then `  - value` lines) —
no nested maps, multi-line scalars, or comments. Pages are normally
scaffolded by `tome new`; this only matters for hand-edits, and `tome lint`
flags anything outside it.

## Repo layout

Wiki root: `wiki/`; raw sources: `raw/`; assets: `raw/assets/`. A personal,
multi-domain vault, not a single-domain corpus: project material and general
knowledge alike live under `wiki/` as first-class pages (frontmatter +
`[[wikilinks]]`). A page's kind comes from its `type:` frontmatter, **not**
its folder — folders exist purely for human traversal, organized by project.

```
<vault>/
├─ raw/      immutable external captures — never hand-edited
├─ inbox/    capture buffer — triaged into the wiki, then cleared
├─ backlog/  Backlog.md kanban tasks, each linking to its plan page
└─ wiki/     the knowledge base (SCHEMA.md, index.md, log.md, <project>/)
```

Each project is `wiki/<name>/` with subfolders (`plans/`, `ideas/`,
`decisions/`, `reports/`, `sources/`, `notes/`) matching page types.

## Capture → compile flow

Capture is cheap: `tome inbox "<note>"` drops a note in `inbox/` (or the
`capture` skill does it for you, mid-session), or a raw external file goes in
`raw/`. Triage routes each item into the wiki as a proper page, then deletes
the inbox item — `retrospect` owns that triage, as one of its regular
evidence sources; nothing else drains the inbox. The `ingest` skill compiles
an external source into a synthesized, cited page — only for a source you
point it at, never auto-scanning `inbox/`. Plans, ideas, and decisions are
authored directly, never through ingest — there's no external source to
cite.

## Surprises and taste

- **Wikilinks resolve by filename slug, case-sensitively** — `[[my-page]]`
  not `[[My-Page]]`; alias with `[[my-page|My Page]]`. `index.md`,
  `log.md`, `SCHEMA.md`, `README.md` don't count toward inbound-link totals,
  so a hub page reads as an orphan until a content page links to it.
- **`status` frontmatter is the single source of truth** for a plan's or
  decision's lifecycle — never duplicate it as hand-maintained prose
  elsewhere; write plan bodies timelessly. `tome set-status` moves plan
  files between `plans/` and `plans/archive/` automatically.
- **A plan without a Backlog.md task is normal** — a task is optional,
  created only when you want active work visible on the kanban board.
- **Reports are dated snapshots**, never edited to stay current — a later
  assessment is a new report page, not an edit to the old one.
- **The index is generated** — never hand-edit `wiki/index.md`; set a page's
  summary with `tome describe`.
- **Task project tagging**: every Backlog.md task carries a `project:<name>`
  label, set on create, since the backlog is one shared board.
- **Fail loud**: if a source is ambiguous, a placement is unclear, or a
  predicate has no evidence, stop and ask rather than guessing silently.

## User preferences

(none recorded yet — add your own here as they emerge: prose vs. bullets,
tooling philosophy, formatting conventions, anything you find yourself
repeating to the agent)
