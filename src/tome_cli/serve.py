#!/usr/bin/env python3
"""
tome_cli.serve — the local browse host for the no-build frontend.

`tome serve` is a stdlib http.server that does four things and nothing more:

  * serves the frontend's static files (ES modules, CSS, vendored libs) out of
    the package's `frontend/` directory — the permanent home for that code;
  * serves the vault's raw `.md` files under `/raw/…` (read-only, path-safe);
  * emits two generated JSON contracts, `/index.json` (the wiki catalogue +
    wikilink graph) and `/board.json` (the Backlog.md kanban), fresh on every
    request so the render always reflects the markdown on disk — the
    render-from-markdown rule ([[render-layer-principle]]), never the reverse;
  * accepts one write, `POST /api/task/<id>/move`, which shells out to the
    pinned backlog.md CLI (`cli.run_backlog`) rather than touching task YAML
    directly — the writes-through-CLI boundary from [[kanban-render-side]].
    A move carries the drop column's status plus `afterId` (the card it now
    sits after, or null for the top slot); the server resolves that into an
    `ordinal` by midpoint math against the column's current on-disk state
    (see `apply_task_move()` below, [[board-sort]]) rather than trusting a
    client-computed number. `board.json` carries a `writable` flag so the
    frontend can tell a live `tome serve` (true) from a frozen static export
    (false, see export_static() below) and hide drag-to-move accordingly —
    the static deploy has no server behind it to accept the POST at all;
  * accepts a sibling write, `POST /api/task`, filing a brand-new bare task —
    a kanban card with no wiki page — the same way, via `cli.run_backlog`
    (see `create_task()` below). Same uncommitted-until-`tome sync` contract
    as the status write, and the same `board.writable` gate;
  * accepts a second write, `POST /api/page`, editing a page's body through
    `cli.write_page` + the lint gate ([[page-editing]]) — see `save_page()`
    below for the conflict/lint contract. Also absent on a static export, and
    gated on the same `board.writable` flag client-side (no separate flag);
  * accepts a third write, `POST /api/frontmatter`, editing a page's title,
    tags, and description through the same `fm_set` + lint-gate machinery
    ([[frontmatter-editing]]) — see `save_frontmatter()` below. The other
    frontmatter fields (slug, type, project, status, created, updated) are
    read-only: they're either structural (derived from the file's path) or
    owned by another surface (the board, `tome mv`). Same conflict model,
    same `board.writable` gate, same absence from the static export;
  * accepts a fourth write, `POST /api/rename`, renaming a page's slug through
    `cli.move_page` — the `tome mv` core ([[slug-rename]]) — see `rename_page()`
    below. The slug is the filename, every `[[wikilink]]`'s target, and the
    page's URL at once, so this moves the file, rewrites inbound links wiki-
    wide, and returns the new URL for the client to redirect to. Same conflict
    model and `board.writable` gate, gated harder on lint (new-errors-only, not
    single-page-scoped), and likewise absent from the static export;
  * accepts a fifth write, `POST /api/new`, scaffolding a brand-new page
    through `cli.new_page` — the `tome new` core ([[page-creation]]) — see
    `create_page()` below. Unlike the other three writes, creation has no
    prior version to race against, so there's no `baseHash`: its guard is
    slug uniqueness, re-checked after a pull. Same `board.writable` gate and
    single-page-scoped lint gate as `save_page`; on rejection the just-
    scaffolded file (and any regenerated index/hub) are rolled back, never
    left half-created. Also absent from the static export;
  * serves the conflict surface ([[conflict-resolution]]): `GET
    /api/conflicts` reports a stopped rebase and the three sides of each
    unmerged file, and `POST /api/conflict/resolve|continue|abort` finish or
    unwind it. These exist because a forked history is the one conflict the
    write paths above can't answer alone — the rest they answer by returning
    the current text in their 409 so the client can merge it in place. Absent
    from the static export like every other write route.

The JSON *schemas* are the deliberate, permanent part of this slice; the
server internals and the frontend are rough by design and hardened in place by
later phases. build_index()/build_board() return plain dicts so the
static-export path (`--export`) can write them to disk unchanged.

stdlib only, imports cli lazily to avoid an import cycle (cli dispatches here).
"""

import hashlib
import importlib.resources
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

FRONTEND_DIR = importlib.resources.files("tome_cli") / "frontend"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


# --------------------------------------------------------------------------- #
# Generated contracts — pure functions of on-disk state, returned as dicts.
# --------------------------------------------------------------------------- #

def build_index(vault_root, conventions):
    """The `/index.json` contract: every wiki page as
    {slug, title, description, type, status, project, path, url, absPath,
    tags, updated, links}. `path` is POSIX-relative to wiki/; `url` is where
    the raw markdown is served; `absPath` is the source file's absolute path
    (forward-slashed, so it drops straight into a `vscode://file/` URI on
    any OS) for the frontend's edit affordance; `links` is the page's
    outbound wikilink slugs (the graph the frontend resolves `[[wikilinks]]`
    against). Pages that failed to read are skipped — the linter is the
    loud channel for those."""
    from tome_cli import cli

    wiki_root, pages = cli.collect(vault_root, conventions)
    out = []
    for p in pages:
        if "read_error" in p:
            continue
        meta = p.get("meta", {})
        rel = p["rel_path"]
        out.append({
            "slug": p["slug"],
            "title": meta.get("title") or p["slug"],
            "description": meta.get("description") or "",
            "type": meta.get("type") or "",
            "status": meta.get("status") or "",
            "project": PurePosixPath(rel).parts[0],
            "path": rel,
            "url": "/raw/" + rel,
            "absPath": (wiki_root / rel).as_posix(),
            "tags": meta.get("tags") or [],
            "updated": meta.get("updated") or "",
            "links": list(dict.fromkeys(p.get("links", []))),
        })
    out.sort(key=lambda e: e["slug"])
    tag_conv = conventions.get("tags", {})
    return {
        "pages": out,
        # The frontmatter editor's tag add-control ([[frontmatter-editing]])
        # offers this taxonomy plus, if allowed, each page's own project —
        # already present in `out` above, so only the taxonomy itself needs
        # to travel from conventions.toml to the client.
        "tagTaxonomy": sorted(tag_conv.get("taxonomy", [])),
        "allowProjectTags": bool(tag_conv.get("allow_project_name_tags")),
        # The new-page form's ([[page-creation]]) type dropdown — the same
        # enum `cli.new_page`/`tome new` validate against, so the client
        # never hardcodes it separately.
        "typeEnum": sorted(conventions.get("types", {}).get("enum", [])),
    }


_STATUSES_RE = re.compile(r"^statuses:\s*(\[.*\])\s*$")
_DEFAULT_STATUS_RE = re.compile(r"^default_status:\s*(.+?)\s*$")


def _read_board_config(backlog_dir):
    """Backlog.md's config.yml holds the canonical status ordering. Only two
    fields matter here and both are single-line; the statuses value is a JSON
    array (`["To Do", …]`), so json.loads reads it without a YAML parser."""
    statuses, default_status = [], ""
    config_path = backlog_dir / "config.yml"
    if not config_path.is_file():
        return statuses, default_status
    for line in config_path.read_text(encoding="utf-8").splitlines():
        m = _STATUSES_RE.match(line)
        if m:
            try:
                statuses = [str(s) for s in json.loads(m.group(1))]
            except (json.JSONDecodeError, TypeError):
                pass
            continue
        m = _DEFAULT_STATUS_RE.match(line)
        if m:
            default_status = m.group(1).strip().strip('"').strip("'")
    return statuses, default_status


