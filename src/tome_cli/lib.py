#!/usr/bin/env python3
"""
tome_cli.lib — the vault primitives, owned by neither surface that uses them.

`cli.py` is a command-line program and `serve.py` is an HTTP server; between
them they had one shared core — frontmatter read/write, page collection, the
address resolver, the generated index and hub blocks, the lint invocation, the
git helpers and conflict model, the page-write core, and the backlog.md
shell-out. That core lived in `cli.py` purely because the CLI came first, so
`serve.py` reached back into it and had to do so *lazily*, inside every
function, to dodge the import cycle `cli.main()`'s dispatch to `serve.cmd_serve`
would otherwise close.

Here, neither layer owns the other's primitives: both import this module
directly, at the top, and this module imports neither of them. What remains
above it is genuinely per-surface — argparse wiring, `cmd_*` bodies and their
printing in `cli.py`; routes, JSON contracts and HTTP status mapping in
`serve.py`.

stdlib only, Python >= 3.11. Imports tome_cli.lint's run()/load_conventions()
rather than duplicating the structural checks.
"""

import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import namedtuple
from datetime import date
from pathlib import Path, PurePosixPath

from tome_cli import lint as tome_lint

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
#
# One non-obvious failure mode of pinning: backlog.md ships an *unsigned*
# native binary, and Windows Smart App Control allows those on cloud
# reputation, which is per-version and can be withdrawn. A pin that ran
# yesterday can start failing with "An Application Control policy has blocked
# this file" (spawn UNKNOWN / errno -4094 through the npm shim) with nothing
# on this machine having changed. It presents as a broken `tome task`,
# `start`, `done`, and `--with-task`. Confirm by running the pinned
# `backlog.exe` directly, then bump — 1.47.1 was blocked this way while
# 1.48.0 ran. Disabling Smart App Control is not the fix.
BACKLOG_VERSION = "1.48.0"

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


# --------------------------------------------------------------------------- #
# Page addressing. The vault emits three incompatible page addresses and, until
# this, nothing translated between them: wikilink slugs
# (`[[render-layer-principle]]`), `tome search`'s display paths
# (`tome/decisions/okf-deferred.md`), and the vault-relative paths `read`,
# index.json and a task's `references:` print (`wiki/tome/...`). So a search
# result pasted into a read failed, and the navigation primitive the vault
# *tells* agents to use wasn't accepted by any read path at all.
#
# `resolve_page` is the one place those spaces collapse: every read/write verb
# and `serve.py`'s page routes go through it, so what one surface prints is
# always valid input to another.
# --------------------------------------------------------------------------- #

WIKILINK_IDENT_RE = re.compile(r"^\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]$")


def resolve_page(vault_root, conventions, ident, pages=None):
    """One page identifier -> its collected page dict. Accepts a bare slug, a
    wiki-relative path, a vault-relative path, or a whole `[[wikilink]]`,
    matched case-insensitively and separator-agnostically (a path copied off a
    Windows shell resolves too), with the `.md` suffix optional on paths.

    Only pages `collect()` actually walked under `wiki/` can resolve, which is
    also this function's safety property: a traversal, an absolute path, or a
    non-page file simply doesn't resolve, so callers taking an identifier from
    an untrusted client need no separate path gate."""
    if pages is None:
        _, pages = collect(vault_root, conventions)
    live = [p for p in pages if "read_error" not in p]

    raw = (ident or "").strip()
    m = WIKILINK_IDENT_RE.match(raw)
    if m:
        raw = m.group(1).strip()
    norm = raw.replace("\\", "/").strip("/")
    if not norm:
        raise VaultError("no page identifier given")
    key = norm.lower()

    path_keys = {key}
    if key.startswith("wiki/"):
        path_keys.add(key[len("wiki/"):])
    path_keys |= {k + ".md" for k in set(path_keys) if not k.endswith(".md")}

    matches = [p for p in live
               if p["rel_path"].replace("\\", "/").lower() in path_keys]
    if not matches:
        matches = [p for p in live if p["slug"].lower() == key]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise VaultError(f"'{ident}' is ambiguous: "
                          + ", ".join(sorted(p["rel_path"] for p in matches)))
    raise VaultError(unresolved_page_message(vault_root, live, norm))


