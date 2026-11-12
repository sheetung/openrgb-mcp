#!/usr/bin/env python3
"""Unit tests for ``openrgb_mcp.renderer`` (CONTRACT.md §5).

Pure standard library, plain ``assert`` statements, no pytest required:

    python tests/test_renderer.py

Prints ``OK`` and exits 0 when every assertion passes.
"""

import copy
import math
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from openrgb_mcp import renderer  # noqa: E402  (path set up above)
from openrgb_mcp.renderer import (  # noqa: E402  (path set up above)
    DEFAULT_PALETTE,
    parse_hex_color,
    render,
)


EMPTY = parse_hex_color("#0a0a0f")          # (10, 10, 15)
BLACK = (0, 0, 0)
DONE = parse_hex_color("#2ecc71")           # (46, 204, 113)
ERROR = parse_hex_color("#ff4d4f")          # (255, 77, 79)
GREEN = parse_hex_color("#2ecc71")
AMBER = parse_hex_color("#ffb703")
RED = parse_hex_color("#ff4d4f")


def apply_color(color, factor):
    """Mirror the renderer's per-channel scaling (round + clamp to 0..255)."""
    return tuple(max(0, min(255, int(round(channel * factor)))) for channel in color)


def breathe_factor(tick, period):
    """Replicate the contract's breathe multiplier exactly."""
    phase = (tick % period) / period
    return 0.55 + 0.45 * (0.5 + 0.5 * math.sin(2.0 * math.pi * phase))


def assert_pixels(pixels, count):
    assert isinstance(pixels, list), "render() must return a list"
    assert len(pixels) == count, (len(pixels), count)
    for pixel in pixels:
        assert isinstance(pixel, tuple), pixel
        assert len(pixel) == 3, pixel
        for channel in pixel:
            assert type(channel) is int, pixel
            assert 0 <= channel <= 255, pixel


# ---------------------------------------------------------------------------
# palette / constants
# ---------------------------------------------------------------------------

def test_default_palette_matches_contract():
    expected = {
        "idle":     {"color": "#2a3a5a", "brightness": 0.25, "effect": "breathe", "period": 4.0},
        "thinking": {"color": "#3a86ff", "brightness": 0.85, "effect": "breathe", "period": 1.2},
        "working":  {"color": "#00d4ff", "brightness": 0.9,  "effect": "breathe", "period": 2.0},
        "waiting":  {"color": "#ffb703", "brightness": 0.9,  "effect": "pulse",   "period": 1.5},
        "done":     {"color": "#2ecc71", "brightness": 1.0,  "effect": "solid",   "period": 0},
        "error":    {"color": "#ff4d4f", "brightness": 1.0,  "effect": "blink",   "period": 0.5},
        "off":      {"color": "#000000", "brightness": 0.0,  "effect": "off",     "period": 0},
    }
    assert DEFAULT_PALETTE == expected
    assert tuple(DEFAULT_PALETTE) == (
        "idle", "thinking", "working", "waiting", "done", "error", "off")
    assert renderer.STATES == tuple(DEFAULT_PALETTE)


def test_hex_parsing():
    assert parse_hex_color("#ff0000") == (255, 0, 0)
    assert parse_hex_color("FF8000") == (255, 128, 0)
    assert parse_hex_color("  #00ff00 ") == (0, 255, 0)
    assert parse_hex_color("#000000") == (0, 0, 0)
    assert parse_hex_color("#abcdef") == (171, 205, 239)
    for bad in ("", "#12345", "#1234567", "red", "#gggggg", "0x123456",
                None, 123, 1.5, ["#ffffff"], b"#ffffff", "##ffffff"):
        assert parse_hex_color(bad) is None, bad


# ---------------------------------------------------------------------------
# shape / count contract
# ---------------------------------------------------------------------------

def test_shape_and_bounds():
    for state in list(DEFAULT_PALETTE) + ["nonsense", None]:
        for tick in (0.0, 0.01, 0.37, 1.0, 2.5, 10.0, -0.4):
            for count in (1, 7, 60):
                assert_pixels(render(state, count, tick=tick), count)
        for extra in ({"progress": 0.42},
                      {"progress": 0.42, "progress_color": "#123456"},
                      {"quota": (3, 8)},
                      {"progress": 1.0, "empty_color": "#ffffff"}):
            assert_pixels(render(state, 5, tick=0.2, **extra), 5)