def build_board(vault_root, conventions):
    """The `/board.json` contract: the kanban read from backlog/tasks/*.md.
    Reuses cli's existing task frontmatter readers rather than adding
    another hand-rolled parser."""
    from tome_cli import cli

    backlog_dir = vault_root / "backlog"
    statuses, default_status = _read_board_config(backlog_dir)

    cards = []
    tasks_dir = backlog_dir / "tasks"
    if tasks_dir.is_dir():
        for path in sorted(tasks_dir.glob("*.md")):
            fm_lines, _ = cli.read_page(path)
            raw_id = cli.fm_get(fm_lines, "id") or ""
            if not raw_id:
                continue
            labels = cli.task_block_list(fm_lines, "labels")
            project = next((l[len("project:"):] for l in labels
                            if l.startswith("project:")), None)
            ordinal_raw = cli.fm_get(fm_lines, "ordinal")
            try:
                ordinal = int(ordinal_raw) if ordinal_raw not in (None, "") else None
            except ValueError:
                ordinal = None
            cards.append({
                "id": raw_id.lower(),
                "rawId": raw_id,
                "title": cli.task_title(fm_lines) or raw_id,
                "status": cli.fm_get(fm_lines, "status") or "",
                "project": project,
                "priority": cli.fm_get(fm_lines, "priority"),
                "ordinal": ordinal,
                "milestone": cli.fm_get(fm_lines, "milestone"),
                "labels": labels,
                "references": cli.task_block_list(fm_lines, "references"),
            })
    return {"statuses": statuses, "defaultStatus": default_status, "cards": cards}


def _board_with_writable(vault_root, conventions, writable):
    """`build_board()` stays a pure function of on-disk state (and its tests
    assert an exact dict shape); `writable` is a serving-time fact layered on
    top, not vault state, so it's added here rather than inside build_board."""
    return {**build_board(vault_root, conventions), "writable": writable}


# --------------------------------------------------------------------------- #
# Task move writes — the one mutation this server accepts, always shelled
# through backlog.md per [[kanban-render-side]]. A move carries a target
# status and an `afterId` position reference; the ordinal the client never
# sees is computed here, by midpoint, against the column's current on-disk
# state ([[board-sort]]). Split out from the HTTP handler so it's
# unit-testable without a live server.
# --------------------------------------------------------------------------- #

_ORDINAL_GAP = 1000
_ORDINAL_BASE = 10000


def _normalize_card_id(raw):
    """`board.json` card ids are `task-<n>` (lowercase); accept the same
    case/prefix variations `apply_task_move`'s own task id does, so an
    `afterId` round-tripped from a card the frontend already has always
    matches. None/empty means "top of column"."""
    if not raw:
        return None
    s = str(raw).strip()
    if s.upper().startswith("TASK-"):
        s = s[len("TASK-"):]
    return f"task-{s}" if s else None


def _column_cards(vault_root, status, exclude_id=None):
    """`(card_id, ordinal)` pairs currently in `status`, ordinal-ascending
    (missing ordinal sorts last, same as the frontend's `?? Infinity`) —
    reuses `build_board`'s own frontmatter readers rather than a second
    parser. `exclude_id` (the card being moved) is left out so its own old
    ordinal never enters the neighbour math, including for an in-column
    reorder."""
    from tome_cli import cli

    tasks_dir = vault_root / "backlog" / "tasks"
    cards = []
    if tasks_dir.is_dir():
        for path in sorted(tasks_dir.glob("*.md")):
            fm_lines, _ = cli.read_page(path)
            raw_id = cli.fm_get(fm_lines, "id") or ""
            if not raw_id:
                continue
            card_id = raw_id.lower()
            if card_id == exclude_id or (cli.fm_get(fm_lines, "status") or "") != status:
                continue
            ordinal_raw = cli.fm_get(fm_lines, "ordinal")
            try:
                ordinal = int(ordinal_raw) if ordinal_raw not in (None, "") else None
            except ValueError:
                ordinal = None
            cards.append((card_id, ordinal))
    cards.sort(key=lambda c: c[1] if c[1] is not None else float("inf"))
    return cards


def _compute_ordinal(cards, after_id):
    """The ordinal for a card dropped after `after_id` in `cards` (ascending,
    `after_id`-excluded already). Returns `(ordinal, needs_rebalance)`:
    `needs_rebalance` is True when the two neighbours are adjacent integers
    with no midpoint left, and the caller must renumber the column first."""
    if not cards:
        return _ORDINAL_BASE, False

    if after_id is None:
        first = cards[0][1]
        return (first - _ORDINAL_GAP) if first is not None else _ORDINAL_BASE, False

    idx = next((i for i, (cid, _) in enumerate(cards) if cid == after_id), None)
    if idx is None:
        # Stale or unknown afterId (e.g. the client's view raced a concurrent
        # edit) — fall back to the bottom rather than failing the move.
        last = cards[-1][1]
        return (last + _ORDINAL_GAP) if last is not None else _ORDINAL_BASE, False

    after_ordinal = cards[idx][1]
    if after_ordinal is None:
        return _ORDINAL_BASE, False
    if idx + 1 >= len(cards):
        return after_ordinal + _ORDINAL_GAP, False

    next_ordinal = cards[idx + 1][1]
    if next_ordinal is None:
        return after_ordinal + _ORDINAL_GAP, False
    if next_ordinal - after_ordinal <= 1:
        return None, True
    return (after_ordinal + next_ordinal) // 2, False


def apply_task_move(vault_root, raw_task_id, status, raw_after_id):
    """Moves a backlog task to `status`, positioned after `raw_after_id` (or
    the top of the column when falsy), via `backlog.md task edit -s
    --ordinal`. Returns (ok, message) — message is empty on success, an
    error string otherwise. `raw_task_id` accepts either case and an
    optional `task-`/`TASK-` prefix, matching what `board.json` cards and
    the frontend's URLs carry."""
    from tome_cli import cli

    task_id = raw_task_id.strip()
    if task_id.upper().startswith("TASK-"):
        task_id = task_id[len("TASK-"):]
    if not task_id.isdigit():
        return False, f"bad task id {raw_task_id!r}"
    if not status:
        return False, "status is required"

    card_id = f"task-{task_id}"
    after_id = _normalize_card_id(raw_after_id)
    cards = _column_cards(vault_root, status, exclude_id=card_id)

    ordinal, needs_rebalance = _compute_ordinal(cards, after_id)
    if needs_rebalance:
        renumbered = []
        for i, (cid, _) in enumerate(cards):
            new_ordinal = _ORDINAL_BASE + i * _ORDINAL_GAP
            renumbered.append((cid, new_ordinal))
            raw_cid = cid[len("task-"):]
            proc = cli.run_backlog(vault_root, ["task", "edit", raw_cid, "--ordinal", str(new_ordinal)],
                                    capture=True)
            if proc.returncode != 0:
                message = (proc.stderr or proc.stdout).strip() or "backlog task edit failed"
                return False, message
        ordinal, _ = _compute_ordinal(renumbered, after_id)

    proc = cli.run_backlog(vault_root, ["task", "edit", task_id, "-s", status, "--ordinal", str(ordinal)],
                            capture=True)
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip() or "backlog task edit failed"
        return False, message
    return True, ""


