#!/usr/bin/env python3
"""
tome_cli.lint — structural integrity check for this knowledge vault.

The deterministic counterpart to the agent's wiki discipline: SCHEMA.md is
enforced today only by an agent choosing to follow it, and that drifts. This
script catches the decay mechanically — broken wikilinks, orphan pages,
frontmatter gaps, oversized pages, index<->page drift, plans whose status
disagrees with their directory, and ideas misplaced relative to their type.

Pure and deterministic: no LLM, no semantic judgement, never edits. Every
data-shaped rule (required fields, the type enum, the tag taxonomy, the plan
status vocabulary, size caps, the skip-list) is read from conventions.toml — the
single source of truth — not hardcoded here. The algorithmic checks below stay as
named functions whose docstring is their human statement.

Seeded from the llm-wiki plugin's wiki_lint.py (frontmatter parse, page
collection, link extraction, orphan/size/slug checks) but vendored self-contained
so a fork runs it with zero install (stdlib only, Python >= 3.11 for tomllib).

Usage:
    python scripts/tome_lint.py [<wiki-dir>] [options]

Options:
    --conventions PATH   conventions file (default: <repo>/conventions.toml)
    --json               emit JSON instead of text
    --strict             promote warnings to errors for the exit code
    -h, --help           show this help

Exit code: non-zero if any error-severity finding is present (any finding under
--strict), zero otherwise. Warnings are printed but do not gate the exit.
"""

import argparse
import json
import sys
import tomllib
from collections import defaultdict
from pathlib import Path
import re

ERROR = "error"
WARNING = "warning"

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


class Finding:
    """One issue at one location. `path:CODE message` is the greppable line shape."""

    def __init__(self, severity, code, path, message):
        self.severity = severity
        self.code = code
        self.path = path
        self.message = message

    def line(self):
        return f"{self.path}:{self.code} {self.message}"

    def as_dict(self):
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


# --------------------------------------------------------------------------- #
# Parsing helpers (seeded from wiki_lint.py)
# --------------------------------------------------------------------------- #

def strip_code(text):
    """Remove fenced blocks and inline-code spans so literal `[[wikilinks]]`
    written in prose or code samples are not mistaken for real links."""
    text = FENCE_RE.sub("", text)
    text = INLINE_CODE_RE.sub("", text)
    return text


# The frontmatter subset this parser supports — the single contract every
# script in this repo (tome_lint, wiki_search, tome.py's fm_get/fm_set) is
# built against. A block is `---` fenced; inside it, every non-blank line
# must be one of:
#   - `key: value`      (value optionally single- or double-quoted)
#   - `key: [a, b, c]`  (inline list)
#   - `key:`            (bare, opens a block list)
#   - `  - value`       (block-list item: exactly two spaces, dash, space)
# Nothing else — no nested maps, no multi-line scalars, no comments. Keys
# match `[a-zA-Z_]+`. This is a deliberate hand-rolled subset (stdlib-only,
# no PyYAML), not full YAML; check_frontmatter_syntax() below turns any line
# outside this subset into a loud UNPARSED_FRONTMATTER error instead of the
# silent drop this parser would otherwise do.
FM_KV_RE = re.compile(r"^[a-zA-Z_]+:\s*.*$")


def is_subset_frontmatter_line(raw):
    """True if `raw` matches one of the subset forms documented above:
    `key: value` (covers plain scalars, quoted scalars, inline lists, and
    bare `key:`), or a `  - value` block-list item."""
    return bool(FM_KV_RE.match(raw)) or raw.startswith("  - ")


def parse_frontmatter(text):
    """Return (metadata, body, malformed). malformed=True if a frontmatter block
    was opened with --- but could not be parsed. Lines outside the documented
    subset above are silently skipped here (kept lenient on purpose);
    check_frontmatter_syntax() is what flags them loudly."""
    if not text.startswith("---"):
        return {}, text, False
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text, True
    fm_text = m.group(1)
    body = text[m.end():]
    meta = {}
    current_key = None
    for raw in fm_text.split("\n"):
        if not raw.strip():
            continue
        kv = re.match(r"^([a-zA-Z_]+):\s*(.*)$", raw)
        if kv:
            key, value = kv.group(1), kv.group(2).strip()
            if value.startswith("[") and value.endswith("]"):
                meta[key] = [x.strip().strip('"').strip("'") for x in value[1:-1].split(",") if x.strip()]
            elif value:
                meta[key] = value.strip('"').strip("'")
            else:
                meta[key] = []
                current_key = key
        elif raw.startswith("  - ") and current_key:
            meta[current_key].append(raw[4:].strip().strip('"').strip("'"))
    return meta, body, False


