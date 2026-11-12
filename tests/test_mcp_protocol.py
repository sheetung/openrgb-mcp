#!/usr/bin/env python3
"""End-to-end test with the *official* MCP client SDK.

The server is started as a real child process over the stdio transport and
driven with ``mcp.client.stdio`` / ``ClientSession`` - i.e. exactly how a
standards-compliant MCP client (DSH, Claude Desktop, ...) talks to it.

The OpenRGB SDK server is replaced by ``tests/mock_openrgb.py`` so the test is
hermetic and never touches real hardware.

    python tests/test_mcp_protocol.py

Prints ``OK`` and exits 0 when every assertion passes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (os.path.join(ROOT, "src"), ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import mock_openrgb as mock  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

SERVER = os.path.join(ROOT, "server.py")


async def _run(server: mock.MockServer) -> None:
    env = dict(os.environ)
    env["OPENRGB_PORT"] = str(server.port)
    env["OPENRGB_DEVICE"] = "Mock Strip"
    env["OPENRGB_MCP_STATE"] = os.path.join(HERE, ".state-test.json")
    env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
    params = StdioServerParameters(
        command=sys.executable,
        args=[SERVER],
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.server_info.name == "openrgb", init.server_info
            assert init.instructions, "server should advertise instructions"
            assert init.capabilities.tools is not None, init.capabilities

            # -- resources ------------------------------------------------
            resources = await session.list_resources()
            uris = {str(r.uri) for r in resources.resources}
            assert "openrgb://status" in uris, uris
            assert "openrgb://devices" in uris, uris

            read_status = await session.read_resource("openrgb://status")
            payload = json.loads(read_status.contents[0].text)
            assert "state" in payload, payload

            # -- prompts --------------------------------------------------
            prompts = await session.list_prompts()
            names = {p.name for p in prompts.prompts}
            assert "status-lamp" in names, names
            got = await session.get_prompt("status-lamp")
            assert got.messages, got

            # -- tools ----------------------------------------------------
            tools = await session.list_tools()
            by_name = {t.name: t for t in tools.tools}
            expected = {
                "list_devices", "status", "set_state", "set_progress",
                "set_quota", "set_color", "set_leds", "set_effect", "restore",
            }
            assert expected <= set(by_name), sorted(expected - set(by_name))

            # every tool must advertise an input schema and an output schema
            for name in expected:
                tool = by_name[name]
                assert tool.input_schema.get("properties") is not None, (name, tool.input_schema)
                assert tool.output_schema is not None, f"{name} has no outputSchema"
                assert tool.description, f"{name} has no description"

            # annotations are part of the standard tool metadata
            assert by_name["status"].annotations is not None
            assert by_name["status"].annotations.read_only_hint is True

            # -- calls ----------------------------------------------------
            result = await session.call_tool("list_devices", {})
            assert not result.is_error, result.content
            data = result.structured_content
            assert data is not None, "structured content missing"
            assert data["led_count"] == 10, data
            assert data["selected"] == "Mock Strip", data

            result = await session.call_tool(
                "set_state", {"state": "thinking", "detail": "protocol test"}
            )
            assert not result.is_error, result.content
            assert result.structured_content["state"] == "thinking", result.structured_content

            result = await session.call_tool("set_progress", {"percent": 40})
            assert not result.is_error, result.content
            assert result.structured_content["percent"] == 40.0, result.structured_content
            # in global control the progress is the brightness of the whole strip
            assert result.structured_content["control"] == "global", result.structured_content

            result = await session.call_tool("set_quota", {"remaining": 25, "total": 100})
            assert not result.is_error, result.content
            assert result.structured_content["ratio"] == 0.25, result.structured_content

            result = await session.call_tool("status", {})
            assert not result.is_error, result.content
            assert result.structured_content["led_count"] == 10

            # invalid arguments must be rejected by the schema, not crash
            result = await session.call_tool("set_state", {"state": "nope"})
            assert result.is_error, result.content

            # the lamp was actually driven: either a hardware effect
            # (UPDATEMODE) or per-LED colours (UPDATELEDS)
            driven = (
                server.count_of(mock.RGBCONTROLLER_UPDATEMODE)
                + server.count_of(mock.RGBCONTROLLER_UPDATELEDS)
            )
            assert driven >= 3, driven


def main() -> int:
    with mock.MockServer() as server:
        asyncio.run(_run(server))
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
