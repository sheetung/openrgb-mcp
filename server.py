#!/usr/bin/env python3
"""Convenience entry point: ``python server.py`` starts the MCP server.

The real implementation lives in the installable package ``openrgb_mcp``
(see pyproject.toml). This shim keeps the repository root usable as a script
and keeps existing client configurations pointing at ``server.py`` working.
"""

from __future__ import annotations

import os
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from openrgb_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
