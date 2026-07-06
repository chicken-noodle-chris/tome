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
        export_env(plugin_root)
    except Exception:
        pass

    vault = find_vault_root()
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
