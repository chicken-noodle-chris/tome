#!/usr/bin/env python3
"""Shim — the real code now lives at src/tome_cli/lint.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from tome_cli.lint import main  # noqa: E402

if __name__ == "__main__":
    main()
