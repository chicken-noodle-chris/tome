#!/usr/bin/env python3
"""Stop hook: if the vault worktree is dirty at end of turn, block once with a
reminder to run `tome sync -m ...`. Never commits, never pulls — a fail-loud
backstop for the forget-to-sync failure mode, which is otherwise low-stakes
(the next sync sweeps stragglers).

Only fires for a session whose cwd is actually inside a vault (walk-up to
conventions.toml) — no longer falls back to $VAULT_ROOT. A session doing
vault work from elsewhere (via $VAULT_ROOT) no longer gets the nag; that's
an accepted trade-off since such work flows through `tome sync` anyway,
whereas the false positive this fixes was constant: any session in any
project would block merely because the vault was left dirty by something
unrelated (e.g. hand-editing in Obsidian). The SessionStart hook
(tome_session_context.py) keeps the $VAULT_ROOT fallback — awareness is
cheap and correct everywhere; blocking is not."""
import json
import pathlib
import subprocess
import sys


def find_vault_root():
    """Walk up from cwd looking for conventions.toml — the vault the
    session's cwd is actually inside. See module docstring for why this no
    longer falls back to $VAULT_ROOT."""
    cur = pathlib.Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if (d / "conventions.toml").is_file():
            return d
    return None


def main():
    vault = find_vault_root()

    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    if payload.get("stop_hook_active") or vault is None or not vault.exists():
        return

    status = subprocess.run(["git", "-C", str(vault), "status", "--porcelain"],
                             capture_output=True, text=True)
    if not status.stdout.strip():
        return

    print(json.dumps({
        "decision": "block",
        "reason": "Unsynced vault changes — run `tome sync -m \"...\"` before stopping.",
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
