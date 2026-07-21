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
  * accepts one write, `POST /api/task/<id>/status`, which shells out to the
    pinned backlog.md CLI (`cli.run_backlog`) rather than touching task YAML
    directly — the writes-through-CLI boundary from [[kanban-render-side]].
    `board.json` carries a `writable` flag so the frontend can tell a live
    `tome serve` (true) from a frozen static export (false, see
    export_static() below) and hide drag-to-move accordingly — the static
    deploy has no server behind it to accept the POST at all;
  * accepts a second write, `POST /api/page`, editing a page's body through
    `cli.write_page` + the lint gate ([[page-editing]]) — see `save_page()`
    below for the conflict/lint contract. Also absent on a static export, and
    gated on the same `board.writable` flag client-side (no separate flag).

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
# Task status writes — the one mutation this server accepts, always shelled
# through backlog.md per [[kanban-render-side]]. Split out from the HTTP
# handler so it's unit-testable without a live server.
# --------------------------------------------------------------------------- #

def apply_task_status(vault_root, raw_task_id, status):
    """Moves a backlog task to `status` via `backlog.md task edit -s`. Returns
    (ok, message) — message is empty on success, an error string otherwise.
    `raw_task_id` accepts either case and an optional `task-`/`TASK-` prefix,
    matching what `board.json` cards and the frontend's URLs carry."""
    from tome_cli import cli

    task_id = raw_task_id.strip()
    if task_id.upper().startswith("TASK-"):
        task_id = task_id[len("TASK-"):]
    if not task_id.isdigit():
        return False, f"bad task id {raw_task_id!r}"
    if not status:
        return False, "status is required"

    proc = cli.run_backlog(vault_root, ["task", "edit", task_id, "-s", status], capture=True)
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip() or "backlog task edit failed"
        return False, message
    return True, ""


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


def save_page(vault_root, conventions, rel, body, base_hash):
    """The [[page-editing]] save path: optimistic-concurrency write of one
    page's body, gated by a lint check scoped to just that page. Returns
    (http_status, payload_dict) — never raises, so the HTTP handler can pass
    the pair straight through to `_send_json`.

    1. Pull, so the conflict check below is against the latest remote.
    2. Hash the file's current bytes; a `base_hash` mismatch means the page
       changed since the client opened it — refuse, write nothing (409).
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

    pull = cli.run_git(vault_root, ["pull", "--rebase", "--autostash"])
    if pull.returncode != 0:
        return 500, {"error": (pull.stderr or pull.stdout).strip() or "git pull failed"}

    original_bytes = target.read_bytes()
    current_hash = hashlib.sha256(original_bytes).hexdigest()
    if base_hash != current_hash:
        return 409, {"error": "page changed since you opened it",
                      "currentHash": current_hash}

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

    push_code = cli._push_with_retry(vault_root)
    if push_code != 0:
        return 500, {"error": "commit landed locally but push failed — resolve manually"}

    return 200, {"hash": hashlib.sha256(target.read_bytes()).hexdigest()}


# --------------------------------------------------------------------------- #
# HTTP handler — GET (four route families, path-safe raw reads) plus two
# POST write routes.
# --------------------------------------------------------------------------- #

_TASK_STATUS_RE = re.compile(r"^/api/task/([^/]+)/status$")


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
            if path.startswith("/raw/"):
                return self._send_raw(path[len("/raw/"):])
            if path.startswith("/app/"):
                return self._send_frontend(path[len("/app/"):])
            self._send_error(404, "not found")
        except BrokenPipeError:
            pass  # client navigated away mid-response — nothing to report

    def do_POST(self):
        TomeHandler.last_activity = time.monotonic()
        path = unquote(urlparse(self.path).path)
        try:
            m = _TASK_STATUS_RE.match(path)
            if m:
                length = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw_body) if raw_body else {}
                except json.JSONDecodeError:
                    return self._send_json({"error": "malformed JSON body"}, status=400)
                status = str(payload.get("status") or "").strip()
                ok, message = apply_task_status(self.vault_root, m.group(1), status)
                if not ok:
                    return self._send_json({"error": message}, status=400)
                return self._send_json(_board_with_writable(self.vault_root, self.conventions, True))
            if path == "/api/page":
                length = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw_body) if raw_body else {}
                except json.JSONDecodeError:
                    return self._send_json({"error": "malformed JSON body"}, status=400)
                rel = payload.get("path")
                body = payload.get("body")
                base_hash = str(payload.get("baseHash") or "")
                if not rel or body is None:
                    return self._send_json({"error": "path and body are required"}, status=400)
                status_code, result = save_page(self.vault_root, self.conventions, rel, body, base_hash)
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
    print("  POST /api/task/<id>/status  (status moves, shelled to backlog.md)")
    print("  POST /api/page              (body edits, conflict + lint gated)")

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
