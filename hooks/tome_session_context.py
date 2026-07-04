#!/usr/bin/env python3
"""SessionStart hook: inject a terse pointer to the knowledge vault, when one
resolves, so every session opens vault-aware without hand-wiring a pointer
into the user's global CLAUDE.md. Silent (exit 0) when no vault resolves —
this hook is awareness-only and can't block a session start anyway. Injects
only the pointer, never the index body: cost scales with vault size, and
this line is paid in every session."""
import json
import os
import pathlib
import sys


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
    vault = find_vault_root()
    if vault is None or not vault.exists():
        return

    context = (
        f"Knowledge vault at {vault}. Read wiki/index.md for what it knows "
        "and wiki/SCHEMA.md for conventions. The tome CLI (tome help) owns "
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
