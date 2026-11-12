#!/usr/bin/env python3
"""openrgb-status MCP server.

Bridges AI-session state (thinking / working / progress / done / error ...) to
an OpenRGB device, using the OpenRGB SDK network protocol.  Speaks MCP over
stdio (newline-delimited JSON-RPC 2.0), no third-party dependencies.

Tools (model-facing name = mcp__openrgb__<name>):
  list_devices  status  set_state  set_progress  set_quota  set_color
  set_leds  set_effect  restore

Run directly for a self-test:  python server.py --self-test
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import openrgb_client as oc  # noqa: E402
import renderer  # noqa: E402

SERVER_NAME = "openrgb-status"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2025-03-26"
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, ".state.json")

STATES = ("idle", "thinking", "working", "waiting", "done", "error", "off")
STATE_ALIASES = {
    "think": "thinking",
    "reasoning": "thinking",
    "busy": "working",
    "running": "working",
    "progress": "working",
    "complete": "done",
    "completed": "done",
    "success": "done",
    "finished": "done",
    "fail": "error",
    "failed": "error",
    "ask": "waiting",
    "input": "waiting",
    "idle_off": "off",
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": None,  # None = auto-discover (OPENRGB_PORT, OpenRGB log, 6742)
    "device": None,  # device name substring; None = first device
    "fps": 12,
    "brightness": 1.0,
    "animate": True,
    "done_hold_seconds": 8,  # after "done", fade back to idle (0 = hold forever)
    "palette": {},
}


def _load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


class StatusEngine:
    """Owns the OpenRGB connection and the animation loop."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        merged = dict(DEFAULT_CONFIG)
        if config:
            merged.update({k: v for k, v in config.items() if v is not None or k == "port"})
        if not merged.get("device"):
            merged["device"] = os.environ.get("OPENRGB_DEVICE") or None
        self.config = merged
        self.client: Optional[oc.OpenRGBClient] = None
        self.device: Optional[oc.Device] = None
        self.palette: Dict[str, Any] = dict(renderer.DEFAULT_PALETTE)
        self.palette.update(self.config.get("palette") or {})

        self.state = "idle"
        self.detail: Optional[str] = None
        self.progress: Optional[float] = None
        self.progress_color: Optional[str] = None
        self.quota: Optional[Tuple[int, int]] = None
        self.custom_color: Optional[str] = None
        self.state_since = time.time()

        self._last_frame: Optional[List[Tuple[int, int, int]]] = None
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_error: Optional[str] = None
        self.original_colors: Optional[List[str]] = None

        self._load_state_file()

    # -- persistence ----------------------------------------------------
    def _load_state_file(self) -> None:
        data = _load_json(STATE_PATH, {})
        if not isinstance(data, dict):
            return
        if data.get("port"):
            self.config["port"] = data["port"]
        if data.get("device"):
            self.config["device"] = data["device"]
        colors = data.get("original_colors")
        if isinstance(colors, list) and colors:
            self.original_colors = [str(c) for c in colors]

    def _save_state_file(self) -> None:
        data = {
            "port": self.client.port if self.client else self.config.get("port"),
            "device": self.device.name if self.device else self.config.get("device"),
            "original_colors": self.original_colors,
        }
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # -- connection -----------------------------------------------------
    def ensure_connected(self) -> oc.Device:
        with self._lock:
            if self.client is not None and self.device is not None:
                return self.device
            client = oc.OpenRGBClient(
                host=self.config["host"],
                port=self.config.get("port"),
                name="dsh-openrgb-status",
                timeout=3.0,
            )
            client.connect()
            device = client.find_device(self.config.get("device"))
            device = client.ensure_per_led_mode(device)
            self.client = client
            self.device = device
            if self.original_colors is None and device.colors:
                self.original_colors = [c.to_hex() for c in device.colors]
            self._save_state_file()
            return device

    def _drop_connection(self) -> None:
        with self._lock:
            if self.client is not None:
                self.client.close()
            self.client = None
            self.device = None
            self._last_frame = None

    def _push_frame(self, frame: List[Tuple[int, int, int]]) -> None:
        if self.device is None or self.client is None:
            return
        colors = [oc.RGBColor(r, g, b) for (r, g, b) in frame]
        try:
            self.client.set_device_colors(self.device, colors)
        except (oc.ConnectionFailed, oc.ProtocolError, OSError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._drop_connection()

    # -- rendering ------------------------------------------------------
    def current_frame(self, tick: Optional[float] = None) -> List[Tuple[int, int, int]]:
        device = self.device
        if device is None:
            return []
        if tick is None:
            tick = time.time() - self.state_since
        frame = renderer.render(
            state=self.state,
            led_count=device.led_count,
            tick=tick,
            palette=self.palette,
            color=self.custom_color,
            progress=self.progress,
            progress_color=self.progress_color,
            quota=self.quota,
        )
        brightness = float(self.config.get("brightness") or 1.0)
        if brightness < 1.0:
            frame = [
                (round(r * brightness), round(g * brightness), round(b * brightness))
                for (r, g, b) in frame
            ]
        return frame

    def _render_once(self) -> None:
        frame = self.current_frame()
        if self._last_frame != frame:
            self._push_frame(frame)
            self._last_frame = frame

    # -- animation thread ----------------------------------------------
    def _needs_animation(self) -> bool:
        if not self.config.get("animate", True):
            return False
        effect = (self.palette.get(self.state) or {}).get("effect", "solid")
        if effect in ("breathe", "pulse", "blink"):
            return True
        return self.progress is not None or self.quota is not None

    def start_animation(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="openrgb-anim", daemon=True
            )
            self._thread.start()

    def stop_animation(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    def _loop(self) -> None:
        fps = max(1, min(60, int(self.config.get("fps") or 12)))
        interval = 1.0 / fps
        while not self._stop.is_set():
            try:
                self.ensure_connected()
                self._handle_done_hold()
                if self._needs_animation():
                    self._render_once()
                time.sleep(interval)
            except Exception as exc:  # keep the loop alive, remember the error
                self.last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(2.0)

    def _handle_done_hold(self) -> None:
        hold = float(self.config.get("done_hold_seconds") or 0)
        if self.state != "done" or hold <= 0:
            return
        if time.time() - self.state_since >= hold:
            self.set_state("idle", detail=None, animate=False)

    # -- state mutation --------------------------------------------------
    def _reset_view(self) -> None:
        self.progress = None
        self.progress_color = None
        self.quota = None
        self.custom_color = None

    def set_state(
        self,
        state: str,
        detail: Optional[str] = None,
        animate: Optional[bool] = None,
    ) -> Dict[str, Any]:
        normalized = STATE_ALIASES.get(str(state).strip().lower(), str(state).strip().lower())
        if normalized not in STATES:
            raise ValueError(
                f"unknown state {state!r}; valid: {', '.join(STATES)}"
            )
        with self._lock:
            self._reset_view()
            self.state = normalized
            self.detail = detail
            self.state_since = time.time()
            if animate is not None:
                self.config["animate"] = bool(animate)
            device = self.ensure_connected()
            self._render_once()
            self.start_animation()
            return self.describe()

    def set_progress(
        self,
        percent: Optional[float] = None,
        done: Optional[float] = None,
        total: Optional[float] = None,
        color: Optional[str] = None,
        detail: Optional[str] = None,
        state: str = "working",
    ) -> Dict[str, Any]:
        if percent is None:
            if done is None or not total:
                raise ValueError("pass percent (0-100) or both done and total")
            percent = (float(done) / float(total)) * 100.0
        value = float(percent)
        if 0.0 < value <= 1.0 and (done is None):
            # tolerate 0..1 as a fraction
            value *= 100.0
        value = max(0.0, min(100.0, value))
        with self._lock:
            self._reset_view()
            self.state = STATE_ALIASES.get(state, state)
            if self.state not in STATES:
                self.state = "working"
            self.progress = value / 100.0
            self.progress_color = color
            self.detail = detail
            self.state_since = time.time()
            self.ensure_connected()
            self._render_once()
            self.start_animation()
            result = self.describe()
            result["percent"] = round(value, 1)
            result["filled_leds"] = self._filled_leds(self.progress)
            result["led_count"] = self.device.led_count if self.device else 0
            return result

    def set_quota(
        self,
        remaining: float,
        total: float,
        detail: Optional[str] = None,
    ) -> Dict[str, Any]:
        if total <= 0:
            raise ValueError("total must be > 0")
        remaining = max(0.0, min(float(total), float(remaining)))
        with self._lock:
            self._reset_view()
            self.state = "working" if self.state in ("idle", "off") else self.state
            self.quota = (int(round(remaining)), int(round(total)))
            self.detail = detail
            self.state_since = time.time()
            self.ensure_connected()
            self._render_once()
            self.start_animation()
            result = self.describe()
            result["remaining"] = remaining
            result["total"] = total
            result["ratio"] = round(remaining / total, 3)
            result["filled_leds"] = self._filled_leds(remaining / total)
            return result

    def set_color(self, color: str, brightness: Optional[float] = None) -> Dict[str, Any]:
        parsed = oc.RGBColor.from_hex(color)
        with self._lock:
            self._reset_view()
            self.state = "working"
            self.custom_color = parsed.to_hex()
            if brightness is not None:
                self.config["brightness"] = max(0.0, min(1.0, float(brightness)))
            self.state_since = time.time()
            self.ensure_connected()
            self._render_once()
            self.start_animation()
            return self.describe()

    def set_leds(self, colors: List[str]) -> Dict[str, Any]:
        parsed = [oc.RGBColor.from_hex(c) for c in colors]
        with self._lock:
            device = self.ensure_connected()
            if len(parsed) != device.led_count:
                raise ValueError(
                    f"got {len(parsed)} colors but the device has "
                    f"{device.led_count} LEDs"
                )
            self.stop_animation()
            self.client.set_device_colors(device, parsed)
            self._last_frame = [c.to_tuple() for c in parsed]
            self.state = "working"
            self._reset_view()
            self.state_since = time.time()
            return self.describe()

    def set_effect(self, effect: str, color: Optional[str] = None) -> Dict[str, Any]:
        """Apply a physical OpenRGB hardware effect (Direct/Static/Breathing/...)."""
        with self._lock:
            device = self.ensure_connected()
            self.stop_animation()
            target = None
            for mode in device.modes:
                if mode.name.lower() == effect.strip().lower():
                    target = mode
                    break
            if target is None:
                raise ValueError(
                    "unknown effect; OpenRGB modes on this device: "
                    + ", ".join(m.name for m in device.modes)
                )
            self.client._apply_mode(device, target)
            device = self.client._refresh_one(device.index)
            self.device = device
            if color and target.per_led:
                rgb = oc.RGBColor.from_hex(color)
                self.client.set_device_colors(device, [rgb] * device.led_count)
            self.state = "working"
            self._reset_view()
            self.state_since = time.time()
            return self.describe()

    def restore(self) -> Dict[str, Any]:
        with self._lock:
            device = self.ensure_connected()
            self.stop_animation()
            colors = self.original_colors
            if colors and len(colors) == device.led_count:
                self.client.set_device_colors(
                    device, [oc.RGBColor.from_hex(c) for c in colors]
                )
                self._last_frame = [oc.RGBColor.from_hex(c).to_tuple() for c in colors]
            self.state = "idle"
            self._reset_view()
            self.state_since = time.time()
            result = self.describe()
            result["restored"] = bool(colors)
            return result

    def off(self) -> Dict[str, Any]:
        with self._lock:
            device = self.ensure_connected()
            self.stop_animation()
            self.client.set_device_colors(device, [oc.RGBColor.black()] * device.led_count)
            self._last_frame = [(0, 0, 0)] * device.led_count
            self.state = "off"
            self._reset_view()
            self.state_since = time.time()
            return self.describe()

    # -- introspection ---------------------------------------------------
    def _filled_leds(self, frac: Optional[float]) -> int:
        if frac is None or self.device is None:
            return 0
        return max(0, min(self.device.led_count, round(frac * self.device.led_count)))

    def describe(self) -> Dict[str, Any]:
        with self._lock:
            device = self.device
            info: Dict[str, Any] = {
                "state": self.state,
                "detail": self.detail,
                "led_count": device.led_count if device else None,
                "device": device.name if device else None,
                "port": self.client.port if self.client else self.config.get("port"),
                "protocol_version": (
                    self.client.protocol_version if self.client else None
                ),
                "animation_running": bool(
                    self._thread is not None and self._thread.is_alive()
                ),
                "animate": bool(self.config.get("animate", True)),
                "fps": self.config.get("fps"),
                "brightness": self.config.get("brightness"),
                "progress": self.progress,
                "quota": self.quota,
                "custom_color": self.custom_color,
                "state_age_s": round(time.time() - self.state_since, 1),
                "last_error": self.last_error,
            }
            if device is not None:
                info["active_mode"] = device.active.name if device.active else None
                info["zones"] = [
                    {"index": z.index, "name": z.name, "leds": z.num_leds}
                    for z in device.zones
                ]
            return info

    def list_devices(self) -> Dict[str, Any]:
        with self._lock:
            device = self.ensure_connected()
            devices = [d.to_dict(include_leds=False) for d in self.client.devices]
            return {
                "port": self.client.port,
                "protocol_version": self.client.protocol_version,
                "selected": device.name,
                "led_count": device.led_count,
                "devices": devices,
            }

    def shutdown(self) -> None:
        self.stop_animation()
        with self._lock:
            if self.client is not None:
                self.client.close()
            self.client = None


# --------------------------------------------------------------------------
# MCP tool definitions
# --------------------------------------------------------------------------
def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


STATE_ENUM = list(STATES)

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "list_devices",
        "description": (
            "读取 OpenRGB 设备清单：设备名、类型、zone 分区、每个分区灯珠数量、"
            "总 LED 数量、可用模式。用于确定进度条/额度条可用的灯珠数。"
        ),
        "inputSchema": _schema({}),
    },
    {
        "name": "status",
        "description": (
            "返回状态灯当前状态（state、LED 数量、设备、端口、动画是否运行、"
            "进度/额度、最近错误）。排障或确认灯效是否生效时调用。"
        ),
        "inputSchema": _schema({}),
    },
    {
        "name": "set_state",
        "description": (
            "切换主机灯光为当前会话状态。state：idle(空闲)、thinking(思考中，蓝色呼吸)、"
            "working(执行中，青色呼吸)、waiting(等待用户输入，琥珀色脉冲)、"
            "done(完成，绿色常亮)、error(出错，红色闪烁)、off(关闭)。"
            "在开始思考/执行/完成/出错时调用；detail 可传一句说明（仅记录，不显示在灯上）。"
        ),
        "inputSchema": _schema(
            {
                "state": {"type": "string", "enum": STATE_ENUM},
                "detail": {"type": "string"},
                "animate": {"type": "boolean", "description": "是否启用动画（默认 true）"},
            },
            ["state"],
        ),
    },
    {
        "name": "set_progress",
        "description": (
            "把任务进度显示成灯带进度条：已完成的灯珠点亮，其余暗色。"
            "传 percent(0-100)，或 done/total。state 默认 working，可选 thinking/done 等。"
        ),
        "inputSchema": _schema(
            {
                "percent": {"type": "number", "minimum": 0, "maximum": 100},
                "done": {"type": "number", "minimum": 0},
                "total": {"type": "number", "minimum": 0},
                "color": {"type": "string", "description": "点亮部分颜色 #RRGGBB"},
                "state": {"type": "string", "enum": STATE_ENUM},
                "detail": {"type": "string"},
            },
        ),
    },
    {
        "name": "set_quota",
        "description": (
            "用灯珠数量显示剩余额度：rem/total 点亮对应比例的灯珠，"
            "剩余 >50% 绿色、20%-50% 琥珀色、<20% 红色。"
            "例如剩余 token 额度、API 余额、剩余步数等。"
        ),
        "inputSchema": _schema(
            {
                "remaining": {"type": "number", "minimum": 0},
                "total": {"type": "number", "minimum": 0, "exclusiveMinimum": True},
                "detail": {"type": "string"},
            },
            ["remaining", "total"],
        ),
    },
    {
        "name": "set_color",
        "description": "把所有灯珠设为单一颜色（#RRGGBB），可选 brightness 0-1。",
        "inputSchema": _schema(
            {
                "color": {"type": "string"},
                "brightness": {"type": "number", "minimum": 0, "maximum": 1},
            },
            ["color"],
        ),
    },
    {
        "name": "set_leds",
        "description": (
            "直接指定每颗灯珠的颜色：colors 为 #RRGGBB 数组，长度必须等于总 LED 数量"
            "（可用 list_devices 查询）。用于自定义渐变/图案。"
        ),
        "inputSchema": _schema(
            {"colors": {"type": "array", "items": {"type": "string"}}},
            ["colors"],
        ),
    },
    {
        "name": "set_effect",
        "description": (
            "调用 OpenRGB 设备自带硬件模式（如 Direct、Static、Breathing、Strobe、"
            "Cycling、Random、Wave、Water）。会停止状态动画；之后可用 set_state 恢复。"
        ),
        "inputSchema": _schema(
            {
                "effect": {"type": "string"},
                "color": {"type": "string", "description": "可选，设置该模式下所有灯珠颜色"},
            },
            ["effect"],
        ),
    },
    {
        "name": "restore",
        "description": "恢复第一次接管前的原始灯效颜色，并停止状态动画。",
        "inputSchema": _schema({}),
    },
]

