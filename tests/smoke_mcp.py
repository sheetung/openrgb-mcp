"""Smoke-test the MCP stdio server end to end (spawns server.py as a child)."""

import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable
SERVER = os.path.join(HERE, "server.py")


class McpProc:
    def __init__(self):
        self.proc = subprocess.Popen(
            [PYTHON, SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
            cwd=HERE,
        )
        self._next_id = 0
        self._lock = threading.Lock()
        self.stderr_lines = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self):
        for line in self.proc.stderr:
            self.stderr_lines.append(line.decode("utf-8", "replace").rstrip())

    def request(self, method, params=None, timeout=20.0):
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            payload = {"jsonrpc": "2.0", "id": msg_id, "method": method}
            if params is not None:
                payload["params"] = params
            self.proc.stdin.write((json.dumps(payload) + "\n").encode())
            self.proc.stdin.flush()
            deadline = time.time() + timeout
            while time.time() < deadline:
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError("server closed stdout")
                message = json.loads(line.decode())
                if message.get("id") == msg_id:
                    return message
            raise TimeoutError(method)

    def notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write((json.dumps(payload) + "\n").encode())
        self.proc.stdin.flush()

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def main():
    proc = McpProc()
    try:
        init = proc.request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"},
        })
        result = init["result"]
        assert result["protocolVersion"] == "2025-03-26", result
        assert "tools" in result["capabilities"], result
        print("initialize OK:", result["serverInfo"])
        proc.notify("notifications/initialized")

        listed = proc.request("tools/list")["result"]["tools"]
        names = [t["name"] for t in listed]
        expected = {"list_devices", "status", "set_state", "set_progress",
                    "set_quota", "set_color", "set_leds", "set_effect", "restore"}
        assert expected <= set(names), (expected - set(names))
        print("tools/list OK:", len(names), "tools")

        def call(name, args=None):
            response = proc.request("tools/call", {"name": name, "arguments": args or {}})
            if "error" in response:
                raise RuntimeError(response["error"])
            result = response["result"]
            text = result["content"][0]["text"]
            return result, text

        result, text = call("list_devices")
        assert not result.get("isError"), text
        devices = json.loads(text)
        assert devices["led_count"] > 0, devices
        print("list_devices OK: leds =", devices["led_count"],
              "device =", devices["selected"])

        for state in ("thinking", "working", "waiting", "done", "error", "idle"):
            result, text = call("set_state", {"state": state})
            assert not result.get("isError"), (state, text)
            assert json.loads(text)["state"] == state, text
        print("set_state OK (all 6 states)")

        result, text = call("set_progress", {"percent": 35})
        data = json.loads(text)
        assert not result.get("isError"), text
        assert data["filled_leds"] == round(0.35 * data["led_count"]), data
        print("set_progress OK: filled", data["filled_leds"], "/", data["led_count"])

        result, text = call("set_quota", {"remaining": 25, "total": 100})
        data = json.loads(text)
        assert data["ratio"] == 0.25, data
        print("set_quota OK: ratio", data["ratio"], "filled", data["filled_leds"])

        result, text = call("status")
        assert json.loads(text)["led_count"] > 0, text
        print("status OK")

        # error path: invalid state must come back as isError, not a crash
        response = proc.request("tools/call", {"name": "set_state",
                                               "arguments": {"state": "nope"}})
        assert response["result"].get("isError") is True, response
        print("error path OK")

        result, text = call("set_color", {"color": "#123456"})
        assert not result.get("isError"), text
        print("set_color OK")

        print("SMOKE OK")
        return 0
    finally:
        proc.close()
        if proc.stderr_lines:
            print("--- server stderr ---", file=sys.stderr)
            print("\n".join(proc.stderr_lines[-10:]), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())