def create_task(vault_root, title, status, project, priority, description):
    """Files a bare backlog task — a kanban card with no wiki page — via
    `backlog task create` ([[in-ui-creation]]'s New Task form). Matches
    apply_task_status's writes-through-CLI discipline: no page, no lint gate,
    and (like a drag-to-move) no commit here either — task writes stay
    uncommitted, picked up by the next `tome sync`. Returns (ok, message);
    on success message is the new card's board id (e.g. "task-79")."""
    from tome_cli import cli

    title = (title or "").strip()
    if not title:
        return False, "title is required"
    if not status:
        return False, "status is required"

    argv = ["task", "create", title, "-s", status, "--plain"]
    if description:
        argv += ["-d", description]
    if project:
        argv += ["-l", f"project:{project}"]
    if priority:
        argv += ["--priority", priority]

    proc = cli.run_backlog(vault_root, argv, capture=True)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip() or "backlog task create failed"

    m = re.search(r"^File: (.+)$", proc.stdout, re.MULTILINE)
    if not m:
        return False, "task created but its file path could not be parsed"
    fm_lines, _ = cli.read_page(Path(m.group(1).strip()))
    raw_id = cli.fm_get(fm_lines, "id") or ""
    if not raw_id:
        return False, "task created but its id could not be read"
    return True, raw_id.lower()


def _page_path(vault_root, rel):
    """Resolve a wiki-relative path to an existing `.md` file under wiki/, or
    None if it's unsafe, non-.md, or doesn't exist — the same path-safety
    gate `_send_raw` applies, reused here since both routes accept a
    client-supplied wiki-relative path."""
    safe = _safe_join(rel)
    if safe is None or safe.suffix != ".md":
        return None
    wiki_root = (vault_root / "wiki").resolve()
    target = (wiki_root / str(safe)).resolve()
    if wiki_root not in target.parents:
        return None
    return target if target.is_file() else None


# --------------------------------------------------------------------------- #
# Conflicts ([[conflict-resolution]]). Two triggers, one three-way model: a
# save racing a local write (A), and a `git pull --rebase` whose history forked
# (B). Both hand the resolver a base, the user's buffer, and an external
# version — only the *sources* differ, so both are described by the same
# `conflict` object on the wire:
#
#   {"type": "local-drift"|"git-fork", ...provenance..., + sides}
#
# A is the workhorse: every write path pulls before its conflict gate, so a
# remote change that rebases cleanly arrives looking like plain disk drift. B
# is the residual — committed histories that genuinely conflict — and is the
# only case that leaves the tree mid-rebase, which the endpoints below exist
# to get it back out of.
# --------------------------------------------------------------------------- #

def _local_drift_conflict(target, current_hash):
    """The 409 body for a stale `baseHash`: the sides the resolver needs plus
    who/when provenance. There's no author for an uncommitted local write —
    it was VS Code, an agent, or a `tome` command — so the *who* is honestly
    omitted rather than guessed, and mtime carries the *when*."""
    return {
        "error": "page changed since you opened it",
        "currentHash": current_hash,
        "conflict": {
            "type": "local-drift",
            "source": "disk",
            "theirs": target.read_text(encoding="utf-8"),
            "mtime": target.stat().st_mtime,
        },
    }


def _git_bytes(vault_root, args):
    """`cli.run_git` decodes with the locale codec (cp1252 on Windows), which
    would mangle any non-ASCII page. Blob reads must be byte-exact, so this
    runs git itself and decodes UTF-8 explicitly."""
    from tome_cli import cli

    return subprocess.run(["git", *args], cwd=str(vault_root),
                          capture_output=True, env=cli._git_env())


def _git_text(vault_root, args):
    """The stdout of `args` as UTF-8 text, or None if git failed."""
    proc = _git_bytes(vault_root, args)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


_COMMIT_FORMAT = "%an%x00%ae%x00%aI%x00%h%x00%s"


def _commit_meta(vault_root, rev):
    """{author, email, date, sha, subject} for `rev`, or None if it doesn't
    resolve (REBASE_HEAD only exists mid-rebase, for instance)."""
    out = _git_text(vault_root, ["log", "-1", f"--format={_COMMIT_FORMAT}", rev])
    if out is None:
        return None
    parts = out.rstrip("\n").split("\0")
    if len(parts) < 5:
        return None
    return {"author": parts[0], "email": parts[1], "date": parts[2],
            "sha": parts[3], "subject": parts[4]}


def rebase_in_progress(vault_root):
    """True while a rebase is stopped part-way — the state a conflicted
    `git pull --rebase` leaves behind. Public because cli.py checks it too,
    to point a failed `tome sync` at the browser resolver."""
    from tome_cli import cli

    for name in ("rebase-merge", "rebase-apply"):
        probe = cli.run_git(vault_root, ["rev-parse", "--git-path", name])
        if probe.returncode != 0:
            continue
        path = Path(probe.stdout.strip())
        if not path.is_absolute():
            path = vault_root / path
        if path.exists():
            return True
    return False


def git_conflict_state(vault_root):
    """The `git-fork` conflict object: every file the stopped rebase left
    unmerged, each with the three sides the resolver wants, plus provenance
    for both.

    The stage-to-side mapping is the one thing here that is easy to get
    backwards. During a rebase git replays *your* commits onto the upstream,
    so HEAD is the upstream side: stage `:2:` ("ours") is the **remote**, and
    stage `:3:` ("theirs") is **your** commit being replayed. The resolver's
    `mine`/`theirs` therefore come from `:3:`/`:2:` respectively — inverted
    from the raw git labels, and named from the user's point of view.

    Returns {"rebase": False} when no rebase is in flight.
    """
    if not rebase_in_progress(vault_root):
        return {"rebase": False, "files": []}

    listing = _git_text(vault_root, ["diff", "--name-only", "--diff-filter=U"]) or ""
    files = []
    for rel in [line for line in listing.splitlines() if line.strip()]:
        base = _git_text(vault_root, ["show", f":1:{rel}"])
        remote = _git_text(vault_root, ["show", f":2:{rel}"])
        local = _git_text(vault_root, ["show", f":3:{rel}"])
        files.append({
            "path": rel,
            # An add/add conflict has no stage 1; the resolver treats a missing
            # ancestor as an empty one, which makes every line a conflict —
            # honest, since there genuinely is no common ancestor.
            "base": base or "",
            "mine": local or "",
            "theirs": remote or "",
        })

    return {
        "rebase": True,
        "files": files,
        # HEAD mid-rebase is the upstream tip the replay is landing on: the
        # remote commit whose lines the user is being asked to weigh.
        "theirsCommit": _commit_meta(vault_root, "HEAD"),
        # REBASE_HEAD is the commit currently being replayed — the user's own.
        "mineCommit": _commit_meta(vault_root, "REBASE_HEAD"),
    }


def _pull_or_conflict(vault_root):
    """Every write path's first step. Returns None when the pull landed, else
    the (status, payload) the caller should return: a 409 carrying the
    `git-fork` conflict when the rebase stopped on one — the resolver's cue,
    replacing the old dead-end 'resolve manually' — or a plain 500."""
    from tome_cli import cli

    pull = cli.run_git(vault_root, ["pull", "--rebase", "--autostash"])
    if pull.returncode == 0:
        return None
    state = git_conflict_state(vault_root)
    if state["rebase"]:
        return 409, {"error": "the vault's history diverged from the remote",
                     "conflict": {"type": "git-fork", **state}}
    return 500, {"error": (pull.stderr or pull.stdout).strip() or "git pull failed"}