def unresolved_page_message(vault_root, pages, ident):
    """A corrective refusal, not a negative one. A remote agent can't run
    `tome lint`, can't usefully browse SCHEMA.md, and learns the address
    vocabulary exclusively from what tome says when it says no — and a bare
    "no such page" makes it pattern-match the shape of the complaint instead
    of the fix. So name the vault, the accepted forms, and the nearest slugs."""
    stem = PurePosixPath(ident).stem.lower()
    near = difflib.get_close_matches(stem, sorted({p["slug"] for p in pages}),
                                      n=3, cutoff=0.5)
    msg = (f"no page '{ident}' in the vault at {vault_root} — address a page by "
           f"slug ('render-layer-principle'), by wiki-relative path "
           f"('tome/decisions/okf-deferred.md'), or by vault-relative path "
           f"('wiki/tome/decisions/okf-deferred.md')")
    if near:
        msg += f". Closest slugs: {', '.join(near)}"
    return msg


def validate_slug(slug, pages, allow_existing=False):
    if not SLUG_RE.match(slug):
        raise VaultError(f"'{slug}' is not lowercase kebab-case")
    if not allow_existing and slug in all_slugs(pages):
        raise VaultError(f"slug '{slug}' already exists")


# --------------------------------------------------------------------------- #
# Generated index
# --------------------------------------------------------------------------- #

