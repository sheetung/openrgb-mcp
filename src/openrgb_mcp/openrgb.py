"""OpenRGB SDK client (pure standard library, no third-party dependencies).

Protocol reference: OpenRGB SDK, protocol version 4 (negotiated with the
server - the local OpenRGB build speaks v5, we negotiate down to v4).

Framing:  b"ORGB" + u32 device_index + u32 packet_type + u32 packet_size + data
Colors:   4 bytes per LED, struct layout "BBBx" (r, g, b, pad).

Includes port discovery: OpenRGB can run its SDK server on a non-default
port, and that port is written to the OpenRGB log at startup.
"""

from __future__ import annotations

import glob
import os
import re
import socket
import struct
import threading
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

HEADER = struct.Struct("<4sIII")
MAGIC = b"ORGB"
MAX_PROTOCOL_VERSION = 4

# packet types
P_CONTROLLER_COUNT = 0
P_CONTROLLER_DATA = 1
P_PROTOCOL_VERSION = 40
P_CLIENT_NAME = 50
P_DEVICE_LIST_UPDATED = 100
P_RESIZE_ZONE = 1000
P_UPDATE_LEDS = 1050
P_UPDATE_ZONE_LEDS = 1051
P_UPDATE_SINGLE_LED = 1052
P_SET_CUSTOM_MODE = 1100
P_UPDATE_MODE = 1101
P_SAVE_MODE = 1102

# mode flags
F_HAS_SPEED = 1 << 0
F_HAS_DIRECTION_LR = 1 << 1
F_HAS_DIRECTION_UD = 1 << 2
F_HAS_DIRECTION_HV = 1 << 3
F_HAS_BRIGHTNESS = 1 << 4
F_HAS_PER_LED_COLOR = 1 << 5
F_HAS_MODE_SPECIFIC_COLOR = 1 << 6
F_HAS_RANDOM_COLOR = 1 << 7

ZONE_NAMES = {
    0: "single",
    1: "linear",
    2: "matrix",
    3: "linear_loop",
    4: "matrix_loop_x",
    5: "matrix_loop_y",
    6: "segmented",
}

DEVICE_TYPE_NAMES = {
    0: "motherboard", 1: "dram", 2: "gpu", 3: "cooler", 4: "ledstrip",
    5: "keyboard", 6: "mouse", 7: "mousemat", 8: "headset", 9: "headset_stand",
    10: "gamepad", 11: "light", 12: "speaker", 13: "virtual", 14: "storage",
    15: "case", 16: "microphone", 17: "accessory", 18: "keypad", 19: "unknown",
}


class ProtocolError(Exception):
    """The server sent data we could not parse / that violates the protocol."""


class ConnectionFailed(Exception):
    """Could not establish an OpenRGB SDK connection."""


# --------------------------------------------------------------------------
# colors / models
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RGBColor:
    r: int
    g: int
    b: int

    def __post_init__(self):
        object.__setattr__(self, "r", _clamp(self.r))
        object.__setattr__(self, "g", _clamp(self.g))
        object.__setattr__(self, "b", _clamp(self.b))

    @classmethod
    def from_hex(cls, value: str) -> "RGBColor":
        text = str(value).strip().lstrip("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        if len(text) != 6:
            raise ValueError(f"invalid hex color: {value!r}")
        return cls(int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))

    def to_hex(self) -> str:
        return f"#{self.r:02x}{self.g:02x}{self.b:02x}"

    def to_tuple(self) -> Tuple[int, int, int]:
        return (self.r, self.g, self.b)

    def scaled(self, factor: float) -> "RGBColor":
        f = max(0.0, min(1.0, float(factor)))
        return RGBColor(round(self.r * f), round(self.g * f), round(self.b * f))

    @staticmethod
    def black() -> "RGBColor":
        return RGBColor(0, 0, 0)


def _clamp(value: int) -> int:
    return max(0, min(255, int(value)))


@dataclass
class Segment:
    index: int
    name: str
    segment_type: int
    start_idx: int
    leds_count: int


