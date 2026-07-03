#!/usr/bin/env python3
"""
setup_quartz.py — bootstrap the Quartz browse view for a vault.

`quartz/` is gitignored inside a vault (it's a derived build tree, not vault
content), so a fresh clone has no browse view until this script runs. It
clones Quartz, installs its deps, wires the vault's `wiki/` in as the content
source, applies the vault's tracked `quartz.config.yaml` + `quartz.lock.json`,
and installs the community plugins those files pin.

Quartz v5's plugins are standalone Git repos resolved through
`quartz.lock.json` rather than vendored in the Quartz repo itself, so a
config alone isn't enough to build — `quartz plugin install` has to fetch
them into `.quartz/plugins` first. Running it with no flags installs the
exact commits pinned in the lockfile (mirrors `npm ci` vs `npm install`), so
a fresh clone reproduces the plugin versions this was last verified against
instead of pulling whatever's newest on GitHub that day.

The config/lockfile are propagated into `quartz/` with a byte-compare-then-
copy (`shutil.copy2` when they differ), not a hardlink — a hardlink looks
byte-identical right up until either path is edited via write-by-rename
(common in editors, including on Windows), which silently severs the link
and leaves the copy stale with no error. Copy-if-different re-syncs on
every run instead.

stdlib only; shells out to git/npm/npx. Idempotent: safe to re-run — an
existing checkout is left alone (delete `quartz/` and re-run for a
from-scratch rebuild or to pick up a QUARTZ_REF bump), and the content link,
config, lockfile, and plugin install are only touched when out of date.

The vault this bootstraps is resolved the same way tome.py resolves it:
--vault PATH, else walk up from cwd looking for conventions.toml, else
$VAULT_ROOT. The script itself lives at the plugin root, which is
never the vault root.

Usage:
    python setup_quartz.py [--vault PATH]
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

QUARTZ_REPO = "https://github.com/jackyzha0/quartz.git"
# Pinned to an exact commit, not a moving branch like "v5" — reproducibility
# for the same reason quartz.lock.json pins plugin commits. Bump deliberately
# (verify the new commit builds, then update this constant), never silently.
QUARTZ_REF = "9cf87ff1c248a8ca551093214b0fec3b31415009"

WINDOWS = sys.platform == "win32"


def resolve_vault_root(explicit):
    """--vault flag -> walk up from cwd looking for conventions.toml ->
    VAULT_ROOT env var -> fail loud. Mirrors tome.py's resolution: the vault
    you're standing in beats the global default."""
    def _validated(source, raw):
        p = Path(raw).resolve()
        if not (p / "conventions.toml").is_file():
            sys.exit(f"{source}={p} has no conventions.toml — not a vault root")
        return p

    if explicit:
        return _validated("--vault", explicit)
    cur = Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if (d / "conventions.toml").is_file():
            return d
    if os.environ.get("VAULT_ROOT"):
        return _validated("VAULT_ROOT", os.environ["VAULT_ROOT"])
    sys.exit(f"no vault found: no conventions.toml walking up from {cur}, "
              "and VAULT_ROOT is unset — pass --vault PATH or set VAULT_ROOT")


def run(cmd, cwd=None):
    print(f"$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=cwd, shell=WINDOWS, check=True)


def clone_or_update(quartz_dir):
    if (quartz_dir / ".git").exists():
        print(f"quartz/ already checked out — leaving it as is "
              "(delete quartz/ and re-run to pick up a QUARTZ_REF bump)")
        return
    if quartz_dir.exists():
        sys.exit(f"{quartz_dir} exists but isn't a git checkout — "
                  "remove it and re-run to clone fresh")
    # `git clone --branch` only accepts refs (branches/tags), not arbitrary
    # commit SHAs — pinning to an exact commit needs the lower-level
    # init/fetch/checkout dance instead. GitHub supports a shallow fetch of
    # a full SHA, so this still avoids pulling the whole repo history.
    quartz_dir.mkdir(parents=True, exist_ok=True)
    run(["git", "init"], cwd=quartz_dir)
    run(["git", "remote", "add", "origin", QUARTZ_REPO], cwd=quartz_dir)
    run(["git", "fetch", "--depth", "1", "origin", QUARTZ_REF], cwd=quartz_dir)
    run(["git", "checkout", "FETCH_HEAD"], cwd=quartz_dir)


def install_deps(quartz_dir):
    run(["npm", "ci"], cwd=quartz_dir)


def link_content(quartz_dir, wiki_dir):
    content = quartz_dir / "content"
    if content.exists():
        if content.resolve() == wiki_dir.resolve():
            return
        # A fresh clone ships its own sample content/ here; anything else
        # under quartz/ is derived and disposable, so replace it outright.
        shutil.rmtree(content)
    if WINDOWS:
        # Directory symlinks silently no-op as an empty folder on this
        # platform without elevated privileges; a junction needs none.
        run(["cmd", "/c", "mklink", "/J", str(content), str(wiki_dir)])
    else:
        content.symlink_to(wiki_dir, target_is_directory=True)


def _copy_if_different(src, dest, root):
    """Copy src -> dest only when the bytes differ (or dest doesn't exist
    yet). A hardlink would keep the two paths byte-identical automatically,
    but a write-by-rename edit (many editors, including some on Windows,
    replace-via-rename rather than write-in-place) silently severs a
    hardlink, leaving dest stale with no error. Comparing bytes and copying
    is slightly more work per run but can't silently drift."""
    if dest.exists() and dest.read_bytes() == src.read_bytes():
        return
    shutil.copy2(src, dest)
    print(f"copied {src.relative_to(root)} -> {dest.relative_to(root)}")


def apply_config(root, quartz_dir):
    _copy_if_different(root / "quartz.config.yaml", quartz_dir / "quartz.config.yaml", root)
    _copy_if_different(root / "quartz.lock.json", quartz_dir / "quartz.lock.json", root)


def install_plugins(quartz_dir):
    # No flags: installs the commits pinned in quartz.lock.json rather than
    # `--from-config` (which would re-resolve to each plugin's latest ref).
    run(["npx", "quartz", "plugin", "install"], cwd=quartz_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vault", help="explicit vault root (default: walk-up "
                                         "from cwd, else $VAULT_ROOT)")
    args = parser.parse_args()

    root = resolve_vault_root(args.vault)
    quartz_dir = root / "quartz"
    wiki_dir = root / "wiki"

    clone_or_update(quartz_dir)
    install_deps(quartz_dir)
    link_content(quartz_dir, wiki_dir)
    apply_config(root, quartz_dir)
    install_plugins(quartz_dir)
    print("\nBrowse view ready. Serve it with:\n")
    print(f"    cd {quartz_dir.relative_to(root)} && npx quartz build --serve\n")


if __name__ == "__main__":
    main()
