#!/usr/bin/env python3
"""
tome.py — the vault's mechanical conventions, owned by code instead of prose.

wiki/SCHEMA.md documents the vault's rules; historically they were enforced
only by an agent choosing to follow them, and that drifts (BAD_TAG failures,
index entries bloated into paragraphs, log entries out of format, work
stranded uncommitted). This CLI is the fix: the agent still owns prose (page
bodies, judgment calls, what links to what); this tool owns invariants
(scaffolding, the generated index, status/archive moves, renames, git, the
log format).

stdlib only, Python >= 3.11 (needs tomllib). No pip installs — a fork runs it
bare. Imports scripts/tome_lint.py's run()/load_conventions() rather than
duplicating the structural checks.

Usage:
    python scripts/tome.py <command> [args...]
    python scripts/tome.py help

Run `python scripts/tome.py help` for the command overview, or
`python scripts/tome.py <command> -h` for one copy-pasteable example per
command.
"""

import argparse
import os
import re
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tome_lint  # noqa: E402 — same directory, see module docstring above

ERROR = tome_lint.ERROR
Finding = tome_lint.Finding
FRONTMATTER_RE = tome_lint.FRONTMATTER_RE

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# type -> the taxonomy tag paired with the project-name tag on new pages.
# Everything not listed here gets "project" (matches the observed convention
# across plan/report/source/concept/synthesis/project pages in this vault).
TYPE_TAG = {"idea": "idea", "decision": "decision"}

# type -> generated-index group header, in the fixed display order.
GROUP_FOR_TYPE = {
    "plan": "Plans",
    "idea": "Ideas",
    "report": "Reports",
    "decision": "Decisions",
    "source": "Sources",
    "concept": "Notes",
    "synthesis": "Notes",
    "entity": "Notes",
}
GROUP_ORDER = ["Plans — live", "Plans — archived", "Ideas", "Ideas — archived",
               "Reports", "Decisions", "Sources", "Notes"]

CROSS_CUTTING_DIRS = ("ideas", "general")


class VaultError(Exception):
    """A fail-loud, user-facing error. main() prints str(e) and exits 1."""


# --------------------------------------------------------------------------- #
# Root resolution / conventions loading
# --------------------------------------------------------------------------- #

def resolve_vault_root(explicit):
    """--vault flag -> VAULT_ROOT env var -> walk up from cwd looking for
    conventions.toml -> fail loud. No hardcoded home paths."""
    candidates = []
    if explicit:
        candidates.append(("--vault", Path(explicit)))
    elif os.environ.get("VAULT_ROOT"):
        candidates.append(("VAULT_ROOT", Path(os.environ["VAULT_ROOT"])))
    if candidates:
        source, p = candidates[0]
        p = p.resolve()
        if not (p / "conventions.toml").is_file():
            raise VaultError(f"{source}={p} has no conventions.toml — not a vault root")
        return p
    cur = Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if (d / "conventions.toml").is_file():
            return d
    raise VaultError(
        "could not find conventions.toml by walking up from "
        f"{cur} — pass --vault PATH or set VAULT_ROOT"
    )


def load_conventions(vault_root):
    return tome_lint.load_conventions(vault_root / "conventions.toml")


# --------------------------------------------------------------------------- #
# Frontmatter read/write helpers
#
# tome_lint.parse_frontmatter() is read-only (dict out). Lifecycle commands
# need to mutate specific keys while leaving everything else — body,
# formatting, comments a human added — untouched, so this operates on the raw
# frontmatter *lines* rather than round-tripping through a dict serializer.
# --------------------------------------------------------------------------- #

def read_page(path):
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise VaultError(f"{path}: no parseable frontmatter block")
    fm_lines = m.group(1).split("\n")
    body = text[m.end():]
    return fm_lines, body


def write_page(path, fm_lines, body):
    text = "---\n" + "\n".join(fm_lines) + "\n---\n" + body
    path.write_text(text, encoding="utf-8", newline="\n")


def fm_get(fm_lines, key):
    pat = re.compile(rf"^{re.escape(key)}:\s*(.*)$")
    for line in fm_lines:
        m = pat.match(line)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return None