def test_zero_led_count_returns_empty_list():
    for count in (0, -1, -999, 0.0, -2.5):
        for state in DEFAULT_PALETTE:
            assert render(state, count) == []
    assert render("idle", 0, progress=0.5) == []
    assert render("idle", 0, quota=(5, 10)) == []
    assert render("idle", 0, tick=3.2, color="#ffffff") == []
    assert render("idle", 0, palette={"idle": {"color": "#ffffff"}}) == []
    assert render("idle", "not-a-number") == []


def test_led_count_coercion_and_single_led():
    assert len(render("thinking", 1)) == 1
    assert len(render("thinking", 1.0)) == 1
    assert len(render("done", "12")) == 12
    assert len(render("done", True)) == 1
    assert render("done", 1, progress=1.0) == [DONE]
    assert render("done", 1, progress=0.99) == [DONE]


# ---------------------------------------------------------------------------
# per-state rendering
# ---------------------------------------------------------------------------

def test_off_state_is_always_black():
    for tick in (0.0, 0.5, 2.0, 100.0, -3.0):
        assert render("off", 6, tick=tick) == [BLACK] * 6
    assert render("off", 4, color="#ffffff") == [BLACK] * 4
    assert render("off", 4, progress=1.0) == [BLACK] * 4  # state colour is black


def test_done_state_is_solid():
    for tick in (0.0, 0.13, 1.0, 999.0):
        assert render("done", 5, tick=tick) == [DONE] * 5
    assert render("done", 2, tick=0.4, palette={"done": {"period": 9.0}}) == [DONE] * 2


def test_breathe_matches_contract_formula():
    cases = [
        ("idle", 0.0), ("idle", 0.7), ("idle", 2.9), ("idle", 3.99),
        ("thinking", 0.0), ("thinking", 0.1), ("thinking", 0.35),
        ("thinking", 0.6), ("thinking", 0.9), ("thinking", 1.1),
        ("working", 0.0), ("working", 0.5), ("working", 1.25), ("working", 1.9),
    ]
    for state, tick in cases:
        spec = DEFAULT_PALETTE[state]
        color = parse_hex_color(spec["color"])
        expected = apply_color(color, spec["brightness"] * breathe_factor(tick, spec["period"]))
        assert render(state, 4, tick=tick) == [expected] * 4, (state, tick)


def test_breathe_monotonic_and_bounded():
    for state in ("idle", "thinking", "working"):
        spec = DEFAULT_PALETTE[state]
        color = parse_hex_color(spec["color"])
        period = spec["period"]
        brightness = spec["brightness"]
        peak = apply_color(color, brightness * 1.0)
        trough = apply_color(color, brightness * 0.55)

        samples = [render(state, 1, tick=index * period / 80)[0] for index in range(81)]

        rising = samples[:21]
        for previous, current in zip(rising, rising[1:]):
            assert previous[2] <= current[2], (state, previous, current)

        for pixel in samples:
            for channel, low, high in zip(pixel, trough, peak):
                assert low <= channel <= high, (state, pixel, low, high)

        assert samples[20] == peak, (state, samples[20], peak)
        assert samples[60] == trough, (state, samples[60], trough)


def test_blink_alternates():
    period = DEFAULT_PALETTE["error"]["period"]
    assert period == 0.5
    for tick in (0.0, 0.1, 0.249):
        assert render("error", 3, tick=tick) == [ERROR] * 3, tick
    for tick in (0.25, 0.4, 0.499, 0.75, 1.25):
        assert render("error", 3, tick=tick) == [BLACK] * 3, tick
    assert render("error", 3, tick=0.5) == [ERROR] * 3
    assert render("error", 3, tick=1.0) == [ERROR] * 3


