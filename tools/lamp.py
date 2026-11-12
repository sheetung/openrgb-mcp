#!/usr/bin/env python3
"""lamp.py — 手动控制 OpenRGB 状态灯的命令行工具。

直接操作设备（默认，快），或通过 MCP 服务器走一遍完整协议链路（--via-mcp）。

    python tools/lamp.py devices
    python tools/lamp.py status
    python tools/lamp.py state thinking --detail "正在分析需求"
    python tools/lamp.py state done
    python tools/lamp.py progress 60
    python tools/lamp.py progress 3/10
    python tools/lamp.py quota 30 100
    python tools/lamp.py color "#ff8800" --brightness 0.7
    python tools/lamp.py gradient "#ff0000" "#0000ff"      # 逐灯珠渐变
    python tools/lamp.py leds "#ff0000,#00ff00,#0000ff"
    python tools/lamp.py led 0 "#ff0000"                   # 只点亮第 0 颗
    python tools/lamp.py effect Breathing --color "#3a86ff"
    python tools/lamp.py off
    python tools/lamp.py restore                          # 恢复原始灯效
    python tools/lamp.py watch --delay 3                  # 循环演示所有状态
    python tools/lamp.py ramp --seconds 6                 # 亮度缓升缓降（测硬件渐变）

常用选项：
    --via-mcp     改用 MCP 服务器执行（会拉起一个临时 server 进程）
    --device NAME 按名称片段选择设备（默认第一个）
    --port N      指定 OpenRGB SDK 端口（默认自动发现）
    --json        以 JSON 输出结果
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (os.path.join(ROOT, "src"), ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from openrgb_mcp.engine import STATES  # noqa: E402
from openrgb_mcp.openrgb import OpenRGBClient, RGBColor  # noqa: E402

SERVER = os.path.join(ROOT, "server.py")


# ---------------------------------------------------------------------------
# backend: 直接操作设备 / 通过 MCP
# ---------------------------------------------------------------------------
class DirectBackend:
    """Straight to the OpenRGB SDK (no MCP in the loop)."""

    name = "direct"

    def __init__(self, device: Optional[str] = None, port: Optional[int] = None):
        self.client = OpenRGBClient(port=port, name="openrgb-mcp-cli").connect()
        self.device = self.client.find_device(device)

    def devices(self) -> dict:
        return {
            "port": self.client.port,
            "protocol_version": self.client.protocol_version,
            "selected": self.device.name,
            "led_count": self.device.led_count,
            "devices": [d.to_dict() for d in self.client.devices],
        }

    def status(self) -> dict:
        active = self.device.active
        return {
            "device": self.device.name,
            "led_count": self.device.led_count,
            "active_mode": active.name if active else None,
            "zones": [{"name": z.name, "leds": z.num_leds} for z in self.device.zones],
        }

    def state(self, state: str, detail: Optional[str] = None) -> dict:
        mode = self._hardware_mode(state)
        if mode is not None:
            color = self._palette_color(state)
            self.client.apply_hardware_effect(self.device, mode, color=color)
            self.device = self.client.devices[self.device.index]
            return {"state": state, "mode": mode.name, "color": color.to_hex()}
        self.device = self.client.ensure_per_led_mode(self.device)
        colors = [self._palette_color(state)] * self.device.led_count
        self.client.set_device_colors(self.device, colors)
        return {"state": state, "mode": "Direct", "color": colors[0].to_hex()}

    def progress(self, done: float, total: float, color: Optional[str]) -> dict:
        self.device = self.client.ensure_per_led_mode(self.device)
        frac = max(0.0, min(1.0, done / total)) if total else 0.0
        filled = round(frac * self.device.led_count)
        rgb = RGBColor.from_hex(color) if color else RGBColor(0, 212, 255)
        colors = [rgb] * filled + [RGBColor.black()] * (self.device.led_count - filled)
        self.client.set_device_colors(self.device, colors)
        return {"percent": round(frac * 100, 1), "filled_leds": filled,
                "led_count": self.device.led_count}

    def quota(self, remaining: float, total: float) -> dict:
        return self.progress(remaining, total, None)

    def color(self, hex_color: str, brightness: Optional[float]) -> dict:
        self.device = self.client.ensure_per_led_mode(self.device)
        rgb = RGBColor.from_hex(hex_color)
        if brightness is not None:
            rgb = rgb.scaled(brightness)
        self.client.set_device_colors(self.device, [rgb] * self.device.led_count)
        return {"color": rgb.to_hex(), "led_count": self.device.led_count}

    def leds(self, colors: Sequence[str]) -> dict:
        self.device = self.client.ensure_per_led_mode(self.device)
        if len(colors) != self.device.led_count:
            raise ValueError(
                f"需要 {self.device.led_count} 个颜色，收到 {len(colors)} 个"
            )
        rgbs = [RGBColor.from_hex(c) for c in colors]
        self.client.set_device_colors(self.device, rgbs)
        return {"led_count": len(rgbs)}

    def led(self, index: int, hex_color: str) -> dict:
        self.device = self.client.ensure_per_led_mode(self.device)
        colors = list(self.device.colors) or [RGBColor.black()] * self.device.led_count
        colors[index] = RGBColor.from_hex(hex_color)
        self.client.set_device_colors(self.device, colors)
        return {"led": index, "color": RGBColor.from_hex(hex_color).to_hex()}

    def effect(self, name: str, color: Optional[str]) -> dict:
        mode = self.client.find_mode(self.device, [name])
        if mode is None:
            raise ValueError(
                "设备没有这个模式，可用: "
                + ", ".join(m.name for m in self.device.modes)
            )
        rgb = RGBColor.from_hex(color) if color else None
        self.device = self.client.apply_hardware_effect(self.device, mode, color=rgb)
        return {"effect": mode.name, "color": rgb.to_hex() if rgb else None}

    def restore(self) -> dict:
        self.device = self.client.ensure_per_led_mode(self.device)
        colors = list(self.device.colors)
        # prefer the snapshot the server captured before it took over
        try:
            import json

            from openrgb_mcp.config import state_path

            data = json.loads(open(state_path(), encoding="utf-8").read())
            original = data.get("original_colors")
            if isinstance(original, list) and len(original) == self.device.led_count:
                colors = [RGBColor.from_hex(c) for c in original]
        except (OSError, ValueError):
            pass
        self.client.set_device_colors(self.device, colors)
        return {"restored": True, "led_count": len(colors)}

    def off(self) -> dict:
        mode = self.client.find_mode(self.device, ["Off", "Dark"])
        if mode is not None:
            self.client.apply_hardware_effect(self.device, mode)
            self.device = self.client.devices[self.device.index]
            return {"mode": mode.name}
        self.device = self.client.ensure_per_led_mode(self.device)
        self.client.set_device_colors(self.device, [RGBColor.black()] * self.device.led_count)
        return {"mode": "Direct", "led_count": self.device.led_count}

    def ramp(self, seconds: float) -> dict:
        """Slow brightness sweep - shows whether the hardware fades smoothly."""
        self.device = self.client.ensure_per_led_mode(self.device)
        steps = max(2, int(seconds * 8))
        for index in range(steps):
            phase = index / (steps - 1)
            level = 0.05 + 0.95 * (0.5 - 0.5 * __import__("math").cos(phase * 2 * 3.14159))
            rgb = RGBColor.from_hex("#3a86ff").scaled(level)
            self.client.set_device_colors(self.device, [rgb] * self.device.led_count)
            time.sleep(seconds / steps)
        return {"ramp_seconds": seconds, "steps": steps}

    def watch(self, delay: float) -> dict:
        for state in STATES:
            info = self.state(state)
            print(f"{state:9} -> {info['mode']:10} {info['color']}")
            time.sleep(delay)
        return {"watched": list(STATES)}

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _palette_color(state: str) -> RGBColor:
        from openrgb_mcp import renderer

        spec = renderer.DEFAULT_PALETTE.get(state) or renderer.DEFAULT_PALETTE["idle"]
        return RGBColor.from_hex(spec["color"])

    def _hardware_mode(self, state: str):
        names = {
            "thinking": ("Breathing", "Breathe"),
            "working": ("Breathing", "Breathe"),
            "waiting": ("Breathing", "Breathe"),
            "error": ("Breathing", "Breathe"),
            "done": ("Static", "Solid"),
            "idle": ("Static", "Solid"),
        }.get(state, ())
        if not names:
            return None
        return self.client.find_mode(self.device, names)

    def close(self) -> None:
        self.client.close()


class McpBackend:
    """Same commands, but executed by the MCP server over stdio."""

    name = "mcp"

    def __init__(self, device: Optional[str] = None, port: Optional[int] = None):
        self.device = device
        self.port = port
        self._session = None
        self._stack = None

    async def _open(self):
        if self._session is not None:
            return self._session
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
        if self.device:
            env["OPENRGB_DEVICE"] = self.device
        if self.port:
            env["OPENRGB_PORT"] = str(self.port)
        params = StdioServerParameters(command=sys.executable, args=[SERVER], env=env)
        self._stack = stdio_client(params)
        read, write = await self._stack.__aenter__()
        self._client = ClientSession(read, write)
        self._session = await self._client.__aenter__()
        await self._session.initialize()
        return self._session

    async def _call(self, tool: str, args: dict) -> dict:
        session = await self._open()
        result = await session.call_tool(tool, args)
        if result.is_error:
            raise RuntimeError(result.content[0].text)
        return result.structured_content or {}

    async def _a_devices(self) -> dict:
        return await self._call("list_devices", {})

    async def _a_status(self) -> dict:
        return await self._call("status", {})

    async def _a_state(self, state: str, detail: Optional[str]) -> dict:
        return await self._call("set_state", {"state": state, "detail": detail})

    async def _a_progress(self, done: float, total: float, color: Optional[str]) -> dict:
        args = {"done": done, "total": total}
        if color:
            args["color"] = color
        return await self._call("set_progress", args)

    async def _a_quota(self, remaining: float, total: float) -> dict:
        return await self._call("set_quota", {"remaining": remaining, "total": total})

    async def _a_color(self, hex_color: str, brightness: Optional[float]) -> dict:
        args = {"color": hex_color}
        if brightness is not None:
            args["brightness"] = brightness
        return await self._call("set_color", args)

    async def _a_leds(self, colors: List[str]) -> dict:
        return await self._call("set_leds", {"colors": colors})

    async def _a_effect(self, name: str, color: Optional[str]) -> dict:
        args = {"effect": name}
        if color:
            args["color"] = color
        return await self._call("set_effect", args)

    async def _a_restore(self) -> dict:
        return await self._call("restore", {})

    async def _a_off(self) -> dict:
        return await self._call("set_state", {"state": "off"})

    async def _a_watch(self, delay: float) -> dict:
        for state in STATES:
            info = await self._a_state(state, None)
            print(f"{state:9} -> {info.get('active_mode')} | {info.get('render_mode')}")
            await asyncio.sleep(delay)
        return {"watched": list(STATES)}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
        if self._stack is not None:
            await self._stack.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _gradient(colors: Sequence[str], count: int) -> List[str]:
    """Interpolate between two or more colours across ``count`` LEDs."""
    if len(colors) < 2:
        raise ValueError("渐变至少需要两个颜色")
    stops = [RGBColor.from_hex(c) for c in colors]
    out: List[str] = []
    for index in range(count):
        position = index * (len(stops) - 1) / max(1, count - 1)
        low = min(int(position), len(stops) - 2)
        frac = position - low
        a, b = stops[low], stops[low + 1]
        out.append(
            RGBColor(
                round(a.r + (b.r - a.r) * frac),
                round(a.g + (b.g - a.g) * frac),
                round(a.b + (b.b - a.b) * frac),
            ).to_hex()
        )
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lamp.py",
        description="手动控制 OpenRGB 状态灯",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--via-mcp", action="store_true", help="通过 MCP 服务器执行")
    parser.add_argument("--device", default=None, help="设备名称片段")
    parser.add_argument("--port", type=int, default=None, help="OpenRGB SDK 端口")
    parser.add_argument("--json", action="store_true", help="JSON 输出")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("devices", help="列出设备 / 分区 / 模式")
    sub.add_parser("status", help="显示当前状态")

    p = sub.add_parser("state", help="切换状态")
    p.add_argument("state", choices=list(STATES))
    p.add_argument("--detail", default=None)

    p = sub.add_parser("progress", help="进度条")
    p.add_argument("value", help="百分比（如 60）或 done/total（如 3/10）")

    p = sub.add_parser("quota", help="剩余额度条")
    p.add_argument("remaining", type=float)
    p.add_argument("total", type=float)

    p = sub.add_parser("color", help="全灯单色")
    p.add_argument("color")
    p.add_argument("--brightness", type=float, default=None)

    p = sub.add_parser("gradient", help="多段渐变")
    p.add_argument("colors", nargs="+", help="两个及以上颜色")

    p = sub.add_parser("leds", help="逐灯指定颜色（逗号分隔）")
    p.add_argument("colors")

    p = sub.add_parser("led", help="只点亮某一颗灯")
    p.add_argument("index", type=int)
    p.add_argument("color")

    p = sub.add_parser("effect", help="调用设备自带效果")
    p.add_argument("name")
    p.add_argument("--color", default=None)

    sub.add_parser("off", help="关灯")
    sub.add_parser("restore", help="恢复原始灯效")

    p = sub.add_parser("watch", help="循环演示所有状态")
    p.add_argument("--delay", type=float, default=3.0)

    p = sub.add_parser("ramp", help="亮度缓升缓降（测硬件渐变是否平滑）")
    p.add_argument("--seconds", type=float, default=6.0)
    return parser


def _parse_progress(value: str) -> tuple[float, float]:
    if "/" in value:
        done, total = value.split("/", 1)
        return float(done), float(total)
    percent = float(value)
    return percent, 100.0


def run_direct(args) -> dict:
    backend = DirectBackend(device=args.device, port=args.port)
    try:
        command = args.command
        if command == "devices":
            return backend.devices()
        if command == "status":
            return backend.status()
        if command == "state":
            return backend.state(args.state, args.detail)
        if command == "progress":
            done, total = _parse_progress(args.value)
            return backend.progress(done, total, None)
        if command == "quota":
            return backend.quota(args.remaining, args.total)
        if command == "color":
            return backend.color(args.color, args.brightness)
        if command == "gradient":
            colors = _gradient(args.colors, backend.device.led_count)
            return backend.leds(colors)
        if command == "leds":
            colors = [c.strip() for c in args.colors.split(",") if c.strip()]
            return backend.leds(colors)
        if command == "led":
            return backend.led(args.index, args.color)
        if command == "effect":
            return backend.effect(args.name, args.color)
        if command == "off":
            return backend.off()
        if command == "restore":
            return backend.restore()
        if command == "watch":
            return backend.watch(args.delay)
        if command == "ramp":
            return backend.ramp(args.seconds)
        raise SystemExit(f"未知命令: {command}")
    finally:
        backend.close()


async def run_mcp(args) -> dict:
    backend = McpBackend(device=args.device, port=args.port)
    try:
        command = args.command
        if command == "devices":
            return await backend._a_devices()
        if command == "status":
            return await backend._a_status()
        if command == "state":
            return await backend._a_state(args.state, args.detail)
        if command == "progress":
            done, total = _parse_progress(args.value)
            return await backend._a_progress(done, total, None)
        if command == "quota":
            return await backend._a_quota(args.remaining, args.total)
        if command == "color":
            return await backend._a_color(args.color, args.brightness)
        if command in ("gradient", "leds"):
            colors = args.colors
            if command == "gradient":
                count = (await backend._a_devices()).get("led_count", 60)
                colors = _gradient(args.colors, count)
            else:
                colors = [c.strip() for c in args.colors.split(",") if c.strip()]
            return await backend._a_leds(colors)
        if command == "led":
            devices = await backend._a_devices()
            count = devices.get("led_count", 60)
            colors = ["#000000"] * count
            colors[args.index] = args.color
            return await backend._a_leds(colors)
        if command == "effect":
            return await backend._a_effect(args.name, args.color)
        if command == "off":
            return await backend._a_off()
        if command == "restore":
            return await backend._a_restore()
        if command == "watch":
            return await backend._a_watch(args.delay)
        if command == "ramp":
            print("ramp 需要直连设备，请去掉 --via-mcp", file=sys.stderr)
            return {"skipped": "ramp"}
        raise SystemExit(f"未知命令: {command}")
    finally:
        await backend.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    args = build_parser().parse_args(argv)
    try:
        if args.via_mcp:
            result = asyncio.run(run_mcp(args))
        else:
            result = run_direct(args)
    except Exception as exc:  # noqa: BLE001 - CLI feedback
        print(f"错误: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        import json

        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for key, value in result.items():
            print(f"{key:14} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