def extract_links(body):
    """Wikilink targets (the slug before any |) in a page body, code stripped."""
    return [m.group(1).strip() for m in WIKILINK_RE.finditer(strip_code(body))]


# --------------------------------------------------------------------------- #
# Page collection
# --------------------------------------------------------------------------- #

def collect_pages(wiki_root, skip_files, skip_dirs):
    """Every content page under wiki/ as a dict. Skips the meta/catalog files and
    generated directories named in conventions, plus dotfiles (e.g. the page
    template). Links *from* the skipped meta files therefore never count toward
    inbound totals — a hub legitimately reads as an orphan."""
    pages = []
    for md_path in sorted(wiki_root.rglob("*.md")):
        rel = md_path.relative_to(wiki_root)
        if rel.parts[0] in skip_files or rel.parts[0] in skip_dirs:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        page = {"path": md_path, "rel_path": rel.as_posix(), "slug": md_path.stem}
        try:
            text = md_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as e:
            page["read_error"] = str(e)
            pages.append(page)
            continue
        meta, body, malformed = parse_frontmatter(text)
        fm_match = FRONTMATTER_RE.match(text) if text.startswith("---") else None
        page.update(
            meta=meta,
            line_count=text.count("\n") + 1,
            links=extract_links(body),
            malformed_fm=malformed,
            fm_lines=fm_match.group(1).split("\n") if fm_match else [],
        )
        pages.append(page)
    return pages


def project_names(wiki_root, skip_dirs):
    """Top-level directory names under wiki/ — the legitimate project-name tags."""
    return {
        d.name
        for d in wiki_root.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d.name not in skip_dirs
    }


# --------------------------------------------------------------------------- #
# Checks — each returns a list[Finding]. The docstring is the human rule.
# --------------------------------------------------------------------------- #

def check_read_errors(pages):
    """A page that cannot be read is an error (can't be verified at all)."""
    return [Finding(ERROR, "READ_ERROR", p["rel_path"], p["read_error"])
            for p in pages if "read_error" in p]


def check_duplicate_slugs(pages):
    """Wikilinks resolve by filename slug, so two pages sharing a slug make links
    ambiguous — an error."""
    slug_to_paths = defaultdict(list)
    for p in pages:
        slug_to_paths[p["slug"]].append(p["rel_path"])
    out = []
    for slug, paths in sorted(slug_to_paths.items()):
        if len(paths) > 1:
            out.append(Finding(ERROR, "DUPLICATE_SLUG", paths[0],
                               f"slug '{slug}' also defined at {', '.join(paths[1:])}"))
    return out


def check_links_and_orphans(pages, resolvable):
    """A wikilink whose target matches no page slug is broken (error). A content
    page with no inbound links from other content pages is an orphan (warning —
    a hub legitimately reads as an orphan until a content page links it)."""
    out = []
    inbound = defaultdict(set)
    for p in pages:
        for link in p.get("links", []):
            inbound[link].add(p["slug"])
    for p in pages:
        for link in dict.fromkeys(p.get("links", [])):  # de-dup, preserve order
            if link not in resolvable:
                out.append(Finding(ERROR, "BROKEN_LINK", p["rel_path"],
                                   f"[[{link}]] does not resolve"))
        if not inbound.get(p["slug"]):
            out.append(Finding(WARNING, "ORPHAN", p["rel_path"],
                               f"no inbound links to [[{p['slug']}]]"))
    return out


def check_frontmatter(pages, required):
    """Every page must declare the required frontmatter fields; a frontmatter
    block that won't parse is malformed. Both are errors."""
    out = []
    for p in pages:
        if "read_error" in p:
            continue
        if p["malformed_fm"]:
            out.append(Finding(ERROR, "MALFORMED_FRONTMATTER", p["rel_path"],
                               "frontmatter block could not be parsed"))
            continue
        missing = [f for f in required if p["meta"].get(f) in ("", None, [])]
        if missing:
            out.append(Finding(ERROR, "MISSING_FRONTMATTER", p["rel_path"],
                               f"missing: {', '.join(missing)}"))
    return out


