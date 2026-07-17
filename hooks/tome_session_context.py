#!/usr/bin/env python3
"""SessionStart hook: two jobs.

1. Put `tome` on PATH for the session's Bash commands. Only hooks see
   $CLAUDE_PLUGIN_ROOT resolved; agents' Bash commands don't. Bridging via
   $CLAUDE_ENV_FILE (sourced into every subsequent Bash invocation) means
   agents just run `tome <cmd>` — no path archaeology, and the entry tracks
   plugin updates because it's re-resolved every session. Also exports
   TOME_PLUGIN_ROOT (for supporting scripts like wiki_search.py) and
   TOME_PYTHON — this hook's own sys.executable, a known-good interpreter,
   so the `tome` shim never has to guess python vs python3. Paths are
   written in POSIX form: the env file is sourced by bash, where the colon
   in a raw `C:\\...` entry would split PATH.

2. Inject the read/write conventions for the vault, when one resolves, so
   every session is vault-aware even without a skill invoked and without
   hand-wiring this into the user's global CLAUDE.md (which now stays
   vault-agnostic — this hook is the single source for the pointer). The
   context text itself lives in tome_cli.cli.prime_terse_text — this hook
   just imports and prints it, so it's byte-identical to `tome prime`'s
   terse tier and there's exactly one spot to edit.

Env export runs even with no vault (`tome init` needs it); context injection
stays vault-gated and silent otherwise — awareness-only, can't block."""
import json
import os
import pathlib
import re
import sys


def to_posix(path):
    """C:\\Users\\x -> /c/Users/x; POSIX paths pass through unchanged."""
    p = str(path).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):(/.*)?$", p)
    if m:
        p = "/" + m.group(1).lower() + (m.group(2) or "")
    return p


def export_env(plugin_root, vault_root=None):
    """Append tome exports to $CLAUDE_ENV_FILE. Returns True when written,
    False when the mechanism is unavailable (older Claude Code) — the caller
    degrades to spelling out the invocation path in context instead.
    vault_root is only passed when resolution needed the sibling-checkout
    scan below; a vault found by cwd walk-up needs no export because
    tome.py's own walk-up will find it again from wherever a Bash command's
    cwd happens to be inside it."""
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        return False
    lines = ""
    if plugin_root is not None:
        scripts = to_posix(plugin_root / "scripts")
        lines += (
            f'export PATH="{scripts}:$PATH"\n'
            f'export TOME_PLUGIN_ROOT="{to_posix(plugin_root)}"\n'
            f'export TOME_PYTHON="{to_posix(sys.executable)}"\n'
        )
    if vault_root is not None:
        lines += f'export VAULT_ROOT="{to_posix(vault_root)}"\n'
    if not lines:
        return False
    with open(env_file, "a", encoding="utf-8") as f:
        f.write(lines)
    return True


def find_nearby_vault(cur):
    """Scan one level away from cwd for a conventions.toml: cur's own
    children first, then cur's siblings (cur.parent's other children). A
    multi-repo cloud workspace shows up in two shapes depending on whether a
    "primary" repo was designated: with one, cwd starts inside that repo and
    the vault sits alongside it as a sibling checkout; with none, cwd starts
    at the workspace root itself, one level *above* every checkout, so the
    vault is a child of cwd instead. Checking both directions covers either
    shape without needing to know which one a given session landed in.
    Deterministic first match by sorted name; a workspace with more than one
    vault-shaped directory nearby is not a case this needs to disambiguate."""
    candidates = []
    try:
        candidates.extend(sorted(cur.iterdir()))
    except OSError:
        pass
    try:
        candidates.extend(c for c in sorted(cur.parent.iterdir()) if c != cur)
    except OSError:
        pass
    for child in candidates:
        if child.is_dir() and (child / "conventions.toml").is_file():
            return child
    return None


def find_vault_root():
    """Walk up from cwd looking for conventions.toml; in a Claude Code Remote
    (cloud) session, fall back to scanning nearby checkouts (children and
    siblings of cwd — see find_nearby_vault); else $VAULT_ROOT — same
    resolution tome.py itself uses, plus the nearby-checkout scan. Unlike the
    Stop hook, this fallback stays: awareness is cheap and correct
    everywhere, whereas blocking is not. The nearby scan is gated on
    CLAUDE_CODE_REMOTE=true since it isn't a safe assumption for an arbitrary
    local multi-repo checkout on someone's laptop.

    Returns (vault_path_or_None, found_via_nearby_scan)."""
    cur = pathlib.Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if (d / "conventions.toml").is_file():
            return d, False
    if os.environ.get("CLAUDE_CODE_REMOTE") == "true":
        nearby = find_nearby_vault(cur)
        if nearby is not None:
            return nearby, True
    env = os.environ.get("VAULT_ROOT")
    if env:
        return pathlib.Path(env), False
    return None, False


def main():
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    plugin_root = pathlib.Path(plugin_root) if plugin_root else None

    vault, via_nearby_scan = find_vault_root()

    try:
        export_env(plugin_root, vault if via_nearby_scan else None)
    except Exception:
        pass

    if vault is None or not vault.exists():
        return

    # Import path is this file's own location (hooks/ and src/ are always
    # siblings under the plugin root), independent of whether
    # $CLAUDE_PLUGIN_ROOT happens to be set — export_env above is the only
    # thing that actually needs that env var (it must match what agents'
    # Bash sees).
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root / "src"))
    from tome_cli.cli import prime_terse_text

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": prime_terse_text(vault),
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