def _push_or_conflict(vault_root):
    """The tail of every write path. `cli._push_with_retry` re-pulls on a
    rejected push, so its failure can also be a stopped rebase — same 409, so
    a fork that shows up at push time lands in the resolver instead of the
    same dead end."""
    from tome_cli import cli

    if cli._push_with_retry(vault_root) == 0:
        return None
    state = git_conflict_state(vault_root)
    if state["rebase"]:
        return 409, {"error": "your commit landed locally, but the vault's "
                              "history diverged from the remote",
                     "conflict": {"type": "git-fork", **state}}
    return 500, {"error": "commit landed locally but push failed — resolve manually"}


def resolve_conflict_file(vault_root, rel, content):
    """Write one resolved file from the resolver's merged buffer and stage it.
    Scoped hard to the paths git itself reports as unmerged: the resolver is
    only ever allowed to finish a conflict git handed it, never to write an
    arbitrary path."""
    from tome_cli import cli

    state = git_conflict_state(vault_root)
    if not state["rebase"]:
        return 409, {"error": "no rebase is in progress"}
    if rel not in [f["path"] for f in state["files"]]:
        return 400, {"error": f"{rel!r} is not an unmerged file in this rebase"}
    if not isinstance(content, str):
        return 400, {"error": "content must be a string"}

    (vault_root / rel).write_text(content, encoding="utf-8", newline="\n")
    add = cli.run_git(vault_root, ["add", "--", rel])
    if add.returncode != 0:
        return 500, {"error": (add.stderr or "git add failed").strip()}
    return 200, {"conflict": git_conflict_state(vault_root)}


def continue_rebase(vault_root):
    """`git rebase --continue` once every conflicted file is resolved, then
    push. A rebase replays commit by commit, so continuing can stop again on
    the *next* commit — that's not an error, it's the next thing to resolve,
    so the fresh state goes back to the client either way.

    `-c core.editor=true` keeps git from opening an editor for the replayed
    commit's message: there is no terminal behind this server to host one.
    """
    from tome_cli import cli

    state = git_conflict_state(vault_root)
    if not state["rebase"]:
        return 409, {"error": "no rebase is in progress"}
    if state["files"]:
        return 400, {"error": f"{len(state['files'])} file(s) still unmerged",
                     "conflict": state}

    cont = cli.run_git(vault_root, ["-c", "core.editor=true", "rebase", "--continue"])
    after = git_conflict_state(vault_root)
    if after["rebase"]:
        if after["files"]:
            return 200, {"done": False, "conflict": after}
        return 500, {"error": (cont.stderr or cont.stdout).strip()
                              or "git rebase --continue failed",
                     "conflict": after}
    if cont.returncode != 0:
        return 500, {"error": (cont.stderr or cont.stdout).strip()
                              or "git rebase --continue failed"}

    push = cli._push_with_retry(vault_root)
    if push != 0:
        return 500, {"done": True, "error": "rebase finished but the push failed — "
                                            "run `tome sync` to retry"}
    return 200, {"done": True}


def abort_rebase(vault_root):
    """The cancel path: `git rebase --abort` returns the tree to the known
    state it had before the pull, rather than leaving it half-resolved."""
    from tome_cli import cli

    if not rebase_in_progress(vault_root):
        return 409, {"error": "no rebase is in progress"}
    proc = cli.run_git(vault_root, ["rebase", "--abort"])
    if proc.returncode != 0:
        return 500, {"error": (proc.stderr or proc.stdout).strip()
                              or "git rebase --abort failed"}
    return 200, {"aborted": True}


def save_page(vault_root, conventions, rel, body, base_hash):
    """The [[page-editing]] save path: optimistic-concurrency write of one
    page's body, gated by a lint check scoped to just that page. Returns
    (http_status, payload_dict) — never raises, so the HTTP handler can pass
    the pair straight through to `_send_json`.

    1. Pull, so the conflict check below is against the latest remote. A pull
       that stops on a forked history is itself a conflict (409, `git-fork`).
    2. Hash the file's current bytes; a `base_hash` mismatch means the page
       changed since the client opened it — refuse, write nothing, and return
       the current text plus its provenance (409, `local-drift`) so the client
       can open the three-way resolver instead of asking the user to
       copy-and-reload ([[conflict-resolution]]).
    3. Recombine the on-disk frontmatter with the new body via
       `cli.write_page` (frontmatter itself is out of scope for this editor).
    4. Lint the whole vault but gate only on findings whose `path` is this
       page — an unrelated pre-existing error elsewhere must not block an
       otherwise-clean save. Any error here restores the original bytes (422).
    5. Commit + push, scoped to just this file, reusing `cli._push_with_retry`
       (a scoped `cli.sync_core` call would re-pull and re-lint the whole
       tree; this only needs its push-retry half).
    """
    from tome_cli import cli

    target = _page_path(vault_root, rel)
    if target is None:
        return 404, {"error": "no such page"}

    conflict = _pull_or_conflict(vault_root)
    if conflict is not None:
        return conflict

    original_bytes = target.read_bytes()
    current_hash = hashlib.sha256(original_bytes).hexdigest()
    if base_hash != current_hash:
        return 409, _local_drift_conflict(target, current_hash)

    fm_lines, _old_body = cli.read_page(target)
    try:
        cli.write_page(target, fm_lines, body)
    except cli.VaultError as e:
        return 400, {"error": str(e)}

    wiki_root = (vault_root / "wiki").resolve()
    rel_str = target.relative_to(wiki_root).as_posix()  # lint findings key by this
    pages, findings = cli.run_all_lint_checks(vault_root, conventions)
    errors = [f for f in findings if f.severity == cli.ERROR and f.path == rel_str]
    if errors:
        target.write_bytes(original_bytes)
        return 422, {"error": "lint failed", "findings": [f.as_dict() for f in errors]}

    vault_rel_str = target.relative_to(vault_root).as_posix()  # git wants this one
    add = cli.run_git(vault_root, ["add", "--", vault_rel_str])
    if add.returncode != 0:
        target.write_bytes(original_bytes)
        return 500, {"error": (add.stderr or "git add failed").strip()}

    commit = cli.run_git(vault_root, ["commit", "-m", f"edit: {target.stem}"])
    if commit.returncode != 0:
        return 500, {"error": (commit.stderr or commit.stdout).strip() or "git commit failed"}

    push_conflict = _push_or_conflict(vault_root)
    if push_conflict is not None:
        return push_conflict

    return 200, {"hash": hashlib.sha256(target.read_bytes()).hexdigest()}


_FM_EDITABLE_FIELDS = {"title", "tags", "description"}


def _rebuild_derived(vault_root, conventions, wiki_root, ptype, project):
    """Re-run the index (and, for a plan, the hub) generation that title/tags/
    description feed into — the same always-run step `cmd_describe`/`cmd_new`
    take after a frontmatter write, done here explicitly since this path has
    no CLI command to fall through to. Called both after a save (to make the
    new state current) and to undo a rejected save (regenerating from the
    just-restored bytes, so a failed edit never leaves index.md/the hub
    pointing at frontmatter that no longer exists on disk)."""
    from tome_cli import cli

    _, pages = cli.collect(vault_root, conventions)
    index_path = cli.rebuild_index(vault_root, conventions, wiki_root, pages)
    hub_path = cli.regenerate_hub(conventions, wiki_root, pages, project) if ptype == "plan" else None
    return index_path, hub_path