def check_frontmatter_syntax(pages):
    """parse_frontmatter() silently drops any line outside the documented
    subset (nested maps, multi-line scalars, tab-indented list items,
    comments) rather than erroring. Re-scan the raw lines and flag those
    loudly instead — a page malformed enough to fail check_frontmatter
    already reports that; skip it here to avoid double-reporting."""
    out = []
    for p in pages:
        if "read_error" in p or p["malformed_fm"]:
            continue
        for raw in p.get("fm_lines", []):
            if not raw.strip():
                continue
            if not is_subset_frontmatter_line(raw):
                out.append(Finding(ERROR, "UNPARSED_FRONTMATTER", p["rel_path"],
                                   f"line outside the supported subset: {raw!r}"))
    return out


def check_size(pages, soft_cap, hard_cap):
    """Pages over the hard cap must split (error); over the soft cap, consider
    splitting (warning)."""
    out = []
    for p in pages:
        if "read_error" in p:
            continue
        n = p["line_count"]
        if n > hard_cap:
            out.append(Finding(ERROR, "OVERSIZE_HARD", p["rel_path"],
                               f"{n} lines (hard cap {hard_cap})"))
        elif n > soft_cap:
            out.append(Finding(WARNING, "OVERSIZE_SOFT", p["rel_path"],
                               f"{n} lines (soft cap {soft_cap})"))
    return out


def check_type_and_tags(pages, type_enum, tag_vocab):
    """A page's `type` must be in the closed enum; every tag must be in the
    controlled vocabulary (taxonomy plus, optionally, project names). Both error."""
    out = []
    for p in pages:
        if "read_error" in p or p["malformed_fm"]:
            continue
        t = p["meta"].get("type")
        if isinstance(t, str) and t and t not in type_enum:
            out.append(Finding(ERROR, "BAD_TYPE", p["rel_path"],
                               f"type '{t}' not in enum"))
        tags = p["meta"].get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        for tag in tags:
            if tag and tag not in tag_vocab:
                out.append(Finding(ERROR, "BAD_TAG", p["rel_path"],
                                   f"tag '{tag}' not in taxonomy"))
    return out


def check_plan_dirs(pages, live, terminal):
    """A type:plan page whose status is terminal must live under plans/archive/;
    a live status must not. Disagreement is an error (the directory then lies
    about whether the work is actionable)."""
    out = []
    for p in pages:
        if "read_error" in p or p["malformed_fm"]:
            continue
        if p["meta"].get("type") != "plan":
            continue
        status = p["meta"].get("status")
        if not isinstance(status, str):
            continue
        archived = "/plans/archive/" in "/" + p["rel_path"]
        if status in terminal and not archived:
            out.append(Finding(ERROR, "PLAN_DIR", p["rel_path"],
                               f"terminal status '{status}' must live under plans/archive/"))
        elif status in live and archived:
            out.append(Finding(ERROR, "PLAN_DIR", p["rel_path"],
                               f"live status '{status}' must not live under plans/archive/"))
    return out


def check_idea_placement(pages, folders):
    """Ideas have no status field to check placement against (unlike plans),
    but the inverse is still mechanically checkable: a page declared
    type:idea should live under an idea folder (archived or not), and a
    page living there should be type:idea. Covers both project-scoped
    (<project>/ideas/) and the cross-cutting top-level wiki/ideas/. Advisory
    only (warning) — there's no ground truth beyond the declared type
    itself, so this can't gate a sync the way PLAN_DIR does."""
    idea_folder = folders.get("idea", "ideas")
    out = []
    for p in pages:
        if "read_error" in p or p["malformed_fm"]:
            continue
        parts = p["rel_path"].split("/")
        is_idea = p["meta"].get("type") == "idea"
        in_idea_folder = parts[0] == idea_folder or (len(parts) >= 2 and parts[1] == idea_folder)
        if is_idea and not in_idea_folder:
            out.append(Finding(WARNING, "IDEA_DIR", p["rel_path"],
                                f"type 'idea' but not under {idea_folder}/"))
        elif in_idea_folder and not is_idea:
            out.append(Finding(WARNING, "IDEA_DIR", p["rel_path"],
                                f"lives under {idea_folder}/ but type is not 'idea'"))
    return out


