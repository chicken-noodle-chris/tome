#!/usr/bin/env python3
"""Stop hook: if the vault worktree is dirty at end of turn, block once with a
reminder to run `tome sync -m ...`. Never commits, never pulls — a fail-loud
backstop for the forget-to-sync failure mode, which is otherwise low-stakes
(the next sync sweeps stragglers)."""
import json
import os
import pathlib
import subprocess
import sys


def find_vault_root():
    """Walk up from cwd looking for conventions.toml, else $VAULT_ROOT (same
    resolution tome.py itself uses: the vault the session is standing in
    beats the global default, so a session inside vault B reminds about B,
    not about the personal vault). None if neither finds a vault — the
    plugin and the vault are always separate repos now, so there is no
    "the hook's own location is the vault" fallback to fall back to."""
    cur = pathlib.Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if (d / "conventions.toml").is_file():
            return d
    env = os.environ.get("VAULT_ROOT")
    if env:
        return pathlib.Path(env)
    return None


VAULT = find_vault_root()

try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {}

if payload.get("stop_hook_active") or VAULT is None or not VAULT.exists():
    sys.exit(0)

status = subprocess.run(["git", "-C", str(VAULT), "status", "--porcelain"], capture_output=True, text=True)
if not status.stdout.strip():
    sys.exit(0)

print(json.dumps({
    "decision": "block",
    "reason": "Unsynced vault changes — run `tome sync -m \"...\"` before stopping.",
}))
