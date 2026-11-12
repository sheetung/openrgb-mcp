#!/usr/bin/env python3
"""lamp UI — 网页测试台，手动驱动并观察状态灯。

启动后会拉起一个本地 HTTP 页面（默认 http://127.0.0.1:8760），页面上的所有
操作都会通过**官方 MCP 客户端**转发给 MCP server，和 DSH/Claude Desktop 走的
是同一条协议链路。

    python tools/ui.py                 # 默认 127.0.0.1:8760
    python tools/ui.py --port 9000
    python tools/ui.py --no-browser

注意：MCP server 是本脚本自己拉起的独立进程。请不要在 AI 正在操作灯的同时用
这个页面，两边会互相覆盖状态。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (os.path.join(ROOT, "src"), ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from openrgb_mcp import renderer  # noqa: E402
from openrgb_mcp.engine import STATES  # noqa: E402

SERVER = os.path.join(ROOT, "server.py")

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenRGB 状态灯 · 测试台</title>
<style>
  :root {
    --bg:#0b0d12; --panel:#12151d; --line:#242a36; --text:#e2e8f0; --muted:#848ea0;
    --accent:#3a86ff;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif; }
  header { padding:18px 24px; border-bottom:1px solid var(--line);
           display:flex; align-items:center; gap:18px; flex-wrap:wrap; }
  h1 { font-size:17px; margin:0; font-weight:600; }
  .pill { padding:3px 10px; border-radius:999px; background:#1b2030;
          border:1px solid var(--line); font-size:12px; color:var(--muted); }
  .pill b { color:var(--text); font-weight:600; }
  main { display:grid; grid-template-columns: minmax(0,1fr) 340px; gap:18px; padding:18px 24px 40px; align-items:start; }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  section { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px 18px; }
  h2 { font-size:13px; margin:0 0 12px; color:var(--muted); font-weight:600;
       text-transform:uppercase; letter-spacing:.08em; }
  #strip { width:100%; display:block; border-radius:8px; background:#07090d; }
  .zone-label { color:var(--muted); font-size:11px; margin:8px 0 2px; }
  .row { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
  button { background:#1a2030; color:var(--text); border:1px solid var(--line);
           border-radius:8px; padding:8px 13px; cursor:pointer; font-size:13px; }
  button:hover { border-color:#3b465c; background:#1e2536; }
  button.active { background:var(--accent); border-color:var(--accent); color:#06101f; }
  input[type=range] { width:100%; accent-color:var(--accent); }
  input[type=color] { width:46px; height:32px; padding:0; border:1px solid var(--line);
                       border-radius:6px; background:none; }
  input[type=text], input[type=number] { background:#0e1219; border:1px solid var(--line);
        color:var(--text); border-radius:8px; padding:7px 10px; width:100%; }
  label { display:block; color:var(--muted); font-size:12px; margin:12px 0 5px; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  #log { font-family:Consolas,monospace; font-size:12px; color:var(--muted);
         max-height:170px; overflow:auto; background:#0a0d13; border:1px solid var(--line);
         border-radius:8px; padding:10px; }
  .badge { font-size:11px; padding:2px 8px; border-radius:999px; border:1px solid var(--line); }
  .badge.hw { color:#2ecc71; border-color:#2ecc7155; background:#2ecc7114; }
  .badge.sw { color:#ffb703; border-color:#ffb70355; background:#ffb70314; }
  .hint { color:var(--muted); font-size:12px; margin-top:8px; }
</style>
</head>
<body>
<header>
  <h1>OpenRGB 状态灯 · 测试台</h1>
  <span class="pill">设备 <b id="device">-</b></span>
  <span class="pill">LED <b id="leds">-</b></span>
  <span class="pill">端口 <b id="port">-</b></span>
  <span class="pill">状态 <b id="state">-</b></span>
  <span class="pill">硬件模式 <b id="mode">-</b></span>
  <span class="pill" id="render">-</span>
</header>

<main>
  <section>
    <h2>灯带预览</h2>
    <canvas id="strip" height="150"></canvas>
    <div class="zone-label" id="zones">-</div>
    <div class="hint">预览由与实机相同的渲染器生成；标注「硬件效果」时，灯光由控制器自己驱动（页面里做的是同色近似动画）。</div>
    <div class="row">
      <button onclick="ramp(6)">亮度缓升缓降 6s</button>
      <button onclick="stopRamp()">停止</button>
      <span class="pill">平滑度 <b id="smooth">-</b></span>
    </div>
  </section>

  <section>
    <h2>控制</h2>
    <div class="row" id="states"></div>

    <label>进度条</label>
    <input type="range" id="progress" min="0" max="100" value="0">
    <div class="hint"><span id="progressValue">0</span>% · <button onclick="sendProgress()">应用到灯带</button></div>

    <label>剩余额度</label>
    <div class="grid2">
      <input type="number" id="remaining" value="30" min="0">
      <input type="number" id="total" value="100" min="1">
    </div>
    <div class="row"><button onclick="sendQuota()">显示额度条</button></div>

    <label>单色 / 渐变</label>
    <div class="row">
      <input type="color" id="color" value="#3a86ff">
      <input type="text" id="gradient" value="#ff0000,#0000ff" placeholder="渐变：#ff0000,#0000ff">
      <button onclick="sendColor()">单色</button>
      <button onclick="sendGradient()">渐变</button>
    </div>

    <label>设备自带效果</label>
    <div class="row"><input type="text" id="effect" value="Breathing">
      <button onclick="sendEffect()">应用</button></div>

    <div class="row">
      <button onclick="send('restore')">恢复原灯效</button>
      <button onclick="send('off')">关灯</button>
    </div>

    <h2 style="margin-top:18px">日志</h2>
    <div id="log"></div>
  </section>
</main>

<script>
const STATES = __STATES__;
let lastFrame = null, rampTimer = null, lastColors = null;

document.getElementById('states').innerHTML = STATES.map(s =>
  `<button onclick="setState('${s}')" id="btn-${s}">${s}</button>`).join('');

function log(msg) {
  const el = document.getElementById('log');
  el.innerHTML = `<div>${new Date().toLocaleTimeString()} ${msg}</div>` + el.innerHTML;
}

async function post(action, extra) {
  const res = await fetch('/api/action', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign({action}, extra || {}))
  });
  const data = await res.json();
  if (data.error) { log('✗ ' + data.error); return null; }
  log('✓ ' + action + ' ' + JSON.stringify(data.result || {}));
  return data.result;
}

function setState(state) {
  post('state', {state});
  Object.values(document.querySelectorAll('#states button'))
       .forEach(b => b.classList.toggle('active', b.id === 'btn-' + state));
}
const send = a => post(a);
function sendProgress() {
  const percent = Number(document.getElementById('progress').value);
  document.getElementById('progressValue').textContent = percent;
  post('progress', {percent});
}
function sendQuota() {
  post('quota', {remaining: Number(document.getElementById('remaining').value),
                  total: Number(document.getElementById('total').value)});
}
function sendColor() { post('color', {color: document.getElementById('color').value}); }
function sendGradient() {
  const list = document.getElementById('gradient').value.split(',')
                 .map(s => s.trim()).filter(Boolean);
  post('gradient', {colors: list});
}
function sendEffect() { post('effect', {effect: document.getElementById('effect').value}); }
document.getElementById('progress').oninput = e =>
  document.getElementById('progressValue').textContent = e.target.value;

/* ---- 亮度缓升缓降：逐灯写入，走 MCP 的 set_leds ---- */
function ramp(seconds) {
  stopRamp();
  const steps = Math.round(seconds * 12), start = Date.now();
  rampTimer = setInterval(() => {
    const phase = (Date.now() - start) / (seconds * 1000);
    const level = 0.05 + 0.95 * (0.5 - 0.5 * Math.cos(phase * 2 * Math.PI));
    const leds = lastFrame ? lastFrame.led_count : 200;
    const r = Math.round(58 * level), g = Math.round(134 * level), b = Math.round(255 * level);
    const colors = new Array(leds).fill('#' + [r,g,b].map(v =>
      v.toString(16).padStart(2,'0')).join(''));
    fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action:'leds', colors})}).catch(()=>{});
  }, 80);
}
function stopRamp() { if (rampTimer) { clearInterval(rampTimer); rampTimer = null; } }

/* ---- 预览绘制 ---- */
function draw(frame) {
  const canvas = document.getElementById('strip');
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth, height = 150;
  canvas.width = width * dpr; canvas.height = height * dpr;
  canvas.style.height = height + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.fillStyle = '#07090d'; ctx.fillRect(0, 0, width, height);
  const n = frame.led_count || 200;
  const perRow = Math.ceil(n / 2);
  const gap = 3, r = Math.max(2, Math.min(6, (width - 20) / perRow / 2 - gap));
  const zoneHeight = (height - 30) / 2;
  let idx = 0;
  for (let row = 0; row < 2; row++) {
    const y = 18 + row * (zoneHeight + 8);
    for (let c = 0; c < perRow && idx < n; c++, idx++) {
      const rgb = (frame.colors && frame.colors[idx]) || [0,0,0];
      const x = 14 + c * (r * 2 + gap);
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`; ctx.fill();
      if (frame.hardware && frame.pulse !== undefined) {
        ctx.globalAlpha = 0.28;
        ctx.beginPath(); ctx.arc(x, y, r * 2.4, 0, Math.PI * 2);
        ctx.fillStyle = `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`; ctx.fill();
        ctx.globalAlpha = 1;
      }
    }
  }
}

async function poll() {
  try {
    const res = await fetch('/api/frame');
    const frame = await res.json();
    if (frame.error) { return; }
    if (frame.colors) {
      if (lastColors) {
        let maxDelta = 0;
        for (let i = 0; i < frame.colors.length; i++) {
          const a = frame.colors[i], b = lastColors[i];
          if (!b) continue;
          maxDelta = Math.max(maxDelta, Math.abs(a[0]-b[0]), Math.abs(a[1]-b[1]), Math.abs(a[2]-b[2]));
        }
        if (maxDelta) {
          document.getElementById('smooth').textContent = 'Δ' + maxDelta;
        }
      }
      lastColors = frame.colors;
    }
    lastFrame = frame;
    document.getElementById('device').textContent = frame.device || '-';
    document.getElementById('leds').textContent = frame.led_count || '-';
    document.getElementById('port').textContent = frame.port || '-';
    document.getElementById('state').textContent = frame.state || '-';
    document.getElementById('mode').textContent = frame.active_mode || '-';
    const badge = document.getElementById('render');
    badge.textContent = frame.render_mode === 'hardware' ? '硬件效果' : '逐灯珠';
    badge.className = 'pill ' + (frame.render_mode === 'hardware' ? 'hw' : 'sw');
    document.getElementById('zones').textContent = (frame.zones || [])
      .map(z => `${z.name} · ${z.leds} 颗`).join('　');
    if (frame.progress !== null && frame.progress !== undefined) {
      const pct = Math.round(frame.progress * 100);
      document.getElementById('progress').value = pct;
      document.getElementById('progressValue').textContent = pct;
    }
    if (frame.state) {
      Object.values(document.querySelectorAll('#states button'))
           .forEach(b => b.classList.toggle('active', b.id === 'btn-' + frame.state));
    }
    draw(frame);
  } catch (e) { /* keep polling */ }
}
setInterval(poll, 200);
poll();
</script>
</body>
</html>
"""