def fm_set(fm_lines, key, value, quote=False):
    """Replace key's value if present; else insert right after the tags:
    block (or the end, if there is no tags: line)."""
    val_str = f'"{value}"' if quote else str(value)
    pat = re.compile(rf"^{re.escape(key)}:\s*.*$")
    for i, line in enumerate(fm_lines):
        if pat.match(line):
            fm_lines[i] = f"{key}: {val_str}"
            return fm_lines
    tags_idx = None
    for i, line in enumerate(fm_lines):
        if re.match(r"^tags:\s*", line):
            tags_idx = i
            break
    if tags_idx is None:
        fm_lines.append(f"{key}: {val_str}")
        return fm_lines
    j = tags_idx + 1
    while j < len(fm_lines) and fm_lines[j].startswith("  - "):
        j += 1
    fm_lines.insert(j, f"{key}: {val_str}")
    return fm_lines


def today():
    return date.today().isoformat()


def validate_oneline(value, field_name, max_chars=None):
    """Frontmatter string fields are always written double-quoted (fm_set /
    cmd_new), so a literal '"' would corrupt the block — reject it rather
    than silently mis-writing (see the regenerate-past-builds repair during
    the description migration, which hit exactly this)."""
    if "\n" in value:
        raise VaultError(f"{field_name} must be a single line")
    if '"' in value:
        raise VaultError(f'{field_name} must not contain a literal " character')
    if max_chars is not None and len(value) > max_chars:
        raise VaultError(f"{field_name} is {len(value)} chars (cap {max_chars})")


# --------------------------------------------------------------------------- #
# Page collection (thin wrapper over tome_lint's, kept in sync with it)
# --------------------------------------------------------------------------- #

def collect(vault_root, conventions):
    wiki_root = vault_root / "wiki"
    skip_files = set(conventions["skip"]["files"])
    skip_dirs = set(conventions["skip"]["dirs"])
    pages = tome_lint.collect_pages(wiki_root, skip_files, skip_dirs)
    return wiki_root, pages


def find_page(pages, slug):
    matches = [p for p in pages if p["slug"] == slug and "read_error" not in p]
    if not matches:
        raise VaultError(f"no page with slug '{slug}'")
    if len(matches) > 1:
        raise VaultError(f"slug '{slug}' is ambiguous: "
                          + ", ".join(p["rel_path"] for p in matches))
    return matches[0]


def all_slugs(pages):
    return {p["slug"] for p in pages}


def validate_slug(slug, pages, allow_existing=False):
    if not SLUG_RE.match(slug):
        raise VaultError(f"'{slug}' is not lowercase kebab-case")
    if not allow_existing and slug in all_slugs(pages):
        raise VaultError(f"slug '{slug}' already exists")


# --------------------------------------------------------------------------- #
# Generated index
# --------------------------------------------------------------------------- #

INDEX_PREAMBLE = """# Wiki Index

**Generated file — do not hand-edit.** Regenerate with
`python scripts/tome.py index rebuild` (every lifecycle command does this
automatically). Change a page's one-line summary with
`tome describe <slug> "..."`, never by editing this file directly.

The catalog of all pages in this wiki, organized by project. The LLM reads
this first when answering queries to identify candidate pages.

When this file exceeds ~300 lines or the wiki passes ~150 pages, shard into
`wiki/indexes/<project>.md`. See the `scaling-playbook.md` reference in the
`llm-wiki` skill for the migration procedure.

---
"""


def page_description(p):
    desc = p["meta"].get("description")
    return desc if isinstance(desc, str) and desc else "(no description)"


def index_line(p, alias=None):
    link = f"[[{p['slug']}|{alias}]]" if alias else f"[[{p['slug']}]]"
    return f"- {link} — {page_description(p)}"


