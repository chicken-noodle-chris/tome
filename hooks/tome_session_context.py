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

2. Inject a terse pointer to the knowledge vault, when one resolves, so
   every session opens vault-aware without hand-wiring a pointer into the
   user's global CLAUDE.md. Injects only the pointer, never the index body:
   cost scales with vault size, and this line is paid in every session.

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


def export_env(plugin_root):
    """Append tome exports to $CLAUDE_ENV_FILE. Returns True when written,
    False when the mechanism is unavailable (older Claude Code) — the caller
    degrades to spelling out the invocation path in context instead."""
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file or plugin_root is None:
        return False
    scripts = to_posix(plugin_root / "scripts")
    lines = (
        f'export PATH="{scripts}:$PATH"\n'
        f'export TOME_PLUGIN_ROOT="{to_posix(plugin_root)}"\n'
        f'export TOME_PYTHON="{to_posix(sys.executable)}"\n'
    )
    with open(env_file, "a", encoding="utf-8") as f:
        f.write(lines)
    return True


def find_vault_root():
    """Walk up from cwd looking for conventions.toml, else $VAULT_ROOT — same
    resolution tome.py itself uses. Unlike the Stop hook, this fallback stays:
    awareness is cheap and correct everywhere, whereas blocking is not."""
    cur = pathlib.Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if (d / "conventions.toml").is_file():
            return d
    env = os.environ.get("VAULT_ROOT")
    if env:
        return pathlib.Path(env)
    return None


def main():
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    plugin_root = pathlib.Path(plugin_root) if plugin_root else None
    try:
        on_path = export_env(plugin_root)
    except Exception:
        on_path = False

    vault = find_vault_root()
    if vault is None or not vault.exists():
        return

    if on_path:
        invoke = "on PATH in Bash — run `tome help`"
    elif plugin_root is not None:
        invoke = (
            f'run as: python "{plugin_root / "scripts" / "tome.py"}" <cmd>'
        )
    else:
        invoke = "run `tome help`"

    context = (
        f"Knowledge vault at {vault}. Read wiki/index.md for what it knows "
        f"and wiki/SCHEMA.md for conventions. The tome CLI ({invoke}) owns "
        "writes; start and end vault work with tome sync."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