def test_pulse_varies_and_is_bounded():
    spec = DEFAULT_PALETTE["waiting"]
    color = parse_hex_color(spec["color"])
    period = spec["period"]
    brightness = spec["brightness"]
    ceiling = apply_color(color, brightness * 1.0)

    samples = [render("waiting", 1, tick=index * period / 20)[0] for index in range(21)]
    assert len(set(samples)) >= 5, "pulse must vary over a period"

    peak = samples[5]    # phase 0.25 (sin maximum)
    trough = samples[15]  # phase 0.75 (sin minimum)
    assert peak[0] > trough[0], (peak, trough)
    assert peak[0] >= 218, peak       # near full red at the peak
    assert 0 < trough[0] <= 69        # dim but not extinguished at the trough

    for pixel in samples:
        for channel, high in zip(pixel, ceiling):
            assert 0 <= channel <= high, (pixel, ceiling)


def test_unknown_state_falls_back_to_idle():
    for tick in (0.0, 0.37, 2.2):
        expected = render("idle", 7, tick=tick)
        assert render("nonsense", 7, tick=tick) == expected
        assert render(None, 7, tick=tick) == expected
        assert render("", 7, tick=tick) == expected
        assert render("   ", 7, tick=tick) == expected
        assert render(42, 7, tick=tick) == expected
        assert render(["thinking"], 7, tick=tick) == expected


def test_state_name_normalization():
    assert render("  THINKING  ", 5, tick=0.2) == render("thinking", 5, tick=0.2)
    assert render("Working", 3, tick=0.2) == render("working", 3, tick=0.2)


# ---------------------------------------------------------------------------
# progress bar
# ---------------------------------------------------------------------------

def test_progress_bar_basic_and_boundaries():
    state_color = parse_hex_color(DEFAULT_PALETTE["thinking"]["color"])
    assert render("thinking", 10, progress=0.0) == [EMPTY] * 10
    assert render("thinking", 10, progress=1.0) == [state_color] * 10
    assert render("thinking", 10, progress=0.3) == [state_color] * 3 + [EMPTY] * 7
    assert render("thinking", 10, progress=0.0, tick=3.3) == [EMPTY] * 10


def test_progress_uses_progress_color_and_empty_color():
    assert render("thinking", 6, progress=0.5, progress_color="#112233") == \
        [(17, 34, 51)] * 3 + [EMPTY] * 3
    assert render("done", 4, progress=0.5, empty_color="#123456") == \
        [DONE] * 2 + [(18, 52, 86)] * 2
    # progress_color overrides even the explicit `color` parameter
    assert render("done", 4, progress=0.5, color="#ffffff", progress_color="#0000ff") == \
        [(0, 0, 255)] * 2 + [EMPTY] * 2


def test_progress_clamps_and_uses_python_round():
    assert render("done", 5, progress=-2.0) == [EMPTY] * 5
    assert render("done", 5, progress=3.0) == [DONE] * 5
    assert render("done", 5, progress=float("nan")) == [EMPTY] * 5
    assert render("done", 5, progress="0.4") == [DONE] * 2 + [EMPTY] * 3
    # contract says round(); Python's round() is banker's rounding:
    assert render("done", 10, progress=0.25) == [DONE] * 2 + [EMPTY] * 8
    assert render("done", 10, progress=0.75) == [DONE] * 8 + [EMPTY] * 2
    assert render("done", 1, progress=0.5) == [EMPTY]


def test_progress_overrides_animation():
    # error blinks dark during the second half of its 0.5 s period ...
    assert render("error", 8, tick=0.4) == [BLACK] * 8
    # ... but a progress bar is static and replaces the state effect.
    assert render("error", 8, tick=0.4, progress=0.5) == [ERROR] * 4 + [EMPTY] * 4


# ---------------------------------------------------------------------------
# quota bar
# ---------------------------------------------------------------------------

def test_quota_tiers():
    def bar(filled, color):
        return [color] * filled + [EMPTY] * (10 - filled)

    assert render("thinking", 10, quota=(10, 10)) == bar(10, GREEN)
    assert render("thinking", 10, quota=(8, 10)) == bar(8, GREEN)    # frac 0.8
    assert render("thinking", 10, quota=(6, 10)) == bar(6, GREEN)    # frac 0.6
    assert render("thinking", 10, quota=(5, 10)) == bar(5, AMBER)    # frac 0.5 -> amber
    assert render("thinking", 10, quota=(3, 10)) == bar(3, AMBER)
    assert render("thinking", 10, quota=(2, 10)) == bar(2, RED)      # frac 0.2 -> red
    assert render("thinking", 10, quota=(1, 10)) == bar(1, RED)
    assert render("thinking", 10, quota=(0, 10)) == bar(0, RED)