def generate_index(pages, conventions, wiki_root):
    live_statuses = set(conventions["plan_status"]["live"])
    terminal_statuses = set(conventions["plan_status"]["terminal"])

    top_dirs = sorted(
        d.name for d in wiki_root.iterdir()
        if d.is_dir() and not d.name.startswith(".")
        and d.name not in set(conventions["skip"]["dirs"])
    )
    projects = [d for d in top_dirs if d not in CROSS_CUTTING_DIRS]

    by_project = {proj: [] for proj in projects}
    cross_ideas = []
    general = []
    for p in pages:
        parts = Path(p["rel_path"]).parts
        top = parts[0]
        if top in by_project:
            by_project[top].append(p)
        elif top == "ideas":
            cross_ideas.append(p)
        elif top == "general":
            general.append(p)
        # else: page outside any recognized top-level dir — shouldn't happen;
        # fail loud via the lint's INDEX_MISSING check rather than silently
        # dropping it here.

    out = [INDEX_PREAMBLE]

    for proj in sorted(projects):
        proj_pages = by_project[proj]
        hub = next((p for p in proj_pages
                    if p["rel_path"] == f"{proj}/{proj}.md"
                    and p["meta"].get("type") == "project"), None)
        out.append(f"## {proj.capitalize()}")
        out.append("")
        if hub:
            title = hub["meta"].get("title") or proj.capitalize()
            out.append(index_line(hub, alias=title))
            out.append("")

        groups = {name: [] for name in GROUP_ORDER}
        for p in proj_pages:
            if hub is not None and p is hub:
                continue
            t = p["meta"].get("type")
            rel = "/" + p["rel_path"]
            if t == "plan":
                status = p["meta"].get("status")
                archived = "/plans/archive/" in rel
                groups["Plans — archived" if (status in terminal_statuses or archived)
                       else "Plans — live"].append(p)
            elif t == "idea":
                archived = "/archive/" in rel
                groups["Ideas — archived" if archived else "Ideas"].append(p)
            elif t in GROUP_FOR_TYPE:
                groups[GROUP_FOR_TYPE[t]].append(p)
            # unrecognized/missing type: omitted from grouping; lint's
            # BAD_TYPE / INDEX_MISSING checks are what should catch this.

        for group_name in GROUP_ORDER:
            members = sorted(groups[group_name], key=lambda p: p["slug"])
            if not members:
                continue
            out.append(f"**{group_name}:**")
            out.extend(index_line(p) for p in members)
            out.append("")

    out.append("## Ideas (cross-cutting)")
    out.append("")
    out.append("(future-project ideas and loose notions not tied to an "
                "existing project)")
    out.append("")
    for p in sorted(cross_ideas, key=lambda p: p["slug"]):
        out.append(index_line(p))
    if cross_ideas:
        out.append("")

    out.append("## General")
    out.append("")
    out.append("(genuinely cross-cutting reference knowledge — spans projects)")
    out.append("")
    for p in sorted(general, key=lambda p: p["slug"]):
        out.append(index_line(p))
    if general:
        out.append("")

    text = "\n".join(out)
    if not text.endswith("\n"):
        text += "\n"
    # Collapse the trailing double-blank left by the loop's blank-line
    # bookkeeping into a single final newline.
    while text.endswith("\n\n"):
        text = text[:-1]
    return text


def rebuild_index(vault_root, conventions, wiki_root=None, pages=None):
    if wiki_root is None or pages is None:
        wiki_root, pages = collect(vault_root, conventions)
    index_path = wiki_root / conventions["index"]["file"]
    index_path.write_text(generate_index(pages, conventions, wiki_root),
                           encoding="utf-8", newline="\n")
    return index_path


# --------------------------------------------------------------------------- #
# vault lint (tome_lint.run() + two checks that need the generated index)
# --------------------------------------------------------------------------- #

def check_description_cap(pages, conventions):
    max_chars = conventions.get("description", {}).get("max_chars", 140)
    out = []
    for p in pages:
        if "read_error" in p or p.get("malformed_fm"):
            continue
        desc = p["meta"].get("description")
        if isinstance(desc, str) and len(desc) > max_chars:
            out.append(Finding(ERROR, "DESC_TOO_LONG", p["rel_path"],
                                f"description is {len(desc)} chars (cap {max_chars})"))
    return out


