"""Mock OpenRGB SDK TCP server for protocol tests (standard library only).

The mock implements the subset of the OpenRGB SDK network protocol described in
``openrgb-mcp/CONTRACT.md`` section 2, wire-compatible with the real OpenRGB
server (verified byte-for-byte against a live server for the framing and
``data_size`` conventions listed below):

* frame: ``b"ORGB" + u32 device_index + u32 packet_type + u32 packet_size + data``
* server advertises protocol version 5, the tests negotiate down to v4
  (client layout from CONTRACT.md section 2.1)
* ``str`` = ``u16 (len + 1)`` + utf-8 bytes + b"\\0"
* controller data ``data_size`` = *total* length of the data block including the
  4-byte size field itself (measured on the real server: 6985 == 6985)
* mode data ``size`` = length of everything after the size field + 4

Features
--------
* two configurable devices by default: a 4x4 matrix keyboard with segments and
  several modes (a per-LED ``Direct`` mode among them) and a simple linear LED
  strip
* every received packet is recorded (header bytes, device index, packet type,
  declared packet size and raw data) so tests can assert on the wire format
* fault injection: refused / aborted connections, delayed responses (client
  timeout), truncated ("half") responses, corrupt controller ``data_size``,
  dropped connections after N packets, and optional ``DEVICE_LIST_UPDATED``
  pushes before responses

Typical use::

    with MockServer() as srv:
        client = OpenRGBClient(port=srv.port).connect()
        ...
        assert srv.received_types() == [40, 50, 0, 1]

Run this file directly for a self-test of the mock itself::

    python tests/mock_openrgb.py
"""

from __future__ import annotations

import socket
import struct
import threading
import time

# ---------------------------------------------------------------------------
# Packet types (OpenRGB SDK)
# ---------------------------------------------------------------------------
REQUEST_CONTROLLER_COUNT = 0
REQUEST_CONTROLLER_DATA = 1
REQUEST_PROTOCOL_VERSION = 40
SET_CLIENT_NAME = 50
DEVICE_LIST_UPDATED = 100
RGBCONTROLLER_RESIZEZONE = 1000
RGBCONTROLLER_UPDATELEDS = 1050
RGBCONTROLLER_UPDATEZONELEDS = 1051
RGBCONTROLLER_SETCUSTOMMODE = 1100
RGBCONTROLLER_UPDATEMODE = 1101

MAGIC = b"ORGB"
HEADER_SIZE = 16

SERVER_PROTOCOL_VERSION = 5
CLIENT_PROTOCOL_VERSION = 4  # layout the client under test speaks

# Device types (only relevant for device_type assertions)
DEVICE_TYPE_MOTHERBOARD = 0
DEVICE_TYPE_LEDSTRIP = 4
DEVICE_TYPE_KEYBOARD = 5

# Zone types
ZONE_TYPE_SINGLE = 0
ZONE_TYPE_LINEAR = 1
ZONE_TYPE_MATRIX = 2
ZONE_TYPE_LINEAR_LOOP = 3

# Mode flags (CONTRACT.md section 2.2)
MODE_FLAG_HAS_SPEED = 1 << 0
MODE_FLAG_HAS_DIRECTION_LR = 1 << 1
MODE_FLAG_HAS_DIRECTION_UD = 1 << 2
MODE_FLAG_HAS_DIRECTION_HV = 1 << 3
MODE_FLAG_HAS_BRIGHTNESS = 1 << 4
MODE_FLAG_HAS_PER_LED_COLOR = 1 << 5
MODE_FLAG_HAS_MODE_SPECIFIC_COLOR = 1 << 6
MODE_FLAG_HAS_RANDOM_COLOR = 1 << 7

# Color modes
COLOR_MODE_NONE = 0
COLOR_MODE_PER_LED = 1
COLOR_MODE_MODE_SPECIFIC = 2
COLOR_MODE_RANDOM = 3

MATRIX_EMPTY_SLOT = 0xFFFFFFFF


class _CloseConnection(Exception):
    """Internal: the handler must drop the connection after a fault."""


# ---------------------------------------------------------------------------
# Wire packing helpers
# ---------------------------------------------------------------------------
def pack_str(value):
    """Pack an OpenRGB string: u16 length (incl. NUL) + utf-8 + b"\\0"."""
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return struct.pack("<H", len(raw) + 1) + raw + b"\x00"