@dataclass
class Zone:
    index: int
    name: str
    zone_type: int
    leds_min: int
    leds_max: int
    num_leds: int
    start_idx: int = 0
    matrix_height: Optional[int] = None
    matrix_width: Optional[int] = None
    segments: List[Segment] = field(default_factory=list)

    @property
    def type_name(self) -> str:
        return ZONE_NAMES.get(self.zone_type, f"unknown({self.zone_type})")

    @property
    def is_matrix(self) -> bool:
        return self.zone_type in (2, 4, 5)


@dataclass
class Mode:
    index: int
    name: str
    value: int
    flags: int
    speed: int
    brightness: int
    color_mode: int
    num_colors: int
    raw: bytes  # packed payload WITHOUT the leading u32 size field

    @property
    def per_led(self) -> bool:
        return bool(self.flags & F_HAS_PER_LED_COLOR)

    @property
    def packed(self) -> bytes:
        """Full wire payload (u32 size prefix + body)."""
        return struct.pack("<I", len(self.raw) + 4) + self.raw

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "value": self.value,
            "per_led": self.per_led,
            "has_speed": bool(self.flags & F_HAS_SPEED),
            "has_brightness": bool(self.flags & F_HAS_BRIGHTNESS),
            "color_mode": self.color_mode,
        }