def check_index_generated_drift(pages, conventions, wiki_root, index_path):
    generated = generate_index(pages, conventions, wiki_root)
    try:
        actual = index_path.read_text(encoding="utf-8")
    except OSError as e:
        return [Finding(ERROR, "READ_ERROR", index_path.name, str(e))]
    if actual != generated:
        return [Finding(ERROR, "INDEX_DRIFT", index_path.name,
                         "index.md does not match a fresh rebuild — "
                         "run `python scripts/tome.py index rebuild`")]
    return []


def cmd_lint(vault_root, conventions, args):
    wiki_root = vault_root / "wiki"
    index_path = wiki_root / conventions["index"]["file"]
    pages, findings = tome_lint.run(wiki_root, conventions, index_path)
    findings += check_description_cap(pages, conventions)
    findings += check_index_generated_drift(pages, conventions, wiki_root, index_path)
    print(tome_lint.render_text(pages, findings))
    gating = findings if args.strict else [f for f in findings if f.severity == ERROR]
    return 1 if gating else 0


# --------------------------------------------------------------------------- #
# Lifecycle commands
# --------------------------------------------------------------------------- #

def cmd_new(vault_root, conventions, args):
    wiki_root, pages = collect(vault_root, conventions)
    type_enum = set(conventions["types"]["enum"])
    if args.type not in type_enum:
        raise VaultError(f"type '{args.type}' not in {sorted(type_enum)}")
    max_chars = conventions.get("description", {}).get("max_chars", 140)
    validate_oneline(args.desc, "--desc", max_chars)
    validate_oneline(args.title, "--title")

    if args.type == "project":
        project = args.slug
        validate_slug(project, pages)
        path = wiki_root / project / f"{project}.md"
    else:
        if not args.project:
            raise VaultError("--project is required for non-project types")
        project = args.project
        if not (wiki_root / project).is_dir():
            raise VaultError(f"no such project: wiki/{project}/ does not exist "
                              f"(create it first with `tome new project {project} ...`)")
        validate_slug(args.slug, pages)
        folders = conventions["folders"]
        if args.type not in folders:
            raise VaultError(f"no [folders] mapping for type '{args.type}'")
        path = wiki_root / project / folders[args.type] / f"{args.slug}.md"

    if path.exists():
        raise VaultError(f"{path} already exists")

    title = args.title
    tag_kind = TYPE_TAG.get(args.type, "project")
    fm_lines = [
        f"type: {args.type}",
        f'title: "{title}"',
        f"tags: [{project}, {tag_kind}]",
        f'description: "{args.desc}"',
        f"created: {today()}",
        f"updated: {today()}",
    ]
    if args.type in ("plan", "decision"):
        fm_lines.append("status: proposed")

    path.parent.mkdir(parents=True, exist_ok=True)
    write_page(path, fm_lines, f"\n# {title}\n\nTBD.\n")

    _, pages = collect(vault_root, conventions)
    rebuild_index(vault_root, conventions, wiki_root, pages)

    slug = project if args.type == "project" else args.slug
    print(f"Created {path.relative_to(vault_root)}")
    print("Next: edit the body, link it from the project hub, then:")
    print(f'  python scripts/tome.py log author "authored {slug}"')
    print('  python scripts/tome.py sync -m "..."')
    return 0


def cmd_describe(vault_root, conventions, args):
    wiki_root, pages = collect(vault_root, conventions)
    page = find_page(pages, args.slug)
    max_chars = conventions.get("description", {}).get("max_chars", 140)
    validate_oneline(args.text, "description", max_chars)

    fm_lines, body = read_page(page["path"])
    fm_set(fm_lines, "description", args.text, quote=True)
    fm_set(fm_lines, "updated", today())
    write_page(page["path"], fm_lines, body)

    _, pages = collect(vault_root, conventions)
    rebuild_index(vault_root, conventions, wiki_root, pages)
    print(f"Updated description for [[{args.slug}]]")
    return 0


