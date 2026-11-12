"""Status engine: owns the OpenRGB connection and the animation loop.

Framework-free on purpose - the MCP layer (server.py) is a thin wrapper around
this class, so the behaviour can be unit tested without a running server.
"""

from __future__ import annotations

import json
import threading
import time
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

        self._last_frame: Optional[List[Tuple[int, int, int]]] = None
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_error: Optional[str] = None
        self.original_colors: Optional[List[str]] = None

        self._load_state_file()

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
            if self.state in ("idle", "off"):
                self.state = "working"
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
        parsed = RGBColor.from_hex(color)
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
        parsed = [RGBColor.from_hex(c) for c in colors]
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
            device = self.ensure_connected()
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
