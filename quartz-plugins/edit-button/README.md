# edit-button

A Quartz 5 component plugin that adds an **Edit** button to every content page,
deep-linking to the page's source markdown in VS Code via `vscode://file/…`.

Browse in Quartz, one click, edit in the editor — closing the read/edit gap for
a vault whose canonical form is markdown on disk.

## How it works

Quartz's `content/` directory is a junction/symlink to the vault's `wiki/`, so
the component resolves each rendered page's `filePath` back through the link with
`fs.realpathSync` at build time and emits a `vscode://file/<abs-path>` link.
Virtual pages (the board, tag and folder indexes) have no backing file and are
skipped automatically.

No client-side JavaScript: the button is a plain anchor.

## Options

| Option  | Type     | Default  | Description                    |
| ------- | -------- | -------- | ------------------------------ |
| `label` | `string` | `"Edit"` | Text shown next to the pencil. |

## Development

```
npm install
npm run dev    # tsup --watch: rebuilds dist/ on save
npm run build  # one-shot build; commit the resulting dist/
```

`dist/` is committed on purpose — Quartz's installer uses the pre-built output
directly (`hasPrebuiltDist`), so a fresh vault never compiles the plugin. Run
`npm run build` and commit `dist/` after changing anything under `src/`.