def cmd_set_status(vault_root, conventions, args):
    wiki_root, pages = collect(vault_root, conventions)
    page = find_page(pages, args.slug)
    ptype = page["meta"].get("type")

    if ptype == "plan":
        live = set(conventions["plan_status"]["live"])
        terminal = set(conventions["plan_status"]["terminal"])
        valid = live | terminal
        if args.status not in valid:
            raise VaultError(f"plan status must be one of {sorted(valid)}")
    elif ptype == "decision":
        valid = {"proposed", "current"}
        if args.status not in valid:
            raise VaultError(f"decision status must be one of {sorted(valid)}")
    else:
        raise VaultError(f"type '{ptype}' does not carry a status")

    fm_lines, body = read_page(page["path"])
    fm_set(fm_lines, "status", args.status)
    fm_set(fm_lines, "updated", today())
    write_page(page["path"], fm_lines, body)

    new_path = page["path"]
    if ptype == "plan":
        terminal = set(conventions["plan_status"]["terminal"])
        currently_archived = "archive" in page["path"].parent.parts
        should_be_archived = args.status in terminal
        if should_be_archived and not currently_archived:
            new_path = page["path"].parent / "archive" / page["path"].name
        elif not should_be_archived and currently_archived:
            new_path = page["path"].parent.parent / page["path"].name
        if new_path != page["path"]:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            page["path"].rename(new_path)

    _, pages = collect(vault_root, conventions)
    rebuild_index(vault_root, conventions, wiki_root, pages)
    print(f"Set [[{args.slug}]] status -> {args.status}"
          + (f" (moved to {new_path.relative_to(vault_root)})" if new_path != page["path"] else ""))
    return 0


CODE_SPAN_RE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)


def replace_outside_code(text, old, new):
    """Replace whole-word occurrences of `old` with `new`, skipping fenced
    and inline code spans (mirrors tome_lint.strip_code's code-awareness,
    but rewrites in place instead of stripping)."""
    parts = CODE_SPAN_RE.split(text)
    for i in range(0, len(parts), 2):  # even indices are non-code segments
        parts[i] = re.sub(re.escape(old), new, parts[i])
    return "".join(parts)


def cmd_mv(vault_root, conventions, args):
    wiki_root, pages = collect(vault_root, conventions)
    page = find_page(pages, args.slug)
    if page["meta"].get("type") == "project":
        raise VaultError(
            f"'{args.slug}' is a project hub — renaming it would break the "
            f"wiki/<name>/<name>.md hub convention and silently drop it from "
            f"the index. Hub renames aren't supported."
        )
    validate_slug(args.new_slug, pages)

    new_path = page["path"].parent / f"{args.new_slug}.md"
    if new_path.exists():
        raise VaultError(f"{new_path} already exists")
    page["path"].rename(new_path)

    # Two exact link forms only — a bare prefix match here would also catch
    # (and corrupt) unrelated slugs that happen to start with this one, e.g.
    # renaming "vault" must not touch "[[vault-cli-extras]]".
    old_bare, new_bare = f"[[{args.slug}]]", f"[[{args.new_slug}]]"
    old_alias, new_alias = f"[[{args.slug}|", f"[[{args.new_slug}|"
    touched = []
    for p in pages:
        if p["path"] == page["path"]:
            continue
        if "read_error" in p:
            continue
        text = p["path"].read_text(encoding="utf-8")
        if old_bare not in text and old_alias not in text:
            continue
        rewritten = replace_outside_code(text, old_bare, new_bare)
        rewritten = replace_outside_code(rewritten, old_alias, new_alias)
        if rewritten != text:
            p["path"].write_text(rewritten, encoding="utf-8", newline="\n")
            touched.append(p["rel_path"])

    _, pages = collect(vault_root, conventions)
    rebuild_index(vault_root, conventions, wiki_root, pages)
    print(f"Renamed {args.slug} -> {args.new_slug} ({new_path.relative_to(vault_root)})")
    if touched:
        print("Rewrote inbound links in:")
        for t in touched:
            print(f"  {t}")
    return 0


