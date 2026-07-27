#!/usr/bin/env python3
"""
tome_cli.cli — the vault's mechanical conventions, owned by code instead of prose.

wiki/SCHEMA.md documents the vault's rules; historically they were enforced
only by an agent choosing to follow them, and that drifts (BAD_TAG failures,
index entries bloated into paragraphs, log entries out of format, work
stranded uncommitted). This CLI is the fix: the agent still owns prose (page
bodies, judgment calls, what links to what); this tool owns invariants
(scaffolding, the generated index, status/archive moves, renames, git, the
log format).

stdlib only, Python >= 3.11 (needs tomllib). No pip installs — a fork runs it
bare. The vault primitives this drives — frontmatter, collection, the page
resolver, the index/hub generators, the lint invocation, git, the page-write
core, the backlog.md shell-out — live in tome_cli.lib, shared with
tome_cli.serve; what's left here is the command layer over them.

Usage:
    python scripts/tome.py <command> [args...]
    python scripts/tome.py help

Run `python scripts/tome.py help` for the command overview, or
`python scripts/tome.py <command> -h` for one copy-pasteable example per
command.
"""

import argparse
import importlib.resources
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from tome_cli import lib
from tome_cli import lint as tome_lint
from tome_cli import search as tome_search

# Scaffolding sources for `tome init` — package data shipped inside
# tome_cli, resolved through importlib.resources so it works both from a
# checkout and from an installed wheel (a plain filesystem path would break
# the moment the package is zipped or otherwise not laid out as a directory).
TEMPLATES_DIR = importlib.resources.files("tome_cli") / "templates"

# TOME_OPS_PROFILE restricts the command surface for headless remote
# consumers that should be structurally unable to do more than they're
# trusted to. "help" and "doctor" are always reachable (self-diagnosis must
# work even under a misconfigured or unrecognized profile); every other
# command defaults to guarded — a profile allows only what it names, so a
# command added later without touching this table is blocked automatically
# under any profile.
ALWAYS_ALLOWED_COMMANDS = frozenset({"help", "doctor"})
OPS_PROFILES = {
    "read-capture": frozenset({"search", "prime", "doctor", "help", "inbox"}),
    # The knowledge-half write surface ([[remote-authoring]]): everything
    # read-capture allows, plus authoring a page and editing its body and
    # frontmatter-adjacent fields. Still refuses `rm` (deletion from a surface
    # whose operator can't see what they're losing), `sync` (the write verbs
    # sync themselves; a whole-tree sync is an operator action), and
    # `task`/`start`/`done` (board writes need Node on the instance and are the
    # project-management branch, not the memory trunk).
    "authoring": frozenset({
        "search", "prime", "doctor", "help", "inbox",
        "read", "write", "append", "new", "describe", "set-status",
        "archive", "mv", "log",
    }),
}

# Ops profiles whose whole allowed surface is python-only — `doctor` reports
# node as skipped rather than warning about a dependency nothing can reach.
NODELESS_PROFILES = frozenset({"read-capture", "authoring"})


def all_registered_commands():
    """Every top-level subcommand name argparse knows about — the ops-profile
    guard test enumerates this so a new command defaults to guarded rather
    than needing to be remembered."""
    parser = build_parser()
    for action in parser._subparsers._group_actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def enforce_ops_profile(command):
    """The single dispatch-point guard behind TOME_OPS_PROFILE. Returns None
    to let the command proceed, or an exit code to short-circuit main()."""
    if command in ALWAYS_ALLOWED_COMMANDS:
        return None
    profile = os.environ.get("TOME_OPS_PROFILE")
    if not profile:
        return None
    allowed = OPS_PROFILES.get(profile)
    if allowed is None:
        print(f"tome: error: unknown TOME_OPS_PROFILE '{profile}' — refusing "
              f"everything but help/doctor until it's fixed or unset",
              file=sys.stderr)
        return 1
    if command not in allowed:
        print(f"tome: error: this deployment is {profile} — '{command}' is "
              f"not permitted (allowed: {', '.join(sorted(allowed))})",
              file=sys.stderr)
        return 1
    return None


def cmd_lint(vault_root, conventions, args):
    pages, findings = lib.run_all_lint_checks(vault_root, conventions)
    print(tome_lint.render_text(pages, findings))
    gating = findings if args.strict else [f for f in findings if f.severity == lib.ERROR]
    return 1 if gating else 0


# --------------------------------------------------------------------------- #
# Lifecycle commands
# --------------------------------------------------------------------------- #

def cmd_new(vault_root, conventions, args):
    with_task = getattr(args, "with_task", None)
    priority = getattr(args, "priority", None)
    acs = getattr(args, "ac", None)
    milestone = getattr(args, "milestone", None)
    if with_task and args.type != "plan":
        raise lib.VaultError("--with-task only applies to `tome new plan`")
    if (priority or acs or milestone) and not with_task:
        raise lib.VaultError("--priority/--ac/--milestone only apply alongside --with-task")

    result = lib.new_page(vault_root, conventions, args.type, args.project, args.slug,
                       args.title, args.desc)
    print(f"Created {result.path.relative_to(vault_root)}")

    touched = list(result.touched_paths)
    if with_task:
        wiki_root = vault_root / "wiki"
        plan_ref = f"wiki/{result.path.relative_to(wiki_root).as_posix()}"
        task_argv = ["task", "create", with_task, "-d", args.desc,
                     "-l", f"project:{args.project}", "--ref", plan_ref, "--plain"]
        if priority:
            task_argv += ["--priority", priority]
        if milestone:
            task_argv += ["--milestone", milestone]
        for ac in acs or []:
            task_argv += ["--ac", ac]
        proc = lib.run_backlog(vault_root, task_argv, capture=True)
        if proc.returncode != 0:
            raise lib.VaultError(f"backlog task create failed: {(proc.stderr or proc.stdout).strip()}")
        m = re.search(r"^File: (.+)$", proc.stdout, re.MULTILINE)
        if m:
            task_path = Path(m.group(1).strip())
            touched.append(task_path)
            print(f"Created backlog task: {task_path.name}")
        else:
            print("Created backlog task (couldn't parse its file path for --sync scoping).")

    sync_result = maybe_sync(vault_root, conventions, args, touched, f"new: {result.slug}")
    if sync_result is not None:
        return sync_result
    print("Next: edit the body, link it from the project hub, then:")
    print(f'  python scripts/tome.py log author "authored {result.slug}"')
    print('  python scripts/tome.py sync -m "..."')
    return 0


def cmd_describe(vault_root, conventions, args):
    wiki_root, pages = lib.collect(vault_root, conventions)
    page = lib.find_page(pages, args.slug)
    max_chars = conventions.get("description", {}).get("max_chars", 140)
    lib.validate_oneline(args.text, "description", max_chars)

    fm_lines, body = lib.read_page(page["path"])
    lib.fm_set(fm_lines, "description", args.text, quote=True)
    lib.fm_set(fm_lines, "updated", lib.today())
    lib.write_page(page["path"], fm_lines, body)

    _, pages = lib.collect(vault_root, conventions)
    index_path = lib.rebuild_index(vault_root, conventions, wiki_root, pages)
    touched = [page["path"], index_path]
    if page["meta"].get("type") == "plan":
        project = Path(page["rel_path"]).parts[0]
        hub_path = lib.regenerate_hub(conventions, wiki_root, pages, project)
        if hub_path is not None:
            touched.append(hub_path)
    print(f"Updated description for [[{args.slug}]]")
    result = maybe_sync(vault_root, conventions, args, touched, f"describe: {args.slug}")
    if result is not None:
        return result
    return 0


def cmd_set_status(vault_root, conventions, args):
    wiki_root, pages = lib.collect(vault_root, conventions)
    page = lib.find_page(pages, args.slug)
    ptype = page["meta"].get("type")

    if ptype == "plan":
        live = set(conventions["plan_status"]["live"])
        terminal = set(conventions["plan_status"]["terminal"])
        valid = live | terminal
        if args.status not in valid:
            raise lib.VaultError(f"plan status must be one of {sorted(valid)}")
    elif ptype == "decision":
        valid = {"proposed", "current"}
        if args.status not in valid:
            raise lib.VaultError(f"decision status must be one of {sorted(valid)}")
    else:
        raise lib.VaultError(f"type '{ptype}' does not carry a status")

    new_path = lib.apply_status(conventions, page, args.status)

    _, pages = lib.collect(vault_root, conventions)
    index_path = lib.rebuild_index(vault_root, conventions, wiki_root, pages)
    touched = [new_path, index_path]
    if new_path != page["path"]:
        # A move stages as new_path's add; the old path's delete needs its
        # own pathspec entry too, or a scoped --sync leaves it unstaged.
        touched.append(page["path"])
    if ptype == "plan":
        project = Path(page["rel_path"]).parts[0]
        hub_path = lib.regenerate_hub(conventions, wiki_root, pages, project)
        if hub_path is not None:
            touched.append(hub_path)
    print(f"Set [[{args.slug}]] status -> {args.status}"
          + (f" (moved to {new_path.relative_to(vault_root)})" if new_path != page["path"] else ""))
    result = maybe_sync(vault_root, conventions, args, touched,
                         f"set-status: {args.slug} -> {args.status}")
    if result is not None:
        return result
    return 0


def cmd_mv(vault_root, conventions, args):
    result = lib.move_page(vault_root, conventions, args.slug, args.new_slug)
    print(f"Renamed {args.slug} -> {args.new_slug} "
          f"({result.new_path.relative_to(vault_root)})")
    if result.touched_rels:
        print("Rewrote inbound links in:")
        for t in result.touched_rels:
            print(f"  {t}")
    sync_result = maybe_sync(vault_root, conventions, args, result.touched_paths,
                             f"mv: {args.slug} -> {args.new_slug}")
    if sync_result is not None:
        return sync_result
    return 0


