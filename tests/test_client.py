"""Protocol regression tests for openrgb_client.py against the mock SDK server.

Run:  python tests/test_client.py     -> prints OK, exit code 0

Everything runs against ``tests/mock_openrgb.py`` on 127.0.0.1 with an
OS-assigned free port, so the real OpenRGB device is never touched.
"""

from __future__ import annotations

import os
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import mock_openrgb as mock  # noqa: E402
from openrgb_client import (  # noqa: E402
    ConnectionFailed,
    OpenRGBClient,
    ProtocolError,
    RGBColor,
    discover_port,
)


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
def test_handshake_and_device_discovery():
    with mock.MockServer() as server:
        client = OpenRGBClient(port=server.port, name="unit-test-client").connect()
        try:
            # server speaks v5, client negotiates down to v4
            assert client.protocol_version == 4, client.protocol_version
            assert mock.CLIENT_PROTOCOL_VERSION == 4
            assert server.requested_versions == [4], server.requested_versions
            assert server.client_names == ["unit-test-client"], server.client_names
            types = server.received_types()
            assert types[:4] == [40, 50, 0, 1], types
            assert len(client.devices) == 2, len(client.devices)
        finally:
            client.close()


def test_controller_data_parsing():
    with mock.MockServer() as server:
        client = OpenRGBClient(port=server.port).connect()
        try:
            keyboard = client.find_device("Mock Keyboard")
            assert keyboard.name == "Mock Keyboard", keyboard.name
            assert keyboard.type_name == "keyboard", keyboard.type_name
            assert keyboard.vendor == "MockVendor", keyboard.vendor
            assert keyboard.serial == "SN-KBD-0001", keyboard.serial
            assert keyboard.led_count == 16, keyboard.led_count
            assert keyboard.total_leds == 16
            assert len(keyboard.colors) == 16, len(keyboard.colors)
            assert keyboard.colors[0] == RGBColor(0, 255, 0), keyboard.colors[0]

            # one matrix zone 4x4 with three segments
            assert len(keyboard.zones) == 1, keyboard.zones
            zone = keyboard.zones[0]
            assert zone.name == "Keyboard Matrix", zone.name
            assert zone.type_name == "matrix", zone.type_name
            assert zone.num_leds == 16, zone.num_leds
            assert zone.matrix_height == 4 and zone.matrix_width == 4
            assert len(zone.segments) == 3, zone.segments
            assert zone.segments[0].name == "Left Half", zone.segments[0]
            assert zone.segments[0].start_idx == 0
            assert zone.segments[2].leds_count == 4

            # modes: Direct / Breathing / Off, active index 1
            assert [m.name for m in keyboard.modes] == ["Direct", "Breathing", "Off"]
            assert keyboard.active_mode == 1, keyboard.active_mode
            assert keyboard.active.name == "Breathing", keyboard.active.name
            assert keyboard.active.per_led is False
            assert keyboard.modes[0].per_led is True
            assert keyboard.modes[2].num_colors == 0

            strip = client.find_device("Mock Strip")
            assert strip.type_name == "ledstrip", strip.type_name
            assert strip.led_count == 10, strip.led_count
            assert len(strip.zones) == 1
            assert strip.zones[0].type_name == "linear"
            assert strip.zones[0].num_leds == 10
            assert strip.active.per_led is True
        finally:
            client.close()


def test_set_device_colors_wire_format():
    with mock.MockServer() as server:
        client = OpenRGBClient(port=server.port).connect()
        try:
            strip = client.find_device("Mock Strip")
            colors = [RGBColor(i * 10, 255 - i * 10, i * 3) for i in range(10)]
            client.set_device_colors(strip, colors)
            assert server.wait_for_packet(mock.RGBCONTROLLER_UPDATELEDS), \
                "no UPDATELEDS received"
            packet = server.last_packet(mock.RGBCONTROLLER_UPDATELEDS)
            assert packet is not None, "no UPDATELEDS received"
            total, parsed = mock.parse_led_packet(packet["data"])
            assert total == len(packet["data"]), (total, len(packet["data"]))
            assert len(parsed) == 10, len(parsed)
            assert parsed[0] == (0, 255, 0), parsed[0]
            assert parsed[9] == (90, 165, 27), parsed[9]
            assert packet["device_index"] == strip.index
        finally:
            client.close()


def test_set_zone_colors_wire_format():
    with mock.MockServer() as server:
        client = OpenRGBClient(port=server.port).connect()
        try:
            strip = client.find_device("Mock Strip")
            zone = strip.zones[0]
            client.set_zone_colors(strip, zone, [RGBColor(1, 2, 3)] * 10)
            assert server.wait_for_packet(mock.RGBCONTROLLER_UPDATEZONELEDS), \
                "no UPDATEZONELEDS received"
            packet = server.last_packet(mock.RGBCONTROLLER_UPDATEZONELEDS)
            assert packet is not None, "no UPDATEZONELEDS received"
            total, zone_index, parsed = mock.parse_zone_led_packet(packet["data"])
            assert total == len(packet["data"])
            assert zone_index == zone.index, (zone_index, zone.index)
            assert len(parsed) == 10, len(parsed)
            assert parsed[0] == (1, 2, 3)
        finally:
            client.close()