def pack_color(r, g, b):
    """Pack one 4-byte OpenRGB color: ``struct.pack("BBBx", r, g, b)``."""
    return struct.pack("BBBx", int(r), int(g), int(b))


def pack_frame(device_index, packet_type, data=b""):
    """Build a complete OpenRGB frame (header + data)."""
    return (
        MAGIC
        + struct.pack("<III", int(device_index), int(packet_type), len(data))
        + data
    )


def unpack_header(header):
    """Return ``(magic, device_index, packet_type, packet_size)`` from 16 bytes."""
    if len(header) != HEADER_SIZE:
        raise ValueError("header must be 16 bytes")
    magic = header[:4]
    device_index, packet_type, packet_size = struct.unpack("<III", header[4:])
    return magic, device_index, packet_type, packet_size


def pack_mode_body(mode, protocol_version=CLIENT_PROTOCOL_VERSION):
    """Serialize a mode body *without* size/id prefix (as embedded in controller data).

    Measured against a live OpenRGB v5 server (requesting v4 data): controller
    data modes start with the ``str`` name -- there is no ``i32 mode_id`` and no
    leading ``u32 size`` field there.  Only the standalone UPDATEMODE payload
    carries the size + id prefix (see :func:`pack_mode_packet`).
    """
    body = pack_str(mode["name"])
    body += struct.pack("<i", mode["value"])
    body += struct.pack("<I", mode["flags"])
    body += struct.pack("<II", mode["speed_min"], mode["speed_max"])
    if protocol_version >= 3:
        body += struct.pack("<II", mode["brightness_min"], mode["brightness_max"])
    body += struct.pack("<II", mode["colors_min"], mode["colors_max"])
    body += struct.pack("<I", mode["speed"])
    if protocol_version >= 3:
        body += struct.pack("<I", mode["brightness"])
    body += struct.pack("<I", mode["direction"])
    body += struct.pack("<I", mode["color_mode"])
    colors = mode.get("colors", [])
    body += struct.pack("<H", len(colors))
    for r, g, b in colors:
        body += pack_color(r, g, b)
    return body


def pack_mode_packet(mode, index, protocol_version=CLIENT_PROTOCOL_VERSION):
    """Serialize a standalone UPDATEMODE (1101) payload: u32 size + i32 index + body."""
    body = pack_mode_body(mode, protocol_version)
    return struct.pack("<Ii", len(body) + 8, index) + body


def pack_zone_body(zone, protocol_version=CLIENT_PROTOCOL_VERSION):
    """Serialize ZoneData (v4 layout; segments only at protocol >= 4)."""
    body = pack_str(zone["name"])
    body += struct.pack("<i", zone["zone_type"])
    body += struct.pack(
        "<III", zone["leds_min"], zone["leds_max"], zone["num_leds"]
    )
    matrix = zone.get("matrix")
    if matrix:
        height, width, entries = matrix
        body += struct.pack("<H", height * width)
        body += struct.pack("<II", height, width)
        for entry in entries:
            body += struct.pack("<I", entry)
    else:
        body += struct.pack("<H", 0)
    if protocol_version >= 4:
        segments = zone.get("segments", [])
        body += struct.pack("<H", len(segments))
        for segment in segments:
            body += pack_str(segment["name"])
            body += struct.pack(
                "<iII",
                segment["type"],
                segment["start_idx"],
                segment["leds_count"],
            )
    return body


def pack_controller_data(device, protocol_version=CLIENT_PROTOCOL_VERSION, data_size_delta=0):
    """Serialize a controller data response.

    ``data_size`` counts the whole data block including itself (matches the real
    server); ``data_size_delta`` corrupts it to provoke ``ProtocolError`` tests.
    """
    body = struct.pack("<i", device["device_type"])
    body += pack_str(device["name"])
    # MetaData (protocol >= 1)
    body += pack_str(device.get("vendor", ""))
    body += pack_str(device.get("description", ""))
    body += pack_str(device.get("version", ""))
    body += pack_str(device.get("serial", ""))
    body += pack_str(device.get("location", ""))

    modes = device.get("modes", [])
    body += struct.pack("<H", len(modes))
    body += struct.pack("<i", device.get("active_mode", 0))
    for mode in modes:
        body += pack_mode_body(mode, protocol_version)

    zones = device.get("zones", [])
    body += struct.pack("<H", len(zones))
    for zone in zones:
        body += pack_zone_body(zone, protocol_version)

    leds = device.get("leds", [])
    body += struct.pack("<H", len(leds))
    for led in leds:
        body += pack_str(led["name"])
        body += struct.pack("<I", led["value"])

    colors = device.get("colors", [])
    body += struct.pack("<H", len(colors))
    for r, g, b in colors:
        body += pack_color(r, g, b)

    data_size = len(body) + 4 + data_size_delta
    return struct.pack("<I", data_size) + body