HANDLERS = {}  # name -> (engine_method, arg_names)


def _handler(name: str, *arg_names: str):
    def register(func):
        HANDLERS[name] = (func, arg_names)
        return func
    return register


@_handler("list_devices")
def _tool_list_devices(engine: StatusEngine) -> Any:
    return engine.list_devices()


@_handler("status")
def _tool_status(engine: StatusEngine) -> Any:
    return engine.describe()


@_handler("set_state")
def _tool_set_state(engine: StatusEngine, state: str, detail: Optional[str] = None,
                    animate: Optional[bool] = None) -> Any:
    return engine.set_state(state, detail=detail, animate=animate)


@_handler("set_progress")
def _tool_set_progress(engine: StatusEngine, **kwargs) -> Any:
    return engine.set_progress(
        percent=kwargs.get("percent"),
        done=kwargs.get("done"),
        total=kwargs.get("total"),
        color=kwargs.get("color"),
        detail=kwargs.get("detail"),
        state=kwargs.get("state", "working"),
    )


@_handler("set_quota")
def _tool_set_quota(engine: StatusEngine, **kwargs) -> Any:
    return engine.set_quota(
        remaining=kwargs["remaining"],
        total=kwargs["total"],
        detail=kwargs.get("detail"),
    )


@_handler("set_color")
def _tool_set_color(engine: StatusEngine, **kwargs) -> Any:
    return engine.set_color(kwargs["color"], brightness=kwargs.get("brightness"))


