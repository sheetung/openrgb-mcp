"""Status engine: owns the OpenRGB connection and the animation loop.

Framework-free on purpose - the MCP layer (server.py) is a thin wrapper around
this class, so the behaviour can be unit tested without a running server.

Several agents may share one lamp (several DSH sessions, an agent plus the
local test console, ...).  Every engine registers its state in a small shared
registry file; the lamp then shows the *highest priority* state of all active
agents and every engine renders that same result - so the agents never fight
over the hardware and the lamp never flickers.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import renderer
from .config import load_config, state_path
from .openrgb import (
    ConnectionFailed,
    OpenRGBClient,
    ProtocolError,
    RGBColor,
)

STATES = ("idle", "thinking", "working", "waiting", "done", "error", "off")

# which state wins when several agents are active (higher = more urgent)
STATE_PRIORITY = {
    "off": 60,
    "error": 50,
    "waiting": 40,
    "working": 30,
    "thinking": 20,
    "done": 10,
    "idle": 0,
}

# an agent that has not reported for this long is considered gone
REGISTRY_TTL = 90.0

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


def _load_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def registry_path() -> Path:
    """Shared file where agents announce their state."""
    env = os.environ.get("OPENRGB_MCP_REGISTRY")
    if env:
        return Path(env)
    return state_path().parent / ".agents.json"


class StatusEngine:
    """Owns the OpenRGB connection, the current state and the animation loop."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or load_config()
        self.client: Optional[OpenRGBClient] = None
        self.device = None
        self.palette: Dict[str, Any] = dict(renderer.DEFAULT_PALETTE)
        self.palette.update(self.config.get("palette") or {})

        self.state = "idle"
        self.detail: Optional[str] = None
        self.progress: Optional[float] = None
        self.progress_color: Optional[str] = None
        self.quota: Optional[Tuple[int, int]] = None
        self.custom_color: Optional[str] = None
        self.state_since = time.time()
        # True while the controller animates on its own (hardware effect);
        # in that mode we must not write per-LED frames at all.
        self.hardware_mode = False

        self._last_frame: Optional[List[Tuple[int, int, int]]] = None
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_error: Optional[str] = None
        self.original_colors: Optional[List[str]] = None

        # shared-lamp coordination
        self.agent_id = os.environ.get("OPENRGB_AGENT_ID") or uuid.uuid4().hex[:8]
        self.agent_label = os.environ.get("OPENRGB_AGENT_LABEL") or self.agent_id
        self.share_lamp = bool(self.config.get("share_lamp", True))
        self.display_state = "idle"
        self.active_agents: List[Dict[str, Any]] = []

        self._load_state_file()

    # -- shared registry -------------------------------------------------
    def _read_registry(self) -> Dict[str, Dict[str, Any]]:
        data = _load_json(registry_path(), {})
        if not isinstance(data, dict):
            return {}
        now = time.time()
        alive: Dict[str, Dict[str, Any]] = {}
        for agent_id, entry in data.items():
            if not isinstance(entry, dict):
                continue
            if now - float(entry.get("ts", 0)) > REGISTRY_TTL:
                continue
            alive[agent_id] = entry
        return alive

    def _write_registry(self, entries: Dict[str, Dict[str, Any]]) -> None:
        try:
            path = registry_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(entries, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _register(self) -> None:
        """Announce our state and work out what the lamp should show."""
        if not self.share_lamp:
            self.display_state = self.state
            self.active_agents = [
                {"id": self.agent_id, "label": self.agent_label, "state": self.state}
            ]
            return
        with self._lock:
            entries = self._read_registry()
            entries[self.agent_id] = {
                "label": self.agent_label,
                "state": self.state,
                "detail": self.detail,
                "pid": os.getpid(),
                "ts": time.time(),
            }
            self._write_registry(entries)
            self.active_agents = [
                {
                    "id": agent_id,
                    "label": entry.get("label", agent_id),
                    "state": entry.get("state", "idle"),
                }
                for agent_id, entry in entries.items()
            ]
            # the most urgent state wins; ties go to the most recent report
            winner = max(
                entries.items(),
                key=lambda item: (
                    STATE_PRIORITY.get(item[1].get("state", "idle"), 0),
                    float(item[1].get("ts", 0)),
                ),
            )
            self.display_state = winner[1].get("state", "idle")
            if winner[0] == self.agent_id:
                self.display_state = self.state

    # -- persistence ----------------------------------------------------
    def _load_state_file(self) -> None:
        data = _load_json(state_path(), {})
        if not isinstance(data, dict):
            return
        if data.get("port") and not self.config.get("port"):
            self.config["port"] = data["port"]
        if data.get("device") and not self.config.get("device"):
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
            with open(state_path(), "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # -- connection -----------------------------------------------------
    def ensure_connected(self):
        with self._lock:
            if self.client is not None and self.device is not None:
                return self.device
            client = OpenRGBClient(
                host=self.config.get("host", "127.0.0.1"),
                port=self.config.get("port"),
                name="openrgb-mcp",
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

    def _push_frame(self, frame: Sequence[Tuple[int, int, int]]) -> None:
        if self.device is None or self.client is None:
            return
        colors = [RGBColor(r, g, b) for (r, g, b) in frame]
        try:
            self.client.set_device_colors(self.device, colors)
        except (ConnectionFailed, ProtocolError, OSError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._drop_connection()

    # -- rendering ------------------------------------------------------
    def owns_display(self) -> bool:
        """True when this agent is the one whose state the lamp shows."""
        return not self.share_lamp or self.display_state == self.state

    def current_frame(self, tick: Optional[float] = None) -> List[Tuple[int, int, int]]:
        device = self.device
        if device is None:
            return []
        if tick is None:
            tick = time.time() - self.state_since
        mine = self.owns_display()
        frame = renderer.render(
            state=self.display_state,
            led_count=device.led_count,
            tick=tick,
            palette=self.palette,
            color=self.custom_color if mine else None,
            progress=self.progress if mine else None,
            progress_color=self.progress_color if mine else None,
            quota=self.quota if mine else None,
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
        if self.hardware_mode:
            # the controller animates by itself - writing frames would flicker
            return False
        if not self.config.get("animate", True):
            return False
        effect = (self.palette.get(self.display_state) or {}).get("effect", "solid")
        if effect in ("breathe", "pulse", "blink"):
            return True
        return self.owns_display() and (
            self.progress is not None or self.quota is not None
        )

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
        last_display = None
        while not self._stop.is_set():
            try:
                self.ensure_connected()
                self._register()
                self._handle_done_hold()
                if self.display_state != last_display:
                    # another agent took over (or gave the lamp back)
                    last_display = self.display_state
                    self._apply_display()
                elif self._needs_animation():
                    self._render_once()
                time.sleep(interval)
            except Exception as exc:  # keep the loop alive, remember the error
                self.last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(2.0)

    def _apply_display(self) -> None:
        """Render whatever state the shared lamp should currently show."""
        target = self.display_state
        if self._apply_hardware_state(target):
            return
        self.hardware_mode = False
        try:
            self.ensure_per_led()
        except (ConnectionFailed, ProtocolError, OSError):
            return
        self._render_once()

    def _handle_done_hold(self) -> None:
        hold = float(self.config.get("done_hold_seconds") or 0)
        if self.state != "done" or hold <= 0:
            return
        if not self.owns_display():
            return
        if time.time() - self.state_since >= hold:
            self.set_state("idle", detail=None, animate=False)

    # -- state mutation --------------------------------------------------
    def _reset_view(self) -> None:
        self.progress = None
        self.progress_color = None
        self.quota = None
        self.custom_color = None

    # hardware effect preference per state: the controller animates on its
    # own, which is smooth and does not flood the USB link with LED writes
    _HARDWARE_MODES = {
        "thinking": ("Breathing", "Breathe"),
        "working": ("Breathing", "Breathe"),
        "waiting": ("Breathing", "Breathe"),
        "error": ("Breathing", "Breathe"),
        "done": ("Static", "Solid"),
        "idle": ("Static", "Solid"),
        "off": ("Off", "Dark"),
    }

    # effect speed per state (0 = calm .. 255 = fast on most controllers)
    _HARDWARE_SPEED = {
        "thinking": 40,
        "working": 110,
        "waiting": 35,
        "error": 120,
        "done": 0,
        "idle": 0,
        "off": 0,
    }

    def _apply_hardware_state(
        self,
        state: str,
        color: Optional[RGBColor] = None,
        brightness: Optional[int] = None,
        speed: Optional[int] = None,
    ) -> bool:
        """Hand the LEDs to the controller's own effect. True when done."""
        device = self.device
        if device is None or self.client is None:
            return False
        mode = self.client.find_mode(device, self._HARDWARE_MODES.get(state, ()))
        if mode is None:
            return False
        spec = self.palette.get(state) or {}
        if color is None:
            try:
                color = RGBColor.from_hex(spec.get("color", "#ffffff"))
            except ValueError:
                color = RGBColor(255, 255, 255)
        if brightness is None and mode.flags & 0x10:  # HAS_BRIGHTNESS
            brightness = self._level(float(spec.get("brightness", 1.0)))
        if speed is None:
            speed = self._HARDWARE_SPEED.get(state)
        if state == "off":
            color = None
            brightness = 0
        self.client.apply_hardware_effect(
            device, mode, color=color, brightness=brightness, speed=speed
        )
        self.device = self.client.devices[device.index]
        self._last_frame = None
        self.hardware_mode = True
        return True

    def set_state(
        self,
        state: str,
        detail: Optional[str] = None,
        animate: Optional[bool] = None,
    ) -> Dict[str, Any]:
        normalized = STATE_ALIASES.get(
            str(state).strip().lower(), str(state).strip().lower()
        )
        if normalized not in STATES:
            raise ValueError(f"unknown state {state!r}; valid: {', '.join(STATES)}")
        with self._lock:
            self._reset_view()
            self.state = normalized
            self.detail = detail
            self.state_since = time.time()
            if animate is not None:
                self.config["animate"] = bool(animate)
            self.ensure_connected()
            # tell the other agents what we are doing and find out what
            # the shared lamp should show
            self._register()
            self._apply_display()
            # keep the timer thread alive (it handles done -> idle and picks
            # up state changes of other agents); it must not write frames
            # while the hardware animates
            self.start_animation()
            return self.describe()
            # fallback: software animation (per-LED writes)
            self.hardware_mode = False
            self.ensure_per_led()
            self._render_once()
            self.start_animation()
            return self.describe()

    def ensure_per_led(self):
        """Switch the device into a per-LED mode (needed for bars/patterns)."""
        with self._lock:
            device = self.ensure_connected()
            device = self.client.ensure_per_led_mode(device)
            self.device = device
            self.hardware_mode = False
            return device

    # -- global (whole-strip) control ----------------------------------
    def _global(self) -> bool:
        return str(self.config.get("control", "global")).lower() != "per_led"

    def _max_level(self) -> int:
        """Hard ceiling for the brightness we ever request (default 70%)."""
        cap = self.config.get("max_brightness", 0.7)
        try:
            cap = float(cap)
        except (TypeError, ValueError):
            cap = 0.7
        cap = max(0.05, min(1.0, cap))
        return max(4, int(round(cap * 255)))

    def _level(self, fraction: float) -> int:
        """Map a 0..1 fraction onto the capped brightness range."""
        fraction = max(0.0, min(1.0, float(fraction)))
        brightness = float(self.config.get("brightness") or 1.0)
        level = int(round(fraction * brightness * self._max_level()))
        return max(4, min(self._max_level(), level))

    def _apply_global(
        self,
        mode_names: Sequence[str],
        color: RGBColor,
        brightness: int,
        speed: Optional[int] = None,
    ) -> bool:
        """Drive the whole strip through one hardware effect."""
        device = self.device
        if device is None or self.client is None:
            return False
        mode = self.client.find_mode(device, mode_names)
        if mode is None:
            return False
        self.client.apply_hardware_effect(
            device, mode, color=color, brightness=brightness, speed=speed
        )
        self.device = self.client.devices[device.index]
        self._last_frame = None
        self.hardware_mode = True
        return True

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
        if 0.0 < value <= 1.0 and done is None:
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
            self._register()
            if self._global():
                # whole strip: brightness = progress, colour = state colour
                spec = self.palette.get(self.state) or {}
                rgb = RGBColor.from_hex(color) if color else RGBColor.from_hex(
                    spec.get("color", "#00d4ff")
                )
                level = self._level(value / 100.0)
                self.stop_animation()
                if not self._apply_global(
                    ("Breathing", "Breathe", "Static", "Solid"),
                    rgb, level, speed=60,
                ):
                    # device has no usable hardware effect - fall back to LEDs
                    self.ensure_per_led()
                    self._render_once()
            else:
                self.ensure_per_led()
                self.stop_animation()
                self._render_once()
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
        ratio = remaining / total
        with self._lock:
            self._reset_view()
            if self.state in ("idle", "off"):
                self.state = "working"
            self.quota = (int(round(remaining)), int(round(total)))
            self.detail = detail
            self.state_since = time.time()
            self.ensure_connected()
            self._register()
            if self._global():
                # whole strip: brightness = remaining, colour by tier
                rgb = (
                    RGBColor.from_hex("#2ecc71") if ratio > 0.5
                    else RGBColor.from_hex("#ffb703") if ratio > 0.2
                    else RGBColor.from_hex("#ff4d4f")
                )
                level = self._level(ratio)
                self.stop_animation()
                if not self._apply_global(
                    ("Breathing", "Breathe", "Static", "Solid"),
                    rgb, level, speed=60,
                ):
                    self.ensure_per_led()
                    self._render_once()
            else:
                self.ensure_per_led()
                self.stop_animation()
                self._render_once()
            result = self.describe()
            result["remaining"] = remaining
            result["total"] = total
            result["ratio"] = round(ratio, 3)
            result["filled_leds"] = self._filled_leds(ratio)
            return result

    def set_color(self, color: str, brightness: Optional[float] = None) -> Dict[str, Any]:
        parsed = RGBColor.from_hex(color)
        with self._lock:
            self._reset_view()
            self.state = "working"
            self.custom_color = parsed.to_hex()
            if brightness is not None:
                self.config["brightness"] = max(0.0, min(1.0, float(brightness)))
            self.state_since = time.time()
            self.ensure_connected()
            if self._global():
                level = self._level(float(self.config.get("brightness") or 1.0))
                self.stop_animation()
                if not self._apply_global(("Static", "Solid"), parsed, level):
                    self.ensure_per_led()
                    self._render_once()
            else:
                self.ensure_per_led()
                self.stop_animation()
                self._render_once()
            return self.describe()

    def set_leds(self, colors: List[str]) -> Dict[str, Any]:
        if self._global():
            raise ValueError(
                "该设备按整条灯带控制（风扇灯珠位置无法对应），逐灯珠不可用；"
                "请改用 set_color / set_progress / set_quota，"
                "或在 config.json 中把 control 设为 per_led"
            )
        parsed = [RGBColor.from_hex(c) for c in colors]
        with self._lock:
            device = self.ensure_per_led()
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
        """Apply a hardware effect exposed by the device (e.g. 'Static')."""
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
                    "unknown effect; modes available on this device: "
                    + ", ".join(m.name for m in device.modes)
                )
            self.client._apply_mode(device, target)
            device = self.client._refresh_one(device.index)
            self.device = device
            if color and target.per_led:
                rgb = RGBColor.from_hex(color)
                self.client.set_device_colors(device, [rgb] * device.led_count)
            self.state = "working"
            self._reset_view()
            self.state_since = time.time()
            return self.describe()

    def restore(self) -> Dict[str, Any]:
        with self._lock:
            device = self.ensure_per_led()
            self.stop_animation()
            colors = self.original_colors
            if colors and len(colors) == device.led_count:
                self.client.set_device_colors(
                    device, [RGBColor.from_hex(c) for c in colors]
                )
                self._last_frame = [RGBColor.from_hex(c).to_tuple() for c in colors]
            self.state = "idle"
            self._reset_view()
            self.state_since = time.time()
            result = self.describe()
            result["restored"] = bool(colors)
            return result

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
                "render_mode": "hardware" if self.hardware_mode else "per_led",
                "control": self.config.get("control", "global"),
                "display_state": self.display_state,
                "agent_id": self.agent_id,
                "agents": self.active_agents,
                "animate": bool(self.config.get("animate", True)),
                "fps": int(self.config.get("fps") or 12),
                "brightness": float(self.config.get("brightness") or 1.0),
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
            else:
                info["active_mode"] = None
                info["zones"] = []
            return info

    def list_devices(self) -> Dict[str, Any]:
        with self._lock:
            device = self.ensure_connected()
            return {
                "port": self.client.port,
                "protocol_version": self.client.protocol_version,
                "selected": device.name,
                "led_count": device.led_count,
                "devices": [d.to_dict(include_leds=False) for d in self.client.devices],
            }

    def shutdown(self) -> None:
        self.stop_animation()
        with self._lock:
            if self.client is not None:
                self.client.close()
            self.client = None
            # leave the shared registry clean
            if self.share_lamp:
                try:
                    entries = self._read_registry()
                    entries.pop(self.agent_id, None)
                    self._write_registry(entries)
                except OSError:
                    pass
