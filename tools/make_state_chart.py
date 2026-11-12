#!/usr/bin/env python3
"""Render docs/states.png straight from the real renderer.

Every LED pixel in the chart comes from ``renderer.render()`` - the same code
path the MCP server drives the hardware with - so the picture cannot drift from
the implementation.

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

W, H = 1680, 880
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

    title_f = _font(FONT_CANDIDATES, 40)
    sub_f = _font(FONT_CANDIDATES, 21)
    label_f = _font(FONT_CANDIDATES, 24)
    small_f = _font(FONT_CANDIDATES, 18)
    mono_f = _font(MONO_CANDIDATES, 19)

    sharp = Image.new("RGB", (W, H), BG)
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(sharp)
    gdraw = ImageDraw.Draw(glow)

    draw.text((56, 44), "OpenRGB 状态灯 · 真实渲染输出", font=title_f, fill=TEXT)
    draw.text(
        (56, 98),
        "每颗灯珠的颜色直接来自 renderer.render()，与 MCP server 驱动硬件的代码路径一致",
        font=sub_f,
        fill=MUTED,
    )
    draw.line([(56, 140), (W - 56, 140)], fill=LINE, width=2)

    rows = [
        ("thinking", "思考中", "蓝 · 呼吸 1.2s"),
        ("working", "执行中", "青 · 呼吸 2.0s"),
        ("waiting", "等待用户输入", "琥珀 · 脉冲 1.5s"),
        ("done", "完成", "绿 · 常亮"),
        ("error", "出错", "红 · 闪烁 0.5s"),
        ("idle", "空闲", "暗蓝 · 呼吸 4.0s"),
        ("off", "关闭", "全黑"),
    ]

    led_count = 24
    x0 = 306
    pitch = 27
    radius = 9
    y = 184

    def strip(colors, cy):
        for i, rgb in enumerate(colors):
            cx = x0 + i * pitch
            draw.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius],
                fill=rgb, outline=RIM, width=1,
            )
            if max(rgb) > 8:
                gr = radius + 16
                gdraw.ellipse(
                    [cx - gr, cy - gr, cx + gr, cy + gr], fill=rgb
                )

    for state, cn, desc in rows:
        spec = renderer.DEFAULT_PALETTE[state]
        period = float(spec.get("period") or 0)
        tick = (period / 4.0) if period > 0 else 0.0   # brightest frame
        colors = renderer.render(state, led_count, tick=tick)
        draw.text((56, y - 6), cn, font=label_f, fill=TEXT)
        draw.text((56, y + 28), desc, font=small_f, fill=MUTED)
        draw.text((x0, y + 30), spec["color"], font=mono_f, fill=MUTED)
        strip(colors, y + 12)
        y += 66

    y += 14
    draw.line([(56, y - 14), (W - 56, y - 14)], fill=LINE, width=2)
    y += 6

    bars = [
        ("进度条 60%", dict(progress=0.6), "已完成 60% 的灯珠点亮，其余熄灭"),
        ("额度条 30/100", dict(quota=(30, 100)), ">50% 绿 / 20-50% 琥珀 / <20% 红"),
    ]
    for cn, kwargs, desc in bars:
        draw.text((56, y - 6), cn, font=label_f, fill=TEXT)
        draw.text((56, y + 28), desc, font=small_f, fill=MUTED)
        strip(renderer.render("working", led_count, **kwargs), y + 12)
        y += 72

    draw.text(
        (56, H - 52),
        "openrgb-mcp · 灯珠颜色由 renderer.render() 生成 · 实际格数 = 设备灯珠数（示意图 24 格）",
        font=small_f,
        fill=MUTED,
    )

    blurred = glow.filter(ImageFilter.GaussianBlur(20)).point(lambda v: v // 2)
    img = ImageChops.add(sharp, blurred)
    img.save(out)
    print("wrote", out, img.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())