class LampUI:
    """HTTP front-end that drives the MCP server over stdio."""

    def __init__(self, host: str, port: int, device: Optional[str] = None):
        self.host = host
        self.port = port
        self.device = device
        self.session = None
        self._stack = None
        self._client = None
        self._loop = None
        self._loop_thread = None
        self._lock = threading.Lock()

    # -- MCP plumbing --------------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._loop_thread = threading.Thread(
                target=self._run_loop, name="lamp-ui-mcp", daemon=True
            )
            self._loop_thread.start()
        return self._loop

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _open_session(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
        if self.device:
            env["OPENRGB_DEVICE"] = self.device
        params = StdioServerParameters(command=sys.executable, args=[SERVER], env=env)
        self._stack = stdio_client(params)
        read, write = await self._stack.__aenter__()
        self._client = ClientSession(read, write)
        self.session = await self._client.__aenter__()
        await self.session.initialize()
        return self.session

    def call(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        loop = self._ensure_loop()

        async def _do() -> Dict[str, Any]:
            session = self.session or await self._open_session()
            result = await session.call_tool(tool, args)
            if result.is_error:
                raise RuntimeError(result.content[0].text)
            return result.structured_content or {}

        with self._lock:
            return asyncio.run_coroutine_threadsafe(_do(), loop).result(timeout=60)

    # -- actions -------------------------------------------------------
    def action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        action = payload.get("action")
        if action == "state":
            return self.call("set_state", {"state": payload["state"],
                                           "detail": payload.get("detail")})
        if action == "progress":
            return self.call("set_progress", {"percent": float(payload["percent"])})
        if action == "quota":
            return self.call("set_quota", {"remaining": float(payload["remaining"]),
                                           "total": float(payload["total"])})
        if action == "color":
            return self.call("set_color", {"color": payload["color"]})
        if action == "gradient":
            return self.call("set_leds", {"colors": self._gradient(payload["colors"])})
        if action == "leds":
            return self.call("set_leds", {"colors": payload["colors"]})
        if action == "effect":
            return self.call("set_effect", {"effect": payload["effect"]})
        if action == "off":
            return self.call("set_state", {"state": "off"})
        if action == "restore":
            return self.call("restore", {})
        raise ValueError(f"未知操作: {action}")

    def _gradient(self, colors, count: Optional[int] = None) -> list:
        stops = [renderer.parse_hex_color(c) for c in colors]
        stops = [s for s in stops if s is not None]
        if len(stops) < 2:
            raise ValueError("渐变至少需要两个颜色")
        if count is None:
            count = self.call("list_devices", {}).get("led_count", 60)
        out = []
        for index in range(count):
            position = index * (len(stops) - 1) / max(1, count - 1)
            low = min(int(position), len(stops) - 2)
            frac = position - low
            a, b = stops[low], stops[low + 1]
            out.append(
                "#%02x%02x%02x" % (
                    round(a[0] + (b[0] - a[0]) * frac),
                    round(a[1] + (b[1] - a[1]) * frac),
                    round(a[2] + (b[2] - a[2]) * frac),
                )
            )
        return out

    def frame(self) -> Dict[str, Any]:
        status = self.call("status", {})
        devices = self.call("list_devices", {})
        state = status.get("state", "idle")
        spec = renderer.DEFAULT_PALETTE.get(state) or renderer.DEFAULT_PALETTE["idle"]
        colors = renderer.render(
            state,
            devices.get("led_count", 60),
            palette=None,
            progress=status.get("progress"),
            quota=tuple(status["quota"]) if status.get("quota") else None,
        )
        return {
            "state": state,
            "device": status.get("device") or devices.get("selected"),
            "led_count": devices.get("led_count", 60),
            "port": status.get("port"),
            "active_mode": status.get("active_mode"),
            "render_mode": status.get("render_mode", "per_led"),
            "zones": status.get("zones", []),
            "progress": status.get("progress"),
            "quota": status.get("quota"),
            "colors": [list(c) for c in colors],
            "hardware": status.get("render_mode") == "hardware",
        }


def make_handler(ui: LampUI):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                page = PAGE.replace("__STATES__", json.dumps(list(STATES)))
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                return
            if self.path.startswith("/api/frame"):
                try:
                    self._send(200, json.dumps(ui.frame()).encode("utf-8"),
                               "application/json; charset=utf-8")
                except Exception as exc:  # noqa: BLE001
                    self._send(200, json.dumps({"error": str(exc)}).encode("utf-8"),
                               "application/json; charset=utf-8")
                return
            self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")

        def do_POST(self):
            if not self.path.startswith("/api/action"):
                self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
                result = ui.action(payload)
                self._send(200, json.dumps({"result": result}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as exc:  # noqa: BLE001
                self._send(200, json.dumps({"error": f"{type(exc).__name__}: {exc}"},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenRGB 状态灯网页测试台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8760)
    parser.add_argument("--device", default=None, help="设备名称片段")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    ui = LampUI(args.host, args.port, args.device)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(ui))
    url = f"http://{args.host}:{args.port}/"
    print(f"状态灯测试台已启动: {url}")
    print("Ctrl+C 退出")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