INDEX_PREAMBLE = """# Wiki Index

Everything this vault knows, one line each, organized by project — the map of
the agent's memory of its owner. Read it before answering from your own
knowledge when a question touches this person, their projects, or their past
decisions; the summaries are there to pick which pages to open.

**Generated file — do not hand-edit.** Regenerate with
`python scripts/tome.py index rebuild` (every lifecycle command does this
automatically). Change a page's one-line summary with
`tome describe <slug> "..."`, never by editing this file directly.

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


def check_inbox_backlog(vault_root, conventions):
    """Warn when the capture queue has stopped draining: more than
    [inbox].max_items waiting, or the oldest older than max_age_days. Either
    alone is the signal here — unlike the page-staleness check, which needs
    both thresholds because a fresh page starts with no links. A deep queue
    and an old item mean the same thing, that nothing has run triage.

    This matters because capture is only half a loop: `tome inbox` writes to
    inbox/, and nothing reaches the wiki until retrospect triages it, so a
    stalled queue is memory the agent believes it saved and didn't. Lint is
    where it's surfaced because lint gates every `tome sync` — the queue gets
    seen without anyone thinking to look. Opt-in, like [staleness]: a vault
    with no [inbox] section isn't newly gated on thresholds it never chose.
    Age comes from the YYYY-MM-DD filename prefix `tome inbox` writes, so no
    file needs opening."""
    cfg = conventions.get("inbox")
    if not cfg:
        return []
    inbox_dir = vault_root / "inbox"
    if not inbox_dir.is_dir():
        return []

    notes = sorted(p.name for p in inbox_dir.glob("*.md"))
    if not notes:
        return []

    reasons = []
    max_items = cfg.get("max_items")
    if max_items is not None and len(notes) > max_items:
        reasons.append(f"{len(notes)} notes waiting (cap {max_items})")

    max_age_days = cfg.get("max_age_days")
    if max_age_days is not None:
        dates = []
        for name in notes:
            try:
                dates.append(date.fromisoformat(name[:10]))
            except ValueError:
                continue  # hand-named note: no date to judge, count only
        if dates:
            age = (date.today() - min(dates)).days
            if age > max_age_days:
                reasons.append(f"oldest is {age} days old (cap {max_age_days})")

    if not reasons:
        return []
    return [Finding(WARNING, "INBOX_STALLED", "inbox/",
                    f"{'; '.join(reasons)} — run retrospect to triage")]


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
    findings += check_inbox_backlog(vault_root, conventions)
    return pages, findings


# --------------------------------------------------------------------------- #
# Page mutation cores — the reusable middles of `tome new`, `tome mv` and the
# status moves, each doing only the on-disk work plus the index/hub regen it
# implies. None of them does git, task creation, or printing: every caller —
# a `cmd_*` in cli.py, a route in serve.py — owns its own commit/sync and its
# own output.
# --------------------------------------------------------------------------- #

NewPageResult = namedtuple("NewPageResult", "path touched_paths slug")


def new_page(vault_root, conventions, type_, project, slug, title, desc):
    """Scaffold a new page on disk: validate type/project/slug, write the
    frontmatter + body scaffold, and regenerate the index (and, for a plan
    or project, the project hub). Returns a NewPageResult; raises VaultError
    on a bad type, missing/unknown project, bad/taken slug, or a path that
    already exists."""
    wiki_root, pages = collect(vault_root, conventions)
    type_enum = set(conventions["types"]["enum"])
    if type_ not in type_enum:
        raise VaultError(f"type '{type_}' not in {sorted(type_enum)}")
    max_chars = conventions.get("description", {}).get("max_chars", 140)
    validate_oneline(desc, "description", max_chars)
    validate_oneline(title, "title")

    if type_ == "project":
        project = slug
        validate_slug(project, pages)
        path = wiki_root / project / f"{project}.md"
    else:
        if not project:
            raise VaultError("project is required for non-project types")
        if not (wiki_root / project).is_dir():
            raise VaultError(f"no such project: wiki/{project}/ does not exist "
                              f"(create it first with `tome new project {project} ...`)")
        validate_slug(slug, pages)
        folders = conventions["folders"]
        if type_ not in folders:
            raise VaultError(f"no [folders] mapping for type '{type_}'")
        path = wiki_root / project / folders[type_] / f"{slug}.md"

    if path.exists():
        raise VaultError(f"{path} already exists")

    tag_kind = TYPE_TAG.get(type_, "project")
    fm_lines = [
        f"type: {type_}",
        f'title: "{title}"',
        f"tags: [{project}, {tag_kind}]",
        f'description: "{desc}"',
        f"created: {today()}",
        f"updated: {today()}",
    ]
    if type_ in ("plan", "decision"):
        fm_lines.append("status: proposed")

    path.parent.mkdir(parents=True, exist_ok=True)
    if type_ == "project":
        body = (f"\n# {title}\n\n{desc}\n\n"
                f"## Plans\n\n{HUB_MARKER_START}\n{HUB_MARKER_END}\n")
    else:
        body = f"\n# {title}\n\nTBD.\n"
    write_page(path, fm_lines, body)

    _, pages = collect(vault_root, conventions)
    index_path = rebuild_index(vault_root, conventions, wiki_root, pages)

    result_slug = project if type_ == "project" else slug
    touched_paths = [path, index_path]
    if type_ in ("plan", "project"):
        hub_path = regenerate_hub(conventions, wiki_root, pages, project)
        if hub_path is not None and hub_path not in touched_paths:
            touched_paths.append(hub_path)

    return NewPageResult(path, touched_paths, result_slug)


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


CODE_SPAN_RE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)


def replace_outside_code(text, old, new):
    """Replace whole-word occurrences of `old` with `new`, skipping fenced
    and inline code spans (mirrors tome_lint.strip_code's code-awareness,
    but rewrites in place instead of stripping)."""
    parts = CODE_SPAN_RE.split(text)
    for i in range(0, len(parts), 2):  # even indices are non-code segments
        parts[i] = re.sub(re.escape(old), new, parts[i])
    return "".join(parts)


# `touched_rels` are the wiki-relative paths whose inbound links were rewritten
# (for the CLI's report); `touched_paths` is the full absolute-path set git must
# stage — old path (now a deletion), new path, rebuilt index, every rewritten
# linker, and the project hub for a plan.
MoveResult = namedtuple("MoveResult", "old_path new_path touched_rels touched_paths")


def move_page(vault_root, conventions, slug, new_slug):
    """Rename a page's slug on disk: move the file, rewrite every inbound
    `[[wikilink]]` across the wiki, and regenerate the index (and, for a plan,
    the project hub). Returns a MoveResult; raises VaultError on a bad/unknown/
    ambiguous slug, a project hub, or a destination collision."""
    wiki_root, pages = collect(vault_root, conventions)
    page = find_page(pages, slug)
    if page["meta"].get("type") == "project":
        raise VaultError(
            f"'{slug}' is a project hub — renaming it would break the "
            f"wiki/<name>/<name>.md hub convention and silently drop it from "
            f"the index. Hub renames aren't supported."
        )
    validate_slug(new_slug, pages)

    old_path = page["path"]
    new_path = old_path.parent / f"{new_slug}.md"
    if new_path.exists():
        raise VaultError(f"{new_path} already exists")
    old_path.rename(new_path)

    # Two exact link forms only — a bare prefix match here would also catch
    # (and corrupt) unrelated slugs that happen to start with this one, e.g.
    # renaming "vault" must not touch "[[vault-cli-extras]]".
    old_bare, new_bare = f"[[{slug}]]", f"[[{new_slug}]]"
    old_alias, new_alias = f"[[{slug}|", f"[[{new_slug}|"
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
        if p["path"] == old_path:
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
    touched_paths = [new_path, index_path, old_path] + [wiki_root / t for t in touched
                                                        if (wiki_root / t) != new_path]
    if page["meta"].get("type") == "plan":
        project = Path(page["rel_path"]).parts[0]
        hub_path = regenerate_hub(conventions, wiki_root, pages, project)
        if hub_path is not None and hub_path not in touched_paths:
            touched_paths.append(hub_path)
    return MoveResult(old_path, new_path, touched, touched_paths)


# --------------------------------------------------------------------------- #
# git plumbing
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


def _git_bytes(vault_root, args):
    """`run_git` decodes with the locale codec (cp1252 on Windows), which
    would mangle any non-ASCII page. Blob reads must be byte-exact, so this
    runs git itself and decodes UTF-8 explicitly."""
    return subprocess.run(["git", *args], cwd=str(vault_root),
                          capture_output=True, env=_git_env())


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
    `git pull --rebase` leaves behind."""
    for name in ("rebase-merge", "rebase-apply"):
        probe = run_git(vault_root, ["rev-parse", "--git-path", name])
        if probe.returncode != 0:
            continue
        path = Path(probe.stdout.strip())
        if not path.is_absolute():
            path = vault_root / path
        if path.exists():
            return True
    return False