def save_frontmatter(vault_root, conventions, rel, fields, base_hash):
    """The [[frontmatter-editing]] save path: optimistic-concurrency write of
    title/tags/description, gated by a lint check scoped to just this page.
    Returns (http_status, payload_dict), mirroring `save_page()`.

    1. Pull, hash-check `base_hash` exactly as `save_page` does (409 on a
       stale base, nothing written).
    2. Diff each editable field against the page's *parsed* frontmatter
       (`cli.collect`'s dict, not raw fm_lines — tags is a list there, so the
       comparison doesn't need its own list-vs-string parsing) and reject
       (400, nothing written) any value that would corrupt the hand-rolled
       frontmatter subset once quoted/inlined: a literal quote or newline, or
       — for tags — a comma/bracket that would split a inline-list entry.
       No changed fields is a no-op 200, not a write.
    3. Apply changed fields via `fm_set` — the same primitive `cmd_describe`
       uses — bump `updated`, and write through `cli.write_page`.
    4. Regenerate the index (+ hub, if this is a plan): unlike `save_page`'s
       body edits, title/tags/description feed the generated index, so this
       must happen *before* the lint gate below or every save would trip
       INDEX_DRIFT against itself.
    5. Lint gate scoped to this page's `rel_str`, same rule as `save_page`:
       an unrelated pre-existing error elsewhere must not block an otherwise-
       clean save. On any error here, restore the original bytes *and*
       regenerate the index/hub again so they don't keep pointing at
       frontmatter that no longer exists (422).
    6. Commit every touched path (page, index, hub) + push, reusing
       `cli._push_with_retry` like `save_page`.
    """
    from tome_cli import cli

    target = _page_path(vault_root, rel)
    if target is None:
        return 404, {"error": "no such page"}

    unknown = set(fields) - _FM_EDITABLE_FIELDS
    if unknown:
        return 400, {"error": f"unsupported field(s): {', '.join(sorted(unknown))}"}

    conflict = _pull_or_conflict(vault_root)
    if conflict is not None:
        return conflict

    original_bytes = target.read_bytes()
    current_hash = hashlib.sha256(original_bytes).hexdigest()
    if base_hash != current_hash:
        return 409, _local_drift_conflict(target, current_hash)

    wiki_root = (vault_root / "wiki").resolve()
    rel_str = target.relative_to(wiki_root).as_posix()
    _, pages = cli.collect(vault_root, conventions)
    page = next((p for p in pages if p["rel_path"] == rel_str and "read_error" not in p), None)
    if page is None:
        return 400, {"error": "page frontmatter could not be parsed"}

    changed = {}
    if "title" in fields:
        new_title = fields["title"]
        if not isinstance(new_title, str) or not new_title.strip():
            return 400, {"error": "title must be a non-empty string"}
        try:
            cli.validate_oneline(new_title, "title")
        except cli.VaultError as e:
            return 400, {"error": str(e)}
        if new_title != (page["meta"].get("title") or ""):
            changed["title"] = new_title

    if "tags" in fields:
        new_tags = fields["tags"]
        if not isinstance(new_tags, list) or not all(isinstance(t, str) for t in new_tags):
            return 400, {"error": "tags must be a list of strings"}
        new_tags = [t.strip() for t in new_tags]
        if not all(new_tags):
            return 400, {"error": "tags must not be empty"}
        for t in new_tags:
            if any(ch in t for ch in ',[]"\'\n'):
                return 400, {"error": f"tag {t!r} contains an unsupported character"}
        if new_tags != (page["meta"].get("tags") or []):
            changed["tags"] = new_tags

    if "description" in fields:
        new_desc = fields["description"]
        if not isinstance(new_desc, str):
            return 400, {"error": "description must be a string"}
        max_chars = conventions.get("description", {}).get("max_chars", 140)
        try:
            cli.validate_oneline(new_desc, "description", max_chars)
        except cli.VaultError as e:
            return 400, {"error": str(e)}
        if new_desc != (page["meta"].get("description") or ""):
            changed["description"] = new_desc

    if not changed:
        return 200, {"hash": current_hash}

    fm_lines, body = cli.read_page(target)
    if "title" in changed:
        cli.fm_set(fm_lines, "title", changed["title"], quote=True)
    if "tags" in changed:
        cli.fm_set(fm_lines, "tags", "[" + ", ".join(changed["tags"]) + "]")
    if "description" in changed:
        cli.fm_set(fm_lines, "description", changed["description"], quote=True)
    cli.fm_set(fm_lines, "updated", cli.today())
    try:
        cli.write_page(target, fm_lines, body)
    except cli.VaultError as e:
        return 400, {"error": str(e)}

    ptype = page["meta"].get("type")
    project = PurePosixPath(rel_str).parts[0]
    index_path, hub_path = _rebuild_derived(vault_root, conventions, wiki_root, ptype, project)

    _, findings = cli.run_all_lint_checks(vault_root, conventions)
    errors = [f for f in findings if f.severity == cli.ERROR and f.path == rel_str]
    if errors:
        target.write_bytes(original_bytes)
        _rebuild_derived(vault_root, conventions, wiki_root, ptype, project)
        return 422, {"error": "lint failed", "findings": [f.as_dict() for f in errors]}

    touched = [target, index_path] + ([hub_path] if hub_path is not None else [])
    rel_paths = [str(p.resolve().relative_to(vault_root)) for p in touched]
    add = cli.run_git(vault_root, ["add", "--", *rel_paths])
    if add.returncode != 0:
        target.write_bytes(original_bytes)
        _rebuild_derived(vault_root, conventions, wiki_root, ptype, project)
        return 500, {"error": (add.stderr or "git add failed").strip()}

    commit = cli.run_git(vault_root, ["commit", "-m", f"edit frontmatter: {target.stem}"])
    if commit.returncode != 0:
        return 500, {"error": (commit.stderr or commit.stdout).strip() or "git commit failed"}

    push_conflict = _push_or_conflict(vault_root)
    if push_conflict is not None:
        return push_conflict

    return 200, {"hash": hashlib.sha256(target.read_bytes()).hexdigest()}


def _reset_move(vault_root, result):
    """Undo `cli.move_page`'s on-disk changes when a rename is rejected after
    the move ran. Unlike save_page/save_frontmatter — which snapshot the one
    edited file's bytes — a rename spans many files (the renamed page, every
    rewritten linker, the index, the hub), so the reset is scoped to exactly
    the paths the move touched: unlink the new (untracked) file, then restore
    every other touched path from HEAD (the deleted original, the rewritten
    linkers, the regenerated index/hub). Nothing outside `touched_paths` is
    reset, so a concurrent unrelated dirty file is left alone."""
    from tome_cli import cli

    if result.new_path.exists():
        result.new_path.unlink()
    rel_paths = [str(p.resolve().relative_to(vault_root)) for p in result.touched_paths
                 if p != result.new_path]
    if rel_paths:
        cli.run_git(vault_root, ["checkout", "HEAD", "--", *rel_paths])