# ---------------------------------------------------------------------------
# Reference parser (used for the mock self-test / cross-checks)
# ---------------------------------------------------------------------------
def parse_controller_data(data):
    """Parse controller data bytes into plain dicts, enforcing exact data_size."""
    offset = 0
    declared = struct.unpack_from("<I", data, offset)[0]
    if declared != len(data):
        raise ValueError(
            "data_size mismatch: declared %d, actual %d" % (declared, len(data))
        )
    offset += 4
    device = {}
    device["device_type"] = struct.unpack_from("<i", data, offset)[0]
    offset += 4
    device["name"], offset = _read_str(data, offset)
    for key in ("vendor", "description", "version", "serial", "location"):
        device[key], offset = _read_str(data, offset)

    count = struct.unpack_from("<H", data, offset)[0]
    offset += 2
    device["active_mode"] = struct.unpack_from("<i", data, offset)[0]
    offset += 4
    modes = []
    for index in range(count):
        mode = {"index": index}
        mode["name"], offset = _read_str(data, offset)
        mode["value"] = struct.unpack_from("<i", data, offset)[0]
        offset += 4
        mode["flags"] = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        mode["speed_min"], mode["speed_max"] = struct.unpack_from("<II", data, offset)
        offset += 8
        mode["brightness_min"], mode["brightness_max"] = struct.unpack_from(
            "<II", data, offset
        )
        offset += 8
        mode["colors_min"], mode["colors_max"] = struct.unpack_from("<II", data, offset)
        offset += 8
        mode["speed"] = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        mode["brightness"] = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        mode["direction"] = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        mode["color_mode"] = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        ncolors = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        mode["colors"] = []
        for _ in range(ncolors):
            mode["colors"].append(struct.unpack_from("<BBBx", data, offset))
            offset += 4
        modes.append(mode)
    device["modes"] = modes

    count = struct.unpack_from("<H", data, offset)[0]
    offset += 2
    zones = []
    for _ in range(count):
        zone = {}
        zone["name"], offset = _read_str(data, offset)
        zone["zone_type"] = struct.unpack_from("<i", data, offset)[0]
        offset += 4
        zone["leds_min"], zone["leds_max"], zone["num_leds"] = struct.unpack_from(
            "<III", data, offset
        )
        offset += 12
        matrix_size = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        zone["matrix"] = None
        if matrix_size > 0:
            height, width = struct.unpack_from("<II", data, offset)
            offset += 8
            entries = list(struct.unpack_from("<%dI" % (height * width), data, offset))
            offset += 4 * height * width
            zone["matrix"] = {"height": height, "width": width, "entries": entries}
        nsegments = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        zone["segments"] = []
        for _ in range(nsegments):
            segment = {}
            segment["name"], offset = _read_str(data, offset)
            segment["type"], segment["start_idx"], segment["leds_count"] = (
                struct.unpack_from("<iII", data, offset)
            )
            offset += 12
            zone["segments"].append(segment)
        zones.append(zone)
    device["zones"] = zones

    count = struct.unpack_from("<H", data, offset)[0]
    offset += 2
    leds = []
    for _ in range(count):
        name, offset = _read_str(data, offset)
        value = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        leds.append({"name": name, "value": value})
    device["leds"] = leds

    count = struct.unpack_from("<H", data, offset)[0]
    offset += 2
    colors = []
    for _ in range(count):
        colors.append(struct.unpack_from("<BBBx", data, offset))
        offset += 4
    device["colors"] = colors

    if offset != len(data):
        raise ValueError(
            "controller data over-read: consumed %d of %d bytes" % (offset, len(data))
        )
    return device


def _read_str(data, offset):
    length = struct.unpack_from("<H", data, offset)[0]
    offset += 2
    raw = data[offset : offset + length]
    if len(raw) != length:
        raise ValueError("truncated string")
    if not raw.endswith(b"\x00"):
        raise ValueError("string is not NUL terminated")
    return raw[:-1].decode("utf-8"), offset + length


