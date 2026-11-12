"""The MCP server itself - built on the official Model Context Protocol SDK.

Standard MCP surface:

* **tools** (``mcp__<serverName>__<tool>`` in the client) - nine tools, each
  with a JSON schema for the arguments and a JSON schema for the result
  (``outputSchema``), so clients can validate the structured content.
* **resources** - ``openrgb://status`` and ``openrgb://devices`` expose the
  current lamp state and the device inventory as JSON documents.
* **prompt** - ``status-lamp`` explains how to drive the lamp.

Transports: ``stdio`` (default, for local clients such as DSH/Claude Desktop)
and ``streamable-http`` (for remote clients).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from .config import load_config
from .engine import StatusEngine
from .models import DeviceListResult, StatusResult

INSTRUCTIONS = """\
OpenRGB status lamp. Use set_state to mirror the conversation state on the
host LEDs, set_progress / set_quota to turn the LED count into a progress or
remaining-budget bar, and list_devices to learn how many LEDs the device has.
"""

_server = MCPServer(
    name="openrgb",
    title="OpenRGB Status Lamp",
    version=__version__,
    instructions=INSTRUCTIONS,
)

_engine: Optional[StatusEngine] = None


def engine() -> StatusEngine:
    """Lazily create the shared status engine (no OpenRGB connection yet)."""
    global _engine
    if _engine is None:
        _engine = StatusEngine(load_config())
    return _engine


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------
@_server.tool(
    name="list_devices",
    title="List OpenRGB devices",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
def list_devices() -> DeviceListResult:
    """Read the OpenRGB device inventory: devices, zones, modes and how many
    LEDs each device has. The LED count is the length of a progress bar."""
    return DeviceListResult(**engine().list_devices())


@_server.tool(
    name="status",
    title="Show current lamp state",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
def status() -> StatusResult:
    """Report the current lamp state: state, LED count, device, SDK port,
    whether the animation is running, active progress/quota and the last error."""
    eng = engine()
    try:
        eng.ensure_connected()
    except Exception:  # noqa: BLE001 - status must work even without OpenRGB
        pass
    return StatusResult(**eng.describe())


@_server.tool(
    name="set_state",
    title="Switch the lamp to a session state",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True
    ),
)
def set_state(
    state: str,
    detail: Optional[str] = None,
    animate: Optional[bool] = None,
) -> StatusResult:
    """Switch the host LEDs to a conversation state.

    state: idle | thinking | working | waiting | done | error | off
    (aliases such as complete/failed/ask are accepted).
    All animations are slow, continuous fades - the lamp never strobes.
    thinking = blue breathing, working = cyan breathing, waiting = amber fade,
    done = solid green, error = red fade, idle = dim blue breathing,
    off = lights out. detail is a short note kept in the status output.
    animate: set to false to freeze the effect."""
    return StatusResult(**engine().set_state(state, detail=detail, animate=animate))


@_server.tool(
    name="set_progress",
    title="Show task progress as an LED bar",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True
    ),
)
def set_progress(
    percent: Optional[float] = None,
    done: Optional[float] = None,
    total: Optional[float] = None,
    color: Optional[str] = None,
    state: str = "working",
    detail: Optional[str] = None,
) -> StatusResult:
    """Turn the LED strip into a progress bar: the completed part is lit, the
    rest is dark.

    Pass percent (0-100) or both done and total. color overrides the bar
    colour (#RRGGBB). state selects the palette entry (default 'working')."""
    return StatusResult(
        **engine().set_progress(
            percent=percent,
            done=done,
            total=total,
            color=color,
            detail=detail,
            state=state,
        )
    )


@_server.tool(
    name="set_quota",
    title="Show remaining budget as an LED bar",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True
    ),
)
def set_quota(remaining: float, total: float, detail: Optional[str] = None) -> StatusResult:
    """Show how much of a budget is left using the LED count: the bar is
    filled proportionally to remaining/total and coloured green above 50%,
    amber between 20% and 50%, red at 20% or less.

    Use it for token budgets, API balance, remaining steps and similar."""
    return StatusResult(**engine().set_quota(remaining=remaining, total=total, detail=detail))