def rename_page(vault_root, conventions, rel, new_slug, base_hash):
    """The [[slug-rename]] save path: rename a page's slug through
    `cli.move_page` — the same core `tome mv` uses — under the same optimistic-
    concurrency gate as the body/frontmatter editors. Returns (http_status,
    payload_dict), never raising, mirroring `save_page`/`save_frontmatter`.

    A slug rename is categorically heavier than a field edit: the slug is the
    filename, the target every `[[wikilink]]` resolves against, and the page's
    own URL — so a botched rename dangles links across the whole vault. This
    path therefore gates harder than save_page's single-page lint scope.

    1. Validate the new slug's shape, then pull so the conflict check is
       against the latest remote.
    2. Hash the file's current bytes; a `base_hash` mismatch means the page
       changed since the client opened it — refuse, rename nothing (409).
    3. Snapshot the pre-move lint errors, then call `cli.move_page` (file move
       + wiki-wide link rewrite + index/hub regen). A VaultError from it
       (bad/taken slug, project hub, collision) is a 400, nothing moved.
    4. Lint gate: any error present after the move that wasn't there before is
       a hard failure (422) — this catches a linker the rewrite somehow left
       dangling even on a page outside the touched set, which a scoped-to-
       touched-paths gate would miss; pre-existing unrelated errors are
       ignored. On failure the move is reset (there's no single buffer to
       restore — the whole touched set is rolled back from HEAD).
    5. Commit the union of touched paths (old path's deletion, new path, rebuilt
       index/hub, every rewritten linker) + push, reusing `cli._push_with_retry`
       like the other write paths. Return the new slug's in-app URL.

    As with every write route, this endpoint is absent from the static export.
    """
    from tome_cli import cli

    target = _page_path(vault_root, rel)
    if target is None:
        return 404, {"error": "no such page"}

    if not isinstance(new_slug, str) or not cli.SLUG_RE.match(new_slug):
        return 400, {"error": f"{new_slug!r} is not a valid slug (lowercase kebab-case)"}

    conflict = _pull_or_conflict(vault_root)
    if conflict is not None:
        return conflict

    original_bytes = target.read_bytes()
    current_hash = hashlib.sha256(original_bytes).hexdigest()
    if base_hash != current_hash:
        # No resolver payload here, unlike save_page/save_frontmatter: two
        # different names for one page is not a thing three-way merge has an
        # answer to, so a slug conflict stays refuse-and-reload
        # ([[conflict-resolution]]).
        return 409, {"error": "page changed since you opened it",
                     "currentHash": current_hash}

    slug = target.stem  # the file's stem is its slug (find_page keys on it)
    if new_slug == slug:
        return 200, {"slug": slug, "url": f"?page={slug}", "hash": current_hash}

    def _err_sig(findings):
        return {(f.code, f.path, f.message) for f in findings
                if f.severity == cli.ERROR}

    _, pre_findings = cli.run_all_lint_checks(vault_root, conventions)
    pre_errors = _err_sig(pre_findings)

    try:
        result = cli.move_page(vault_root, conventions, slug, new_slug)
    except cli.VaultError as e:
        return 400, {"error": str(e)}

    _, post_findings = cli.run_all_lint_checks(vault_root, conventions)
    new_errors = [f for f in post_findings
                  if f.severity == cli.ERROR and (f.code, f.path, f.message) not in pre_errors]
    if new_errors:
        _reset_move(vault_root, result)
        return 422, {"error": "lint failed", "findings": [f.as_dict() for f in new_errors]}

    rel_paths = [str(p.resolve().relative_to(vault_root)) for p in result.touched_paths]
    add = cli.run_git(vault_root, ["add", "-A", "--", *rel_paths])
    if add.returncode != 0:
        _reset_move(vault_root, result)
        return 500, {"error": (add.stderr or "git add failed").strip()}

    commit = cli.run_git(vault_root, ["commit", "-m", f"mv: {slug} -> {new_slug}"])
    if commit.returncode != 0:
        _reset_move(vault_root, result)
        return 500, {"error": (commit.stderr or commit.stdout).strip() or "git commit failed"}

    push_conflict = _push_or_conflict(vault_root)
    if push_conflict is not None:
        return push_conflict

    return 200, {"slug": new_slug, "url": f"?page={new_slug}",
                 "hash": hashlib.sha256(result.new_path.read_bytes()).hexdigest()}


def _reset_create(vault_root, result):
    """Undo `cli.new_page`'s on-disk changes when a create is rejected after
    scaffolding ran — mirrors `_reset_move`: unlink the new (untracked) page,
    then restore every other touched path from HEAD (the regenerated index
    and, for a plan/project, the hub)."""
    from tome_cli import cli

    if result.path.exists():
        result.path.unlink()
    rel_paths = [str(p.resolve().relative_to(vault_root)) for p in result.touched_paths
                 if p != result.path]
    if rel_paths:
        cli.run_git(vault_root, ["checkout", "HEAD", "--", *rel_paths])


def create_page(vault_root, conventions, type_, project, slug, title, desc, link_task=None):
    """The [[page-creation]] save path: scaffold a new page through
    `cli.new_page` — the same core `tome new` uses. Returns (http_status,
    payload_dict), never raising, mirroring save_page/save_frontmatter/
    rename_page.

    Creation has no `baseHash` — there's no prior version to race against.
    Its analogous guard is slug uniqueness, checked fresh against the vault
    state after the pull below rather than against a client-supplied hash.

    1. Pull, so the uniqueness check inside `cli.new_page` runs against the
       latest remote — a slug someone else just created elsewhere is caught
       here, not after the write.
    2. Call `cli.new_page`. A VaultError (bad type, missing/unknown project,
       bad/taken slug, path collision) is a 422 with the reason, not a CLI
       traceback — nothing is written.
    3. Lint gate scoped to the new page's path — a freshly scaffolded page is
       valid by construction, so this rarely fires. On failure there's no
       prior buffer to restore (unlike save_page/save_frontmatter): the
       just-created file is deleted and the index/hub reset from HEAD via
       `_reset_create`, never leaving a half-created page (422).
    4. When `link_task` names an existing backlog task (the New Task form's
       "Save & create plan" handoff, [[in-ui-creation]]), add the new page to
       that task's `references` — the same task<->plan link `tome new plan
       --with-task` writes, minted the other direction since the task already
       exists. Existing references are preserved: `task edit --ref` sets the
       whole list, so this reads them first and re-passes every one alongside
       the new page. A bad task id rolls the scaffold back like a lint
       failure; a failed edit rolls back both the scaffold and, if the task
       file was already touched, restores it from HEAD too.
    5. Commit the touched set (new page, index, hub for a plan/project, and
       the linked task if any) + push, reusing `cli._push_with_retry`.
       Returns the new page's in-app URL for the client to redirect to.

    As with every write route, this endpoint is absent from the static
    export.
    """
    from tome_cli import cli

    conflict = _pull_or_conflict(vault_root)
    if conflict is not None:
        return conflict

    try:
        result = cli.new_page(vault_root, conventions, type_, project, slug, title, desc)
    except cli.VaultError as e:
        return 422, {"error": str(e)}

    wiki_root = (vault_root / "wiki").resolve()
    rel_str = result.path.relative_to(wiki_root).as_posix()
    _, findings = cli.run_all_lint_checks(vault_root, conventions)
    errors = [f for f in findings if f.severity == cli.ERROR and f.path == rel_str]
    if errors:
        _reset_create(vault_root, result)
        return 422, {"error": "lint failed", "findings": [f.as_dict() for f in errors]}

    task_path = None
    if link_task:
        task_num = str(link_task).strip()
        if task_num.upper().startswith("TASK-"):
            task_num = task_num[len("TASK-"):]
        task_path = cli.find_task_file(vault_root, task_num)
        if task_path is None:
            _reset_create(vault_root, result)
            return 400, {"error": f"no such task: {link_task}"}

        plan_ref = f"wiki/{rel_str}"
        fm_lines, _ = cli.read_page(task_path)
        refs = cli.task_references(fm_lines)
        task_id = cli.task_id_from_path(task_path)
        edit_argv = ["task", "edit", task_id]
        for r in refs:
            edit_argv += ["--ref", r]
        edit_argv += ["--ref", plan_ref]
        proc = cli.run_backlog(vault_root, edit_argv, capture=True)
        if proc.returncode != 0:
            _reset_create(vault_root, result)
            return 400, {"error": (proc.stderr or proc.stdout).strip() or "linking task failed"}

    rel_paths = [str(p.resolve().relative_to(vault_root)) for p in result.touched_paths]
    if task_path is not None:
        rel_paths.append(str(task_path.resolve().relative_to(vault_root)))
    add = cli.run_git(vault_root, ["add", "--", *rel_paths])
    if add.returncode != 0:
        _reset_create(vault_root, result)
        if task_path is not None:
            cli.run_git(vault_root, ["checkout", "HEAD", "--", str(task_path.resolve().relative_to(vault_root))])
        return 500, {"error": (add.stderr or "git add failed").strip()}

    commit_msg = f"new: {result.slug}" + (f" (linked {link_task})" if task_path is not None else "")
    commit = cli.run_git(vault_root, ["commit", "-m", commit_msg])
    if commit.returncode != 0:
        _reset_create(vault_root, result)
        if task_path is not None:
            cli.run_git(vault_root, ["checkout", "HEAD", "--", str(task_path.resolve().relative_to(vault_root))])
        return 500, {"error": (commit.stderr or commit.stdout).strip() or "git commit failed"}

    push_conflict = _push_or_conflict(vault_root)
    if push_conflict is not None:
        return push_conflict

    return 200, {"slug": result.slug, "url": f"?page={result.slug}"}