def cmd_rm(vault_root, conventions, args):
    wiki_root, pages = lib.collect(vault_root, conventions)
    page = lib.find_page(pages, args.slug)
    if page["meta"].get("type") == "project":
        raise lib.VaultError(
            f"'{args.slug}' is a project hub — deleting it would orphan every "
            f"page under wiki/{args.slug}/ and break the hub convention. "
            f"Hub deletions aren't supported."
        )

    inbound = [p for p in pages
               if p["path"] != page["path"] and "read_error" not in p
               and args.slug in p.get("links", [])]

    if page["meta"].get("type") == "plan":
        # A marker-managed hub's own listing of this plan isn't a real
        # blocker: rm regenerates that hub right after deleting, so the
        # link disappears as part of this same operation. Only prose
        # outside the markers (a hand-authored mention) should still count.
        hub_path = lib.hub_path_for(wiki_root, Path(page["rel_path"]).parts[0])
        if hub_path.exists():
            hub_text = hub_path.read_text(encoding="utf-8")
            if lib.HUB_MARKER_START in hub_text and lib.HUB_MARKER_END in hub_text:
                outside = lib.HUB_MARKERS_RE.sub("", hub_text)
                if f"[[{args.slug}]]" not in outside and f"[[{args.slug}|" not in outside:
                    inbound = [p for p in inbound if p["path"] != hub_path]

    if inbound and not args.force:
        print(f"'{args.slug}' has inbound links from {len(inbound)} page(s) — "
              f"refusing to delete:", file=sys.stderr)
        for p in inbound:
            print(f"  {p['rel_path']}", file=sys.stderr)
            for line in p["path"].read_text(encoding="utf-8").splitlines():
                if f"[[{args.slug}]]" in line or f"[[{args.slug}|" in line:
                    print(f"    {line.strip()}", file=sys.stderr)
        print("Fix those links first (a deleted target can't be auto-rewritten "
              "to anything), or pass --force to delete anyway.", file=sys.stderr)
        return 1

    rel_path = page["rel_path"]
    removed_path = page["path"]
    removed_type = page["meta"].get("type")
    removed_project = Path(rel_path).parts[0]
    removed_path.unlink()

    _, pages = lib.collect(vault_root, conventions)
    index_path = lib.rebuild_index(vault_root, conventions, wiki_root, pages)
    touched = [removed_path, index_path]
    regenerated_hub = None
    if removed_type == "plan":
        regenerated_hub = lib.regenerate_hub(conventions, wiki_root, pages, removed_project)
        if regenerated_hub is not None:
            touched.append(regenerated_hub)

    print(f"Removed {rel_path}")
    if inbound:
        print(f"WARNING: {len(inbound)} page(s) still link to [[{args.slug}]] — "
              f"now broken:", file=sys.stderr)
        for p in inbound:
            print(f"  {p['rel_path']}", file=sys.stderr)
    if (vault_root / "backlog").is_dir():
        print("Note: a backlog/ task may still reference this page — check "
              "`tome task task list --plain`.")
    if regenerated_hub is None:
        print("Reminder: update the project hub by hand if it linked this page.")
    result = maybe_sync(vault_root, conventions, args, touched, f"rm: {args.slug}")
    if result is not None:
        return result
    print('Next: tome log <op> "..." then tome sync -m "..."')
    return 0


def cmd_archive(vault_root, conventions, args):
    """archive/--restore for status-less types (ideas, reports, sources,
    notes): moves the file to/from a sibling archive/ folder. Plans and
    decisions have their own status-driven lifecycle (`set-status`) — no
    slug change means no inbound `[[link]]` needs rewriting either way."""
    wiki_root, pages = lib.collect(vault_root, conventions)
    page = lib.find_page(pages, args.slug)
    ptype = page["meta"].get("type")
    if ptype in ("plan", "decision"):
        raise lib.VaultError(f"'{args.slug}' is a {ptype} — archive it with "
                          f"`tome set-status {args.slug} <terminal-status>` instead")
    if ptype == "project":
        raise lib.VaultError(f"'{args.slug}' is a project hub — archiving it isn't supported")

    currently_archived = "archive" in page["path"].parent.parts
    if args.restore:
        if not currently_archived:
            raise lib.VaultError(f"'{args.slug}' is not archived")
        new_path = page["path"].parent.parent / page["path"].name
    else:
        if currently_archived:
            raise lib.VaultError(f"'{args.slug}' is already archived")
        new_path = page["path"].parent / "archive" / page["path"].name

    new_path.parent.mkdir(parents=True, exist_ok=True)
    page["path"].rename(new_path)
    fm_lines, body = lib.read_page(new_path)
    lib.fm_set(fm_lines, "updated", lib.today())
    lib.write_page(new_path, fm_lines, body)

    _, pages = lib.collect(vault_root, conventions)
    index_path = lib.rebuild_index(vault_root, conventions, wiki_root, pages)
    touched = [new_path, index_path, page["path"]]

    verb = "Restored" if args.restore else "Archived"
    print(f"{verb} [[{args.slug}]] ({new_path.relative_to(vault_root)})")
    result = maybe_sync(vault_root, conventions, args, touched, f"archive: {args.slug}")
    if result is not None:
        return result
    return 0


# --------------------------------------------------------------------------- #
# Page bodies — read / write / append ([[remote-authoring]]). The write core
# itself is `lib`'s, shared with serve.py's routes; what's here is the terminal
# half of it — argument plumbing and the rendering of a `PageWriteResult`.
# --------------------------------------------------------------------------- #

# One-clause fixes keyed by lint code, printed under a refused write. Same
# reasoning as `unresolved_page_message`: the refusal is the only schema a
# remote agent gets to see, so it has to carry the repair.
LINT_FIX_HINTS = {
    "BROKEN_LINK": "that [[target]] has no page — create it with `tome new`, or "
                   "fix the slug (`tome search` finds the real one)",
    "OVERSIZE_HARD": "split the page, or move detail onto a linked one",
    "BAD_TAG": "use a tag from conventions.toml's [tags] taxonomy",
    "BAD_TYPE": "use a type from conventions.toml's [types].enum",
    "DESC_TOO_LONG": 'shorten it with `tome describe <slug> "..."`',
    "MALFORMED_FRONTMATTER": "frontmatter must be a `---`-fenced block of "
                             "`key: value` lines",
    "PLAN_DIR": "move it with `tome set-status <slug> <status>`, not by hand",
    "INDEX_MISSING": "run `tome index rebuild`",
    "INDEX_BROKEN": "run `tome index rebuild`",
    "INDEX_DRIFT": "run `tome index rebuild`",
    "HUB_DRIFT": "run `tome index rebuild`",
}


def report_page_write(result, verb):
    """Render a PageWriteResult for a terminal, returning the exit code."""
    payload = result.payload
    if result.kind == "ok":
        state = "committed + pushed" if payload["committed"] else "not committed (--no-sync)"
        print(f"{verb} {payload['path']} — {state}")
        print(f"hash: {payload['hash']}")
        return 0
    if result.kind == "lint-failed":
        print("tome: refusing the write — the page would fail lint:", file=sys.stderr)
        for f in payload["findings"]:
            print(f"  {f['path']}:{f['code']} {f['message']}", file=sys.stderr)
            hint = LINT_FIX_HINTS.get(f["code"])
            if hint:
                print(f"    fix: {hint}", file=sys.stderr)
        print("The page was restored; nothing was written.", file=sys.stderr)
        return 1
    if result.kind == "conflict":
        print(f"tome: {payload.get('error', 'conflict')}", file=sys.stderr)
        if payload.get("currentHash"):
            print(f"Its current hash is {payload['currentHash']} — re-read the page "
                  f"(`tome read <ident> --json`), re-apply your change, and write "
                  f"again with that hash.", file=sys.stderr)
        else:
            print("Run `tome serve` to resolve the diverged history in the browser, "
                  "or finish the rebase by hand with git.", file=sys.stderr)
        return 1
    print(f"tome: error: {payload.get('error', result.kind)}", file=sys.stderr)
    return 1


def _body_input(args):
    """The text a write/append verb is given: the positional argument, else
    --body-file, else stdin — one source only, so a caller that passes two
    can't silently have one ignored."""
    text = getattr(args, "text", None)
    body_file = getattr(args, "body_file", None)
    if text is not None and body_file:
        raise lib.VaultError("give the text as an argument or --body-file, not both")
    if text is not None:
        return text
    if body_file:
        path = Path(body_file)
        if not path.is_file():
            raise lib.VaultError(f"no such file: {path}")
        return path.read_text(encoding="utf-8")
    return sys.stdin.read()