# ---------------------------------------------------------------------------
# Payload parsers for packets the client sends (used by tests)
# ---------------------------------------------------------------------------
def parse_led_packet(data):
    """Parse an UPDATELEDS (1050) payload.

    Returns ``(total_size, [(r, g, b), ...])``.  ``total_size`` must equal
    ``len(data)`` per the contract, which is what the tests assert.
    """
    total_size = struct.unpack_from("<I", data, 0)[0]
    count = struct.unpack_from("<H", data, 4)[0]
    colors = []
    offset = 6
    for _ in range(count):
        colors.append(struct.unpack_from("<BBBx", data, offset))
        offset += 4
    if offset != len(data):
        raise ValueError(
            "UPDATELEDS payload does not match count: %d vs %d bytes" % (count, len(data))
        )
    return total_size, colors


def parse_zone_led_packet(data):
    """Parse an UPDATEZONELEDS (1051) payload.

    Returns ``(total_size, zone_index, [(r, g, b), ...])``.
    """
    total_size = struct.unpack_from("<I", data, 0)[0]
    zone_index = struct.unpack_from("<i", data, 4)[0]
    count = struct.unpack_from("<H", data, 8)[0]
    colors = []
    offset = 10
    for _ in range(count):
        colors.append(struct.unpack_from("<BBBx", data, offset))
        offset += 4
    if offset != len(data):
        raise ValueError(
            "UPDATEZONELEDS payload does not match count: %d vs %d bytes"
            % (count, len(data))
        )
    return total_size, zone_index, colors


def parse_mode_packet(data):
    """Parse an UPDATEMODE (1101) payload: ``u32 size + i32 index + mode body``.

    The contract calls the ``i32`` after the size field ``mode_id``; the client
    stores ``Mode.index`` there, so both names are exposed.
    """
    size = struct.unpack_from("<I", data, 0)[0]
    if size != len(data):
        raise ValueError("mode size mismatch: declared %d, actual %d" % (size, len(data)))
    mode = {}
    offset = 4
    mode["index"] = struct.unpack_from("<i", data, offset)[0]
    mode["mode_id"] = mode["index"]
    offset += 4
    mode["name"], offset = _read_str(data, offset)
    mode["value"] = struct.unpack_from("<i", data, offset)[0]
    offset += 4
    mode["flags"] = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    mode["speed_min"], mode["speed_max"] = struct.unpack_from("<II", data, offset)
    offset += 8
    mode["brightness_min"], mode["brightness_max"] = struct.unpack_from(
        "<II", data, offset
    )
    offset += 8
    mode["colors_min"], mode["colors_max"] = struct.unpack_from("<II", data, offset)
    offset += 8
    mode["speed"] = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    mode["brightness"] = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    mode["direction"] = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    mode["color_mode"] = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    count = struct.unpack_from("<H", data, offset)[0]
    offset += 2
    colors = []
    for _ in range(count):
        colors.append(struct.unpack_from("<BBBx", data, offset))
        offset += 4
    mode["colors"] = colors
    if offset != len(data):
        raise ValueError("mode payload over-read")
    return mode


# ---------------------------------------------------------------------------
# Default device fixtures
# ---------------------------------------------------------------------------
def _mode(
    name,
    value=0,
    flags=0,
    speed_min=0,
    speed_max=0,
    brightness_min=0,
    brightness_max=100,
    colors_min=0,
    colors_max=0,
    speed=0,
    brightness=100,
    direction=0,
    color_mode=COLOR_MODE_NONE,
    colors=(),
):
    # No mode_id: a controller-data mode is identified by its list index only
    # (verified against the real v5 server at protocol version 4).
    return {
        "name": name,
        "name": name,
        "value": value,
        "flags": flags,
        "speed_min": speed_min,
        "speed_max": speed_max,
        "brightness_min": brightness_min,
        "brightness_max": brightness_max,
        "colors_min": colors_min,
        "colors_max": colors_max,
        "speed": speed,
        "brightness": brightness,
        "direction": direction,
        "color_mode": color_mode,
        "colors": list(colors),
    }