# --------------------------------------------------------------------------- #
# HTTP handler — GET (four route families, path-safe raw reads) plus five
# POST write routes.
# --------------------------------------------------------------------------- #

_TASK_MOVE_RE = re.compile(r"^/api/task/([^/]+)/move$")


class TomeHandler(BaseHTTPRequestHandler):
    # Set by cmd_serve() before the server starts.
    vault_root = None
    conventions = None
    last_activity = None

    server_version = "tome-serve"

    def log_message(self, fmt, *args):
        # One terse line per request instead of BaseHTTPRequestHandler's noisy
        # default; keeps `tome serve`'s console readable.
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    def do_GET(self):
        TomeHandler.last_activity = time.monotonic()
        path = unquote(urlparse(self.path).path)
        try:
            if path in ("/", "/index.html"):
                return self._send_frontend("index.html")
            if path == "/index.json":
                return self._send_json(build_index(self.vault_root, self.conventions))
            if path == "/board.json":
                return self._send_json(_board_with_writable(self.vault_root, self.conventions, True))
            if path == "/api/conflicts":
                # Polled once on load so a tree left mid-rebase by a failed
                # `tome sync` surfaces in the resolver on its own, without
                # waiting for the user's next save to trip it.
                return self._send_json(git_conflict_state(self.vault_root))
            if path.startswith("/raw/"):
                return self._send_raw(path[len("/raw/"):])
            if path.startswith("/app/"):
                return self._send_frontend(path[len("/app/"):])
            self._send_error(404, "not found")
        except BrokenPipeError:
            pass  # client navigated away mid-response — nothing to report

    def _json_body(self):
        """The POST body as a dict — or None, having already sent the 400,
        when it isn't one. Every write route takes the same shape of body, so
        they all read it through here."""
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            self._send_json({"error": "malformed JSON body"}, status=400)
            return None
        return payload

    def do_POST(self):
        TomeHandler.last_activity = time.monotonic()
        path = unquote(urlparse(self.path).path)
        try:
            m = _TASK_MOVE_RE.match(path)
            if m:
                payload = self._json_body()
                if payload is None:
                    return
                status = str(payload.get("status") or "").strip()
                ok, message = apply_task_move(self.vault_root, m.group(1), status, payload.get("afterId"))
                if not ok:
                    return self._send_json({"error": message}, status=400)
                return self._send_json(_board_with_writable(self.vault_root, self.conventions, True))
            if path == "/api/task":
                payload = self._json_body()
                if payload is None:
                    return
                ok, result = create_task(self.vault_root, payload.get("title"), payload.get("status"),
                                          payload.get("project"), payload.get("priority"),
                                          payload.get("description"))
                if not ok:
                    return self._send_json({"error": result}, status=400)
                board = _board_with_writable(self.vault_root, self.conventions, True)
                return self._send_json({**board, "taskId": result})
            if path == "/api/page":
                payload = self._json_body()
                if payload is None:
                    return
                rel = payload.get("path")
                body = payload.get("body")
                base_hash = str(payload.get("baseHash") or "")
                if not rel or body is None:
                    return self._send_json({"error": "path and body are required"}, status=400)
                status_code, result = save_page(self.vault_root, self.conventions, rel, body, base_hash)
                return self._send_json(result, status=status_code)
            if path == "/api/frontmatter":
                payload = self._json_body()
                if payload is None:
                    return
                rel = payload.get("path")
                fields = payload.get("fields")
                base_hash = str(payload.get("baseHash") or "")
                if not rel or not isinstance(fields, dict):
                    return self._send_json({"error": "path and fields are required"}, status=400)
                status_code, result = save_frontmatter(self.vault_root, self.conventions, rel, fields, base_hash)
                return self._send_json(result, status=status_code)
            if path == "/api/rename":
                payload = self._json_body()
                if payload is None:
                    return
                rel = payload.get("path")
                new_slug = payload.get("newSlug")
                base_hash = str(payload.get("baseHash") or "")
                if not rel or not new_slug:
                    return self._send_json({"error": "path and newSlug are required"}, status=400)
                status_code, result = rename_page(self.vault_root, self.conventions, rel, new_slug, base_hash)
                return self._send_json(result, status=status_code)
            if path == "/api/new":
                payload = self._json_body()
                if payload is None:
                    return
                type_ = payload.get("type")
                project = payload.get("project")
                slug = payload.get("slug")
                title = payload.get("title")
                desc = payload.get("description")
                link_task = payload.get("linkTask")
                if not type_ or not slug or not title or desc is None:
                    return self._send_json({"error": "type, slug, title, and description are required"},
                                            status=400)
                status_code, result = create_page(self.vault_root, self.conventions,
                                                   type_, project, slug, title, desc, link_task)
                return self._send_json(result, status=status_code)
            if path == "/api/conflict/resolve":
                payload = self._json_body()
                if payload is None:
                    return
                rel = payload.get("path")
                content = payload.get("content")
                if not rel or content is None:
                    return self._send_json({"error": "path and content are required"}, status=400)
                status_code, result = resolve_conflict_file(self.vault_root, rel, content)
                return self._send_json(result, status=status_code)
            if path == "/api/conflict/continue":
                status_code, result = continue_rebase(self.vault_root)
                return self._send_json(result, status=status_code)
            if path == "/api/conflict/abort":
                status_code, result = abort_rebase(self.vault_root)
                return self._send_json(result, status=status_code)
            self._send_error(404, "not found")
        except BrokenPipeError:
            pass

    # -- responders -------------------------------------------------------- #

    def _send_bytes(self, body, content_type, status=200, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self._send_bytes(body, CONTENT_TYPES[".json"], status)

    def _send_error(self, status, message):
        self._send_bytes(message.encode("utf-8"), "text/plain; charset=utf-8", status)

    def _send_frontend(self, rel):
        # Resolve within FRONTEND_DIR; reject any traversal out of it.
        safe = _safe_join(rel)
        if safe is None:
            return self._send_error(400, "bad path")
        resource = FRONTEND_DIR
        for part in safe.parts:
            resource = resource / part
        try:
            body = resource.read_bytes()
        except (FileNotFoundError, IsADirectoryError, OSError):
            return self._send_error(404, "not found")
        self._send_bytes(body, _content_type(safe.name))

    def _send_raw(self, rel):
        safe = _safe_join(rel)
        if safe is None:
            return self._send_error(400, "bad path")
        wiki_root = (self.vault_root / "wiki").resolve()
        target = (wiki_root / str(safe)).resolve()
        if wiki_root not in target.parents:
            return self._send_error(400, "bad path")
        if not target.is_file():
            return self._send_error(404, "not found")
        content = target.read_bytes()
        # ETag doubles as the page-editing conflict token ([[page-editing]]):
        # the frontend echoes it back as `baseHash` on POST /api/page, so a
        # page edited underneath the client is caught without a separate
        # client-side hashing round trip.
        etag = hashlib.sha256(content).hexdigest()
        self._send_bytes(content, _content_type(target.name), extra_headers={"ETag": etag})


def _safe_join(rel):
    """A request-relative POSIX path with no absolute/`..`/empty components,
    or None if it tries to escape. Callers still re-check the resolved path
    against the intended root — this is the cheap first gate."""
    pure = PurePosixPath(rel)
    if pure.is_absolute():
        return None
    if any(part in ("..", "") for part in pure.parts):
        return None
    return pure


def _content_type(name):
    suffix = PurePosixPath(name).suffix.lower()
    return CONTENT_TYPES.get(suffix, "application/octet-stream")


# --------------------------------------------------------------------------- #
# Static export — the same frontend and contracts, written to disk once for
# any static host to serve (no python process, no write endpoints, ever).
# --------------------------------------------------------------------------- #

def _copy_tree(src, dest):
    """Recursively copy an importlib.resources Traversable (src) into a real
    filesystem directory (dest) — plain Path.iterdir()/shutil can't be
    trusted on a Traversable (e.g. inside a zipped wheel)."""
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.is_dir():
            _copy_tree(item, dest / item.name)
        else:
            (dest / item.name).write_bytes(item.read_bytes())


def export_static(vault_root, conventions, out_dir):
    """Write the frontend + a point-in-time index.json/board.json/raw/*.md
    snapshot to out_dir. The frontend's fetches are all root-absolute
    (/index.json, /board.json, /raw/…, /app/…), so this layout is servable
    by any static host exactly like `tome serve`'s routes, just frozen."""
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "index.html").write_bytes((FRONTEND_DIR / "index.html").read_bytes())
    app_dir = out_dir / "app"
    app_dir.mkdir(exist_ok=True)
    for item in FRONTEND_DIR.iterdir():
        if item.name == "index.html":
            continue
        if item.is_dir():
            _copy_tree(item, app_dir / item.name)
        else:
            (app_dir / item.name).write_bytes(item.read_bytes())

    (out_dir / "index.json").write_text(
        json.dumps(build_index(vault_root, conventions), ensure_ascii=False, indent=2),
        encoding="utf-8")
    (out_dir / "board.json").write_text(
        json.dumps(_board_with_writable(vault_root, conventions, False), ensure_ascii=False, indent=2),
        encoding="utf-8")

    wiki_root = (vault_root / "wiki").resolve()
    raw_dir = out_dir / "raw"
    for src in wiki_root.rglob("*.md"):
        dest = raw_dir / src.relative_to(wiki_root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())

    return out_dir