def _mid_rebase_hint(vault_root):
    """Printed wherever a git step can leave the tree stopped mid-rebase.
    'Resolve manually' used to be the whole answer; the browser now has a
    three-way resolver for exactly this ([[conflict-resolution]]), so point
    at it instead of leaving the user alone with git."""
    if not rebase_in_progress(vault_root):
        return
    print("tome: the tree is stopped mid-rebase. Run `tome serve` to resolve "
          "the conflicted files in the browser, or finish it by hand with "
          "git.", file=sys.stderr)


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
        print("tome: push rejected and the retry rebase failed.", file=sys.stderr)
        _mid_rebase_hint(vault_root)
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
# only case that leaves the tree mid-rebase, which serve.py's endpoints exist
# to get it back out of.
#
# The model lives here rather than with the browser resolver that renders it:
# `_page_body_write` below needs the same pull/push gates, so both surfaces
# read one description of what a conflict is.
# --------------------------------------------------------------------------- #

# `kind` is the transport-neutral outcome of a page write; `serve.PAGE_WRITE_
# STATUS` maps it to HTTP and `cli.report_page_write` renders it for a terminal.
PageWriteResult = namedtuple("PageWriteResult", "kind payload")


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


def local_drift_conflict(target, current_hash):
    """The conflict payload for a stale `baseHash` (a 409 over HTTP): the
    sides the resolver needs plus
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


def pull_or_conflict(vault_root):
    """Every write path's first step. Returns None when the pull landed, else
    the `PageWriteResult` the caller should surface: a `conflict` carrying
    the `git-fork` state when the rebase stopped on one — the resolver's cue,
    replacing the old dead-end 'resolve manually' — or a plain `error`."""
    pull = run_git(vault_root, ["pull", "--rebase", "--autostash"])
    if pull.returncode == 0:
        return None
    state = git_conflict_state(vault_root)
    if state["rebase"]:
        return PageWriteResult("conflict", {
            "error": "the vault's history diverged from the remote",
            "conflict": {"type": "git-fork", **state}})
    return PageWriteResult("error", {
        "error": (pull.stderr or pull.stdout).strip() or "git pull failed"})


def push_or_conflict(vault_root):
    """The tail of every write path. `_push_with_retry` re-pulls on a
    rejected push, so its failure can also be a stopped rebase — same
    `conflict`, so a fork that shows up at push time lands in the resolver
    instead of the same dead end."""
    if _push_with_retry(vault_root) == 0:
        return None
    state = git_conflict_state(vault_root)
    if state["rebase"]:
        return PageWriteResult("conflict", {
            "error": "your commit landed locally, but the vault's history "
                     "diverged from the remote",
            "conflict": {"type": "git-fork", **state}})
    return PageWriteResult("error", {
        "error": "commit landed locally but push failed — resolve manually"})


# --------------------------------------------------------------------------- #
# Page bodies — read / write / append ([[remote-authoring]]).
#
# Locally an agent edits a page body with its native file tools, and `prime`'s
# own text says so; remotely there are no file tools, so the absence of any
# body-write verb is total — a surface reaching the vault through MCP could
# scaffold a page with `tome new` and then not put a sentence in it. The
# capability already existed and was well-tested, it was just reachable only
# over HTTP, so this is mostly an exposure problem: the core lives here now and
# `cli.py`'s verbs and `serve.py`'s routes are two consumers of it rather than
# its owners.
# --------------------------------------------------------------------------- #

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def page_read(vault_root, conventions, ident):
    """(page, full_text, sha256-of-bytes) for one identifier. The hash is over
    the file's raw bytes — the same token `serve`'s ETag emits and every write
    path checks — so a read closes the read-modify-write loop without a second
    call to obtain one."""
    page = resolve_page(vault_root, conventions, ident)
    raw = page["path"].read_bytes()
    return page, raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()


def append_to_body(body, text, under=None):
    """`text` at the end of `body`, or at the end of the section headed by
    `under` — before the next heading at the same or a higher level, so the
    addition lands *inside* the section rather than after the document. `under`
    matches a heading's text with or without its `#` markers, case-insensitively.
    Headings inside fenced code are skipped (a `# comment` in a shell sample is
    not a section)."""
    chunk = text.strip("\n")
    if not chunk.strip():
        raise VaultError("nothing to append")
    if under is None:
        return body.rstrip("\n") + "\n\n" + chunk + "\n"

    want = under.strip().lstrip("#").strip().lower()
    lines = body.split("\n")

    headings = []  # (index, level, text), fenced lines excluded
    in_fence = False
    for i, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEADING_RE.match(line)
        if m:
            headings.append((i, len(m.group(1)), m.group(2).strip()))

    start = next(((i, level) for i, level, text_ in headings
                  if text_.lower() == want), None)
    if start is None:
        known = ", ".join(f"'{t}'" for _, _, t in headings)
        raise VaultError(f"no section headed '{under}' in this page"
                          + (f" — it has {known}" if known else " (it has no headings)"))
    start_idx, level = start

    end = len(lines)
    for i, lvl, _ in headings:
        if i > start_idx and lvl <= level:
            end = i
            break
    while end > start_idx + 1 and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[:end] + ["", chunk] + lines[end:]).rstrip("\n") + "\n"


def write_page_body(vault_root, conventions, ident, body, base_hash, sync=True):
    """Replace a page's body, leaving its frontmatter untouched (`describe`/
    `mv`/`set-status` own their own fields). `base_hash` is the conflict token
    `page_read` hands back; None skips the check."""
    return _page_body_write(vault_root, conventions, ident, base_hash,
                             lambda _old: body, sync=sync, verb="edit")


def append_page_body(vault_root, conventions, ident, text, under=None,
                      base_hash=None, sync=True):
    """Accretion — the shape memory actually wants. No conflict token is
    required, precisely because an append doesn't overwrite: the ordering of
    two concurrent appends is not a conflict worth refusing, and forcing a
    whole-body round-trip for one new bullet is itself a lost-update risk."""
    return _page_body_write(vault_root, conventions, ident, base_hash,
                             lambda old: append_to_body(old, text, under),
                             sync=sync, verb="append")


def _page_body_write(vault_root, conventions, ident, base_hash, transform,
                      sync, verb):
    """The shared write path, in the order the guarantees depend on:

    1. Resolve the identifier (a miss writes nothing and explains the address
       vocabulary).
    2. Pull, so the conflict check below is against the latest remote. A pull
       that stops on a forked history is itself a conflict.
    3. Hash the current bytes; a `base_hash` mismatch means the page moved
       under the caller — refuse, write nothing, hand back the current text.
    4. Recombine the on-disk frontmatter with the new body.
    5. Lint the vault but gate only on findings keyed to *this* page — a
       pre-existing error elsewhere must not block an otherwise clean write.
       Any error restores the original bytes.
    6. Commit + push, scoped to the one file.

    Sync is on by default here where every other write command opts in, for
    three reasons: the browser editor has behaved this way since
    [[page-editing]] shipped, so this is newly *named*, not newly true; a
    hash token is only meaningful against a synced state, so an unpushed write
    is one whose next token is already suspect; and remotely the vault is a
    disposable clone, where an unsynced write is simply a lost one. `--no-sync`
    stays for a local agent batching several edits behind one commit."""
    try:
        page = resolve_page(vault_root, conventions, ident)
    except VaultError as e:
        return PageWriteResult("not-found", {"error": str(e)})
    target = page["path"]

    if sync:
        conflict = pull_or_conflict(vault_root)
        if conflict is not None:
            return conflict

    original_bytes = target.read_bytes()
    current_hash = hashlib.sha256(original_bytes).hexdigest()
    if base_hash is not None and base_hash != current_hash:
        return PageWriteResult("conflict", local_drift_conflict(target, current_hash))

    try:
        fm_lines, old_body = read_page(target)
        write_page(target, fm_lines, transform(old_body))
    except VaultError as e:
        target.write_bytes(original_bytes)
        return PageWriteResult("invalid", {"error": str(e)})

    wiki_root = (vault_root / "wiki").resolve()
    rel_str = target.relative_to(wiki_root).as_posix()  # lint findings key by this
    _, findings = run_all_lint_checks(vault_root, conventions)
    errors = [f for f in findings if f.severity == ERROR and f.path == rel_str]
    if errors:
        target.write_bytes(original_bytes)
        return PageWriteResult("lint-failed", {"error": "lint failed",
                                               "findings": [f.as_dict() for f in errors]})

    def _ok(committed):
        return PageWriteResult("ok", {
            "hash": hashlib.sha256(target.read_bytes()).hexdigest(),
            "path": f"wiki/{rel_str}",
            "slug": page["slug"],
            "committed": committed,
        })

    if not sync:
        return _ok(False)

    vault_rel_str = target.relative_to(vault_root).as_posix()  # git wants this one
    add = run_git(vault_root, ["add", "--", vault_rel_str])
    if add.returncode != 0:
        target.write_bytes(original_bytes)
        return PageWriteResult("error", {"error": (add.stderr or "git add failed").strip()})

    commit = run_git(vault_root, ["commit", "-m", f"{verb}: {target.stem}"])
    if commit.returncode != 0:
        return PageWriteResult("error", {"error": (commit.stderr or commit.stdout).strip()
                                                  or "git commit failed"})

    push_conflict = push_or_conflict(vault_root)
    if push_conflict is not None:
        return push_conflict

    return _ok(True)


# --------------------------------------------------------------------------- #
# Backlog.md — the pinned CLI shell-out plus the read-only parsers for the task
# files it owns. tome never hand-writes a task file; it drives backlog.md
# through `run_backlog` and reads back with the helpers below.
# --------------------------------------------------------------------------- #

_backlog_script = None  # memoized _find_backlog_script() hit, resolved once per process


def _find_backlog_script():
    """Filesystem path to the pinned backlog.md `cli.js`, or None if it can't
    be located. Used only by the multi-line path in `run_backlog` below.

    npm keeps one npx-cache directory per package spec, keyed by a hash tome
    has no way to derive, so this globs the cache and keeps only a copy whose
    own package.json reads exactly `BACKLOG_VERSION` — a neighbouring cache
    entry for a different pin must never be the one that runs."""
    probe = subprocess.run(["npm", "config", "get", "cache"], capture_output=True,
                            text=True, shell=(sys.platform == "win32"))
    if probe.returncode != 0:
        return None
    cache = Path(probe.stdout.strip())
    if not cache.is_dir():
        return None
    for manifest in sorted((cache / "_npx").glob("*/node_modules/backlog.md/package.json")):
        try:
            version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
        except (OSError, json.JSONDecodeError):
            continue
        script = manifest.parent / "cli.js"
        if version == BACKLOG_VERSION and script.is_file():
            return script
    return None


def backlog_script(refresh=True):
    """`_find_backlog_script()`, memoized, priming the npx cache with one
    cheap single-line invocation if the pinned version isn't there yet."""
    global _backlog_script
    if _backlog_script is None:
        _backlog_script = _find_backlog_script()
    if _backlog_script is None and refresh:
        subprocess.run(["npx", "--yes", f"backlog.md@{BACKLOG_VERSION}", "--version"],
                        capture_output=True, shell=(sys.platform == "win32"))
        _backlog_script = _find_backlog_script()
    return _backlog_script