@_handler("set_leds")
def _tool_set_leds(engine: StatusEngine, colors: List[str]) -> Any:
    return engine.set_leds(colors)


@_handler("set_effect")
def _tool_set_effect(engine: StatusEngine, effect: str, color: Optional[str] = None) -> Any:
    return engine.set_effect(effect, color=color)


@_handler("restore")
def _tool_restore(engine: StatusEngine) -> Any:
    return engine.restore()


# --------------------------------------------------------------------------
# MCP stdio transport
# --------------------------------------------------------------------------
class McpServer:
    def __init__(self, engine: StatusEngine, stdin=None, stdout=None):
        self.engine = engine
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self._write_lock = threading.Lock()

    def send(self, message: Dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            self.stdout.write(payload + "\n")
            self.stdout.flush()

    def reply(self, msg_id: Any, result: Dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def error(self, msg_id: Any, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})

    # -- handlers -------------------------------------------------------
    def handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        requested = params.get("protocolVersion") or PROTOCOL_VERSION
        return {
            "protocolVersion": requested,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "OpenRGB 状态灯。用 set_state 表达对话状态，set_progress/set_quota "
                "把任务进度或剩余额度显示为灯带进度条，list_devices 查询灯珠数量。"
            ),
        }

    def handle_tools_list(self) -> Dict[str, Any]:
        return {"tools": TOOLS}

    def handle_tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        entry = HANDLERS.get(name)
        if entry is None:
            return {
                "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                "isError": True,
            }
        func, arg_names = entry
        try:
            if arg_names:
                kwargs = {k: arguments.get(k) for k in arg_names}
                missing = [k for k in arg_names if k in ("state", "remaining", "total", "color", "colors") and kwargs.get(k) is None]
                if missing:
                    raise ValueError(f"missing required argument(s): {', '.join(missing)}")
                result = func(self.engine, **kwargs)
            else:
                result = func(self.engine, **arguments)
            text = json.dumps(result, ensure_ascii=False, indent=2)
            payload = {"content": [{"type": "text", "text": text}]}
            if isinstance(result, dict):
                payload["structuredContent"] = result
            return payload
        except Exception as exc:
            self.engine.last_error = f"{type(exc).__name__}: {exc}"
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"{type(exc).__name__}: {exc}\n"
                            "提示：确认 OpenRGB 正在运行且 SDK Server 已启用；"
                            "可先调用 status 或 list_devices 排查。"
                        ),
                    }
                ],
                "isError": True,
            }

    # -- main loop ------------------------------------------------------
    def serve(self) -> int:
        while True:
            line = self.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.error(None, -32700, "parse error")
                continue
            method = message.get("method")
            msg_id = message.get("id")
            params = message.get("params") or {}
            if msg_id is None:
                # notification - no response
                if method in ("notifications/initialized", "initialized"):
                    pass
                continue
            try:
                if method == "initialize":
                    self.reply(msg_id, self.handle_initialize(params))
                elif method == "tools/list":
                    self.reply(msg_id, self.handle_tools_list())
                elif method == "tools/call":
                    self.reply(msg_id, self.handle_tools_call(params))
                elif method == "ping":
                    self.reply(msg_id, {})
                elif method in ("resources/list", "prompts/list", "resources/templates/list"):
                    self.reply(msg_id, {method.split("/")[0]: []})
                elif method in ("shutdown",):
                    self.reply(msg_id, {})
                else:
                    self.error(msg_id, -32601, f"method not found: {method}")
            except Exception as exc:  # pragma: no cover - defensive
                self.error(msg_id, -32603, f"{type(exc).__name__}: {exc}")
                traceback.print_exc(file=sys.stderr)
        return 0


def _force_utf8_stdio() -> None:
    """MCP stdio must be UTF-8 even when the parent console uses a legacy
    Windows code page (our tools return Chinese text)."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _force_utf8_stdio()
    if "--self-test" in argv:
        engine = StatusEngine(_load_json(CONFIG_PATH, {}))
        try:
            print(json.dumps(engine.list_devices(), ensure_ascii=False, indent=2)[:1200])
            print(json.dumps(engine.set_state("thinking")["state"], ensure_ascii=False))
            print(json.dumps(engine.set_progress(percent=42)["filled_leds"], ensure_ascii=False))
            print(json.dumps(engine.set_quota(remaining=30, total=100)["ratio"], ensure_ascii=False))
            print(json.dumps(engine.describe(), ensure_ascii=False, indent=2)[:800])
            return 0
        finally:
            engine.shutdown()
    engine = StatusEngine(_load_json(CONFIG_PATH, {}))
    try:
        return McpServer(engine).serve()
    finally:
        engine.shutdown()


if __name__ == "__main__":
    raise SystemExit(_main())