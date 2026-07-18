# board

A Quartz 5 `pageType` plugin that emits a read-only **kanban board** at `/board`
from a vault's Backlog.md task files. Puts your task board on the same browsable
surface as your knowledge, with no write-back server — the board is a render-side
view over `backlog/tasks/*.md` (see the `kanban-render-side` decision in the tome
vault).

## What it does

- At build time, reads Backlog.md's `config.yml` (for the ordered status columns)
  and every `backlog/tasks/*.md` file, and emits a synthetic `/board` page.
- Renders one column per status, cards sorted by `ordinal`. Each card shows the
  task id, priority, title, and its `project:` / `milestone` labels.
- A card links to the task's first `references:` entry (a wiki page such as its
  plan), mapped to the Quartz slug. Tasks with no reference render as plain cards.
- Client-side filtering by `project:` label via a dropdown — no page reload,
  SPA-safe (re-binds on Quartz's `nav` event, cleans up via `addCleanup`).

Read-only: there is no write path of any kind.

## How it locates the backlog

`content/` is a junction/symlink to the vault's `wiki/`, so the emitter resolves
`ctx.argv.directory` with `fs.realpathSync` to get the real `wiki/` path and reads
its fixed sibling `../backlog/`. A vault with no `backlog/` sibling simply gets an
empty board rather than a broken build.

## `--serve` and task edits

Quartz's `--serve` watcher is `chokidar.watch(".", { cwd: <content dir> })` — it
watches only the content directory (`wiki/`). `backlog/` is outside it, so
**editing a task file does not trigger a rebuild** while serving. To see task
changes, re-save any `wiki/` page (which the watcher does see) or restart
`quartz build --serve`. A full `quartz build` always picks them up. In normal use,
task edits go through `tome task`, and you refresh the board with either of those.

## Reusable task reader

`src/tasks.ts` parses the **full** task model — frontmatter plus the description
and acceptance-criteria body sections — even though the board only renders a
subset. This is deliberate: a follow-up that emits a rendered page per task can
`import { parseTask, readAllTasks } from "board"` and consume the extra fields
without duplicating any parsing. Keep that module free of Quartz/JSX imports so it
stays reusable.

## Options

| Option  | Type     | Default   | Description                       |
| ------- | -------- | --------- | --------------------------------- |
| `slug`  | `string` | `"board"` | Slug of the generated board page. |
| `title` | `string` | `"Board"` | Page heading / title.             |

## Development

```
npm install
npm run dev    # tsup --watch: rebuilds dist/ on save
npm run build  # one-shot build; commit the resulting dist/
```

`dist/` is committed on purpose — Quartz's installer uses the pre-built output
directly (`hasPrebuiltDist`), so a fresh vault never compiles the plugin. `yaml`
and `preact` are peer dependencies provided by the Quartz host (never bundled).
Run `npm run build` and commit `dist/` after changing anything under `src/`.