def cmd_read(vault_root, conventions, args):
    page, text, page_hash = lib.page_read(vault_root, conventions, args.ident)
    if not args.json:
        print(text, end="" if text.endswith("\n") else "\n")
        return 0
    rel = page["rel_path"].replace("\\", "/")
    _, body = lib.read_page(page["path"])
    print(json.dumps({
        "path": f"wiki/{rel}",
        "slug": page["slug"],
        "hash": page_hash,
        "frontmatter": page["meta"],
        "body": body,
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_write(vault_root, conventions, args):
    result = lib.write_page_body(vault_root, conventions, args.ident, _body_input(args),
                              args.base_hash, sync=not args.no_sync)
    return report_page_write(result, "Wrote")


def cmd_append(vault_root, conventions, args):
    result = lib.append_page_body(vault_root, conventions, args.ident, _body_input(args),
                               under=args.under, base_hash=args.base_hash,
                               sync=not args.no_sync)
    return report_page_write(result, "Appended to")


def cmd_search(vault_root, conventions, args):
    wiki_root = vault_root / "wiki"
    skip_files = set(conventions["skip"]["files"])
    skip_dirs = set(conventions["skip"]["dirs"])
    pages = tome_search.collect_pages(wiki_root, skip_files, skip_dirs)
    if not pages:
        print(f"No wiki pages found under {wiki_root.relative_to(vault_root)}", file=sys.stderr)
        return 0
    if args.backlinks:
        tome_search.cmd_backlinks(args, pages)
    elif args.top_linked:
        tome_search.cmd_top_linked(args, pages)
    elif args.query:
        tome_search.cmd_search(args, pages)
    else:
        print("Provide query terms, or --backlinks/--top-linked.", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- #
# prime — two tiers of session orientation. The terse tier (prime_terse_text)
# is the single source for both `tome prime` and the SessionStart hook (which
# imports it directly) — one spot to edit, no drift between the two. The full
# tier is the write protocol that used to be a read fan-out spelled out in
# every skill's opening steps.
# --------------------------------------------------------------------------- #

LOG_TAIL_ENTRIES = 15


def prime_terse_text(vault_root):
    """The orientation pointer: what the vault *is*, then the two imperatives
    that follow from it (consult it before answering from your own knowledge;
    write back what's worth saving), then the read/write mechanics.

    Every word here is paid in every single session via the SessionStart hook,
    so the budget is a fixed ~100 tokens and the only lever is which words get
    it. Mechanics an agent can infer, or `tome lint` will enforce anyway, lose
    that contest to the framing and the imperatives — a location plus a command
    surface reads as a library card, not a memory. The worth-saving bar is
    stated here in the words every other surface reuses verbatim: durable,
    non-obvious, not trivially derivable."""
    return (
        f"Knowledge vault at {vault_root} — your memory of this user: their "
        "repo, holding what you know about them, their projects, and their "
        "past decisions. Consult it before answering from your own knowledge "
        "when a question touches any of those, and write back what's worth "
        "saving without being asked — the bar is durable, non-obvious, and "
        "not trivially derivable. Reading: start at wiki/index.md, follow "
        "[[wikilinks]]; `tome search` as fallback. Writing: the tome CLI "
        "(`tome help`) owns it (`tome task` for backlog items); page bodies "
        "with normal file tools; conventions in wiki/SCHEMA.md. Start and end "
        "vault work with `tome sync`."
    )


def log_tail(log_text, n=LOG_TAIL_ENTRIES):
    """The last n `## [date] op | message` entries, whole — never truncated
    mid-entry the way a bare line-count tail would risk."""
    entries = [e for e in re.split(r"(?=^## \[)", log_text, flags=re.MULTILINE)
               if e.startswith("## [")]
    return "".join(entries[-n:]).rstrip("\n")


def read_task_snapshot_fields(path):
    """The fields the prime task snapshot needs from one backlog task file:
    id, status, milestone (single-line, fm_get is enough), title (may wrap
    to a block scalar), labels (block list, for project scoping)."""
    fm_lines, _ = lib.read_page(path)
    return {
        "id": lib.fm_get(fm_lines, "id") or "",
        "status": lib.fm_get(fm_lines, "status") or "",
        "milestone": lib.fm_get(fm_lines, "milestone"),
        "title": lib.task_title(fm_lines),
        "labels": lib.task_block_list(fm_lines, "labels"),
    }


def _task_sort_key(t):
    m = lib.TASK_ID_RE.match(t["id"])
    return int(m.group(1)) if m else t["id"]


def open_task_snapshot(vault_root, project=None):
    """Terse id/status/title listing of every open backlog task (files
    under backlog/tasks/ — `task complete` is what moves a task to
    completed/, so anything still there is open regardless of its exact
    status string), grouped by milestone with done/total counts computed
    across both tasks/ and completed/ so a milestone's already-shipped work
    still counts toward its total (backlog.md's own `milestone list` only
    counts currently-open tasks). None when there's no backlog/tasks/ at
    all — a fresh vault, or one that never adopted Backlog.md."""
    tasks_dir = vault_root / "backlog" / "tasks"
    if not tasks_dir.is_dir():
        return None

    open_tasks = [read_task_snapshot_fields(p) for p in sorted(tasks_dir.glob("*.md"))]
    if project:
        open_tasks = [t for t in open_tasks if f"project:{project}" in t["labels"]]
    if not open_tasks:
        return "(no open tasks)"

    milestone_titles = {}
    milestones_dir = vault_root / "backlog" / "milestones"
    if milestones_dir.is_dir():
        for p in sorted(milestones_dir.glob("*.md")):
            fm_lines, _ = lib.read_page(p)
            mid = lib.fm_get(fm_lines, "id")
            if mid:
                milestone_titles[mid] = lib.fm_get(fm_lines, "title") or mid

    milestone_total = defaultdict(int)
    milestone_done = defaultdict(int)
    completed_dir = vault_root / "backlog" / "completed"
    if completed_dir.is_dir():
        for p in completed_dir.glob("*.md"):
            fm_lines, _ = lib.read_page(p)
            mid = lib.fm_get(fm_lines, "milestone")
            if mid:
                milestone_total[mid] += 1
                milestone_done[mid] += 1
    for t in open_tasks:
        if t["milestone"]:
            milestone_total[t["milestone"]] += 1

    by_milestone = defaultdict(list)
    unmilestoned = []
    for t in open_tasks:
        (by_milestone[t["milestone"]] if t["milestone"] else unmilestoned).append(t)

    lines = []
    for mid in sorted(by_milestone):
        title = milestone_titles.get(mid, mid)
        lines.append(f"{mid} — {title} ({milestone_done[mid]}/{milestone_total[mid]} done):")
        for t in sorted(by_milestone[mid], key=_task_sort_key):
            lines.append(f"  {t['id']} [{t['status']}] {t['title']}")
    if unmilestoned:
        if lines:
            lines.append("")
        lines.append("No milestone:")
        for t in sorted(unmilestoned, key=_task_sort_key):
            lines.append(f"  {t['id']} [{t['status']}] {t['title']}")
    return "\n".join(lines)


def prime_full_text(vault_root, conventions, project):
    """The write protocol: SCHEMA.md and the index always; with a project,
    also that project's hub, every one of its live plan bodies, and a recent
    log.md tail — replacing the read fan-out every skill used to open with.
    The open-task snapshot comes last, when there is a backlog at all.

    Payload order is the argument: what an agent reads first is what it
    concludes the vault is. Knowledge leads, and the board — a genuinely
    useful branch of the vault, not its trunk — trails everything it could
    otherwise be mistaken for the headline of."""
    wiki_root = vault_root / "wiki"
    sections = [
        ((wiki_root / "SCHEMA.md").relative_to(vault_root).as_posix(),
         (wiki_root / "SCHEMA.md").read_text(encoding="utf-8")),
        ((wiki_root / conventions["index"]["file"]).relative_to(vault_root).as_posix(),
         (wiki_root / conventions["index"]["file"]).read_text(encoding="utf-8")),
    ]

    if project:
        if project not in lib.list_projects(wiki_root, conventions):
            raise lib.VaultError(f"no such project: wiki/{project}/ does not exist")
        _, pages = lib.collect(vault_root, conventions)
        hub_path = lib.hub_path_for(wiki_root, project)
        if hub_path.exists():
            sections.append((hub_path.relative_to(vault_root).as_posix(),
                              hub_path.read_text(encoding="utf-8")))

        live_statuses = set(conventions["plan_status"]["live"])
        live_plans = sorted(
            (p for p in pages if p["meta"].get("type") == "plan"
             and Path(p["rel_path"]).parts[0] == project
             and p["meta"].get("status") in live_statuses),
            key=lambda p: p["slug"])
        for p in live_plans:
            sections.append((f"wiki/{p['rel_path']}".replace("\\", "/"),
                              p["path"].read_text(encoding="utf-8")))

        log_path = wiki_root / "log.md"
        sections.append((f"{log_path.relative_to(vault_root).as_posix()} (last {LOG_TAIL_ENTRIES})",
                          log_tail(log_path.read_text(encoding="utf-8"))))

    task_snapshot = open_task_snapshot(vault_root, project)
    if task_snapshot is not None:
        label = f"backlog/tasks (open, project:{project})" if project else "backlog/tasks (open)"
        sections.append((label, task_snapshot))

    return "\n\n".join(f"# {label}\n\n{text}" for label, text in sections)


def cmd_prime(vault_root, conventions, args):
    if args.project and not args.full:
        raise lib.VaultError("a project only applies with --full (the terse tier is vault-level)")
    if args.full:
        print(prime_full_text(vault_root, conventions, args.project))
    else:
        print(prime_terse_text(vault_root))
    return 0


def cmd_log(vault_root, conventions, args):
    ops = conventions.get("log", {}).get("ops")
    if ops and args.op not in ops:
        raise lib.VaultError(f"op '{args.op}' not in {ops}")
    if len(args.message) > 500:
        raise lib.VaultError(f"message is {len(args.message)} chars (cap 500)")
    if "\n" in args.message:
        raise lib.VaultError("message headline must be a single line "
                          "(use --body for multi-paragraph detail)")

    log_path = vault_root / "wiki" / "log.md"
    entry = f"\n## [{lib.today()}] {args.op} | {args.message}\n"
    if args.body:
        entry += f"\n{args.body}\n"
    with log_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(entry)
    print(f"Appended log entry: {args.op} | {args.message}")
    result = maybe_sync(vault_root, conventions, args, [log_path], f"log: {args.op}",
                         message_attr="sync_message")
    if result is not None:
        return result
    return 0


def cmd_index_rebuild(vault_root, conventions, args):
    wiki_root, pages = lib.collect(vault_root, conventions)
    index_path = lib.rebuild_index(vault_root, conventions, wiki_root, pages)
    print(f"Rebuilt {index_path.relative_to(vault_root)}")
    hubs = lib.regenerate_all_hubs(conventions, wiki_root, pages)
    for hub_path in hubs:
        print(f"Regenerated {hub_path.relative_to(vault_root)}")
    return 0


# --------------------------------------------------------------------------- #
# inbox — cheap, schema-free capture (never scanned by lint: it walks wiki/
# only, and inbox/ is a vault-root sibling of wiki/, not nested under it)
# --------------------------------------------------------------------------- #

INBOX_SLUG_MAX_CHARS = 40
INBOX_WORD_RE = re.compile(r"[a-z0-9]+")


def slugify_words(text, max_chars=INBOX_SLUG_MAX_CHARS):
    """Kebab-case slug built word-by-word from text (lowercased) up to
    max_chars, stopping at a word boundary rather than cutting mid-word.
    Non-ASCII/punctuation-only words are simply dropped, never crash; a
    single word longer than max_chars is hard-truncated so it still
    contributes something instead of falling through to the fallback."""
    words = INBOX_WORD_RE.findall(text.lower())
    slug = ""
    for word in words:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > max_chars:
            if not slug:
                slug = word[:max_chars]
            break
        slug = candidate
    return slug or "capture"


def cmd_inbox(vault_root, conventions, args):
    inbox_dir = vault_root / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    slug = slugify_words(args.title if args.title else args.note)
    date_str = lib.today()
    base_name = f"{date_str}-{slug}"
    path = inbox_dir / f"{base_name}.md"
    n = 2
    while path.exists():
        path = inbox_dir / f"{base_name}-{n}.md"
        n += 1

    note = args.note.rstrip("\n")
    body = f"# {date_str} capture\n\n{note}\n"
    path.write_text(body, encoding="utf-8", newline="\n")

    print(f"Captured to {path.relative_to(vault_root)}")
    result = maybe_sync(vault_root, conventions, args, [path], f"inbox: {slug}")
    if result is not None:
        return result
    print("Routed into the wiki at the next retrospect triage.")
    return 0


# --------------------------------------------------------------------------- #
# init — scaffold a fresh vault (runs before a vault root can be resolved,
# so it does not take vault_root/conventions like the other commands)
# --------------------------------------------------------------------------- #

LOG_HEADER = """# Wiki Log

Append-only chronological record of operations on the wiki. Each entry \
begins with `## [YYYY-MM-DD] <op> | <description>` so it's parseable with \
`grep "^## \\[" log.md | tail -N`. See conventions.toml's `[log].ops` for \
the operation vocabulary.

---
"""


def _copy_template(name, dest):
    """Copy a package-data template to dest by reading bytes through
    importlib.resources rather than shutil.copy2 — TEMPLATES_DIR is a
    Traversable, not guaranteed to be a real filesystem path (e.g. inside a
    zipped wheel), so copy2 can't be trusted to work on it directly."""
    dest.write_bytes((TEMPLATES_DIR / name).read_bytes())


def cmd_init(args):
    target = Path(args.path).resolve() if args.path else Path.cwd()
    target.mkdir(parents=True, exist_ok=True)

    to_create = [
        target / "conventions.toml",
        target / ".gitignore",
        target / "CLAUDE.md",
        target / "wiki" / "SCHEMA.md",
        target / "wiki" / "index.md",
        target / "wiki" / "log.md",
        target / "inbox",
        target / "raw" / "assets",
        target / ".claude" / "settings.json",
    ]
    existing = [p for p in to_create if p.exists()]
    if existing:
        raise lib.VaultError(
            "refusing to init: target already has "
            + ", ".join(str(p.relative_to(target)) for p in sorted(existing))
        )

    (target / "wiki").mkdir(parents=True, exist_ok=True)
    (target / "inbox").mkdir(parents=True, exist_ok=True)
    (target / "raw" / "assets").mkdir(parents=True, exist_ok=True)
    (target / ".claude").mkdir(parents=True, exist_ok=True)

    _copy_template("conventions.toml", target / "conventions.toml")
    _copy_template("SCHEMA.md", target / "wiki" / "SCHEMA.md")
    _copy_template("CLAUDE.md", target / "CLAUDE.md")
    _copy_template("vault.gitignore", target / ".gitignore")
    _copy_template("claude-settings.json", target / ".claude" / "settings.json")

    conventions = lib.load_conventions(target)
    (target / "wiki" / "log.md").write_text(
        LOG_HEADER + f"\n## [{lib.today()}] init | Vault created via `tome init`\n",
        encoding="utf-8", newline="\n")
    lib.rebuild_index(target, conventions, target / "wiki", [])

    if not (target / ".git").is_dir():
        subprocess.run(["git", "init"], cwd=str(target), check=True)

    print(f"Initialized a new vault at {target}")
    print("Next steps:")
    print('  - author a first project page: tome new project <name> --title "T" --desc "..."')
    print("  - browse it: tome serve --open")
    print('  - set up a remote, then: tome sync -m "Initial vault"')
    return 0


# --------------------------------------------------------------------------- #
# sync / task
# --------------------------------------------------------------------------- #

def sync_core(vault_root, conventions, message, no_verify, pathspec=None):
    """The shared pull/lint-gate/commit/push core behind `tome sync` and
    every write command's `--sync`. With pathspec=None, stages the whole
    tree (bare `tome sync`'s deliberate whole-tree sweep — the only place a
    ride-along commit should be possible). With a pathspec (relative-to-
    vault-root path strings), stages and commits only those paths, so one
    command's auto-sync can't sweep in another agent's half-finished hand
    edits; whatever else is dirty afterwards is reported, never swallowed."""
    branch = lib.run_git(vault_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch.returncode != 0:
        print(branch.stderr, file=sys.stderr)
        return 1
    if branch.stdout.strip() != "main":
        raise lib.VaultError(f"refusing to sync: current branch is "
                          f"'{branch.stdout.strip()}', not main")

    pull = lib.run_git(vault_root, ["pull", "--rebase", "--autostash"])
    print(pull.stdout, end="")
    if pull.returncode != 0:
        print(pull.stderr, file=sys.stderr)
        lib._mid_rebase_hint(vault_root)
        return 1

    status = lib.run_git(vault_root, ["status", "--porcelain"])
    if not status.stdout.strip():
        print("already in sync")
        return 0

    if pathspec is not None:
        scoped = lib.run_git(vault_root, ["status", "--porcelain", "--", *pathspec])
        if not scoped.stdout.strip():
            print("already in sync (nothing dirty in scope)")
            return 0

    if not message:
        raise lib.VaultError("tree is dirty — a commit message is required: "
                          "`tome sync -m \"...\"`")

    if not no_verify:
        pages, findings = lib.run_all_lint_checks(vault_root, conventions)
        errors = [f for f in findings if f.severity == lib.ERROR]
        if errors:
            print(tome_lint.render_text(pages, findings))
            print("tome: refusing to sync — a dirty commit would publish a "
                  "vault that fails its own lint. Fix the errors above, or "
                  "pass --no-verify to commit anyway.", file=sys.stderr)
            return 1

    add_args = ["add", "-A"] if pathspec is None else ["add", "-A", "--", *pathspec]
    add = lib.run_git(vault_root, add_args)
    if add.returncode != 0:
        print(add.stderr, file=sys.stderr)
        return 1
    commit_args = ["commit", "-m", message]
    author = os.environ.get("TOME_GIT_AUTHOR")
    if author:
        commit_args += ["--author", author]
    commit = lib.run_git(vault_root, commit_args)
    print(commit.stdout, end="")
    if commit.returncode != 0:
        print(commit.stderr, file=sys.stderr)
        return 1
    push_code = lib._push_with_retry(vault_root)
    if push_code != 0:
        return push_code
    print("synced.")

    if pathspec is not None:
        leftover = lib.run_git(vault_root, ["status", "--porcelain"])
        leftover_lines = [l for l in leftover.stdout.splitlines() if l.strip()]
        if leftover_lines:
            print(f"left uncommitted: {len(leftover_lines)} file(s) from elsewhere:")
            for line in leftover_lines:
                print(f"  {line}")
    return 0


def maybe_sync(vault_root, conventions, args, touched_paths, auto_message, message_attr="message"):
    """Called at the tail of every write command. Returns None (caller keeps
    going, e.g. to print its normal hints) when --sync wasn't passed; else
    runs the scoped sync_core over touched_paths and returns its exit code."""
    if not getattr(args, "sync", False):
        return None
    message = getattr(args, message_attr, None) or auto_message
    rel = [str(Path(p).resolve().relative_to(vault_root)) for p in touched_paths]
    return sync_core(vault_root, conventions, message, False, pathspec=rel)


def resolve_entity(vault_root, pages, entity):
    """Resolve one `tome start`/`tome sync <entity>` argument — a page slug
    or a backlog task id — to (page-or-None, task_path-or-None). At least
    one side always resolves; an unknown slug/task id fails loud."""
    m = lib.TASK_ID_RE.match(entity)
    page = None
    task_path = None
    if m:
        task_path = lib.find_task_file(vault_root, m.group(1))
        if task_path is None:
            raise lib.VaultError(f"no backlog task with id 'task-{m.group(1)}'")
        fm_lines, _ = lib.read_page(task_path)
        ref_m = re.search(r"wiki/([^\s'\"]+\.md)", "\n".join(fm_lines))
        if ref_m:
            ref_rel = ref_m.group(1)
            page = next((p for p in pages
                         if p["rel_path"].replace("\\", "/") == ref_rel), None)
            if page is None:
                # A ref that points at no collected page is suspect — warn
                # loudly (naming the task and the dangling ref) rather than
                # silently degrading to a plan-less operation. Plan-less is
                # legal; a ref to nothing is not, so it's a warning, not an error.
                print(f"tome: warning: task-{m.group(1)} references "
                      f"{ref_m.group(0)} which matches no page — "
                      f"treating as plan-less", file=sys.stderr)
    else:
        page = lib.find_page(pages, entity)
        task_path = lib.find_task_for_page(vault_root, page["rel_path"])
    return page, task_path


def resolve_entity_cluster(vault_root, conventions, wiki_root, pages, entity):
    """Resolve one `tome sync <entity>` argument to its closed file cluster:
    the page, its linked task file (if any), the page's project hub (if
    any), index.md, and log.md. Detection is a fixed cluster derived from
    real links, never a heuristic scan."""
    page, task_path = resolve_entity(vault_root, pages, entity)

    cluster = []
    if page is not None:
        cluster.append(page["path"])
        project = Path(page["rel_path"]).parts[0]
        hub_path = wiki_root / project / f"{project}.md"
        if hub_path.exists():
            cluster.append(hub_path)
    if task_path is not None:
        cluster.append(task_path)
    cluster.append(wiki_root / conventions["index"]["file"])
    cluster.append(wiki_root / "log.md")
    return cluster


def cmd_sync(vault_root, conventions, args):
    if args.entities:
        wiki_root, pages = lib.collect(vault_root, conventions)
        cluster, seen = [], set()
        for entity in args.entities:
            for p in resolve_entity_cluster(vault_root, conventions, wiki_root, pages, entity):
                if str(p) not in seen:
                    seen.add(str(p))
                    cluster.append(p)
        pathspec = [str(p.relative_to(vault_root)) for p in cluster]
        message = args.message or f"sync: {', '.join(args.entities)}"
        return sync_core(vault_root, conventions, message, args.no_verify, pathspec=pathspec)
    return sync_core(vault_root, conventions, args.message, args.no_verify, pathspec=None)


def cmd_task(vault_root, conventions, args):
    return lib.run_backlog(vault_root, args.args).returncode


# --------------------------------------------------------------------------- #
# start / done — the pickup-task skill's bundled start/close rituals. Each
# is a fixed program (set status, drive the linked task, log, sync); the
# agent's judgment stays entirely on either side of these two commands.
# --------------------------------------------------------------------------- #

def cmd_start(vault_root, conventions, args):
    wiki_root, pages = lib.collect(vault_root, conventions)
    page, task_path = resolve_entity(vault_root, pages, args.entity)
    if page is None and task_path is None:
        raise lib.VaultError(f"'{args.entity}' did not resolve to a page or a backlog task")
    if page is not None and page["meta"].get("type") != "plan":
        raise lib.VaultError(f"'{page['slug']}' is a {page['meta'].get('type')}, not a plan — "
                          f"tome start only sets plan status")

    touched = []
    plan_path = None
    if page is not None:
        live = set(conventions["plan_status"]["live"])
        if "active" not in live:
            raise lib.VaultError("'active' is not in this vault's plan_status.live vocabulary")
        plan_path = lib.apply_status(conventions, page, "active")
        _, pages = lib.collect(vault_root, conventions)
        index_path = lib.rebuild_index(vault_root, conventions, wiki_root, pages)
        touched += [plan_path, index_path]
        if plan_path != page["path"]:
            touched.append(page["path"])
        project = Path(page["rel_path"]).parts[0]
        hub_path = lib.regenerate_hub(conventions, wiki_root, pages, project)
        if hub_path is not None:
            touched.append(hub_path)
        print(f"Set [[{page['slug']}]] status -> active")

    task_id = None
    if task_path is not None:
        task_id = lib.task_id_from_path(task_path)
        proc = lib.run_backlog(vault_root, ["task", "edit", task_id, "-s", "In Progress", "-a", "@me"],
                            capture=True)
        if proc.returncode != 0:
            raise lib.VaultError(f"backlog task edit failed: {(proc.stderr or proc.stdout).strip()}")
        touched.append(task_path)
        print(f"Moved TASK-{task_id} -> In Progress (@me)")

    subject = page["slug"] if page is not None else f"task-{task_id}"
    log_path = vault_root / "wiki" / "log.md"
    with log_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"\n## [{lib.today()}] work-started | {subject}\n")
    touched.append(log_path)
    print(f"Logged work-started: {subject}")

    if not args.no_sync:
        rel = [str(Path(p).resolve().relative_to(vault_root)) for p in touched]
        result = sync_core(vault_root, conventions, f"start: {args.entity} ({subject})",
                            False, pathspec=rel)
        if result:
            return result

    if task_id is not None:
        lib.run_backlog(vault_root, ["task", task_id, "--plain"])
    if plan_path is not None:
        print()
        print(plan_path.read_text(encoding="utf-8"))
    return 0


def cmd_done(vault_root, conventions, args):
    """Closes a plan (by slug), a plan-linked task (by either), or a
    plan-less task (by task id only — the plan half of resolve_entity simply
    stays None, and the plan-status branch below is skipped entirely).

    Umbrella guard: a plan shared by several phase tasks (one milestone plan,
    many phase tasks) is not archived while any sibling is still open. Closing
    a phase *task* with open siblings closes only the task; closing the *plan
    slug* with open referents is refused unless --force. The last sibling's
    close archives the plan on the current 1:1 path."""
    wiki_root, pages = lib.collect(vault_root, conventions)
    page, task_path = resolve_entity(vault_root, pages, args.slug)
    if page is None and task_path is None:
        raise lib.VaultError(f"'{args.slug}' did not resolve to a plan or a backlog task")
    if page is not None and page["meta"].get("type") != "plan":
        raise lib.VaultError(f"'{page['slug']}' is a {page['meta'].get('type')}, not a plan — "
                          f"tome done only closes out plans")
    if page is None and args.as_status:
        raise lib.VaultError("--as only applies when closing out a plan")

    touched = []
    new_path = None
    old_rel = None

    # Umbrella/milestone guard: a plan referenced by several phase tasks must
    # not be archived while any of them is still open — that would physically
    # move the plan and dangle every sibling's ref. Check open sibling tasks
    # referencing the same plan, excluding the one being closed here.
    named_task = lib.TASK_ID_RE.match(args.slug) is not None
    plan_open_referrers = (
        lib.open_tasks_referencing_plan(vault_root, page["rel_path"], task_path)
        if page is not None else []
    )
    if page is not None and plan_open_referrers:
        if named_task:
            # Closing a phase task: leave the shared plan untouched, close only
            # the task. The last sibling's `tome done` archives the plan.
            print(f"plan [[{page['slug']}]] has {len(plan_open_referrers)} "
                  f"open sibling task(s) — left active")
            archive_plan = False
        elif args.force:
            archive_plan = True
        else:
            # Closing the plan slug directly with live referents: refuse — the
            # tool never decides an umbrella is finished, it only refuses to
            # archive a plan with provably live referents.
            raise lib.VaultError(
                f"plan '{page['slug']}' still has open task(s) referencing it: "
                f"{', '.join(plan_open_referrers)} — close them first, or pass "
                f"--force to archive anyway (dangles their refs)")
    else:
        archive_plan = page is not None

    if archive_plan:
        subject = page["slug"]
    elif task_path is not None:
        subject = f"task-{lib.task_id_from_path(task_path)}"
    else:
        subject = page["slug"]

    if archive_plan:
        target_status = args.as_status or "done"
        terminal = set(conventions["plan_status"]["terminal"])
        if target_status not in terminal:
            raise lib.VaultError(f"'{target_status}' is not a terminal plan status ({sorted(terminal)})")

        old_rel = f"wiki/{page['rel_path']}".replace("\\", "/")
        new_path = lib.apply_status(conventions, page, target_status)
        _, pages = lib.collect(vault_root, conventions)
        index_path = lib.rebuild_index(vault_root, conventions, wiki_root, pages)
        touched += [new_path, index_path]
        if new_path != page["path"]:
            touched.append(page["path"])
        project = Path(page["rel_path"]).parts[0]
        hub_path = lib.regenerate_hub(conventions, wiki_root, pages, project)
        if hub_path is not None:
            touched.append(hub_path)
        print(f"Set [[{page['slug']}]] status -> {target_status} "
              f"(moved to {new_path.relative_to(vault_root)})")

    if task_path is not None:
        task_id = lib.task_id_from_path(task_path)
        task_fm_lines, task_body = lib.read_page(task_path)

        edit_argv = ["task", "edit", task_id, "-s", "Done"]
        if not args.no_check_ac:
            for i in range(1, lib.count_task_acs(task_body) + 1):
                edit_argv += ["--check-ac", str(i)]
        if args.summary:
            edit_argv += ["--final-summary", args.summary]
        ref_note = ""
        if archive_plan:
            refs = lib.task_references(task_fm_lines)
            new_rel = f"wiki/{new_path.relative_to(wiki_root).as_posix()}"
            new_refs = [new_rel if r == old_rel else r for r in refs] or [new_rel]
            for r in new_refs:
                edit_argv += ["--ref", r]
            ref_note = f", ref -> {new_rel}"
        proc = lib.run_backlog(vault_root, edit_argv, capture=True)
        if proc.returncode != 0:
            raise lib.VaultError(f"backlog task edit failed: {(proc.stderr or proc.stdout).strip()}")
        touched.append(task_path)
        print(f"Closed TASK-{task_id}: Done{ref_note}")

        complete = lib.run_backlog(vault_root, ["task", "complete", task_id], capture=True)
        if complete.returncode != 0:
            raise lib.VaultError(f"backlog task complete failed: "
                              f"{(complete.stderr or complete.stdout).strip()}")
        # `task complete` moves the file tasks/ -> completed/ (same name); the
        # sync below needs both paths in its pathspec — the old one to stage
        # the deletion, the new one (untracked) to stage the addition.
        completed_path = vault_root / "backlog" / "completed" / task_path.name
        touched.append(completed_path)
        print(f"Completed TASK-{task_id}")

    log_path = vault_root / "wiki" / "log.md"
    suffix = f": {args.summary}" if args.summary else ""
    with log_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"\n## [{lib.today()}] done | {subject}{suffix}\n")
    touched.append(log_path)
    print(f"Logged done: {subject}")

    if not args.no_sync:
        rel = [str(Path(p).resolve().relative_to(vault_root)) for p in touched]
        result = sync_core(vault_root, conventions, f"done: {subject}", False, pathspec=rel)
        if result:
            return result
    return 0


# --------------------------------------------------------------------------- #
# doctor — the "it is broken" front door. Every check is isolated: it must
# report a Check, never raise, so the report always runs to completion even
# when every leg of the environment is broken. Reuses the same helpers real
# commands use (resolve_vault_root, load_conventions, run_all_lint_checks,
# run_git) so its verdicts match reality rather than a parallel diagnosis.
# --------------------------------------------------------------------------- #

class Check:
    """One diagnostic line. status is one of DOC_OK/DOC_WARN/DOC_FAIL/DOC_INFO;
    remedy is a one-clause fix, shown only when status isn't DOC_OK."""

    def __init__(self, name, status, detail, remedy=""):
        self.name = name
        self.status = status
        self.detail = detail
        self.remedy = remedy


DOC_OK = "ok"
DOC_WARN = "warn"
DOC_FAIL = "FAIL"
DOC_INFO = "info"

REQUIRED_CONVENTION_SECTIONS = (
    "frontmatter", "types", "tags", "plan_status", "size", "skip", "index", "folders",
)


def _safe_check(name, fn, *fn_args):
    """Run one check, catching anything it wasn't written to handle — a
    check that cannot run reports FAIL with the reason instead of crashing
    the whole report."""
    try:
        return fn(*fn_args)
    except Exception as e:
        return Check(name, DOC_FAIL, f"check crashed: {e}", "investigate and re-run")


def _safe_pair(name, fn, *fn_args):
    """Like _safe_check, for checks that also hand back state (vault_root,
    conventions) for later checks to depend on."""
    try:
        return fn(*fn_args)
    except Exception as e:
        return Check(name, DOC_FAIL, f"check crashed: {e}", "investigate and re-run"), None


def check_python():
    v = sys.version_info
    detail = f"{v.major}.{v.minor}.{v.micro} ({sys.executable})"
    if (v.major, v.minor) >= (3, 11):
        return Check("python", DOC_OK, detail)
    return Check("python", DOC_FAIL, detail, "upgrade to Python >= 3.11 (tomllib)")


def check_git_binary():
    path = shutil.which("git")
    if not path:
        return Check("git", DOC_WARN, "not on PATH", "install git (sync and init need it)")
    proc = subprocess.run(["git", "--version"], capture_output=True, text=True)
    version = proc.stdout.strip() if proc.returncode == 0 else "unknown version"
    return Check("git", DOC_OK, f"{version} ({path})")


def check_node(profile=None):
    if profile in NODELESS_PROFILES:
        return Check("node/npm/npx", DOC_INFO,
                      f"skipped — the {profile} profile has no node-dependent "
                      "commands (tome task is guarded off)")
    names = ["node", "npm", "npx"]
    missing = [n for n in names if not shutil.which(n)]
    if missing:
        return Check("node/npm/npx", DOC_WARN, f"missing: {', '.join(missing)}",
                      "install Node.js (backlog.md needs it)")
    versions = []
    for n in names:
        proc = subprocess.run([n, "--version"], capture_output=True, text=True,
                               shell=(sys.platform == "win32"))
        versions.append(f"{n} {proc.stdout.strip() if proc.returncode == 0 else 'unknown'}")
    return Check("node/npm/npx", DOC_OK, ", ".join(versions))


def check_vault_resolution(explicit):
    """Reuses resolve_vault_root itself for the pass/fail decision; only
    re-derives which source matched (for the report line), it doesn't
    re-decide priority."""
    try:
        root = lib.resolve_vault_root(explicit)
    except lib.VaultError as e:
        if not explicit and os.environ.get("VAULT_ROOT"):
            return Check("vault resolution", DOC_FAIL, str(e), "fix or unset VAULT_ROOT"), None
        return Check("vault resolution", DOC_INFO, "no vault found — run `tome init`"), None

    if explicit:
        source = "--vault"
    else:
        cur = Path.cwd().resolve()
        walked = any((d / "conventions.toml").is_file() for d in (cur, *cur.parents))
        source = "walk-up" if walked else "VAULT_ROOT"
    return Check("vault resolution", DOC_OK, f"{root} (via {source})"), root


def check_conventions(vault_root):
    try:
        conventions = lib.load_conventions(vault_root)
    except Exception as e:
        return Check("conventions.toml", DOC_FAIL, f"failed to parse: {e}",
                      "fix conventions.toml syntax"), None
    missing = [s for s in REQUIRED_CONVENTION_SECTIONS if s not in conventions]
    if missing:
        return Check("conventions.toml", DOC_FAIL,
                      f"missing section(s): {', '.join(missing)}",
                      "add the missing section(s) to conventions.toml"), conventions
    return Check("conventions.toml", DOC_OK, "parses; all required sections present"), conventions


def check_vault_shape(vault_root):
    wiki = vault_root / "wiki"
    required = [wiki, wiki / "index.md", wiki / "SCHEMA.md", wiki / "log.md"]
    missing = [p.relative_to(vault_root).as_posix() for p in required if not p.exists()]
    if missing:
        return Check("vault shape", DOC_FAIL, f"missing: {', '.join(missing)}",
                      "restore the missing vault file(s)")
    return Check("vault shape", DOC_OK, "wiki/, index.md, SCHEMA.md, log.md present")


def check_lint(vault_root, conventions):
    _, findings = lib.run_all_lint_checks(vault_root, conventions)
    errors = [f for f in findings if f.severity == lib.ERROR]
    warnings = [f for f in findings if f.severity == lib.WARNING]
    detail = f"{len(errors)} error(s), {len(warnings)} warning(s)"
    if errors:
        return Check("lint", DOC_FAIL, detail, "run `tome lint` for details")
    if warnings:
        return Check("lint", DOC_WARN, detail, "run `tome lint` for details")
    return Check("lint", DOC_OK, detail)


def check_git_state(vault_root):
    if not shutil.which("git"):
        return Check("git state", DOC_WARN, "git not on PATH — cannot inspect", "install git")

    branch = lib.run_git(vault_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch.returncode != 0:
        return Check("git state", DOC_FAIL, branch.stderr.strip() or "not a git repository",
                      "run `git init` in the vault")
    branch_name = branch.stdout.strip()

    remote = lib.run_git(vault_root, ["remote"])
    has_remote = bool(remote.stdout.strip())

    status = lib.run_git(vault_root, ["status", "--porcelain"])
    dirty = bool(status.stdout.strip())

    detail = (f"branch={branch_name}, "
              f"{'remote configured' if has_remote else 'no remote'}, "
              f"{'dirty' if dirty else 'clean'}")

    remedies = []
    if branch_name != "main":
        remedies.append("switch to main before syncing")
    if not has_remote:
        remedies.append("configure a remote before syncing")
    status_level = DOC_WARN if remedies else DOC_OK
    return Check("git state", status_level, detail, "; ".join(remedies))


def check_plugin_freshness():
    """Warns when a dev checkout's plugin.json (this file's own repo, when
    it's actually a checkout and not a hidden pip/wheel install) has moved
    ahead of the plugin cached for the active session — found via
    $TOME_PLUGIN_ROOT, which the SessionStart hook exports from
    $CLAUDE_PLUGIN_ROOT. A directory-source marketplace doesn't auto-refresh
    when the repo advances, so this drift is otherwise silent (task-57: the
    installed plugin sat at 1.2.5 while the repo had shipped 1.2.18)."""
    dev_plugin_json = Path(__file__).resolve().parent.parent.parent / ".claude-plugin" / "plugin.json"
    if not dev_plugin_json.is_file():
        return Check("plugin freshness", DOC_INFO, "not running from a dev checkout — skipped")

    cached_root = os.environ.get("TOME_PLUGIN_ROOT")
    if not cached_root:
        return Check("plugin freshness", DOC_INFO,
                      "TOME_PLUGIN_ROOT unset — no active session plugin to compare")

    cached_plugin_json = Path(cached_root) / ".claude-plugin" / "plugin.json"
    if not cached_plugin_json.is_file():
        return Check("plugin freshness", DOC_WARN,
                      f"{cached_plugin_json} not found", "reinstall the plugin")

    try:
        dev_version = json.loads(dev_plugin_json.read_text(encoding="utf-8"))["version"]
        cached_version = json.loads(cached_plugin_json.read_text(encoding="utf-8"))["version"]
    except (OSError, json.JSONDecodeError, KeyError) as e:
        return Check("plugin freshness", DOC_WARN, f"couldn't read plugin.json: {e}")

    if dev_version == cached_version:
        return Check("plugin freshness", DOC_OK, f"cached matches dev checkout ({cached_version})")
    return Check("plugin freshness", DOC_WARN,
                  f"cached plugin is {cached_version}, dev checkout is {dev_version}",
                  "claude plugin update tome@tome")


def render_check_line(c):
    line = f"{c.status:<4} {c.name}: {c.detail}"
    if c.status != DOC_OK and c.remedy:
        line += f" — {c.remedy}"
    return line


def check_ops_profile():
    profile = os.environ.get("TOME_OPS_PROFILE")
    if not profile:
        return Check("ops profile", DOC_INFO, "unset — full command surface")
    allowed = OPS_PROFILES.get(profile)
    if allowed is None:
        return Check("ops profile", DOC_FAIL, f"unknown profile '{profile}'",
                      "fix or unset TOME_OPS_PROFILE")
    return Check("ops profile", DOC_INFO,
                  f"{profile} (allowed: {', '.join(sorted(allowed))})")


def cmd_doctor(args):
    profile = os.environ.get("TOME_OPS_PROFILE")
    checks = [
        _safe_check("python", check_python),
        _safe_check("git", check_git_binary),
        _safe_check("node/npm/npx", check_node, profile),
        _safe_check("plugin freshness", check_plugin_freshness),
        _safe_check("ops profile", check_ops_profile),
    ]

    vault_check, vault_root = _safe_pair("vault resolution", check_vault_resolution, args.vault)
    checks.append(vault_check)

    conventions = None
    if vault_root is not None:
        conv_check, conventions = _safe_pair("conventions.toml", check_conventions, vault_root)
        checks.append(conv_check)
    else:
        checks.append(Check("conventions.toml", DOC_INFO, "no vault found — skipped"))

    if vault_root is not None:
        checks.append(_safe_check("vault shape", check_vault_shape, vault_root))
    else:
        checks.append(Check("vault shape", DOC_INFO, "no vault found — skipped"))

    if vault_root is not None and conventions is not None:
        checks.append(_safe_check("lint", check_lint, vault_root, conventions))
    else:
        checks.append(Check("lint", DOC_INFO, "no vault or conventions — skipped"))

    if vault_root is not None:
        checks.append(_safe_check("git state", check_git_state, vault_root))
    else:
        checks.append(Check("git state", DOC_INFO, "no vault found — skipped"))

    for c in checks:
        print(render_check_line(c))

    n_ok = sum(1 for c in checks if c.status == DOC_OK)
    n_warn = sum(1 for c in checks if c.status == DOC_WARN)
    n_fail = sum(1 for c in checks if c.status == DOC_FAIL)
    n_info = sum(1 for c in checks if c.status == DOC_INFO)
    print(f"\n{n_ok} ok, {n_warn} warn, {n_fail} FAIL, {n_info} info")
    return 1 if n_fail else 0


# --------------------------------------------------------------------------- #
# help
# --------------------------------------------------------------------------- #

HELP_TEXT = """\
tome.py — mechanical vault operations (see wiki/SCHEMA.md for the "why")

Write commands (new, describe, set-status, mv, rm, log, inbox) all take
--sync [-m "message"]: commit+push right after, scoped to just the files
that command touched (never the whole tree) — a message is auto-generated
if you omit -m.

  tome new <type> <slug> --project <name> --title "T" --desc "..." [--sync]
      Scaffold a page. type: project|plan|idea|decision|report|source|
      concept|synthesis. For type=project, omit --project (slug IS the
      project). Regenerates the index.
      e.g. tome new idea offline-mode --project vaulty --title "Offline mode" --desc "Cache reads for flights."

      For type=plan, add --with-task "Title" to also create a linked
      Backlog task in one shot: labeled project:<name>, --ref pointing at
      the plan, description from --desc. --priority/--ac (repeatable)/
      --milestone pass through to the task; all three only apply alongside
      --with-task.
      e.g. tome new plan offline-mode --project vaulty --title "T" --desc "..." --with-task "Ship offline mode" --priority high --ac "Works on a flight" --milestone cloud-facing-vault

  tome describe <slug> "<one-liner>" [--sync]
      Replace a page's index summary (<=140 chars). Regenerates the index.
      e.g. tome describe vault-cli "Stdlib CLI owning vault mechanics."

  tome set-status <slug> <status> [--sync]
      Plans: proposed|active|blocked|done|superseded|abandoned (moves
      plans/ <-> plans/archive/ automatically). Decisions: proposed|current.
      e.g. tome set-status vault-cli active

  tome mv <slug> <new-slug> [--sync]
      Rename a page; rewrites every inbound [[wikilink]] across the wiki.
      e.g. tome mv vault-cli vaultctl

  tome rm <slug> [--force] [--sync]
      Delete a page. Refuses project hubs always; refuses pages with inbound
      links unless --force (prints the linkers either way). Regenerates the
      index.
      e.g. tome rm scratch-page --force

  tome archive <slug> [--restore] [--sync]
      Move a status-less page (idea, report, source, note — not plan/decision,
      which use `set-status`) to/from a sibling archive/ folder. Regenerates
      the index; no link rewriting needed (slug is unchanged).
      e.g. tome archive my-idea

  tome read <ident> [--json]
      Print a page's markdown. --json emits {path, slug, hash, frontmatter,
      body} instead — `body` is what `tome write` takes, and `hash` is the
      conflict token it demands back, so one read closes the whole
      read-modify-write loop.
      e.g. tome read render-layer-principle --json

  tome write <ident> [text] --base-hash H [--body-file PATH] [--no-sync]
      Replace a page's body (frontmatter untouched — describe/mv/set-status
      own their fields) from the argument, a file, or stdin. Refuses on a
      stale --base-hash, and on any lint error keyed to this page (the
      original bytes are restored either way).
      e.g. tome write my-idea "# My idea\\n\\nRevised." --base-hash 4f3a...

  tome append <ident> [text] [--under "## Heading"] [--body-file PATH] [--no-sync]
      Append to the end of a page's body, or to the end of one named section.
      Needs no --base-hash: an append doesn't overwrite, so concurrent
      appends aren't a conflict worth refusing.
      e.g. tome append truck "- New tyres 2026-07-01." --under "## Log"

  <ident> above is any address another tome surface prints: a bare slug, a
  [[wikilink]], a wiki-relative path (tome/ideas/x.md), or a vault-relative
  one (wiki/tome/ideas/x.md) — case-insensitive, .md optional.

  read/write/append commit and push by *default* (--no-sync opts out), unlike
  every other write command's opt-in --sync: a hash token is only meaningful
  against a synced state, and on a disposable remote clone an unsynced write
  is a lost one.

  tome search "<query>" [--top N] [--type T] [--tag T ...] [--since YYYY-MM-DD]
      BM25 search over wiki pages (fallback when index-first navigation
      doesn't surface the right pages). Also: --backlinks <slug>,
      --top-linked N.
      e.g. tome search "quartz spike" --top 5

  tome prime [project] [--full]
      Print session orientation. Bare: the terse vault pointer (same text
      the SessionStart hook injects). --full prints SCHEMA.md and the index
      instead — not in addition, since the hook already covers the terse
      tier for any session that went through it; with a project, also its
      hub, every live plan's full body, and a recent log.md tail — the
      write protocol, replacing the read fan-out a skill used to open with.
      An open-task snapshot (grouped by milestone with done/total counts,
      scoped to the project when one is given) comes last: knowledge leads,
      the board trails.
      e.g. tome prime tome --full

  tome log <op> "<message>" [--body "..."] [--sync]
      Append a formatted entry to wiki/log.md.
      e.g. tome log work-started "Began TASK-26"

  tome inbox "<note>" [--title "T"] [--sync]
      Drop a schema-free capture note in inbox/YYYY-MM-DD-<slug>.md (slug
      from --title or the note's first few words). Multi-line notes allowed.
      Never scanned by lint; triaged into the wiki by retrospect.
      e.g. tome inbox "Remember: X does Y because Z"

  tome index rebuild
      Regenerate wiki/index.md from page frontmatter.

  tome lint [--strict]
      Structural checks (broken links, orphans, frontmatter, index drift),
      plus two warn-tier signals gated on conventions.toml opting in:
      stale well-linked pages ([staleness]) and a capture queue that has
      stopped draining ([inbox]).

  tome sync [<slug-or-task-id>...] [-m "message"] [--no-verify]
      Pull (always). If dirty: lint-gates (errors abort, --no-verify skips),
      then commit (message required, unless entities given) + push.
      main-only. With entities: scopes the commit to each one's resolved
      cluster (page, linked task, hub, index, log) instead of the whole
      tree, printing anything else left dirty.
      e.g. tome sync -m "Add offline-mode idea"
      e.g. tome sync workflow-compression task-47

  tome task <args...>
      Passthrough to `npx --yes backlog.md@{BACKLOG_VERSION} <args...>` from the
      vault root (the version is pinned, not `@latest`).
      e.g. tome task list --plain

  tome start <plan-slug-or-task-id>
      Bundle the work-started ritual: set the linked plan active, move the
      linked task to In Progress (-a @me), log work-started, sync (unless
      --no-sync), then print the task and full plan body as working context.
      e.g. tome start task-47

  tome done <plan-slug-or-task-id> [--summary "..."] [--as STATUS] [--no-check-ac] [--force] [--no-sync]
      Bundle the close-out ritual: set the plan's terminal status (default
      done; archives it, regenerates hub + index), close the linked task
      (Done, every AC checked unless --no-check-ac, --final-summary if
      --summary given, --ref re-pointed at the archived path, then
      completed), log done, sync (unless --no-sync). A task id with no
      linked plan just closes and completes the task (no plan step; --as
      is rejected).
      Umbrella guard: when a plan is shared by several phase tasks, closing a
      phase task with open siblings closes only the task and leaves the plan
      active; closing the plan slug while open tasks still reference it is
      refused unless --force. The last sibling's close archives the plan.
      e.g. tome done workflow-compression --summary "Shipped pieces 1-3."
      e.g. tome done task-57 --summary "Plan-less task, closed directly."

  tome init [path]
      Scaffold a fresh, empty vault at path (default: cwd). Fail-loud if
      anything it would create already exists.
      e.g. tome init ~/Development/my-vault

  tome doctor
      Diagnose python/git/node, vault resolution, conventions, vault shape,
      lint, git state, and the ops profile. ok/warn/FAIL per line;
      exit 1 on any FAIL. Runs to completion even with no vault or a broken
      one, and under any TOME_OPS_PROFILE (help/doctor always run).
      e.g. tome doctor

  tome serve [--host H] [--port N] [--open] [--export DIR] [--idle-timeout MIN]
      Serve the no-build browse frontend locally (stdlib http.server): the
      frontend's static files, the vault's raw .md under /raw/, and two
      generated JSON contracts (/index.json, /board.json) rebuilt per
      request. Write routes (printed in full on startup): task move/create/
      edit shell out to backlog.md — never a direct YAML write — while page body,
      frontmatter, rename, and new-page writes route through the same tome
      operations and lint gate the CLI uses, and /api/conflict* drives the
      three-way resolver for a stopped rebase.
      --export DIR writes the same frontend plus a frozen
      index.json/board.json/raw/*.md snapshot to DIR instead of serving — a
      read-only static deploy (no write routes, board.json.writable: false)
      for any static host. --idle-timeout MIN auto-exits after MIN idle
      minutes (0 default disables it; the pythonw desktop launcher installed
      as project.gui-scripts uses 30, since it has no console to Ctrl-C).
      e.g. tome serve --open, or tome serve --export ./public

Root resolution: --vault PATH, else walk up from cwd
looking for conventions.toml, else $VAULT_ROOT.

Headless remote consumers (env vars, for a container with no human at the
keyboard — see README.md's "Headless bootstrap" section for the full recipe):

  VAULT_ROOT           Vault root when not standing in one (still overridden
                        by --vault / a walk-up match).
  TOME_OPS_PROFILE      Optional. Narrows the command surface for a
                        deployment that shouldn't be trusted with all of it;
                        unset (the default) is the full surface, since a
                        memory an agent can't write is a library card. Two
                        profiles ship: read-capture allows only search,
                        prime, doctor, help, inbox; authoring adds the
                        knowledge-half write surface (read, write, append,
                        new, describe, set-status, archive, mv, log) while
                        still refusing rm, sync, task, start and done.
                        Everything else — including a command added later —
                        is refused with a clear message. help/doctor always
                        run.
  TOME_GIT_AUTHOR       "Name <email>" applied as author (via `git commit
                        --author`) and, unless GIT_COMMITTER_* is set
                        explicitly, as committer identity on every
                        tome-driven git call, so a vault's git log shows
                        which surface (local session vs. a given remote
                        deployment) made each change and commits work
                        without any git config on the container.
"""


def cmd_help(args):
    # The pinned backlog.md version is substituted rather than written into
    # HELP_TEXT, so help can't drift from BACKLOG_VERSION the way `@latest` did.
    print(HELP_TEXT.replace("{BACKLOG_VERSION}", lib.BACKLOG_VERSION))
    return 0


# --------------------------------------------------------------------------- #
# argparse wiring
# --------------------------------------------------------------------------- #

def add_sync_flag(p, dest="message"):
    """--sync [-m ...] on a write command: commit+push (scoped to just that
    command's touched files) right after it runs. dest differs only for
    `log`, whose positional `message` argument already owns that name."""
    p.add_argument("--sync", action="store_true",
                   help="commit+push this command's touched files after it runs")
    if dest == "message":
        p.add_argument("-m", "--message",
                        help="commit message for --sync (auto-generated if omitted)")
    else:
        p.add_argument("-m", "--sync-message", dest=dest,
                        help="commit message for --sync (auto-generated if omitted)")


def build_parser():
    parser = argparse.ArgumentParser(prog="tome", add_help=True,
                                      description="Vault mechanical operations.")
    parser.add_argument("--vault", help="explicit vault root (default: walk-up "
                                         "from cwd, else $VAULT_ROOT)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("help", help="print the command overview")

    p = sub.add_parser("lint", help="run structural checks",
                        epilog="e.g. tome lint --strict")
    p.add_argument("--strict", action="store_true")

    p = sub.add_parser("sync", help="pull, and commit+push if dirty",
                        epilog='e.g. tome sync -m "message", or tome sync workflow-compression task-47')
    p.add_argument("entities", nargs="*",
                   help="optional slug(s)/task id(s) to scope the commit to "
                        "(page + linked task + hub + index + log only)")
    p.add_argument("-m", "--message", help="commit message (auto-generated "
                                            "if entities given and omitted; "
                                            "required otherwise if dirty)")
    p.add_argument("--no-verify", action="store_true",
                   help="skip the lint gate on the commit path")

    p = sub.add_parser("task", help="passthrough to backlog.md",
                        epilog="e.g. tome task list --plain", add_help=False)
    p.add_argument("args", nargs=argparse.REMAINDER)

    p = sub.add_parser("start", help="bundle the work-started ritual",
                        epilog="e.g. tome start task-47")
    p.add_argument("entity", help="a plan slug or a backlog task id")
    p.add_argument("--no-sync", action="store_true",
                   help="skip the sync that runs by default after this command")

    p = sub.add_parser("done", help="bundle the close-out ritual",
                        epilog='e.g. tome done workflow-compression --summary "..."')
    p.add_argument("slug", help="a plan slug or a backlog task id")
    p.add_argument("--summary", help="the task's final summary")
    p.add_argument("--as", dest="as_status", metavar="STATUS",
                   help="terminal status to set instead of 'done' "
                        "(e.g. superseded, abandoned)")
    p.add_argument("--no-check-ac", action="store_true",
                   help="don't check every acceptance criterion on the linked task")
    p.add_argument("--force", action="store_true",
                   help="archive a plan even while open tasks still reference it "
                        "(dangles their refs — closing a plan slug otherwise "
                        "refuses when it has live referents)")
    p.add_argument("--no-sync", action="store_true",
                   help="skip the sync that runs by default after this command")

    p = sub.add_parser("new", help="scaffold a page",
                        epilog='e.g. tome new plan x --project vaulty --title "T" --desc "..." '
                               '--with-task "Do the thing"')
    p.add_argument("type")
    p.add_argument("slug")
    p.add_argument("--project")
    p.add_argument("--title", required=True)
    p.add_argument("--desc", required=True)
    p.add_argument("--with-task", metavar="TITLE",
                   help="also create a linked Backlog task (plan type only)")
    p.add_argument("--priority", help="task priority — only with --with-task")
    p.add_argument("--ac", action="append",
                   help="task acceptance criterion, repeatable — only with --with-task")
    p.add_argument("--milestone", metavar="NAME",
                   help="assign the linked task to a milestone (id or title) — only with --with-task")
    add_sync_flag(p)

    p = sub.add_parser("describe", help="replace a page's index summary",
                        epilog='e.g. tome describe vault-cli "..."')
    p.add_argument("slug")
    p.add_argument("text")
    add_sync_flag(p)

    p = sub.add_parser("set-status", help="change a plan/decision's status",
                        epilog="e.g. tome set-status vault-cli active")
    p.add_argument("slug")
    p.add_argument("status")
    add_sync_flag(p)

    p = sub.add_parser("mv", help="rename a page, rewriting inbound links",
                        epilog="e.g. tome mv old-slug new-slug")
    p.add_argument("slug")
    p.add_argument("new_slug")
    add_sync_flag(p)

    p = sub.add_parser("rm", help="delete a page, refusing hubs/linked pages by default",
                        epilog="e.g. tome rm scratch-page --force")
    p.add_argument("slug")
    p.add_argument("--force", action="store_true",
                   help="delete even with inbound links, reporting the breakage")
    add_sync_flag(p)

    p = sub.add_parser("archive", help="archive/restore a status-less page (e.g. an idea)",
                        epilog="e.g. tome archive my-idea")
    p.add_argument("slug")
    p.add_argument("--restore", action="store_true",
                   help="restore from archive/ instead of archiving")
    add_sync_flag(p)

    p = sub.add_parser("read", help="print a page's markdown",
                        epilog="e.g. tome read render-layer-principle --json")
    p.add_argument("ident", help="a slug, a wiki-relative path, or a vault-relative one")
    p.add_argument("--json", action="store_true",
                   help="emit {path, slug, hash, frontmatter, body}; the hash is "
                        "the conflict token `tome write` takes back")

    p = sub.add_parser("write", help="replace a page's body",
                        epilog='e.g. tome write my-idea "# My idea\\n\\nBody." --base-hash abc123')
    p.add_argument("ident", help="a slug, a wiki-relative path, or a vault-relative one")
    p.add_argument("text", nargs="?", help="the new body (else --body-file, else stdin)")
    p.add_argument("--body-file", metavar="PATH", help="read the new body from a file")
    p.add_argument("--base-hash", required=True,
                   help="the hash `tome read --json` gave you; a mismatch refuses "
                        "the write instead of clobbering someone else's edit")
    p.add_argument("--no-sync", action="store_true",
                   help="skip the commit+push that runs by default after this command")

    p = sub.add_parser("append", help="append to a page's body or one of its sections",
                        epilog='e.g. tome append truck "- New tyres 2026-07-01." --under "## Log"')
    p.add_argument("ident", help="a slug, a wiki-relative path, or a vault-relative one")
    p.add_argument("text", nargs="?", help="the text to append (else --body-file, else stdin)")
    p.add_argument("--body-file", metavar="PATH", help="read the appended text from a file")
    p.add_argument("--under", metavar="HEADING",
                   help='append inside this section instead of at the end of the '
                        'page (e.g. "## Notes")')
    p.add_argument("--base-hash",
                   help="optional — an append doesn't overwrite, so it needs no "
                        "conflict token")
    p.add_argument("--no-sync", action="store_true",
                   help="skip the commit+push that runs by default after this command")

    p = sub.add_parser("search", help="BM25 search over wiki pages",
                        epilog='e.g. tome search "quartz spike" --top 5')
    p.add_argument("query", nargs="?", default="", help="query terms")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--type", help="filter by frontmatter type")
    p.add_argument("--tag", action="append", default=[], help="filter by tag (repeatable)")
    p.add_argument("--since", help="only pages updated on or after YYYY-MM-DD")
    p.add_argument("--backlinks", help="find pages linking to this slug; ignores the query")
    p.add_argument("--top-linked", type=int, help="show the N most-linked-to pages; ignores the query")

    p = sub.add_parser("prime", help="print session-orientation context",
                        epilog="e.g. tome prime tome --full")
    p.add_argument("project", nargs="?", help="a project to prime the full-tier context for")
    p.add_argument("--full", action="store_true",
                   help="print SCHEMA.md, the index, and (with a project) its hub, live "
                        "plan bodies, and a recent log tail, instead of the terse tier")

    p = sub.add_parser("log", help="append a wiki/log.md entry",
                        epilog='e.g. tome log work-started "..."')
    p.add_argument("op")
    p.add_argument("message")
    p.add_argument("--body")
    add_sync_flag(p, dest="sync_message")

    p = sub.add_parser("inbox", help="drop a schema-free capture note in inbox/",
                        epilog='e.g. tome inbox "Remember: X does Y because Z"')
    p.add_argument("note")
    p.add_argument("--title", help="override the note's derived slug basis")
    add_sync_flag(p)

    idx = sub.add_parser("index", help="index operations")
    idx_sub = idx.add_subparsers(dest="index_command", required=True)
    idx_sub.add_parser("rebuild", help="regenerate wiki/index.md",
                        epilog="e.g. tome index rebuild")

    p = sub.add_parser("init", help="scaffold a fresh vault",
                        epilog="e.g. tome init ~/Development/my-vault")
    p.add_argument("path", nargs="?", help="target directory (default: cwd)")

    sub.add_parser("doctor", help="diagnose the environment and vault",
                    epilog="e.g. tome doctor")

    p = sub.add_parser("serve", help="serve the browse frontend locally",
                        epilog='e.g. tome serve --open, or tome serve --export ./public')
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765,
                   help="port (default: 8765)")
    p.add_argument("--open", action="store_true",
                   help="open the browser once the server is up")
    p.add_argument("--export", metavar="DIR",
                   help="write a static, read-only snapshot to DIR instead of serving")
    p.add_argument("--idle-timeout", type=int, default=0, metavar="MIN",
                   help="auto-exit after MIN idle minutes, 0 disables (default: 0; "
                        "the pythonw launcher installed by project.gui-scripts uses 30)")

    return parser


def main():
    # Windows consoles default to a legacy code page (cp1252/cp437), which
    # can't encode the em-dashes and other punctuation used throughout this
    # CLI's own output (e.g. HELP_TEXT) — reconfigure to UTF-8 rather than
    # let print() crash with a UnicodeEncodeError. Piped/redirected streams
    # on any platform may lack .reconfigure() (e.g. some test harnesses'
    # capture objects), so guard with hasattr instead of assuming stdlib.
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args()

    guard_code = enforce_ops_profile(args.command)
    if guard_code is not None:
        return guard_code

    try:
        if args.command == "init":
            return cmd_init(args)
        if args.command == "help":
            return cmd_help(args)
        if args.command == "doctor":
            return cmd_doctor(args)

        vault_root = lib.resolve_vault_root(args.vault)
        conventions = lib.load_conventions(vault_root)

        if args.command == "lint":
            return cmd_lint(vault_root, conventions, args)
        if args.command == "sync":
            return cmd_sync(vault_root, conventions, args)
        if args.command == "task":
            return cmd_task(vault_root, conventions, args)
        if args.command == "start":
            return cmd_start(vault_root, conventions, args)
        if args.command == "done":
            return cmd_done(vault_root, conventions, args)
        if args.command == "new":
            return cmd_new(vault_root, conventions, args)
        if args.command == "describe":
            return cmd_describe(vault_root, conventions, args)
        if args.command == "set-status":
            return cmd_set_status(vault_root, conventions, args)
        if args.command == "mv":
            return cmd_mv(vault_root, conventions, args)
        if args.command == "rm":
            return cmd_rm(vault_root, conventions, args)
        if args.command == "archive":
            return cmd_archive(vault_root, conventions, args)
        if args.command == "read":
            return cmd_read(vault_root, conventions, args)
        if args.command == "write":
            return cmd_write(vault_root, conventions, args)
        if args.command == "append":
            return cmd_append(vault_root, conventions, args)
        if args.command == "search":
            return cmd_search(vault_root, conventions, args)
        if args.command == "prime":
            return cmd_prime(vault_root, conventions, args)
        if args.command == "log":
            return cmd_log(vault_root, conventions, args)
        if args.command == "inbox":
            return cmd_inbox(vault_root, conventions, args)
        if args.command == "index" and args.index_command == "rebuild":
            return cmd_index_rebuild(vault_root, conventions, args)
        if args.command == "serve":
            # Deferred for startup cost, not for a cycle: serve pulls in
            # http.server/threading/queue, which no other command needs.
            from tome_cli import serve
            return serve.cmd_serve(vault_root, conventions, args)
        parser.error(f"unknown command {args.command}")
    except lib.VaultError as e:
        print(f"tome: error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