def test_ensure_per_led_mode_sends_custom_mode_then_update_mode():
    with mock.MockServer() as server:
        client = OpenRGBClient(port=server.port).connect()
        try:
            keyboard = client.find_device("Mock Keyboard")
            assert keyboard.active.per_led is False, "fixture should start non-per-LED"
            server.reset_records()
            device = client.ensure_per_led_mode(keyboard)
            assert server.wait_for_packet(mock.RGBCONTROLLER_UPDATEMODE), \
                "no UPDATEMODE received"
            types = server.received_types()
            assert mock.RGBCONTROLLER_SETCUSTOMMODE in types, types
            assert mock.RGBCONTROLLER_UPDATEMODE in types, types
            mode_packet = server.last_packet(mock.RGBCONTROLLER_UPDATEMODE)
            parsed = mock.parse_mode_packet(mode_packet["data"])
            assert parsed["name"] == "Direct", parsed
            assert parsed["flags"] & mock.MODE_FLAG_HAS_PER_LED_COLOR
            assert device is not None
        finally:
            client.close()


def test_mode_packed_size_prefix():
    with mock.MockServer() as server:
        client = OpenRGBClient(port=server.port).connect()
        try:
            keyboard = client.find_device("Mock Keyboard")
            direct = keyboard.modes[0]
            packed = direct.packed
            declared = struct.unpack_from("<I", packed, 0)[0]
            assert declared == len(packed), (declared, len(packed))
            parsed = mock.parse_mode_packet(packed)
            assert parsed["name"] == "Direct", parsed
            assert parsed["index"] == 0, parsed
        finally:
            client.close()


def test_wrong_color_count_is_rejected():
    with mock.MockServer() as server:
        client = OpenRGBClient(port=server.port).connect()
        try:
            strip = client.find_device("Mock Strip")
            try:
                client.set_device_colors(strip, [RGBColor(1, 1, 1)] * 3)
            except ValueError:
                pass
            else:
                raise AssertionError("expected ValueError for wrong color count")
            assert server.count_of(mock.RGBCONTROLLER_UPDATELEDS) == 0
        finally:
            client.close()


def test_corrupt_data_size_raises_protocol_error():
    with mock.MockServer(corrupt_data_size={0: 4}) as server:
        try:
            OpenRGBClient(port=server.port).connect()
        except ProtocolError as exc:
            assert "size mismatch" in str(exc), exc
        else:
            raise AssertionError("expected ProtocolError for bad data_size")


def test_device_list_updated_push_is_skipped():
    with mock.MockServer(push_device_list_updated=True) as server:
        client = OpenRGBClient(port=server.port).connect()
        try:
            assert len(client.devices) == 2
            assert client.find_device("Mock Strip").led_count == 10
        finally:
            client.close()


def test_truncated_response_raises():
    with mock.MockServer(truncate_first_response=12) as server:
        try:
            OpenRGBClient(port=server.port, timeout=1.0).connect()
        except (ConnectionError, ProtocolError, ConnectionFailed):
            pass
        else:
            raise AssertionError("expected a connection/protocol error")


def test_connection_refused():
    port = _free_port()
    try:
        OpenRGBClient(port=port, timeout=1.0).connect()
    except ConnectionFailed as exc:
        assert str(port) in str(exc), exc
    else:
        raise AssertionError("expected ConnectionFailed")


def test_discover_port_uses_env(monkeyport=None):
    with mock.MockServer() as server:
        old = os.environ.get("OPENRGB_PORT")
        os.environ["OPENRGB_PORT"] = str(server.port)
        try:
            found = discover_port("127.0.0.1")
            assert found == server.port, (found, server.port)
        finally:
            if old is None:
                os.environ.pop("OPENRGB_PORT", None)
            else:
                os.environ["OPENRGB_PORT"] = old


def test_find_device_unknown_name_raises():
    with mock.MockServer() as server:
        client = OpenRGBClient(port=server.port).connect()
        try:
            try:
                client.find_device("does-not-exist")
            except ProtocolError as exc:
                assert "Mock Keyboard" in str(exc), exc
            else:
                raise AssertionError("expected ProtocolError")
        finally:
            client.close()


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failures = []
    for test in tests:
        started = time.time()
        try:
            test()
            print(f"PASS {test.__name__} ({time.time() - started:.2f}s)")
        except Exception as exc:  # noqa: BLE001 - report and continue
            import traceback
            failures.append((test.__name__, exc))
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    if failures:
        print(f"\n{len(failures)} test(s) failed: "
              + ", ".join(name for name, _ in failures))
        return 1
    print(f"\nOK ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
