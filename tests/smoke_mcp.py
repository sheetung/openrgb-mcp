#!/usr/bin/env python3
"""End-to-end smoke test against the *real* OpenRGB device.

Drives the server exactly like a real MCP client (official SDK, stdio
transport) and checks that the lamp actually changes.

    python tests/smoke_mcp.py

Requires a running OpenRGB with the SDK server enabled. Prints ``SMOKE OK``
and exits 0 when every assertion passes.
"""

from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (os.path.join(ROOT, "src"), ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

SERVER = os.path.join(ROOT, "server.py")


async def _run() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
    params = StdioServerParameters(command=sys.executable, args=[SERVER], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("initialize OK:", init.server_info.name, init.protocol_version)

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            expected = {
                "list_devices", "status", "set_state", "set_progress",
                "set_quota", "set_color", "set_leds", "set_effect", "restore",
            }
            assert expected <= names, sorted(expected - names)
            print(f"tools/list OK: {len(names)} tools")

            result = await session.call_tool("list_devices", {})
            assert not result.is_error, result.content
            data = result.structured_content
            assert data["led_count"] > 0, data
            print(f"list_devices OK: {data['led_count']} LEDs on {data['selected']!r}")

            for state in ("thinking", "working", "waiting", "done", "error", "idle"):
                result = await session.call_tool("set_state", {"state": state})
                assert not result.is_error, (state, result.content)
                assert result.structured_content["state"] == state, result.structured_content
            print("set_state OK (all 6 states)")

            result = await session.call_tool("set_progress", {"percent": 35})
            assert not result.is_error, result.content
            data = result.structured_content
            expected_filled = round(0.35 * data["led_count"])
            assert data["filled_leds"] == expected_filled, data
            print(f"set_progress OK: filled {data['filled_leds']}/{data['led_count']}")

            result = await session.call_tool("set_quota", {"remaining": 25, "total": 100})
            assert not result.is_error, result.content
            assert result.structured_content["ratio"] == 0.25, result.structured_content
            print("set_quota OK: ratio 0.25")

            result = await session.call_tool("status", {})
            assert not result.is_error, result.content
            assert result.structured_content["led_count"] > 0
            print("status OK")

            result = await session.call_tool("set_state", {"state": "nope"})
            assert result.is_error, result.content
            print("error path OK")

            result = await session.call_tool("set_color", {"color": "#123456"})
            assert not result.is_error, result.content
            print("set_color OK")


def main() -> int:
    asyncio.run(_run())
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
