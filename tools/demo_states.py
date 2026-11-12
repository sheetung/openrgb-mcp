#!/usr/bin/env python3
"""Cycle through every lamp state over the real MCP server and report what
the hardware actually did.

    python tools/demo_states.py [--delay 3.0] [--device "Mock Strip"]

Uses the official MCP client SDK, so it exercises exactly the same path a
real client (DSH, Claude Desktop, ...) uses.  For every state it prints:

* the tool result (state, LED count, active hardware mode)
* whether the server had to fall back to per-LED software rendering
* the colour the controller ended up with

Requires a running OpenRGB with the SDK server enabled.
"""

from __future__ import annotations

import argparse
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

from openrgb_mcp.openrgb import OpenRGBClient  # noqa: E402

SERVER = os.path.join(ROOT, "server.py")

STATES = [
    ("idle", "空闲：暗蓝，缓慢呼吸"),
    ("thinking", "思考：蓝色呼吸"),
    ("working", "执行：青色呼吸"),
    ("waiting", "等待用户：琥珀色渐变"),
    ("done", "完成：绿色常亮"),
    ("error", "出错：红色渐变"),
    ("off", "关闭：全黑"),
]


async def _run(delay: float) -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
    params = StdioServerParameters(command=sys.executable, args=[SERVER], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("=== 状态循环 ===", flush=True)
            for state, desc in STATES:
                result = await session.call_tool(
                    "set_state", {"state": state, "detail": desc}
                )
                if result.is_error:
                    print(f"{state:9} ERROR: {result.content[0].text}", flush=True)
                    continue
                data = result.structured_content
                mode = data.get("active_mode")
                path = (
                    "硬件效果（控制器自跑，零 USB 写入）"
                    if data.get("render_mode") == "hardware"
                    else "逐灯珠写入"
                )
                print(
                    f"{state:9} -> {mode or '-':10} | {path:16} | "
                    f"{data.get('led_count')} LEDs | {desc}",
                    flush=True,
                )
                await asyncio.sleep(delay)

            print("\n=== 进度条 / 额度条（切回逐灯珠模式）===", flush=True)
            for call, args in (
                ("set_progress", {"percent": 33, "detail": "进度 33%"}),
                ("set_quota", {"remaining": 30, "total": 100, "detail": "剩余额度 30%"}),
            ):
                result = await session.call_tool(call, args)
                data = result.structured_content
                print(
                    f"{call:12} -> {data.get('active_mode') or '-':10} | "
                    f"filled {data.get('filled_leds')}/{data.get('led_count')}",
                    flush=True,
                )
                await asyncio.sleep(delay)

    # read back what the controller really holds
    print("\n=== 硬件回读 ===", flush=True)
    client = OpenRGBClient().connect()
    try:
        device = client.find_device()
        print(f"device      : {device.name}")
        print(f"active mode : {device.active.name if device.active else '-'}")
        for mode in device.modes:
            if mode.colors:
                print(f"  mode {mode.name:10} colour(s): {[c.to_hex() for c in mode.colors]}")
    finally:
        client.close()
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=float, default=3.0, help="seconds per state")
    args = parser.parse_args()
    return asyncio.run(_run(args.delay))


if __name__ == "__main__":
    raise SystemExit(main())