def _zone(
    name,
    zone_type,
    num_leds,
    leds_min=0,
    leds_max=None,
    matrix=None,
    segments=(),
):
    return {
        "name": name,
        "zone_type": zone_type,
        "leds_min": leds_min,
        "leds_max": num_leds if leds_max is None else leds_max,
        "num_leds": num_leds,
        "matrix": matrix,
        "segments": list(segments),
    }


def _device(
    device_type,
    name,
    vendor,
    description,
    version,
    serial,
    location,
    zones,
    modes,
    leds,
    colors,
    active_mode=0,
):
    return {
        "device_type": device_type,
        "name": name,
        "vendor": vendor,
        "description": description,
        "version": version,
        "serial": serial,
        "location": location,
        "zones": zones,
        "modes": modes,
        "leds": leds,
        "colors": colors,
        "active_mode": active_mode,
    }


def default_devices():
    """Two devices: 4x4 matrix keyboard (segments + modes) and a linear strip.

    Keyboard details intentionally exercise every v4 field the client must parse:

    * ``data_size`` / ``device_type`` / ``name`` / MetaData strings
    * 3 modes, ``active_mode`` = 1 (Breathing), so switching to the per-LED
      ``Direct`` mode exercises SETCUSTOMMODE -> UPDATEMODE
    * one matrix zone (4x4, one ``0xFFFFFFFF`` empty slot) with 3 segments
    * 16 LEDs and 16 distinct initial colors
    """
    keyboard_modes = [
        _mode(
            "Direct",
            value=0,
            flags=MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_HAS_BRIGHTNESS,
            brightness=100,
            color_mode=COLOR_MODE_PER_LED,
        ),
        _mode(
            "Breathing",
            value=1,
            flags=(
                MODE_FLAG_HAS_SPEED
                | MODE_FLAG_HAS_BRIGHTNESS
                | MODE_FLAG_HAS_MODE_SPECIFIC_COLOR
            ),
            speed_min=0,
            speed_max=100,
            speed=30,
            brightness=80,
            color_mode=COLOR_MODE_MODE_SPECIFIC,
            colors=[(255, 0, 0)],
        ),
        _mode("Off", value=2, flags=0, brightness=0),
    ]
    keyboard_matrix = (
        4,
        4,
        [
            0,
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            MATRIX_EMPTY_SLOT,
            11,
            12,
            13,
            14,
            15,
        ],
    )
    keyboard_zone = _zone(
        "Keyboard Matrix",
        ZONE_TYPE_MATRIX,
        16,
        matrix=keyboard_matrix,
        segments=[
            {"name": "Left Half", "type": ZONE_TYPE_LINEAR, "start_idx": 0, "leds_count": 8},
            {"name": "Right Half", "type": ZONE_TYPE_LINEAR, "start_idx": 8, "leds_count": 8},
            {"name": "Logo", "type": ZONE_TYPE_MATRIX, "start_idx": 12, "leds_count": 4},
        ],
    )
    keyboard = _device(
        DEVICE_TYPE_KEYBOARD,
        "Mock Keyboard",
        "MockVendor",
        "Mock 4x4 ARGB Keyboard",
        "1.0",
        "SN-KBD-0001",
        "HID: mock keyboard",
        [keyboard_zone],
        keyboard_modes,
        [{"name": "KBD LED %d" % (i + 1), "value": i} for i in range(16)],
        [(i * 15, 255 - i * 15, (i * 37) % 256) for i in range(16)],
        active_mode=1,
    )

    strip = _device(
        DEVICE_TYPE_LEDSTRIP,
        "Mock Strip",
        "MockVendor",
        "Mock 10 LED Strip",
        "1.0",
        "SN-STR-0002",
        "USB: mock strip",
        [_zone("Strip Zone", ZONE_TYPE_LINEAR, 10)],
        [
            _mode(
                "Direct",
                flags=MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_HAS_BRIGHTNESS,
                color_mode=COLOR_MODE_PER_LED,
            )
        ],
        [{"name": "Strip LED %d" % (i + 1), "value": i} for i in range(10)],
        [((i * 20) % 256, (i * 40) % 256, 255 - i * 10) for i in range(10)],
        active_mode=0,
    )
    return [keyboard, strip]


