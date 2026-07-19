#!/usr/bin/env python3
"""
tome_cli.serve — the local browse host for the no-build frontend.

`tome serve` is a stdlib http.server that does three things and nothing more
(no write endpoints — writes land in a later phase, and stay CLI-shelled per
[[kanban-render-side]]):

  * serves the frontend's static files (ES modules, CSS, vendored libs) out of
    the package's `frontend/` directory — the permanent home for that code;
  * serves the vault's raw `.md` files under `/raw/…` (read-only, path-safe);
  * emits two generated JSON contracts, `/index.json` (the wiki catalogue +
    wikilink graph) and `/board.json` (the Backlog.md kanban), fresh on every
    request so the render always reflects the markdown on disk — the
    render-from-markdown rule ([[render-layer-principle]]), never the reverse.

The JSON *schemas* are the deliberate, permanent part of this slice; the
server internals and the frontend are rough by design and hardened in place by
later phases. build_index()/build_board() return plain dicts so a future
static-export path can write them to disk unchanged.

stdlib only, imports cli lazily to avoid an import cycle (cli dispatches here).
"""

import importlib.resources
import json
import re
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
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
    return {"pages": out}


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
    """The `/board.json` contract: the kanban read from backlog/tasks/*.md,
    mirroring the field set the Quartz board plugin's Task model exposes so
    the eventual full board reuses this shape. Reuses cli's existing task
    frontmatter readers rather than adding another hand-rolled parser."""
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


# --------------------------------------------------------------------------- #
# HTTP handler — GET only, three route families, path-safe raw reads.
# --------------------------------------------------------------------------- #

class TomeHandler(BaseHTTPRequestHandler):
    # Set by the partial() in serve().
    vault_root = None
    conventions = None

    server_version = "tome-serve"

    def log_message(self, fmt, *args):
        # One terse line per request instead of BaseHTTPRequestHandler's noisy
        # default; keeps `tome serve`'s console readable.
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path in ("/", "/index.html"):
                return self._send_frontend("index.html")
            if path == "/index.json":
                return self._send_json(build_index(self.vault_root, self.conventions))
            if path == "/board.json":
                return self._send_json(build_board(self.vault_root, self.conventions))
            if path.startswith("/raw/"):
                return self._send_raw(path[len("/raw/"):])
            if path.startswith("/app/"):
                return self._send_frontend(path[len("/app/"):])
            self._send_error(404, "not found")
        except BrokenPipeError:
            pass  # client navigated away mid-response — nothing to report

    # -- responders -------------------------------------------------------- #

    def _send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self._send_bytes(body, CONTENT_TYPES[".json"])

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
        self._send_bytes(target.read_bytes(), _content_type(target.name))


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
# Command entry point (dispatched from cli.main()).
# --------------------------------------------------------------------------- #

def cmd_serve(vault_root, conventions, args):
    # BaseHTTPRequestHandler is instantiated per-request, so stash the vault
    # context on the class rather than trying to thread it through __init__.
    TomeHandler.vault_root = vault_root
    TomeHandler.conventions = conventions

    httpd = ThreadingHTTPServer((args.host, args.port), TomeHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"tome serve: {url} (vault: {vault_root})")
    print("  serving  /  /index.json  /board.json  /raw/<page>.md  /app/<file>")
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
