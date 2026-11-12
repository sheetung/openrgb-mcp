#!/usr/bin/env python3
"""Regression test for sharing one lamp between several agents.

No hardware needed: the shared registry and the priority rules are pure
bookkeeping.  Run:

    python tests/test_shared_lamp.py

Prints ``OK`` and exits 0 when every assertion passes.
"""

from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (os.path.join(ROOT, "src"), ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

# use a throw-away registry so the test never touches the real one
_tmp = tempfile.mkdtemp(prefix="openrgb-mcp-test-")
os.environ["OPENRGB_MCP_REGISTRY"] = os.path.join(_tmp, ".agents.json")
os.environ["OPENRGB_MCP_STATE"] = os.path.join(_tmp, ".state.json")

from openrgb_mcp.engine import STATE_PRIORITY, StatusEngine, registry_path  # noqa: E402


def test_registry_file_is_used():
    assert str(registry_path()).startswith(_tmp), registry_path()


def test_single_agent_sees_its_own_state():
    a = StatusEngine()
    try:
        a.state = "thinking"
        a._register()
        assert a.display_state == "thinking", a.display_state
        assert len(a.active_agents) == 1, a.active_agents
    finally:
        a.shutdown()


def test_higher_priority_wins():
    a = StatusEngine()
    b = StatusEngine()
    try:
        a.state = "thinking"
        a._register()
        b.state = "working"
        b._register()
        # every engine refreshes the registry on its own heartbeat; after that
        # both must agree on what the lamp shows
        a._register()
        b._register()
        assert a.display_state == "working", a.display_state
        assert b.display_state == "working", b.display_state
        assert len(a.active_agents) == 2, a.active_agents

        b.state = "error"
        b._register()
        a._register()
        assert a.display_state == "error", a.display_state
        assert b.display_state == "error", b.display_state

        # a less urgent state does not take the lamp back
        a.state = "done"
        a._register()
        b._register()
        assert a.display_state == "error", a.display_state
        assert b.display_state == "error", b.display_state

        # once the error is gone, the remaining agent takes over
        b.state = "idle"
        b._register()
        a._register()
        assert a.display_state == "done", a.display_state
        assert b.display_state == "done", b.display_state
    finally:
        a.shutdown()
        b.shutdown()


def test_priority_order():
    assert STATE_PRIORITY["off"] > STATE_PRIORITY["error"]
    assert STATE_PRIORITY["error"] > STATE_PRIORITY["waiting"]
    assert STATE_PRIORITY["waiting"] > STATE_PRIORITY["working"]
    assert STATE_PRIORITY["working"] > STATE_PRIORITY["thinking"]
    assert STATE_PRIORITY["thinking"] > STATE_PRIORITY["done"]
    assert STATE_PRIORITY["done"] > STATE_PRIORITY["idle"]


def test_shutdown_clears_the_registry():
    a = StatusEngine()
    b = StatusEngine()
    a.state = "thinking"
    a._register()
    b.state = "working"
    b._register()
    a._register()
    assert len(a.active_agents) == 2, a.active_agents
    a.shutdown()
    b._register()
    assert len(b.active_agents) == 1, b.active_agents
    b.shutdown()
    # nobody left -> the lamp falls back to idle
    c = StatusEngine()
    try:
        c._register()
        assert c.display_state == "idle", c.display_state
    finally:
        c.shutdown()


def main() -> int:
    tests = [
        test_registry_file_is_used,
        test_single_agent_sees_its_own_state,
        test_higher_priority_wins,
        test_priority_order,
        test_shutdown_clears_the_registry,
    ]
    failures = []
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            import traceback

            failures.append(test.__name__)
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    if failures:
        print(f"\n{len(failures)} test(s) failed: {', '.join(failures)}")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())