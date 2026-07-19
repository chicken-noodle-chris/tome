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
bare. Imports tome_cli.lint's run()/load_conventions() rather than
duplicating the structural checks.

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
import tomllib
from collections import defaultdict
from datetime import date
from pathlib import Path

from tome_cli import lint as tome_lint
from tome_cli import search as tome_search

ERROR = tome_lint.ERROR
WARNING = tome_lint.WARNING
Finding = tome_lint.Finding
FRONTMATTER_RE = tome_lint.FRONTMATTER_RE

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TASK_ID_RE = re.compile(r"^(?:task-)?(\d+)$", re.IGNORECASE)

# `tome task` passthrough version, pinned deliberately rather than
# floating on @latest. Bump deliberately: check `npm view backlog.md
# version`, update this constant, run `tome task task list --plain` against
# a real vault to confirm the new release still behaves, then commit.
BACKLOG_VERSION = "1.47.1"

# Scaffolding sources for `tome init` — package data shipped inside
# tome_cli, resolved through importlib.resources so it works both from a
# checkout and from an installed wheel (a plain filesystem path would break
# the moment the package is zipped or otherwise not laid out as a directory).
TEMPLATES_DIR = importlib.resources.files("tome_cli") / "templates"

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
}


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


class VaultError(Exception):
    """A fail-loud, user-facing error. main() prints str(e) and exits 1."""


# --------------------------------------------------------------------------- #
# Root resolution / conventions loading
# --------------------------------------------------------------------------- #