@_server.tool(
    name="set_color",
    title="Set a single colour on all LEDs",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True
    ),
)
def set_color(color: str, brightness: Optional[float] = None) -> StatusResult:
    """Paint every LED with one colour (#RRGGBB). brightness (0-1) scales it."""
    return StatusResult(**engine().set_color(color, brightness=brightness))


@_server.tool(
    name="set_leds",
    title="Set per-LED colours",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True
    ),
)
def set_leds(colors: List[str]) -> StatusResult:
    """Set an explicit colour per LED (#RRGGBB). The list length must equal the
    device's LED count - query it with list_devices. Use it for gradients and
    patterns."""
    return StatusResult(**engine().set_leds(colors))


@_server.tool(
    name="set_effect",
    title="Run a hardware effect of the device",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True
    ),
)
def set_effect(effect: str, color: Optional[str] = None) -> StatusResult:
    """Hand the LEDs over to one of the device's own effects (for example
    'Static', 'Breathing', 'Wave' - the available names depend on the device
    and are listed by list_devices). This stops the status animation; call
    set_state again to resume it."""
    return StatusResult(**engine().set_effect(effect, color=color))


@_server.tool(
    name="restore",
    title="Restore the original lighting",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True
    ),
)
def restore() -> StatusResult:
    """Restore the colours the device had before this server took over and
    stop the status animation."""
    return StatusResult(**engine().restore())


# ---------------------------------------------------------------------------
# resources
# ---------------------------------------------------------------------------
@_server.resource(
    "openrgb://status",
    name="OpenRGB lamp status",
    title="Current lamp status",
    description="JSON snapshot of the lamp: state, LED count, device, animation, progress/quota.",
    mime_type="application/json",
)
def resource_status() -> str:
    return json.dumps(engine().describe(), ensure_ascii=False, indent=2)


@_server.resource(
    "openrgb://devices",
    name="OpenRGB device inventory",
    title="OpenRGB devices",
    description="JSON inventory of the OpenRGB devices, their zones, modes and LED counts.",
    mime_type="application/json",
)
def resource_devices() -> str:
    return json.dumps(engine().list_devices(), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------
@_server.prompt(
    name="status-lamp",
    title="Drive the OpenRGB status lamp",
    description="Instructions for mirroring the conversation state onto the host LEDs.",
)
def prompt_status_lamp() -> str:
    return (
        "Drive the OpenRGB status lamp while you work:\n"
        "- before reasoning: set_state(state='thinking')\n"
        "- while running tools: set_state(state='working')\n"
        "- after each milestone: set_progress(percent=0-100)\n"
        "- waiting for the user: set_state(state='waiting')\n"
        "- finished successfully: set_state(state='done')\n"
        "- on failure: set_state(state='error', detail='...')\n"
        "- budget left: set_quota(remaining=X, total=Y)\n"
        "Call list_devices once if you need the LED count for a bar."
    )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def _self_test() -> int:
    eng = StatusEngine(load_config())
    try:
        print(json.dumps(eng.list_devices(), ensure_ascii=False, indent=2)[:1200])
        print(json.dumps(eng.set_state("thinking")["state"], ensure_ascii=False))
        print(json.dumps(eng.set_progress(percent=42)["filled_leds"], ensure_ascii=False))
        print(json.dumps(eng.set_quota(remaining=30, total=100)["ratio"], ensure_ascii=False))
        print(json.dumps(eng.describe(), ensure_ascii=False, indent=2)[:600])
        return 0
    finally:
        eng.shutdown()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openrgb-mcp",
        description="MCP server that mirrors AI session state onto OpenRGB hardware.",
    )
    parser.add_argument(
        "--transport",
        default=os.environ.get("OPENRGB_MCP_TRANSPORT", "stdio"),
        choices=["stdio", "streamable-http", "sse"],
        help="transport to serve (default: stdio)",
    )
    parser.add_argument("--host", default=os.environ.get("OPENRGB_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("OPENRGB_MCP_PORT", "8765")))
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="probe the real device and print the current state, then exit",
    )
    args = parser.parse_args(argv)

    if args.config:
        os.environ["OPENRGB_MCP_CONFIG"] = args.config

    if args.self_test:
        return _self_test()

    if args.transport == "stdio":
        _server.run(transport="stdio")
    else:
        try:
            _server.run(transport=args.transport, host=args.host, port=args.port)
        except TypeError:
            # older/newer SDK builds take the endpoint from settings
            _server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