def run_backlog(vault_root, argv, capture=False):
    """Shell out to the pinned backlog.md CLI from the vault root. Used both
    by the raw `tome task` passthrough and by `start`/`done`'s bundled task
    edits — task files are backlog.md-owned, so tome never hand-writes them,
    only drives them through this same entry point.

    Normally that means `npx`, which on Windows reaches the package through a
    `.cmd` shim. A batch shim cannot carry a newline: cmd.exe truncates the
    argument there and drops every flag after it, so a multi-line
    `--notes`/`-d` value would land as its first line alone — silently, with
    a zero exit code. Any argv containing a newline therefore runs the
    resolved `cli.js` directly under `node`, with no shell in the path at
    all. If that script can't be found the write is refused outright (a
    non-zero CompletedProcess every existing caller already knows how to
    surface) rather than allowed through to be quietly truncated.
    """
    if any("\n" in str(a) for a in argv):
        script = backlog_script()
        if script is None:
            return subprocess.CompletedProcess(
                argv, 1, "",
                f"backlog.md@{BACKLOG_VERSION} could not be located, and this edit "
                f"carries a multi-line value that the npx shim would truncate")
        cmd = ["node", str(script), *argv]
        if capture:
            return subprocess.run(cmd, cwd=str(vault_root), capture_output=True, text=True)
        return subprocess.run(cmd, cwd=str(vault_root))

    cmd = ["npx", "--yes", f"backlog.md@{BACKLOG_VERSION}", *argv]
    if capture:
        return subprocess.run(cmd, cwd=str(vault_root), shell=(sys.platform == "win32"),
                               capture_output=True, text=True)
    return subprocess.run(cmd, cwd=str(vault_root), shell=(sys.platform == "win32"))


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
AC_ITEM_RE = re.compile(r"^- \[(.)\] #\d+\s+(.*)$", re.MULTILINE)
DESCRIPTION_RE = re.compile(
    r"<!-- SECTION:DESCRIPTION:BEGIN -->\s*(.*?)\s*<!-- SECTION:DESCRIPTION:END -->", re.DOTALL)