def test_quota_edge_cases():
    assert render("done", 6, quota=(0, 0)) == [EMPTY] * 6
    assert render("done", 6, quota=(5, 0)) == [EMPTY] * 6
    assert render("done", 6, quota=(0.0, 0.0)) == [EMPTY] * 6
    assert render("done", 6, quota=(-3, 6)) == [EMPTY] * 6
    assert render("done", 6, quota=(99, 6)) == [GREEN] * 6
    assert render("done", 6, quota=[3, 6]) == [AMBER] * 3 + [EMPTY] * 3
    assert render("done", 6, quota=(3.0, 6.0)) == [AMBER] * 3 + [EMPTY] * 3
    # unusable quota values are ignored, leaving ordinary state rendering
    for bad in ("nope", (1,), (1, 2, 3), None, 5, (None, 5), ("a", "b"), {"a": 1, "b": 2}):
        assert render("done", 6, quota=bad) == render("done", 6), bad


def test_quota_takes_precedence_over_progress():
    assert render("done", 10, progress=0.1, quota=(9, 10)) == [GREEN] * 9 + [EMPTY]
    # a valid quota wins even when total == 0 (empty bar, not a progress bar)
    assert render("done", 10, progress=0.1, quota=(0, 0)) == [EMPTY] * 10
    # an invalid quota falls through to the progress bar
    assert render("done", 10, progress=0.3, quota="nope") == [DONE] * 3 + [EMPTY] * 7


# ---------------------------------------------------------------------------
# colour overrides
# ---------------------------------------------------------------------------

def test_color_override():
    assert render("done", 3, color="#123456") == [(18, 52, 86)] * 3
    assert render("done", 3, color="123456") == [(18, 52, 86)] * 3
    assert render("done", 3, color=None) == [DONE] * 3
    # invalid hex falls back to the palette colour
    assert render("done", 3, color="#zzzzzz") == [DONE] * 3
    # brightness/animation of the state are preserved
    expected = apply_color((255, 255, 255), 0.85 * breathe_factor(0.0, 1.2))
    assert render("thinking", 2, tick=0.0, color="#ffffff") == [expected] * 2
    # color override does not defeat the blink effect
    assert render("error", 5, tick=0.4, color="#00ff00") == [BLACK] * 5


# ---------------------------------------------------------------------------
# palette robustness
# ---------------------------------------------------------------------------

def test_palette_partial_entries_merge_with_defaults():
    partial = {"idle": {"color": "#ffffff"}}
    expected = apply_color((255, 255, 255), 0.25 * breathe_factor(0.7, 4.0))
    assert render("idle", 3, tick=0.7, palette=partial) == [expected] * 3
    # a state missing from the custom palette keeps its own default entry
    assert render("thinking", 4, tick=0.2, palette=partial) == render("thinking", 4, tick=0.2)
    for palette in ({}, None, ["bad"], 5, "palette"):
        assert render("error", 3, tick=0.1, palette=palette) == render("error", 3, tick=0.1)


def test_palette_custom_states_and_shorthand():
    custom = {"reading": {"color": "#abcdef", "brightness": 1.0,
                          "effect": "solid", "period": 0}}
    assert render("reading", 2, palette=custom) == [(171, 205, 239)] * 2
    # custom state with omitted fields inherits the idle defaults
    seeded = render("reading", 2, tick=0.0, palette={"reading": {"color": "#ffffff"}})
    expected = apply_color((255, 255, 255), 0.25 * breathe_factor(0.0, 4.0))
    assert seeded == [expected] * 2
    # a palette entry may be a bare colour string
    assert render("done", 2, palette={"done": "#112233"}) == [(17, 34, 51)] * 2
    # unusable entries are ignored
    assert render("done", 2, palette={"done": 42}) == [DONE] * 2
    # case/whitespace-insensitive state keys
    assert render("DONE", 2, palette={"done": "#112233"}) == [(17, 34, 51)] * 2