def cmd_log(vault_root, conventions, args):
    ops = conventions.get("log", {}).get("ops")
    if ops and args.op not in ops:
        raise VaultError(f"op '{args.op}' not in {ops}")
    if len(args.message) > 500:
        raise VaultError(f"message is {len(args.message)} chars (cap 500)")
    if "\n" in args.message:
        raise VaultError("message headline must be a single line "
                          "(use --body for multi-paragraph detail)")

    log_path = vault_root / "wiki" / "log.md"
    entry = f"\n## [{today()}] {args.op} | {args.message}\n"
    if args.body:
        entry += f"\n{args.body}\n"
    with log_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(entry)
    print(f"Appended log entry: {args.op} | {args.message}")
    return 0


def cmd_index_rebuild(vault_root, conventions, args):
    index_path = rebuild_index(vault_root, conventions)
    print(f"Rebuilt {index_path.relative_to(vault_root)}")
    return 0


# --------------------------------------------------------------------------- #
# sync / task
# --------------------------------------------------------------------------- #

def run_git(vault_root, args):
    return subprocess.run(["git", *args], cwd=str(vault_root),
                           capture_output=True, text=True)


def cmd_sync(vault_root, conventions, args):
    branch = run_git(vault_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch.returncode != 0:
        print(branch.stderr, file=sys.stderr)
        return 1
    if branch.stdout.strip() != "main":
        raise VaultError(f"refusing to sync: current branch is "
                          f"'{branch.stdout.strip()}', not main")

    pull = run_git(vault_root, ["pull", "--rebase", "--autostash"])
    print(pull.stdout, end="")
    if pull.returncode != 0:
        print(pull.stderr, file=sys.stderr)
        return 1

    status = run_git(vault_root, ["status", "--porcelain"])
    if not status.stdout.strip():
        print("already in sync")
        return 0

    if not args.message:
        raise VaultError("tree is dirty — a commit message is required: "
                          "`tome sync -m \"...\"`")

    add = run_git(vault_root, ["add", "-A"])
    if add.returncode != 0:
        print(add.stderr, file=sys.stderr)
        return 1
    commit = run_git(vault_root, ["commit", "-m", args.message])
    print(commit.stdout, end="")
    if commit.returncode != 0:
        print(commit.stderr, file=sys.stderr)
        return 1
    push = run_git(vault_root, ["push"])
    print(push.stdout, end="")
    if push.returncode != 0:
        print(push.stderr, file=sys.stderr)
        return 1
    print("synced.")
    return 0


def cmd_task(vault_root, conventions, args):
    cmd = ["npx", "--yes", "backlog.md@latest", *args.args]
    proc = subprocess.run(cmd, cwd=str(vault_root), shell=(sys.platform == "win32"))
    return proc.returncode


# --------------------------------------------------------------------------- #
# help
# --------------------------------------------------------------------------- #

HELP_TEXT = """\
tome.py — mechanical vault operations (see wiki/SCHEMA.md for the "why")

  tome new <type> <slug> --project <name> --title "T" --desc "..."
      Scaffold a page. type: project|plan|idea|decision|report|source|
      concept|synthesis. For type=project, omit --project (slug IS the
      project). Regenerates the index.
      e.g. tome new idea offline-mode --project vaulty --title "Offline mode" --desc "Cache reads for flights."

  tome describe <slug> "<one-liner>"
      Replace a page's index summary (<=140 chars). Regenerates the index.
      e.g. tome describe vault-cli "Stdlib CLI owning vault mechanics."

  tome set-status <slug> <status>
      Plans: proposed|active|blocked|done|superseded|abandoned (moves
      plans/ <-> plans/archive/ automatically). Decisions: proposed|current.
      e.g. tome set-status vault-cli active

  tome mv <slug> <new-slug>
      Rename a page; rewrites every inbound [[wikilink]] across the wiki.
      e.g. tome mv vault-cli vaultctl

  tome log <op> "<message>" [--body "..."]
      Append a formatted entry to wiki/log.md.
      e.g. tome log work-started "Began TASK-26"

  tome index rebuild
      Regenerate wiki/index.md from page frontmatter.

  tome lint [--strict]
      Structural checks (broken links, orphans, frontmatter, index drift).

  tome sync [-m "message"]
      Pull (always). If dirty: commit (message required) + push. main-only.
      e.g. tome sync -m "Add offline-mode idea"

  tome task <args...>
      Passthrough to `npx --yes backlog.md@latest <args...>` from the vault root.
      e.g. tome task list --plain

  tome init [path]
      Scaffold a fresh, empty vault at path (default: cwd). Fail-loud if
      anything it would create already exists.
      e.g. tome init ~/Development/my-vault

Root resolution: --vault PATH, else $VAULT_ROOT, else walk up from cwd
looking for conventions.toml.
"""


def cmd_help(vault_root, conventions, args):
    print(HELP_TEXT)
    return 0


# --------------------------------------------------------------------------- #
# argparse wiring
# --------------------------------------------------------------------------- #

def build_parser():
    parser = argparse.ArgumentParser(prog="tome", add_help=True,
                                      description="Vault mechanical operations.")
    parser.add_argument("--vault", help="explicit vault root (default: $VAULT_ROOT "
                                         "or walk-up from cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("help", help="print the command overview")

    p = sub.add_parser("lint", help="run structural checks",
                        epilog="e.g. tome lint --strict")
    p.add_argument("--strict", action="store_true")

    p = sub.add_parser("sync", help="pull, and commit+push if dirty",
                        epilog='e.g. tome sync -m "message"')
    p.add_argument("-m", "--message", help="commit message (required if dirty)")

    p = sub.add_parser("task", help="passthrough to backlog.md",
                        epilog="e.g. tome task list --plain", add_help=False)
    p.add_argument("args", nargs=argparse.REMAINDER)

    p = sub.add_parser("new", help="scaffold a page",
                        epilog='e.g. tome new idea x --project vaulty --title "T" --desc "..."')
    p.add_argument("type")
    p.add_argument("slug")
    p.add_argument("--project")
    p.add_argument("--title", required=True)
    p.add_argument("--desc", required=True)

    p = sub.add_parser("describe", help="replace a page's index summary",
                        epilog='e.g. tome describe vault-cli "..."')
    p.add_argument("slug")
    p.add_argument("text")

    p = sub.add_parser("set-status", help="change a plan/decision's status",
                        epilog="e.g. tome set-status vault-cli active")
    p.add_argument("slug")
    p.add_argument("status")

    p = sub.add_parser("mv", help="rename a page, rewriting inbound links",
                        epilog="e.g. tome mv old-slug new-slug")
    p.add_argument("slug")
    p.add_argument("new_slug")

    p = sub.add_parser("log", help="append a wiki/log.md entry",
                        epilog='e.g. tome log work-started "..."')
    p.add_argument("op")
    p.add_argument("message")
    p.add_argument("--body")

    idx = sub.add_parser("index", help="index operations")
    idx_sub = idx.add_subparsers(dest="index_command", required=True)
    idx_sub.add_parser("rebuild", help="regenerate wiki/index.md",
                        epilog="e.g. tome index rebuild")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        vault_root = resolve_vault_root(args.vault)
        conventions = load_conventions(vault_root)

        if args.command == "help":
            return cmd_help(vault_root, conventions, args)
        if args.command == "lint":
            return cmd_lint(vault_root, conventions, args)
        if args.command == "sync":
            return cmd_sync(vault_root, conventions, args)
        if args.command == "task":
            return cmd_task(vault_root, conventions, args)
        if args.command == "new":
            return cmd_new(vault_root, conventions, args)
        if args.command == "describe":
            return cmd_describe(vault_root, conventions, args)
        if args.command == "set-status":
            return cmd_set_status(vault_root, conventions, args)
        if args.command == "mv":
            return cmd_mv(vault_root, conventions, args)
        if args.command == "log":
            return cmd_log(vault_root, conventions, args)
        if args.command == "index" and args.index_command == "rebuild":
            return cmd_index_rebuild(vault_root, conventions, args)
        parser.error(f"unknown command {args.command}")
    except VaultError as e:
        print(f"tome: error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