def _idle_watchdog(httpd, timeout_seconds):
    """Runs in a daemon thread; shuts the server down once `timeout_seconds`
    have passed with no request. Only meaningful when something started the
    server with no way to Ctrl-C it — the `launch_gui()` pythonw launcher —
    so an idle server doesn't run forever in the background."""
    while True:
        time.sleep(min(30, timeout_seconds))
        idle_for = time.monotonic() - TomeHandler.last_activity
        if idle_for >= timeout_seconds:
            print(f"tome serve: idle {int(idle_for)}s >= {timeout_seconds}s, shutting down.")
            httpd.shutdown()
            return


# --------------------------------------------------------------------------- #
# Command entry point (dispatched from cli.main()).
# --------------------------------------------------------------------------- #

def cmd_serve(vault_root, conventions, args):
    if getattr(args, "export", None):
        out_dir = export_static(vault_root, conventions, Path(args.export).resolve())
        print(f"tome serve --export: wrote a static snapshot to {out_dir}")
        print(f"  serve it read-only with any static host, e.g.: "
              f"python -m http.server --directory \"{out_dir}\" 8000")
        return 0

    # BaseHTTPRequestHandler is instantiated per-request, so stash the vault
    # context on the class rather than trying to thread it through __init__.
    TomeHandler.vault_root = vault_root
    TomeHandler.conventions = conventions
    TomeHandler.last_activity = time.monotonic()

    httpd = ThreadingHTTPServer((args.host, args.port), TomeHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"tome serve: {url} (vault: {vault_root})")
    print("  serving  /  /index.json  /board.json  /raw/<page>.md  /app/<file>")
    print("  POST /api/task/<id>/move    (status + reorder, shelled to backlog.md)")
    print("  POST /api/task              (new bare task, shelled to backlog.md)")
    print("  POST /api/page              (body edits, conflict + lint gated)")
    print("  POST /api/frontmatter       (title/tags/description edits, conflict + lint gated)")
    print("  POST /api/rename            (slug rename via tome mv, conflict + lint gated)")
    print("  POST /api/new                (scaffold via tome new, uniqueness + lint gated)")
    print("  GET  /api/conflicts          (a stopped rebase's unmerged files, three-way)")
    print("  POST /api/conflict/resolve|continue|abort")

    idle_minutes = getattr(args, "idle_timeout", 0) or 0
    if idle_minutes > 0:
        print(f"  auto-exit after {idle_minutes}min idle (--idle-timeout 0 disables)")
        threading.Thread(target=_idle_watchdog, args=(httpd, idle_minutes * 60),
                          daemon=True).start()

    print("  Ctrl-C to stop.")
    if getattr(args, "open", False):
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\ntome serve: stopped.")
    finally:
        httpd.server_close()
    return 0


def launch_gui():
    """Zero-argument entry point installed as an OS-native GUI launcher
    (pythonw on Windows, via project.gui-scripts) so a desktop/Start-Menu
    shortcut opens the browse UI with no terminal window. pythonw provides
    no stdio — sys.stdout/stderr are None — so both are swallowed before
    anything else runs. Vault resolution follows tome's normal rule (walk up
    from cwd, then VAULT_ROOT): point the shortcut's "Start in" folder at the
    vault, or set VAULT_ROOT, so this finds it with no arguments. Always
    opens the browser and auto-exits after 30 idle minutes, since a
    console-less process has no window for the user to close by hand."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    from types import SimpleNamespace

    from tome_cli import cli

    try:
        vault_root = cli.resolve_vault_root(None)
        conventions = cli.load_conventions(vault_root)
    except cli.VaultError as e:
        print(f"tome-serve: {e}")
        return 1

    args = SimpleNamespace(host="127.0.0.1", port=8765, open=True,
                            export=None, idle_timeout=30)
    return cmd_serve(vault_root, conventions, args)
