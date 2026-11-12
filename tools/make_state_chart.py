#!/usr/bin/env python3
"""Render docs/states.png from the real palette and brightness mapping.

The chart shows **global control** (the way the lamp is actually driven on
this machine): one colour for the whole strip, and progress / quota mapped
onto the strip's brightness.

Usage:  python tools/make_state_chart.py [output.png]
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (os.path.join(ROOT, "src"), ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont  # noqa: E402

from openrgb_mcp import renderer  # noqa: E402
from openrgb_mcp.config import load_config  # noqa: E402

W, H = 1180, 960
BG = (11, 13, 18)
TEXT = (226, 232, 240)
MUTED = (132, 142, 160)
LINE = (36, 41, 52)
RIM = (58, 64, 78)

FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/segoeui.ttf",
]
MONO_CANDIDATES = [
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/cour.ttf",
]


def _font(candidates, size):
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "docs", "states.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    config = load_config()
    cap = float(config.get("max_brightness", 0.7) or 0.7)
    cap = max(0.05, min(1.0, cap))

    title_f = _font(FONT_CANDIDATES, 38)
    sub_f = _font(FONT_CANDIDATES, 20)
    label_f = _font(FONT_CANDIDATES, 23)
    small_f = _font(FONT_CANDIDATES, 17)
    mono_f = _font(MONO_CANDIDATES, 18)

    sharp = Image.new("RGB", (W, H), BG)
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(sharp)
    gdraw = ImageDraw.Draw(glow)

    draw.text((52, 40), "OpenRGB 状态灯 · 整体控制", font=title_f, fill=TEXT)
    draw.text(
        (52, 92),
        f"整条灯带一个颜色，进度/额度用整体亮度表示（亮度上限 {int(cap * 100)}%）",
        font=sub_f,
        fill=MUTED,
    )
    draw.line([(52, 130), (W - 52, 130)], fill=LINE, width=2)

    rows = [
        ("idle", "空闲", "暗蓝 · 呼吸 4.0s"),
        ("thinking", "思考中", "蓝 · 呼吸 2.4s"),
        ("working", "执行中", "青 · 呼吸 3.2s"),
        ("waiting", "等待用户输入", "琥珀 · 渐变 3.6s"),
        ("done", "完成", "绿 · 常亮"),
        ("error", "出错", "红 · 渐变 2.8s"),
        ("off", "关闭", "全黑"),
    ]

    x0 = 250
    bar_w = 700
    y = 168

    def bar(color, level, label=""):
        """One bar = the whole strip at the given brightness."""
        level = max(0.0, min(1.0, level))
        rgb = tuple(max(0, min(255, round(c * level))) for c in color)
        draw.rectangle([x0, y, x0 + bar_w, y + 26], fill=rgb, outline=RIM, width=1)
        if max(rgb) > 8:
            gdraw.rectangle([x0 - 6, y - 6, x0 + bar_w + 6, y + 32], fill=rgb)
        if label:
            # CJK-capable font: the mono font has no Chinese glyphs
            draw.text((x0 + bar_w + 18, y + 4), label, font=small_f, fill=MUTED)

    for state, cn, desc in rows:
        spec = renderer.DEFAULT_PALETTE[state]
        color = renderer.parse_hex_color(spec["color"]) or (0, 0, 0)
        level = float(spec.get("brightness", 1.0)) * cap
        if state == "off":
            level = 0.0
        draw.text((52, y - 2), cn, font=label_f, fill=TEXT)
        draw.text((52, y + 30), desc, font=small_f, fill=MUTED)
        bar(color, level, f"{spec['color']}  {int(round(level * 255))}/255")
        y += 62

    y += 10
    draw.line([(52, y - 12), (W - 52, y - 12)], fill=LINE, width=2)
    y += 4

    bars = [
        ("进度条 20%", 0.20, "#00d4ff", "亮度 20% · 51/255"),
        ("进度条 60%", 0.60, "#00d4ff", "亮度 60% · 107/255"),
        ("进度条 100%", 1.00, "#00d4ff", f"亮度 {int(cap * 100)}% · {int(cap * 255)}/255（上限）"),
        ("额度条 30/100", 0.30, "#ffb703", "琥珀色 · 亮度 30%"),
        ("额度条 80/100", 0.80, "#2ecc71", "绿色 · 亮度 80%"),
    ]
    for cn, frac, hex_color, note in bars:
        color = renderer.parse_hex_color(hex_color) or (255, 255, 255)
        draw.text((52, y - 2), cn, font=label_f, fill=TEXT)
        bar(color, frac * cap, note)
        y += 56

    draw.text(
        (52, H - 34),
        "openrgb-mcp · 整体控制：颜色 + 亮度由控制器驱动，无逐帧写入，不闪",
        font=small_f,
        fill=MUTED,
    )

    blurred = glow.filter(ImageFilter.GaussianBlur(18)).point(lambda v: v // 2)
    img = ImageChops.add(sharp, blurred)
    img.save(out)
    print("wrote", out, img.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