def check_index_drift(pages, index_path, resolvable):
    """The index is the master catalog: every content page must appear as a
    wikilink in it (catalog completeness), and every wikilink in the index must
    resolve to a real page (no entries pointing at moved/renamed/deleted pages).
    Both directions are errors."""
    out = []
    rel_index = index_path.name
    try:
        index_text = index_path.read_text(encoding="utf-8")
    except OSError as e:
        return [Finding(ERROR, "READ_ERROR", rel_index, str(e))]
    index_links = set(extract_links(index_text))
    for p in pages:
        if "read_error" in p:
            continue
        if p["slug"] not in index_links:
            out.append(Finding(ERROR, "INDEX_MISSING", p["rel_path"],
                               f"page [[{p['slug']}]] is not catalogued in {rel_index}"))
    for link in sorted(index_links):
        if link not in resolvable:
            out.append(Finding(ERROR, "INDEX_BROKEN", rel_index,
                               f"index entry [[{link}]] does not resolve"))
    return out


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def load_conventions(path):
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def run(wiki_root, conventions, index_path):
    skip_files = set(conventions["skip"]["files"])
    skip_dirs = set(conventions["skip"]["dirs"])

    pages = collect_pages(wiki_root, skip_files, skip_dirs)

    # What a wikilink may resolve to: any content-page slug, plus the meta files
    # (linking *to* SCHEMA/index/log/README is valid even though they aren't scanned).
    content_slugs = {p["slug"] for p in pages}
    meta_slugs = {Path(f).stem for f in skip_files}
    resolvable = content_slugs | meta_slugs

    tag_vocab = set(conventions["tags"]["taxonomy"])
    if conventions["tags"].get("allow_project_name_tags"):
        tag_vocab |= project_names(wiki_root, skip_dirs)

    findings = []
    findings += check_read_errors(pages)
    findings += check_duplicate_slugs(pages)
    findings += check_links_and_orphans(pages, resolvable)
    findings += check_frontmatter(pages, conventions["frontmatter"]["required"])
    findings += check_frontmatter_syntax(pages)
    findings += check_size(pages, conventions["size"]["soft_cap"], conventions["size"]["hard_cap"])
    findings += check_type_and_tags(pages, set(conventions["types"]["enum"]), tag_vocab)
    findings += check_plan_dirs(pages, set(conventions["plan_status"]["live"]),
                                set(conventions["plan_status"]["terminal"]))
    findings += check_idea_placement(pages, conventions["folders"])
    findings += check_index_drift(pages, index_path, resolvable)
    return pages, findings


def render_text(pages, findings):
    errors = [f for f in findings if f.severity == ERROR]
    warnings = [f for f in findings if f.severity == WARNING]
    out = [f"tome_lint: {len(pages)} pages scanned, "
           f"{len(errors)} error(s), {len(warnings)} warning(s)"]
    if errors:
        out.append("")
        out.append("Errors:")
        out += ["  " + f.line() for f in sorted(errors, key=lambda f: (f.path, f.code))]
    if warnings:
        out.append("")
        out.append("Warnings:")
        out += ["  " + f.line() for f in sorted(warnings, key=lambda f: (f.path, f.code))]
    if not findings:
        out.append("No issues found. Vault is healthy.")
    return "\n".join(out)


def main():
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("wiki", nargs="?", type=Path, default=repo_root / "wiki",
                        help="wiki directory (default: <repo>/wiki)")
    parser.add_argument("--conventions", type=Path, default=repo_root / "conventions.toml",
                        help="conventions file (default: <repo>/conventions.toml)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--strict", action="store_true",
                        help="promote warnings to errors for the exit code")
    args = parser.parse_args()

    if not args.wiki.is_dir():
        print(f"tome_lint: wiki directory not found: {args.wiki}", file=sys.stderr)
        sys.exit(2)
    if not args.conventions.is_file():
        print(f"tome_lint: conventions file not found: {args.conventions}", file=sys.stderr)
        sys.exit(2)

    conventions = load_conventions(args.conventions)
    index_path = args.wiki / conventions["index"]["file"]
    pages, findings = run(args.wiki, conventions, index_path)

    if args.json:
        print(json.dumps({
            "pages_scanned": len(pages),
            "findings": [f.as_dict() for f in findings],
        }, indent=2))
    else:
        print(render_text(pages, findings))

    gating = findings if args.strict else [f for f in findings if f.severity == ERROR]
    sys.exit(1 if gating else 0)


if __name__ == "__main__":
    main()