@dataclass
class Device:
    index: int
    name: str
    device_type: int
    vendor: str
    description: str
    version: str
    serial: str
    location: str
    zones: List[Zone]
    leds: List[str]
    modes: List[Mode]
    active_mode: int
    colors: List[RGBColor]

    @property
    def type_name(self) -> str:
        return DEVICE_TYPE_NAMES.get(self.device_type, f"unknown({self.device_type})")

    @property
    def led_count(self) -> int:
        return len(self.leds)

    @property
    def total_leds(self) -> int:
        return len(self.leds)

    @property
    def active(self) -> Optional[Mode]:
        if 0 <= self.active_mode < len(self.modes):
            return self.modes[self.active_mode]
        return None

    def to_dict(self, include_leds: bool = False) -> dict:
        data = {
            "index": self.index,
            "name": self.name,
            "type": self.type_name,
            "vendor": self.vendor,
            "description": self.description,
            "location": self.location,
            "led_count": self.led_count,
            "zone_count": len(self.zones),
            "zones": [
                {
                    "index": z.index,
                    "name": z.name,
                    "type": z.type_name,
                    "led_count": z.num_leds,
                    "start_idx": z.start_idx,
                    "matrix": (
                        [z.matrix_height, z.matrix_width] if z.is_matrix else None
                    ),
                    "segments": [
                        {"name": s.name, "start": s.start_idx, "count": s.leds_count}
                        for s in z.segments
                    ],
                }
                for z in self.zones
            ],
            "modes": [m.to_dict() for m in self.modes],
            "active_mode": self.active_mode,
            "active_mode_name": (self.active.name if self.active else None),
            "active_mode_per_led": bool(self.active and self.active.per_led),
        }
        if include_leds:
            data["leds"] = list(self.leds)
        return data


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------
class _Cursor:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def take(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise ProtocolError(
                f"truncated packet: need {size} bytes at offset {self.pos}, "
                f"only {self.remaining()} available"
            )
        chunk = self.data[self.pos:self.pos + size]
        self.pos += size
        return chunk

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def string(self) -> str:
        length = self.u16()
        raw = self.take(length)
        return raw.decode("utf-8", errors="replace").rstrip("\x00")

    def color(self) -> RGBColor:
        r, g, b, _pad = struct.unpack("<BBBB", self.take(4))
        return RGBColor(r, g, b)

    def color_list(self) -> List[RGBColor]:
        return [self.color() for _ in range(self.u16())]


def _parse_mode(cursor: _Cursor, version: int, index: int) -> Mode:
    # In REQUEST_CONTROLLER_DATA responses the mode carries neither the u32
    # size prefix nor the i32 mode id: those exist only in the UPDATEMODE
    # payload (see Mode.packed).
    start = cursor.pos
    name = cursor.string()
    value = cursor.i32()
    flags = cursor.u32()
    cursor.u32()  # speed_min
    cursor.u32()  # speed_max
    if version >= 3:
        cursor.u32()  # brightness_min
        cursor.u32()  # brightness_max
    cursor.u32()  # colors_min
    cursor.u32()  # colors_max
    speed = cursor.u32()
    brightness = cursor.u32() if version >= 3 else 0
    cursor.u32()  # direction
    color_mode = cursor.u32()
    num_colors = cursor.u16()
    for _ in range(num_colors):
        cursor.color()
    raw = struct.pack("<i", index) + cursor.data[start:cursor.pos]
    return Mode(
        index=index,
        name=name,
        value=value,
        flags=flags,
        speed=speed,
        brightness=brightness,
        color_mode=color_mode,
        num_colors=num_colors,
        raw=raw,
    )


def _parse_zone(cursor: _Cursor, version: int, index: int) -> Zone:
    name = cursor.string()
    zone_type = cursor.i32()
    leds_min = cursor.u32()
    leds_max = cursor.u32()
    num_leds = cursor.u32()
    matrix_size = cursor.u16()
    height = width = None
    if matrix_size > 0:
        height = cursor.u32()
        width = cursor.u32()
        cursor.take(height * width * 4)
    segments: List[Segment] = []
    if version >= 4:
        for seg_index in range(cursor.u16()):
            seg_name = cursor.string()
            seg_type = cursor.i32()
            seg_start = cursor.u32()
            seg_count = cursor.u32()
            segments.append(
                Segment(seg_index, seg_name, seg_type, seg_start, seg_count)
            )
    return Zone(
        index=index,
        name=name,
        zone_type=zone_type,
        leds_min=leds_min,
        leds_max=leds_max,
        num_leds=num_leds,
        matrix_height=height,
        matrix_width=width,
        segments=segments,
    )


def parse_controller_data(data: bytes, version: int) -> Device:
    cursor = _Cursor(data)
    size = cursor.u32()
    # the leading size field counts itself (size == len(data))
    if size != len(data):
        raise ProtocolError(
            f"controller data size mismatch: field says {size}, got {len(data)}"
        )
    device_type = cursor.i32()
    name = cursor.string()
    vendor = cursor.string() if version >= 1 else ""
    description = cursor.string()
    fw_version = cursor.string()
    serial = cursor.string()
    location = cursor.string()
    num_modes = cursor.u16()
    active_mode = cursor.i32()
    modes = [_parse_mode(cursor, version, i) for i in range(num_modes)]
    zones = [_parse_zone(cursor, version, i) for i in range(cursor.u16())]
    leds = []
    for _ in range(cursor.u16()):
        leds.append(cursor.string())
        cursor.u32()
    colors = cursor.color_list()
    if cursor.remaining() != 0:
        raise ProtocolError(f"{cursor.remaining()} trailing bytes in controller data")
    start = 0
    for zone in zones:
        zone.start_idx = start
        start += zone.num_leds
    return Device(
        index=0,
        name=name,
        device_type=device_type,
        vendor=vendor,
        description=description,
        version=fw_version,
        serial=serial,
        location=location,
        zones=zones,
        leds=leds,
        modes=modes,
        active_mode=active_mode,
        colors=colors,
    )


# --------------------------------------------------------------------------
# port discovery
# --------------------------------------------------------------------------
def _log_port() -> Optional[int]:
    log_dirs = [
        os.path.join(os.environ.get("APPDATA", ""), "OpenRGB", "logs"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "OpenRGB", "logs"),
    ]
    candidates: List[str] = []
    for directory in log_dirs:
        if directory and os.path.isdir(directory):
            candidates.extend(glob.glob(os.path.join(directory, "*.log")))
    for path in sorted(candidates, key=os.path.getmtime, reverse=True):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                match = re.search(
                    r"Network connection thread started on port (\d+)",
                    handle.read(),
                )
            if match:
                return int(match.group(1))
        except OSError:
            continue
    return None


def rpc_probe(host: str, port: int, timeout: float = 1.5) -> Optional[int]:
    """Return the server protocol version if this port speaks OpenRGB SDK."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        sock.sendall(HEADER.pack(MAGIC, 0, P_PROTOCOL_VERSION, 4)
                     + struct.pack("<I", MAX_PROTOCOL_VERSION))
        header = _recv_exact(sock, 16)
        magic, _dev, packet_type, size = HEADER.unpack(header)
        if magic != MAGIC or packet_type != P_PROTOCOL_VERSION:
            return None
        body = _recv_exact(sock, size)
        return struct.unpack("<I", body[:4])[0] if len(body) >= 4 else None
    except (OSError, struct.error):
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def discover_port(host: str = "127.0.0.1") -> Optional[int]:
    """Find the SDK port: OPENRGB_PORT env, the port in the OpenRGB log, 6742."""
    candidates: List[int] = []
    env_port = os.environ.get("OPENRGB_PORT", "").strip()
    if env_port.isdigit():
        candidates.append(int(env_port))
    hinted = _log_port()
    if hinted:
        candidates.append(hinted)
    candidates.append(6742)
    seen = set()
    for port in candidates:
        if port in seen:
            continue
        seen.add(port)
        if rpc_probe(host, port) is not None:
            return port
    return None


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    buffer = b""
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            raise ConnectionError("connection closed by OpenRGB SDK server")
        buffer += chunk
    return buffer


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------
class OpenRGBClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: Optional[int] = None,
        name: str = "dsh-openrgb-status",
        timeout: float = 3.0,
    ):
        self.host = host
        self.port = port
        self.name = name
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.protocol_version = MAX_PROTOCOL_VERSION
        self.devices: List[Device] = []
        self._lock = threading.RLock()

    # -- connection -----------------------------------------------------
    def connect(self) -> "OpenRGBClient":
        with self._lock:
            if self.sock is not None:
                return self
            port = self.port
            if port is None:
                port = discover_port(self.host)
                if port is None:
                    raise ConnectionFailed(
                        "OpenRGB SDK server not found (tried OPENRGB_PORT, the "
                        "port recorded in %APPDATA%\\OpenRGB\\logs and 6742). "
                        "Start OpenRGB and make sure 'Start Server on Launch' "
                        "is enabled in its SDK Server settings."
                    )
                self.port = port
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            try:
                sock.connect((self.host, port))
            except OSError as exc:
                sock.close()
                raise ConnectionFailed(
                    f"cannot connect to OpenRGB SDK at {self.host}:{port}: {exc}"
                ) from exc
            self.sock = sock
            try:
                self._negotiate()
                self.refresh()
            except Exception:
                self.close()
                raise
            return self

    def _negotiate(self) -> None:
        self._send(0, P_PROTOCOL_VERSION, struct.pack("<I", MAX_PROTOCOL_VERSION))
        _magic, _dev, packet_type, body = self._recv_packet(
            expected=(P_PROTOCOL_VERSION,)
        )
        server_max = struct.unpack("<I", body[:4])[0]
        self.protocol_version = min(MAX_PROTOCOL_VERSION, server_max)
        if self.protocol_version < 1:
            raise ProtocolError(
                f"unsupported OpenRGB SDK protocol version {self.protocol_version}"
            )
        name_bytes = self.name.encode("utf-8") + b"\x00"
        self._send(0, P_CLIENT_NAME, name_bytes)

    def close(self) -> None:
        with self._lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None

    def __enter__(self) -> "OpenRGBClient":
        return self.connect()

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- low level ------------------------------------------------------
    def _send(self, device_index: int, packet_type: int, data: bytes) -> None:
        if self.sock is None:
            raise ConnectionFailed("not connected")
        self.sock.sendall(
            HEADER.pack(MAGIC, device_index, packet_type, len(data)) + data
        )

    def _recv_packet(self, expected: Sequence[int] = ()) -> Tuple[bytes, int, int, bytes]:
        while True:
            header = _recv_exact(self.sock, 16)
            magic, device_index, packet_type, size = HEADER.unpack(header)
            if magic != MAGIC:
                raise ProtocolError(f"bad signature {magic!r}")
            body = _recv_exact(self.sock, size) if size else b""
            if packet_type == P_DEVICE_LIST_UPDATED:
                # unsolicited notification, ignore (caller can refresh later)
                continue
            if expected and packet_type not in expected:
                raise ProtocolError(
                    f"unexpected packet type {packet_type}, expected {expected}"
                )
            return magic, device_index, packet_type, body

    # -- device data ----------------------------------------------------
    def refresh(self) -> List[Device]:
        with self._lock:
            self._send(0, P_CONTROLLER_COUNT, b"")
            _m, _d, _t, body = self._recv_packet(expected=(P_CONTROLLER_COUNT,))
            count = struct.unpack("<I", body[:4])[0]
            devices: List[Device] = []
            for index in range(count):
                self._send(
                    index,
                    P_CONTROLLER_DATA,
                    struct.pack("<I", self.protocol_version),
                )
                _m, device_index, packet_type, body = self._recv_packet(
                    expected=(P_CONTROLLER_DATA,)
                )
                device = parse_controller_data(body, self.protocol_version)
                device.index = device_index if device_index == index else index
                devices.append(device)
            self.devices = devices
            return devices

    @staticmethod
    def _pick(devices: Sequence[Device], name_part: Optional[str]) -> Optional[Device]:
        if not devices:
            return None
        if name_part:
            needle = name_part.lower()
            for device in devices:
                blob = " ".join(
                    [device.name, device.vendor, device.description, device.location]
                ).lower()
                if needle in blob:
                    return device
            return None
        return devices[0]

    def find_device(self, name_part: Optional[str] = None) -> Device:
        device = self._pick(self.devices, name_part)
        if device is None:
            raise ProtocolError(
                f"no OpenRGB device matching {name_part!r}; available: "
                + ", ".join(repr(d.name) for d in self.devices)
            )
        return device

    # -- mode control ---------------------------------------------------
    def ensure_per_led_mode(
        self, device: Device, mode_name: Optional[str] = None
    ) -> Device:
        """Make sure the device is in a mode that accepts per-LED colors."""
        with self._lock:
            active = device.active
            if active is not None and active.per_led:
                return device
            self._send(device.index, P_SET_CUSTOM_MODE, b"")
            device = self._refresh_one(device.index)
            active = device.active
            if active is not None and active.per_led:
                self._apply_mode(device, active)
                return device
            # fall back: pick a mode that supports per-LED colors explicitly
            target = None
            for mode in device.modes:
                if not mode.per_led:
                    continue
                if mode_name and mode_name.lower() in mode.name.lower():
                    target = mode
                    break
                if target is None or "direct" in mode.name.lower():
                    target = mode
            if target is None:
                raise ProtocolError(
                    f"device {device.name!r} has no per-LED mode; "
                    f"modes: {[m.name for m in device.modes]}"
                )
            self._apply_mode(device, target)
            device = self._refresh_one(device.index)
            return device

    def _refresh_one(self, index: int) -> Device:
        with self._lock:
            self._send(index, P_CONTROLLER_DATA, struct.pack("<I", self.protocol_version))
            _m, device_index, _t, body = self._recv_packet(expected=(P_CONTROLLER_DATA,))
            device = parse_controller_data(body, self.protocol_version)
            device.index = device_index
            self.devices[index] = device
            return device

    def _apply_mode(self, device: Device, mode: Mode) -> None:
        with self._lock:
            self._send(device.index, P_UPDATE_MODE, mode.packed)

    # -- colors ---------------------------------------------------------
    def set_device_colors(
        self, device: Device, colors: Sequence[RGBColor], refresh: bool = False
    ) -> None:
        """Set every LED of the device at once (device must be per-LED)."""
        with self._lock:
            if len(colors) != device.led_count:
                raise ValueError(
                    f"{len(colors)} colors for {device.led_count} LEDs on "
                    f"{device.name!r}"
                )
            payload = struct.pack("<H", len(colors)) + b"".join(
                struct.pack("<BBBx", c.r, c.g, c.b) for c in colors
            )
            data = struct.pack("<I", len(payload) + 4) + payload
            self._send(device.index, P_UPDATE_LEDS, data)
            if refresh:
                self._refresh_one(device.index)

    def set_zone_colors(
        self, device: Device, zone: Zone, colors: Sequence[RGBColor]
    ) -> None:
        with self._lock:
            if len(colors) != zone.num_leds:
                raise ValueError(
                    f"{len(colors)} colors for zone {zone.name!r} with "
                    f"{zone.num_leds} LEDs"
                )
            payload = (
                struct.pack("<iH", zone.index, len(colors))
                + b"".join(struct.pack("<BBBx", c.r, c.g, c.b) for c in colors)
            )
            data = struct.pack("<I", len(payload) + 4) + payload
            self._send(device.index, P_UPDATE_ZONE_LEDS, data)

    def set_all_colors(
        self,
        colors: Sequence[RGBColor],
        device_name_part: Optional[str] = None,
    ) -> Device:
        """Convenience: ensure per-LED mode on the target device, push colors."""
        device = self.find_device(device_name_part)
        device = self.ensure_per_led_mode(device)
        if len(colors) == 1:
            colors = list(colors) * device.led_count
        self.set_device_colors(device, list(colors))
        return device

    # -- misc -----------------------------------------------------------
    def save_mode(self, device: Device) -> None:
        active = device.active
        if active is None:
            raise ProtocolError("device has no active mode")
        with self._lock:
            self._send(device.index, P_SAVE_MODE, active.packed)


# --------------------------------------------------------------------------
# CLI: python openrgb_client.py [--port N] [--json] [--list] [--color RRGGBB]
# --------------------------------------------------------------------------
def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="OpenRGB SDK probe / controller")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--device", default=None, help="device name substring")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--list", action="store_true", help="list devices (default)")
    parser.add_argument("--color", default=None, help="set all LEDs to #RRGGBB")
    args = parser.parse_args(argv)

    try:
        client = OpenRGBClient(port=args.port).connect()
    except (ConnectionFailed, ProtocolError) as exc:
        print(f"ERROR: {exc}")
        return 2
    try:
        if args.json:
            print(json.dumps(
                {
                    "host": client.host,
                    "port": client.port,
                    "protocol_version": client.protocol_version,
                    "devices": [d.to_dict(include_leds=False) for d in client.devices],
                },
                ensure_ascii=False,
                indent=2,
            ))
        else:
            print(f"connected to {client.host}:{client.port} "
                  f"(protocol v{client.protocol_version}), "
                  f"{len(client.devices)} device(s)")
            for device in client.devices:
                print(f"  [{device.index}] {device.name}  ({device.type_name})")
                print(f"      vendor={device.vendor!r} location={device.location!r}")
                print(f"      LEDs={device.led_count}  zones={len(device.zones)}  "
                      f"modes={len(device.modes)}  active_mode="
                      f"{device.active.name if device.active else None!r}")
                for zone in device.zones:
                    print(f"      zone[{zone.index}] {zone.name!r} "
                          f"{zone.type_name} leds={zone.num_leds} "
                          f"start={zone.start_idx}"
                          + (f" matrix={zone.matrix_height}x{zone.matrix_width}"
                             if zone.is_matrix else ""))
        if args.color:
            color = RGBColor.from_hex(args.color)
            device = client.set_all_colors([color], args.device)
            print(f"set {device.led_count} LEDs of {device.name!r} to {color.to_hex()}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(_main())