def test_palette_bad_color_falls_back_to_idle_color():
    idle_color = parse_hex_color(DEFAULT_PALETTE["idle"]["color"])
    fallback = apply_color(idle_color, 1.0)
    assert render("done", 2, palette={"done": {"color": "#nope"}}) == [fallback] * 2


def test_brightness_and_period_robustness():
    # brightness is clamped to 0..1
    assert render("done", 2, palette={"done": {"brightness": 2.0}}) == [DONE] * 2
    assert render("error", 2, tick=0.0, palette={"error": {"brightness": -3}}) == [BLACK] * 2
    assert render("done", 1, palette={"done": {"brightness": "0.5"}})[0] == \
        apply_color((46, 204, 113), 0.5)
    # invalid strings are treated as 0
    assert render("done", 2, palette={"done": {"brightness": "loud"}}) == [BLACK] * 2
    assert render("done", 2, palette={"done": {"brightness": float("nan")}}) == [BLACK] * 2
    # non-positive / unusable periods disable the time-based effects
    for period in (0, 0.0, -1.5, "abc", None):
        assert render("thinking", 2, tick=0.9,
                      palette={"thinking": {"period": period}}) == \
            [apply_color(parse_hex_color("#3a86ff"), 0.85)] * 2, period
    # unknown effect names degrade to solid
    assert render("done", 2, palette={"done": {"effect": "rainbow", "brightness": 1.0}}) == \
        [DONE] * 2


# ---------------------------------------------------------------------------
# misc robustness / purity
# ---------------------------------------------------------------------------

def test_invalid_hex_degradation():
    # bad empty_color -> black
    assert render("done", 2, progress=0.5, empty_color="nonsense") == \
        [DONE] + [BLACK]
    # bad progress_color -> state colour
    assert render("done", 2, progress=0.5, progress_color="nonsense") == \
        [DONE] + [EMPTY]
    # bad explicit color -> palette colour
    assert render("done", 2, color="not-a-color") == [DONE] * 2


def test_non_finite_tick_is_treated_as_zero():
    baseline = render("thinking", 3, tick=0.0)
    for tick in (float("nan"), float("inf"), float("-inf"), "oops", None):
        assert render("thinking", 3, tick=tick) == baseline, tick
    assert len(render("thinking", 3, tick=-5.0)) == 3


def test_purity_and_no_mutation():
    palette_snapshot = copy.deepcopy(DEFAULT_PALETTE)
    custom = {"done": {"color": "#abcdef", "brightness": 0.8}}
    custom_snapshot = copy.deepcopy(custom)

    first = render("done", 4, tick=1.23, palette=custom)
    second = render("done", 4, tick=1.23, palette=custom)

    assert first == second
    assert first is not second
    assert custom == custom_snapshot
    assert DEFAULT_PALETTE == palette_snapshot
    # returned pixels are plain tuples, detached from the palette
    assert isinstance(first[0], tuple)
    assert first[0] == apply_color((171, 205, 239), 0.8)


# ---------------------------------------------------------------------------

TESTS = [
    test_default_palette_matches_contract,
    test_hex_parsing,
    test_shape_and_bounds,
    test_zero_led_count_returns_empty_list,
    test_led_count_coercion_and_single_led,
    test_off_state_is_always_black,
    test_done_state_is_solid,
    test_breathe_matches_contract_formula,
    test_breathe_monotonic_and_bounded,
    test_blink_alternates,
    test_pulse_varies_and_is_bounded,
    test_unknown_state_falls_back_to_idle,
    test_state_name_normalization,
    test_progress_bar_basic_and_boundaries,
    test_progress_uses_progress_color_and_empty_color,
    test_progress_clamps_and_uses_python_round,
    test_progress_overrides_animation,
    test_quota_tiers,
    test_quota_edge_cases,
    test_quota_takes_precedence_over_progress,
    test_color_override,
    test_palette_partial_entries_merge_with_defaults,
    test_palette_custom_states_and_shorthand,
    test_palette_bad_color_falls_back_to_idle_color,
    test_brightness_and_period_robustness,
    test_invalid_hex_degradation,
    test_non_finite_tick_is_treated_as_zero,
    test_purity_and_no_mutation,
]


def main():
    for test in TESTS:
        test()
    print("OK")


if __name__ == "__main__":
    main()