# ---------------------------------------------------------------------------
# Mock server
# ---------------------------------------------------------------------------
class MockServer:
    """A configurable, thread-per-connection mock OpenRGB SDK server.

    Parameters
    ----------
    devices:
        list of device dicts; defaults to :func:`default_devices`.
    protocol_version:
        version the server advertises in the protocol version response.
    port:
        TCP port; 0 lets the OS pick a free one (recommended).
    response_delay:
        seconds to sleep before every response (drives client timeouts).
    fail_connections:
        first N accepted connections are closed immediately (aborted handshake).
    silent_types:
        packet types the server receives but never answers.
    truncate_first_response:
        if set, only this many bytes of the first response are sent before the
        connection is closed (truncated / half packet).
    corrupt_data_size:
        ``{device_index: delta}`` added to that device's declared ``data_size``.
    drop_after_packets:
        close the connection after this many received packets.
    push_device_list_updated:
        send a ``DEVICE_LIST_UPDATED`` (type 100) push frame before each
        response, so the client must skip it while matching request/response.
    """

    def __init__(
        self,
        devices=None,
        protocol_version=SERVER_PROTOCOL_VERSION,
        host="127.0.0.1",
        port=0,
        *,
        response_delay=0.0,
        fail_connections=0,
        silent_types=(),
        truncate_first_response=None,
        corrupt_data_size=None,
        drop_after_packets=None,
        push_device_list_updated=False,
    ):
        self.devices = list(devices) if devices is not None else default_devices()
        self.protocol_version = protocol_version
        self.host = host
        self.port = port
        self.response_delay = response_delay
        self.fail_connections = fail_connections
        self.silent_types = set(silent_types)
        self.truncate_first_response = truncate_first_response
        self.corrupt_data_size = dict(corrupt_data_size or {})
        self.drop_after_packets = drop_after_packets
        self.push_device_list_updated = push_device_list_updated

        self.received = []
        self.client_names = []
        self.requested_versions = []
        self.connections = 0
        self.malformed_frames = []

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._listener = None
        self._accept_thread = None
        self._workers = []
        self._truncated_once = False

    # -- lifecycle -----------------------------------------------------
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False

    def start(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(16)
        listener.settimeout(0.05)
        self.port = listener.getsockname()[1]
        self._listener = listener
        self._stop.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="mock-openrgb-accept", daemon=True
        )
        self._accept_thread.start()
        return self.port

    def stop(self):
        self._stop.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None
        for worker in list(self._workers):
            worker.join(timeout=2.0)
        self._workers = []

    # -- record inspection --------------------------------------------
    def reset_records(self):
        with self._lock:
            self.received = []
            self.client_names = []
            self.requested_versions = []
            self.malformed_frames = []

    def packets_of(self, packet_type):
        with self._lock:
            return [rec for rec in self.received if rec["packet_type"] == packet_type]

    def last_packet(self, packet_type):
        found = self.packets_of(packet_type)
        return found[-1] if found else None

    def count_of(self, packet_type):
        return len(self.packets_of(packet_type))

    def received_types(self):
        with self._lock:
            return [rec["packet_type"] for rec in self.received]

    def wait_for_packet(self, packet_type, count=1, timeout=2.0):
        """Block until at least ``count`` packets of ``packet_type`` arrived."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.count_of(packet_type) >= count:
                return True
            time.sleep(0.005)
        return self.count_of(packet_type) >= count

    # -- internals -----------------------------------------------------
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self.connections += 1
                connection_id = self.connections
            if connection_id <= self.fail_connections:
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            worker = threading.Thread(
                target=self._serve,
                args=(conn, connection_id),
                name="mock-openrgb-conn-%d" % connection_id,
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def _serve(self, conn, connection_id):
        packets_seen = 0
        try:
            conn.settimeout(0.2)
            while not self._stop.is_set():
                try:
                    header = _recv_exact(conn, HEADER_SIZE)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if header is None:
                    break
                if header[:4] != MAGIC:
                    with self._lock:
                        self.malformed_frames.append(header)
                    break
                _magic, device_index, packet_type, packet_size = unpack_header(header)
                try:
                    data = _recv_exact(conn, packet_size) if packet_size else b""
                except socket.timeout:
                    continue
                except OSError:
                    break
                if data is None:
                    break
                with self._lock:
                    self.received.append(
                        {
                            "connection": connection_id,
                            "header": header,
                            "device_index": device_index,
                            "packet_type": packet_type,
                            "packet_size": packet_size,
                            "data": data,
                        }
                    )
                packets_seen += 1
                try:
                    self._handle(conn, device_index, packet_type, data)
                except _CloseConnection:
                    break
                if self.drop_after_packets is not None and packets_seen >= self.drop_after_packets:
                    break
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle(self, conn, device_index, packet_type, data):
        if packet_type == REQUEST_PROTOCOL_VERSION:
            client_version = struct.unpack_from("<I", data, 0)[0] if len(data) >= 4 else None
            with self._lock:
                self.requested_versions.append(client_version)
            self._send_frame(
            conn, device_index, REQUEST_PROTOCOL_VERSION,
            struct.pack("<I", self.protocol_version),
        )
        elif packet_type == SET_CLIENT_NAME:
            raw = data[:-1] if data.endswith(b"\x00") else data
            with self._lock:
                self.client_names.append(raw.decode("utf-8", "replace"))
        elif packet_type == REQUEST_CONTROLLER_COUNT:
            self._send_frame(
                conn, device_index, REQUEST_CONTROLLER_COUNT,
                struct.pack("<I", len(self.devices)),
            )
        elif packet_type == REQUEST_CONTROLLER_DATA:
            requested = struct.unpack_from("<I", data, 0)[0] if len(data) >= 4 else None
            if requested is None or device_index >= len(self.devices):
                return
            delta = self.corrupt_data_size.get(device_index, 0)
            payload = pack_controller_data(
                self.devices[device_index], requested, data_size_delta=delta
            )
            self._send_frame(conn, device_index, REQUEST_CONTROLLER_DATA, payload)
        # 1050 / 1051 / 1100 / 1101 / 1000 are one-way: recording is enough.

    def _send_frame(self, conn, device_index, packet_type, data=b""):
        if packet_type in self.silent_types:
            return
        if self.response_delay:
            time.sleep(self.response_delay)
        if self.push_device_list_updated:
            conn.sendall(pack_frame(device_index, DEVICE_LIST_UPDATED))
        if self.truncate_first_response is not None and not self._truncated_once:
            self._truncated_once = True
            payload = pack_frame(device_index, packet_type, data)
            conn.sendall(payload[: self.truncate_first_response])
            raise _CloseConnection()
        conn.sendall(pack_frame(device_index, packet_type, data))


def _recv_exact(conn, size):
    """Read exactly ``size`` bytes; return None on clean EOF, raise on timeout."""
    buffer = bytearray()
    while len(buffer) < size:
        chunk = conn.recv(size - len(buffer))
        if not chunk:
            return None
        buffer.extend(chunk)
    return bytes(buffer)


if __name__ == "__main__":
    # Minimal self-test: start the mock, drive it with a raw socket client and
    # make sure the server answers and records packets as expected.
    with MockServer() as server:
        sock = socket.create_connection((server.host, server.port), timeout=2.0)
        sock.sendall(pack_frame(0, REQUEST_PROTOCOL_VERSION, struct.pack("<I", 4)))
        magic, index, packet_type, size = unpack_header(_recv_exact(sock, 16))
        assert magic == MAGIC and packet_type == REQUEST_PROTOCOL_VERSION
        assert struct.unpack("<I", _recv_exact(sock, size))[0] == SERVER_PROTOCOL_VERSION

        sock.sendall(pack_frame(0, SET_CLIENT_NAME, b"self-test\x00"))
        sock.sendall(pack_frame(0, REQUEST_CONTROLLER_COUNT))
        magic, _index, packet_type, size = unpack_header(_recv_exact(sock, 16))
        assert struct.unpack("<I", _recv_exact(sock, size))[0] == 2

        for i in range(2):
            sock.sendall(pack_frame(i, REQUEST_CONTROLLER_DATA, struct.pack("<I", 4)))
            magic, _index, packet_type, size = unpack_header(_recv_exact(sock, 16))
            payload = _recv_exact(sock, size)
            parsed = parse_controller_data(payload)
            assert parsed["name"] in ("Mock Keyboard", "Mock Strip")
            assert parsed["name"] == server.devices[i]["name"]
        sock.sendall(pack_frame(0, RGBCONTROLLER_UPDATELEDS, b"\x00" * 10))
        assert server.wait_for_packet(RGBCONTROLLER_UPDATELEDS)
        sock.close()
        assert server.client_names == ["self-test"]
        assert server.requested_versions == [4]
    print("mock self-test OK")