NOTES_RE = re.compile(
    r"<!-- SECTION:NOTES:BEGIN -->\s*(.*?)\s*<!-- SECTION:NOTES:END -->", re.DOTALL)


def count_task_acs(task_body):
    """Count acceptance criteria in a backlog task's body (between its
    AC:BEGIN/AC:END markers), regardless of checked state — `tome done`
    checks all of them by default."""
    m = re.search(r"<!-- AC:BEGIN -->(.*?)<!-- AC:END -->", task_body, re.DOTALL)
    if not m:
        return 0
    return len(AC_LINE_RE.findall(m.group(1)))


def task_description(task_body):
    """A backlog task's description paragraph, from between its
    SECTION:DESCRIPTION:BEGIN/END markers ([[task-detail-view]]'s board.json
    contract) — empty string if the task has no description section."""
    m = DESCRIPTION_RE.search(task_body)
    return m.group(1).strip() if m else ""


def task_notes(task_body):
    """A backlog task's Implementation Notes, from between its
    SECTION:NOTES:BEGIN/END markers — empty string if absent. Same
    board.json contract as `task_description`."""
    m = NOTES_RE.search(task_body)
    return m.group(1).strip() if m else ""


def task_acceptance_criteria(task_body):
    """Acceptance criteria as `{text, checked}` dicts, parsed from the same
    AC:BEGIN/AC:END block `count_task_acs` reads, but keeping each item's
    text and checked state for the board.json detail-view contract."""
    m = re.search(r"<!-- AC:BEGIN -->(.*?)<!-- AC:END -->", task_body, re.DOTALL)
    if not m:
        return []
    return [{"text": text.strip(), "checked": mark.lower() == "x"}
            for mark, text in AC_ITEM_RE.findall(m.group(1))]


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