def resolve_vault_root(explicit):
    """--vault flag -> walk up from cwd looking for conventions.toml ->
    VAULT_ROOT env var -> fail loud. The vault you're standing in always
    beats the global default: VAULT_ROOT exists so sessions in non-vault
    directories still find their vault, not to shadow the vault at your
    feet (a second `tome init`-ed vault would otherwise silently write to
    the wrong repo). No hardcoded home paths."""
    def _validated(source, raw):
        p = Path(raw).resolve()
        if not (p / "conventions.toml").is_file():
            raise VaultError(f"{source}={p} has no conventions.toml — not a vault root")
        return p

    if explicit:
        return _validated("--vault", explicit)
    cur = Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if (d / "conventions.toml").is_file():
            return d
    if os.environ.get("VAULT_ROOT"):
        return _validated("VAULT_ROOT", os.environ["VAULT_ROOT"])
    raise VaultError(
        "no vault found: no conventions.toml walking up from "
        f"{cur}, and VAULT_ROOT is unset — pass --vault PATH or set VAULT_ROOT"
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
# fm_get/fm_set are line-surgery editors, not parsers — they still must only
# ever produce lines within the subset documented above
# tome_lint.parse_frontmatter, since write_page() below enforces exactly that.
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
    """Refuse to write a frontmatter line outside the subset documented above
    tome_lint.parse_frontmatter — cheap insurance that fm_get/fm_set and the
    parser can't silently drift apart. (Checking parse_frontmatter's own
    `malformed` flag on the reconstructed text wouldn't catch this: write_page
    always appends the closing `---` fence itself, so that flag can never
    come back True here — a stray fm_line like a bare "---" would instead get
    silently swallowed into the body across the parser's lazy fence match.)"""
    for line in fm_lines:
        if line.strip() and not tome_lint.is_subset_frontmatter_line(line):
            raise VaultError(f"{path}: frontmatter line outside the supported subset: {line!r}")
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

---
"""


def page_description(p):
    desc = p["meta"].get("description")
    return desc if isinstance(desc, str) and desc else "(no description)"


def index_line(p, alias=None):
    link = f"[[{p['slug']}|{alias}]]" if alias else f"[[{p['slug']}]]"
    return f"- {link} — {page_description(p)}"


def list_projects(wiki_root, conventions):
    """Every wiki/<name>/ top-level dir except the cross-cutting ones and
    whatever conventions.toml skips — shared by index generation, hub-plan
    generation, and their lint checks so the three can't drift apart."""
    top_dirs = sorted(
        d.name for d in wiki_root.iterdir()
        if d.is_dir() and not d.name.startswith(".")
        and d.name not in set(conventions["skip"]["dirs"])
    )
    return [d for d in top_dirs if d not in CROSS_CUTTING_DIRS]


def generate_index(pages, conventions, wiki_root):
    live_statuses = set(conventions["plan_status"]["live"])
    terminal_statuses = set(conventions["plan_status"]["terminal"])

    projects = list_projects(wiki_root, conventions)

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
# Generated hub plan lists — a project hub's live/archived plan bullets are a
# pure function of plan frontmatter, same situation the index was in before
# it became generated. Opt-in per hub via <!-- tome:plans --> markers: a hub
# without them is untouched (hand-authored bullets stay hand-authored).
# Prose outside the markers is never touched.
# --------------------------------------------------------------------------- #

HUB_MARKER_START = "<!-- tome:plans -->"
HUB_MARKER_END = "<!-- /tome:plans -->"
HUB_MARKERS_RE = re.compile(
    re.escape(HUB_MARKER_START) + r".*?" + re.escape(HUB_MARKER_END), re.DOTALL)


def generate_hub_plans_block(pages, conventions, project):
    """Live plans (proposed/active/blocked) then archived (done/superseded/
    abandoned), newest-`updated`-first, each entry `[[slug]] — description`."""
    live_statuses = set(conventions["plan_status"]["live"])
    terminal_statuses = set(conventions["plan_status"]["terminal"])
    project_plans = [p for p in pages
                      if p["meta"].get("type") == "plan"
                      and Path(p["rel_path"]).parts[0] == project]

    def newest_first(p):
        return (p["meta"].get("updated") or "", p["slug"])

    live = sorted((p for p in project_plans if p["meta"].get("status") in live_statuses),
                  key=newest_first, reverse=True)
    archived = sorted((p for p in project_plans if p["meta"].get("status") in terminal_statuses),
                       key=newest_first, reverse=True)

    lines = []
    if live:
        lines.append("**Plans — live:**")
        lines.extend(index_line(p) for p in live)
        lines.append("")
    if archived:
        lines.append("**Plans — archived:**")
        lines.extend(index_line(p) for p in archived)
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def hub_path_for(wiki_root, project):
    return wiki_root / project / f"{project}.md"


def regenerate_hub(conventions, wiki_root, pages, project):
    """No-op when the hub doesn't exist, or exists but hasn't opted in with
    markers — returns None either way so callers can tell "nothing to do"
    apart from "regenerated, unchanged"."""
    hub_path = hub_path_for(wiki_root, project)
    if not hub_path.exists():
        return None
    text = hub_path.read_text(encoding="utf-8")
    if HUB_MARKER_START not in text or HUB_MARKER_END not in text:
        return None
    block = generate_hub_plans_block(pages, conventions, project)
    replacement = f"{HUB_MARKER_START}\n{block}\n{HUB_MARKER_END}"
    new_text = HUB_MARKERS_RE.sub(lambda m: replacement, text, count=1)
    if new_text != text:
        hub_path.write_text(new_text, encoding="utf-8", newline="\n")
    return hub_path


def regenerate_all_hubs(conventions, wiki_root, pages):
    touched = []
    for project in list_projects(wiki_root, conventions):
        hub_path = regenerate_hub(conventions, wiki_root, pages, project)
        if hub_path is not None:
            touched.append(hub_path)
    return touched


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


def check_index_oversize(conventions, index_path):
    soft_cap = conventions.get("index", {}).get("soft_cap_lines", 400)
    try:
        line_count = index_path.read_text(encoding="utf-8").count("\n")
    except OSError as e:
        return [Finding(ERROR, "READ_ERROR", index_path.name, str(e))]
    if line_count > soft_cap:
        return [Finding(WARNING, "INDEX_OVERSIZE", index_path.name,
                         f"{line_count} lines (soft cap {soft_cap}) — "
                         "consider trimming or splitting projects")]
    return []


def check_hub_plans_drift(pages, conventions, wiki_root):
    out = []
    for project in list_projects(wiki_root, conventions):
        hub_path = hub_path_for(wiki_root, project)
        if not hub_path.exists():
            continue
        try:
            text = hub_path.read_text(encoding="utf-8")
        except OSError as e:
            out.append(Finding(ERROR, "READ_ERROR", hub_path.name, str(e)))
            continue
        if HUB_MARKER_START not in text or HUB_MARKER_END not in text:
            continue  # hasn't opted in — nothing to check
        expected = f"{HUB_MARKER_START}\n{generate_hub_plans_block(pages, conventions, project)}\n{HUB_MARKER_END}"
        m = HUB_MARKERS_RE.search(text)
        actual = m.group(0) if m else None
        if actual != expected:
            rel = hub_path.relative_to(wiki_root).as_posix()
            out.append(Finding(ERROR, "HUB_DRIFT", rel,
                                "hub's generated plan list does not match a fresh "
                                "rebuild — run `python scripts/tome.py index rebuild`"))
    return out


def run_all_lint_checks(vault_root, conventions):
    """The full check set `cmd_lint` reports and `cmd_sync`'s commit gate
    enforces — one body so the two commands can't drift apart."""
    wiki_root = vault_root / "wiki"
    index_path = wiki_root / conventions["index"]["file"]
    pages, findings = tome_lint.run(wiki_root, conventions, index_path)
    findings += check_description_cap(pages, conventions)
    findings += check_index_generated_drift(pages, conventions, wiki_root, index_path)
    findings += check_index_oversize(conventions, index_path)
    findings += check_hub_plans_drift(pages, conventions, wiki_root)
    return pages, findings


def cmd_lint(vault_root, conventions, args):
    pages, findings = run_all_lint_checks(vault_root, conventions)
    print(tome_lint.render_text(pages, findings))
    gating = findings if args.strict else [f for f in findings if f.severity == ERROR]
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
        raise VaultError("--with-task only applies to `tome new plan`")
    if (priority or acs or milestone) and not with_task:
        raise VaultError("--priority/--ac/--milestone only apply alongside --with-task")

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
    if args.type == "project":
        body = (f"\n# {title}\n\n{args.desc}\n\n"
                f"## Plans\n\n{HUB_MARKER_START}\n{HUB_MARKER_END}\n")
    else:
        body = f"\n# {title}\n\nTBD.\n"
    write_page(path, fm_lines, body)

    _, pages = collect(vault_root, conventions)
    index_path = rebuild_index(vault_root, conventions, wiki_root, pages)

    slug = project if args.type == "project" else args.slug
    touched = [path, index_path]
    if args.type in ("plan", "project"):
        hub_path = regenerate_hub(conventions, wiki_root, pages, project)
        if hub_path is not None and hub_path not in touched:
            touched.append(hub_path)

    print(f"Created {path.relative_to(vault_root)}")

    if with_task:
        plan_ref = f"wiki/{path.relative_to(wiki_root).as_posix()}"
        task_argv = ["task", "create", with_task, "-d", args.desc,
                     "-l", f"project:{project}", "--ref", plan_ref, "--plain"]
        if priority:
            task_argv += ["--priority", priority]
        if milestone:
            task_argv += ["--milestone", milestone]
        for ac in acs or []:
            task_argv += ["--ac", ac]
        proc = run_backlog(vault_root, task_argv, capture=True)
        if proc.returncode != 0:
            raise VaultError(f"backlog task create failed: {(proc.stderr or proc.stdout).strip()}")
        m = re.search(r"^File: (.+)$", proc.stdout, re.MULTILINE)
        if m:
            task_path = Path(m.group(1).strip())
            touched.append(task_path)
            print(f"Created backlog task: {task_path.name}")
        else:
            print("Created backlog task (couldn't parse its file path for --sync scoping).")

    result = maybe_sync(vault_root, conventions, args, touched, f"new: {slug}")
    if result is not None:
        return result
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
    index_path = rebuild_index(vault_root, conventions, wiki_root, pages)
    touched = [page["path"], index_path]
    if page["meta"].get("type") == "plan":
        project = Path(page["rel_path"]).parts[0]
        hub_path = regenerate_hub(conventions, wiki_root, pages, project)
        if hub_path is not None:
            touched.append(hub_path)
    print(f"Updated description for [[{args.slug}]]")
    result = maybe_sync(vault_root, conventions, args, touched, f"describe: {args.slug}")
    if result is not None:
        return result
    return 0


def apply_status(conventions, page, new_status):
    """Mutate a plan/decision page's frontmatter (status + updated) and, for
    a plan, move it between its status dir and `archive/` if live/terminal-
    ness changed. Pure file mutation — callers re-collect pages and handle
    the index/hub regen and sync themselves. Returns the page's post-move
    path (unchanged if it didn't move)."""
    fm_lines, body = read_page(page["path"])
    fm_set(fm_lines, "status", new_status)
    fm_set(fm_lines, "updated", today())
    write_page(page["path"], fm_lines, body)

    new_path = page["path"]
    if page["meta"].get("type") == "plan":
        terminal = set(conventions["plan_status"]["terminal"])
        currently_archived = "archive" in page["path"].parent.parts
        should_be_archived = new_status in terminal
        if should_be_archived and not currently_archived:
            new_path = page["path"].parent / "archive" / page["path"].name
        elif not should_be_archived and currently_archived:
            new_path = page["path"].parent.parent / page["path"].name
        if new_path != page["path"]:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            page["path"].rename(new_path)
    return new_path


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

    new_path = apply_status(conventions, page, args.status)

    _, pages = collect(vault_root, conventions)
    index_path = rebuild_index(vault_root, conventions, wiki_root, pages)
    touched = [new_path, index_path]
    if new_path != page["path"]:
        # A move stages as new_path's add; the old path's delete needs its
        # own pathspec entry too, or a scoped --sync leaves it unstaged.
        touched.append(page["path"])
    if ptype == "plan":
        project = Path(page["rel_path"]).parts[0]
        hub_path = regenerate_hub(conventions, wiki_root, pages, project)
        if hub_path is not None:
            touched.append(hub_path)
    print(f"Set [[{args.slug}]] status -> {args.status}"
          + (f" (moved to {new_path.relative_to(vault_root)})" if new_path != page["path"] else ""))
    result = maybe_sync(vault_root, conventions, args, touched,
                         f"set-status: {args.slug} -> {args.status}")
    if result is not None:
        return result
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

    # The renamed page's own body may self-link its old slug; the main loop
    # below skips this page (its stale path no longer exists to read), so
    # rewrite it separately against new_path first.
    self_text = new_path.read_text(encoding="utf-8")
    self_rewritten = replace_outside_code(self_text, old_bare, new_bare)
    self_rewritten = replace_outside_code(self_rewritten, old_alias, new_alias)
    if self_rewritten != self_text:
        new_path.write_text(self_rewritten, encoding="utf-8", newline="\n")
        touched.append(new_path.relative_to(wiki_root).as_posix())

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
    index_path = rebuild_index(vault_root, conventions, wiki_root, pages)
    print(f"Renamed {args.slug} -> {args.new_slug} ({new_path.relative_to(vault_root)})")
    if touched:
        print("Rewrote inbound links in:")
        for t in touched:
            print(f"  {t}")
    touched_paths = [new_path, index_path, page["path"]] + [wiki_root / t for t in touched
                                                             if (wiki_root / t) != new_path]
    if page["meta"].get("type") == "plan":
        project = Path(page["rel_path"]).parts[0]
        hub_path = regenerate_hub(conventions, wiki_root, pages, project)
        if hub_path is not None and hub_path not in touched_paths:
            touched_paths.append(hub_path)
    result = maybe_sync(vault_root, conventions, args, touched_paths,
                         f"mv: {args.slug} -> {args.new_slug}")
    if result is not None:
        return result
    return 0


def cmd_rm(vault_root, conventions, args):
    wiki_root, pages = collect(vault_root, conventions)
    page = find_page(pages, args.slug)
    if page["meta"].get("type") == "project":
        raise VaultError(
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
        hub_path = hub_path_for(wiki_root, Path(page["rel_path"]).parts[0])
        if hub_path.exists():
            hub_text = hub_path.read_text(encoding="utf-8")
            if HUB_MARKER_START in hub_text and HUB_MARKER_END in hub_text:
                outside = HUB_MARKERS_RE.sub("", hub_text)
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

    _, pages = collect(vault_root, conventions)
    index_path = rebuild_index(vault_root, conventions, wiki_root, pages)
    touched = [removed_path, index_path]
    regenerated_hub = None
    if removed_type == "plan":
        regenerated_hub = regenerate_hub(conventions, wiki_root, pages, removed_project)
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
    wiki_root, pages = collect(vault_root, conventions)
    page = find_page(pages, args.slug)
    ptype = page["meta"].get("type")
    if ptype in ("plan", "decision"):
        raise VaultError(f"'{args.slug}' is a {ptype} — archive it with "
                          f"`tome set-status {args.slug} <terminal-status>` instead")
    if ptype == "project":
        raise VaultError(f"'{args.slug}' is a project hub — archiving it isn't supported")

    currently_archived = "archive" in page["path"].parent.parts
    if args.restore:
        if not currently_archived:
            raise VaultError(f"'{args.slug}' is not archived")
        new_path = page["path"].parent.parent / page["path"].name
    else:
        if currently_archived:
            raise VaultError(f"'{args.slug}' is already archived")
        new_path = page["path"].parent / "archive" / page["path"].name

    new_path.parent.mkdir(parents=True, exist_ok=True)
    page["path"].rename(new_path)
    fm_lines, body = read_page(new_path)
    fm_set(fm_lines, "updated", today())
    write_page(new_path, fm_lines, body)

    _, pages = collect(vault_root, conventions)
    index_path = rebuild_index(vault_root, conventions, wiki_root, pages)
    touched = [new_path, index_path, page["path"]]

    verb = "Restored" if args.restore else "Archived"
    print(f"{verb} [[{args.slug}]] ({new_path.relative_to(vault_root)})")
    result = maybe_sync(vault_root, conventions, args, touched, f"archive: {args.slug}")
    if result is not None:
        return result
    return 0


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
    """The orientation pointer: what the vault is and how to read/write it.
    Kept under ~50 tokens — this is paid in every single session via the
    SessionStart hook, so its cost is constant, not one-time."""
    return (
        f"Knowledge vault at {vault_root} — accumulated knowledge, notes, and "
        "tasks across projects, not scoped to the current repo. Reading: "
        "start at wiki/index.md, browse by project folder, follow "
        "[[wikilinks]]; grep only as a fallback. Writing: the tome CLI "
        "(`tome help`) owns writes — run `tome help` and follow it (`tome "
        "task` for backlog items); edit page bodies with normal file "
        "tools; conventions in wiki/SCHEMA.md. Start and end vault work "
        "with `tome sync`."
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
    fm_lines, _ = read_page(path)
    return {
        "id": fm_get(fm_lines, "id") or "",
        "status": fm_get(fm_lines, "status") or "",
        "milestone": fm_get(fm_lines, "milestone"),
        "title": task_title(fm_lines),
        "labels": task_block_list(fm_lines, "labels"),
    }


def _task_sort_key(t):
    m = TASK_ID_RE.match(t["id"])
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
            fm_lines, _ = read_page(p)
            mid = fm_get(fm_lines, "id")
            if mid:
                milestone_titles[mid] = fm_get(fm_lines, "title") or mid

    milestone_total = defaultdict(int)
    milestone_done = defaultdict(int)
    completed_dir = vault_root / "backlog" / "completed"
    if completed_dir.is_dir():
        for p in completed_dir.glob("*.md"):
            fm_lines, _ = read_page(p)
            mid = fm_get(fm_lines, "milestone")
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
    """The write protocol: SCHEMA.md, the index, and an open-task snapshot
    always; with a project, also that project's hub, every one of its live
    plan bodies, and a recent log.md tail — replacing the read fan-out every
    skill used to open with."""
    wiki_root = vault_root / "wiki"
    sections = [
        ((wiki_root / "SCHEMA.md").relative_to(vault_root).as_posix(),
         (wiki_root / "SCHEMA.md").read_text(encoding="utf-8")),
        ((wiki_root / conventions["index"]["file"]).relative_to(vault_root).as_posix(),
         (wiki_root / conventions["index"]["file"]).read_text(encoding="utf-8")),
    ]

    task_snapshot = open_task_snapshot(vault_root, project)
    if task_snapshot is not None:
        label = f"backlog/tasks (open, project:{project})" if project else "backlog/tasks (open)"
        sections.append((label, task_snapshot))

    if project:
        if project not in list_projects(wiki_root, conventions):
            raise VaultError(f"no such project: wiki/{project}/ does not exist")
        _, pages = collect(vault_root, conventions)
        hub_path = hub_path_for(wiki_root, project)
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

    return "\n\n".join(f"# {label}\n\n{text}" for label, text in sections)


def cmd_prime(vault_root, conventions, args):
    if args.project and not args.full:
        raise VaultError("a project only applies with --full (the terse tier is vault-level)")
    print(prime_terse_text(vault_root))
    if args.full:
        print()
        print(prime_full_text(vault_root, conventions, args.project))
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
    result = maybe_sync(vault_root, conventions, args, [log_path], f"log: {args.op}",
                         message_attr="sync_message")
    if result is not None:
        return result
    return 0


def cmd_index_rebuild(vault_root, conventions, args):
    wiki_root, pages = collect(vault_root, conventions)
    index_path = rebuild_index(vault_root, conventions, wiki_root, pages)
    print(f"Rebuilt {index_path.relative_to(vault_root)}")
    hubs = regenerate_all_hubs(conventions, wiki_root, pages)
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
    date_str = today()
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
        raise VaultError(
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

    conventions = load_conventions(target)
    (target / "wiki" / "log.md").write_text(
        LOG_HEADER + f"\n## [{today()}] init | Vault created via `tome init`\n",
        encoding="utf-8", newline="\n")
    rebuild_index(target, conventions, target / "wiki", [])

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

def _git_env():
    """Env for git subprocesses. When TOME_GIT_AUTHOR is set, derive
    GIT_COMMITTER_NAME/EMAIL from it (unless already set explicitly): git
    refuses to commit — and to rebase, which rewrites the committer — without
    a committer identity, and the headless containers TOME_GIT_AUTHOR exists
    for have no git config to supply one. `--author` alone can't fix that; it
    only sets the author half."""
    author = os.environ.get("TOME_GIT_AUTHOR")
    if not author:
        return None
    m = re.match(r"^\s*(.+?)\s*<(.+)>\s*$", author)
    if not m:
        return None
    env = os.environ.copy()
    env.setdefault("GIT_COMMITTER_NAME", m.group(1))
    env.setdefault("GIT_COMMITTER_EMAIL", m.group(2))
    return env


def run_git(vault_root, args):
    return subprocess.run(["git", *args], cwd=str(vault_root),
                           capture_output=True, text=True, env=_git_env())


def _push_with_retry(vault_root):
    """Push; on rejection — another writer landed a commit on the remote
    since our pull, guaranteed eventually once a headless remote and a local
    session share a vault — pull --rebase once and retry the push exactly
    once. CLI-owned writes are small and file-disjoint, so a rebase that
    still fails to push means something unusual: fail loud and leave the
    rebase state intact rather than guessing further."""
    push = run_git(vault_root, ["push"])
    if push.returncode == 0:
        print(push.stdout, end="")
        return 0

    retry_pull = run_git(vault_root, ["pull", "--rebase", "--autostash"])
    print(retry_pull.stdout, end="")
    if retry_pull.returncode != 0:
        print(push.stderr, file=sys.stderr)
        print(retry_pull.stderr, file=sys.stderr)
        print("tome: push rejected and the retry rebase failed — tree is "
              "mid-rebase; resolve manually.", file=sys.stderr)
        return 1

    push_retry = run_git(vault_root, ["push"])
    if push_retry.returncode != 0:
        print(push.stderr, file=sys.stderr)
        print(push_retry.stderr, file=sys.stderr)
        print("tome: push rejected again after a rebase retry — resolve "
              "manually.", file=sys.stderr)
        return 1
    print(push_retry.stdout, end="")
    return 0


def sync_core(vault_root, conventions, message, no_verify, pathspec=None):
    """The shared pull/lint-gate/commit/push core behind `tome sync` and
    every write command's `--sync`. With pathspec=None, stages the whole
    tree (bare `tome sync`'s deliberate whole-tree sweep — the only place a
    ride-along commit should be possible). With a pathspec (relative-to-
    vault-root path strings), stages and commits only those paths, so one
    command's auto-sync can't sweep in another agent's half-finished hand
    edits; whatever else is dirty afterwards is reported, never swallowed."""
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

    if pathspec is not None:
        scoped = run_git(vault_root, ["status", "--porcelain", "--", *pathspec])
        if not scoped.stdout.strip():
            print("already in sync (nothing dirty in scope)")
            return 0

    if not message:
        raise VaultError("tree is dirty — a commit message is required: "
                          "`tome sync -m \"...\"`")

    if not no_verify:
        pages, findings = run_all_lint_checks(vault_root, conventions)
        errors = [f for f in findings if f.severity == ERROR]
        if errors:
            print(tome_lint.render_text(pages, findings))
            print("tome: refusing to sync — a dirty commit would publish a "
                  "vault that fails its own lint. Fix the errors above, or "
                  "pass --no-verify to commit anyway.", file=sys.stderr)
            return 1

    add_args = ["add", "-A"] if pathspec is None else ["add", "-A", "--", *pathspec]
    add = run_git(vault_root, add_args)
    if add.returncode != 0:
        print(add.stderr, file=sys.stderr)
        return 1
    commit_args = ["commit", "-m", message]
    author = os.environ.get("TOME_GIT_AUTHOR")
    if author:
        commit_args += ["--author", author]
    commit = run_git(vault_root, commit_args)
    print(commit.stdout, end="")
    if commit.returncode != 0:
        print(commit.stderr, file=sys.stderr)
        return 1
    push_code = _push_with_retry(vault_root)
    if push_code != 0:
        return push_code
    print("synced.")

    if pathspec is not None:
        leftover = run_git(vault_root, ["status", "--porcelain"])
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


def find_task_file(vault_root, task_num):
    """Locate backlog/{tasks,completed}/*.md whose `id:` frontmatter is
    TASK-<task_num>. Filenames encode the title too (`task-47 - Some-Title.md`),
    so this reads frontmatter rather than guessing the full filename. Checks
    completed/ too — `task complete` moves the file there, and a resolved
    entity (`tome sync task-47`, `tome start`/`done`) shouldn't go blind the
    moment a task ships."""
    target_id = f"TASK-{task_num}"
    for subdir in ("tasks", "completed"):
        tasks_dir = vault_root / "backlog" / subdir
        if not tasks_dir.is_dir():
            continue
        for p in tasks_dir.glob("*.md"):
            try:
                fm_lines, _ = read_page(p)
            except VaultError:
                continue
            if fm_get(fm_lines, "id") == target_id:
                return p
    return None


def find_task_for_page(vault_root, page_rel_path):
    """Locate the backlog task (if any) whose `references:` list contains
    this page's current path — the reverse direction of find_task_file's
    task->plan lookup. A plan without a task is normal; returns None."""
    tasks_dir = vault_root / "backlog" / "tasks"
    if not tasks_dir.is_dir():
        return None
    wiki_rel = f"wiki/{page_rel_path}".replace("\\", "/")
    for p in tasks_dir.glob("*.md"):
        try:
            fm_lines, _ = read_page(p)
        except VaultError:
            continue
        if wiki_rel in "\n".join(fm_lines):
            return p
    return None


def open_tasks_referencing_plan(vault_root, plan_rel_path, exclude_path=None):
    """Task ids ('task-<n>') of open backlog tasks (backlog/tasks/*.md) whose
    `references:` include this plan's wiki path, excluding one task file.
    Read-only — backlog.md owns these files, so refs are read via
    read_page/task_references, never rewritten here. cmd_done uses this to
    refuse archiving a plan that still has live phase-task referents (the
    milestone/umbrella case: one plan, many phase tasks)."""
    tasks_dir = vault_root / "backlog" / "tasks"
    if not tasks_dir.is_dir():
        return []
    plan_ref = f"wiki/{plan_rel_path}".replace("\\", "/")
    exclude = exclude_path.resolve() if exclude_path is not None else None
    ids = []
    for p in sorted(tasks_dir.glob("*.md")):
        if exclude is not None and p.resolve() == exclude:
            continue
        try:
            fm_lines, _ = read_page(p)
        except VaultError:
            continue
        if plan_ref in [r.replace("\\", "/") for r in task_references(fm_lines)]:
            ids.append(f"task-{task_id_from_path(p)}")
    ids.sort(key=lambda t: int(TASK_ID_RE.match(t).group(1)))
    return ids


def resolve_entity(vault_root, pages, entity):
    """Resolve one `tome start`/`tome sync <entity>` argument — a page slug
    or a backlog task id — to (page-or-None, task_path-or-None). At least
    one side always resolves; an unknown slug/task id fails loud."""
    m = TASK_ID_RE.match(entity)
    page = None
    task_path = None
    if m:
        task_path = find_task_file(vault_root, m.group(1))
        if task_path is None:
            raise VaultError(f"no backlog task with id 'task-{m.group(1)}'")
        fm_lines, _ = read_page(task_path)
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
        page = find_page(pages, entity)
        task_path = find_task_for_page(vault_root, page["rel_path"])
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
        wiki_root, pages = collect(vault_root, conventions)
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


def run_backlog(vault_root, argv, capture=False):
    """Shell out to the pinned backlog.md CLI from the vault root. Used both
    by the raw `tome task` passthrough and by `start`/`done`'s bundled task
    edits — task files are backlog.md-owned, so tome never hand-writes them,
    only drives them through this same entry point."""
    cmd = ["npx", "--yes", f"backlog.md@{BACKLOG_VERSION}", *argv]
    if capture:
        return subprocess.run(cmd, cwd=str(vault_root), shell=(sys.platform == "win32"),
                               capture_output=True, text=True)
    return subprocess.run(cmd, cwd=str(vault_root), shell=(sys.platform == "win32"))


def cmd_task(vault_root, conventions, args):
    return run_backlog(vault_root, args.args).returncode


def task_id_from_path(task_path):
    fm_lines, _ = read_page(task_path)
    task_id = fm_get(fm_lines, "id") or ""
    if task_id.upper().startswith("TASK-"):
        task_id = task_id[len("TASK-"):]
    return task_id


def task_block_list(fm_lines, key):
    """Parse a backlog task's `<key>:` block-list from its raw frontmatter
    lines (e.g. `references:`/`labels:` followed by `  - value` lines).
    Task files are real YAML (backlog.md-owned), not the vault's hand-rolled
    subset — read only, never written directly here. An inline `key: []`
    (or the key being absent) both fall through to the empty list."""
    out = []
    in_block = False
    for line in fm_lines:
        if re.match(rf"^{re.escape(key)}:\s*$", line):
            in_block = True
            continue
        if in_block:
            m = re.match(r"^\s*-\s*(.+?)\s*$", line)
            if m:
                out.append(m.group(1).strip("'\""))
                continue
            break
    return out


def task_references(fm_lines):
    return task_block_list(fm_lines, "references")


def task_title(fm_lines):
    """A backlog task's title, unfolding a `>-`/`>`/`|-`/`|` YAML block
    scalar if present — long titles wrap past the single frontmatter line
    fm_get's plain-line regex reads, so that alone isn't enough here."""
    for i, line in enumerate(fm_lines):
        m = re.match(r"^title:\s*(.*)$", line)
        if not m:
            continue
        value = m.group(1).strip()
        if value in (">-", ">", "|-", "|"):
            parts = []
            j = i + 1
            while j < len(fm_lines) and (fm_lines[j].startswith("  ") or not fm_lines[j].strip()):
                parts.append(fm_lines[j].strip())
                j += 1
            return " ".join(p for p in parts if p)
        return value.strip('"').strip("'")
    return ""


AC_LINE_RE = re.compile(r"^- \[.\] #(\d+)", re.MULTILINE)


def count_task_acs(task_body):
    """Count acceptance criteria in a backlog task's body (between its
    AC:BEGIN/AC:END markers), regardless of checked state — `tome done`
    checks all of them by default."""
    m = re.search(r"<!-- AC:BEGIN -->(.*?)<!-- AC:END -->", task_body, re.DOTALL)
    if not m:
        return 0
    return len(AC_LINE_RE.findall(m.group(1)))


# --------------------------------------------------------------------------- #
# start / done — the pickup-task skill's bundled start/close rituals. Each
# is a fixed program (set status, drive the linked task, log, sync); the
# agent's judgment stays entirely on either side of these two commands.
# --------------------------------------------------------------------------- #

def cmd_start(vault_root, conventions, args):
    wiki_root, pages = collect(vault_root, conventions)
    page, task_path = resolve_entity(vault_root, pages, args.entity)
    if page is None and task_path is None:
        raise VaultError(f"'{args.entity}' did not resolve to a page or a backlog task")
    if page is not None and page["meta"].get("type") != "plan":
        raise VaultError(f"'{page['slug']}' is a {page['meta'].get('type')}, not a plan — "
                          f"tome start only sets plan status")

    touched = []
    plan_path = None
    if page is not None:
        live = set(conventions["plan_status"]["live"])
        if "active" not in live:
            raise VaultError("'active' is not in this vault's plan_status.live vocabulary")
        plan_path = apply_status(conventions, page, "active")
        _, pages = collect(vault_root, conventions)
        index_path = rebuild_index(vault_root, conventions, wiki_root, pages)
        touched += [plan_path, index_path]
        if plan_path != page["path"]:
            touched.append(page["path"])
        project = Path(page["rel_path"]).parts[0]
        hub_path = regenerate_hub(conventions, wiki_root, pages, project)
        if hub_path is not None:
            touched.append(hub_path)
        print(f"Set [[{page['slug']}]] status -> active")

    task_id = None
    if task_path is not None:
        task_id = task_id_from_path(task_path)
        proc = run_backlog(vault_root, ["task", "edit", task_id, "-s", "In Progress", "-a", "@me"],
                            capture=True)
        if proc.returncode != 0:
            raise VaultError(f"backlog task edit failed: {(proc.stderr or proc.stdout).strip()}")
        touched.append(task_path)
        print(f"Moved TASK-{task_id} -> In Progress (@me)")

    subject = page["slug"] if page is not None else f"task-{task_id}"
    log_path = vault_root / "wiki" / "log.md"
    with log_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"\n## [{today()}] work-started | {subject}\n")
    touched.append(log_path)
    print(f"Logged work-started: {subject}")

    if not args.no_sync:
        rel = [str(Path(p).resolve().relative_to(vault_root)) for p in touched]
        result = sync_core(vault_root, conventions, f"start: {args.entity} ({subject})",
                            False, pathspec=rel)
        if result:
            return result

    if task_id is not None:
        run_backlog(vault_root, ["task", task_id, "--plain"])
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
    wiki_root, pages = collect(vault_root, conventions)
    page, task_path = resolve_entity(vault_root, pages, args.slug)
    if page is None and task_path is None:
        raise VaultError(f"'{args.slug}' did not resolve to a plan or a backlog task")
    if page is not None and page["meta"].get("type") != "plan":
        raise VaultError(f"'{page['slug']}' is a {page['meta'].get('type')}, not a plan — "
                          f"tome done only closes out plans")
    if page is None and args.as_status:
        raise VaultError("--as only applies when closing out a plan")

    touched = []
    new_path = None
    old_rel = None

    # Umbrella/milestone guard: a plan referenced by several phase tasks must
    # not be archived while any of them is still open — that would physically
    # move the plan and dangle every sibling's ref. Check open sibling tasks
    # referencing the same plan, excluding the one being closed here.
    named_task = TASK_ID_RE.match(args.slug) is not None
    plan_open_referrers = (
        open_tasks_referencing_plan(vault_root, page["rel_path"], task_path)
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
            raise VaultError(
                f"plan '{page['slug']}' still has open task(s) referencing it: "
                f"{', '.join(plan_open_referrers)} — close them first, or pass "
                f"--force to archive anyway (dangles their refs)")
    else:
        archive_plan = page is not None

    if archive_plan:
        subject = page["slug"]
    elif task_path is not None:
        subject = f"task-{task_id_from_path(task_path)}"
    else:
        subject = page["slug"]

    if archive_plan:
        target_status = args.as_status or "done"
        terminal = set(conventions["plan_status"]["terminal"])
        if target_status not in terminal:
            raise VaultError(f"'{target_status}' is not a terminal plan status ({sorted(terminal)})")

        old_rel = f"wiki/{page['rel_path']}".replace("\\", "/")
        new_path = apply_status(conventions, page, target_status)
        _, pages = collect(vault_root, conventions)
        index_path = rebuild_index(vault_root, conventions, wiki_root, pages)
        touched += [new_path, index_path]
        if new_path != page["path"]:
            touched.append(page["path"])
        project = Path(page["rel_path"]).parts[0]
        hub_path = regenerate_hub(conventions, wiki_root, pages, project)
        if hub_path is not None:
            touched.append(hub_path)
        print(f"Set [[{page['slug']}]] status -> {target_status} "
              f"(moved to {new_path.relative_to(vault_root)})")

    if task_path is not None:
        task_id = task_id_from_path(task_path)
        task_fm_lines, task_body = read_page(task_path)

        edit_argv = ["task", "edit", task_id, "-s", "Done"]
        if not args.no_check_ac:
            for i in range(1, count_task_acs(task_body) + 1):
                edit_argv += ["--check-ac", str(i)]
        if args.summary:
            edit_argv += ["--final-summary", args.summary]
        ref_note = ""
        if archive_plan:
            refs = task_references(task_fm_lines)
            new_rel = f"wiki/{new_path.relative_to(wiki_root).as_posix()}"
            new_refs = [new_rel if r == old_rel else r for r in refs] or [new_rel]
            for r in new_refs:
                edit_argv += ["--ref", r]
            ref_note = f", ref -> {new_rel}"
        proc = run_backlog(vault_root, edit_argv, capture=True)
        if proc.returncode != 0:
            raise VaultError(f"backlog task edit failed: {(proc.stderr or proc.stdout).strip()}")
        touched.append(task_path)
        print(f"Closed TASK-{task_id}: Done{ref_note}")

        complete = run_backlog(vault_root, ["task", "complete", task_id], capture=True)
        if complete.returncode != 0:
            raise VaultError(f"backlog task complete failed: "
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
        fh.write(f"\n## [{today()}] done | {subject}{suffix}\n")
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
    if profile == "read-capture":
        return Check("node/npm/npx", DOC_INFO,
                      "skipped — read-capture profile has no node-dependent "
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
        root = resolve_vault_root(explicit)
    except VaultError as e:
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
        conventions = load_conventions(vault_root)
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
    _, findings = run_all_lint_checks(vault_root, conventions)
    errors = [f for f in findings if f.severity == ERROR]
    warnings = [f for f in findings if f.severity == WARNING]
    detail = f"{len(errors)} error(s), {len(warnings)} warning(s)"
    if errors:
        return Check("lint", DOC_FAIL, detail, "run `tome lint` for details")
    if warnings:
        return Check("lint", DOC_WARN, detail, "run `tome lint` for details")
    return Check("lint", DOC_OK, detail)


def check_git_state(vault_root):
    if not shutil.which("git"):
        return Check("git state", DOC_WARN, "git not on PATH — cannot inspect", "install git")

    branch = run_git(vault_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch.returncode != 0:
        return Check("git state", DOC_FAIL, branch.stderr.strip() or "not a git repository",
                      "run `git init` in the vault")
    branch_name = branch.stdout.strip()

    remote = run_git(vault_root, ["remote"])
    has_remote = bool(remote.stdout.strip())

    status = run_git(vault_root, ["status", "--porcelain"])
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

  tome search "<query>" [--top N] [--type T] [--tag T ...] [--since YYYY-MM-DD]
      BM25 search over wiki pages (fallback when index-first navigation
      doesn't surface the right pages). Also: --backlinks <slug>,
      --top-linked N.
      e.g. tome search "quartz spike" --top 5

  tome prime [project] [--full]
      Print session orientation. Bare: the terse vault pointer (same text
      the SessionStart hook injects). --full also prints SCHEMA.md, the
      index, and an open-task snapshot (grouped by milestone with
      done/total counts, scoped to the project when one is given); with a
      project, also its hub, every live plan's full body, and a recent
      log.md tail — the write protocol, replacing the read fan-out a skill
      used to open with.
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
      Structural checks (broken links, orphans, frontmatter, index drift).

  tome sync [<slug-or-task-id>...] [-m "message"] [--no-verify]
      Pull (always). If dirty: lint-gates (errors abort, --no-verify skips),
      then commit (message required, unless entities given) + push.
      main-only. With entities: scopes the commit to each one's resolved
      cluster (page, linked task, hub, index, log) instead of the whole
      tree, printing anything else left dirty.
      e.g. tome sync -m "Add offline-mode idea"
      e.g. tome sync workflow-compression task-47

  tome task <args...>
      Passthrough to `npx --yes backlog.md@latest <args...>` from the vault root.
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

  tome serve [--host H] [--port N] [--open] [--export DIR]
      Serve the no-build browse frontend locally (stdlib http.server): the
      frontend's static files, the vault's raw .md under /raw/, and two
      generated JSON contracts (/index.json, /board.json) rebuilt per
      request. Read-only — no write endpoints. --export DIR writes the same
      frontend plus a frozen index.json/board.json/raw/*.md snapshot to DIR
      instead of serving — a static deploy for any static host.
      e.g. tome serve --open, or tome serve --export ./public

Root resolution: --vault PATH, else walk up from cwd
looking for conventions.toml, else $VAULT_ROOT.

Headless remote consumers (env vars, for a container with no human at the
keyboard — see README.md's "Headless bootstrap" section for the full recipe):

  VAULT_ROOT           Vault root when not standing in one (still overridden
                        by --vault / a walk-up match).
  TOME_OPS_PROFILE      Restricts the command surface. read-capture allows
                        only search, prime, doctor, help, inbox — everything
                        else (including a command added later) is refused
                        with a clear message. help/doctor always run.
  TOME_GIT_AUTHOR       "Name <email>" applied as author (via `git commit
                        --author`) and, unless GIT_COMMITTER_* is set
                        explicitly, as committer identity on every
                        tome-driven git call, so a vault's git log shows
                        which surface (local session vs. a given remote
                        deployment) made each change and commits work
                        without any git config on the container.
"""


def cmd_help(args):
    print(HELP_TEXT)
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
                   help="also print SCHEMA.md, the index, and (with a project) its hub, "
                        "live plan bodies, and a recent log tail")

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

        vault_root = resolve_vault_root(args.vault)
        conventions = load_conventions(vault_root)

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
            from tome_cli import serve
            return serve.cmd_serve(vault_root, conventions, args)
        parser.error(f"unknown command {args.command}")
    except VaultError as e:
        print(f"tome: error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
