"""Pure-function LED renderer for the OpenRGB MCP server (CONTRACT.md §5).

The module is deliberately dependency-free (Python standard library only) and
pure: no sockets, no files, no third-party packages, no mutable module state
that ``render()`` writes.  ``render()`` maps a session state onto exactly
``led_count`` RGB pixels, optionally rendered as a progress or remaining-quota
bar.

Animation model (``tick`` is seconds relative to an arbitrary origin):

    breathe   brightness = base * (0.55 + 0.45 * (0.5 + 0.5*sin(2*pi*tick/period)))
    blink     lit during the first half of ``period``, dark during the second
    pulse     smooth square pulse, brightness factor in [0.2, 1.0] of ``base``
    solid     full ``base`` brightness
    off       always black

``breathe``, ``blink`` and ``pulse`` need a positive ``period``; a zero/absent
period makes them behave like ``solid``.

Bars (``progress`` / ``quota``) are static -- ``tick`` is ignored for them --
and take precedence over the state effect: lit cells use the bar colour at full
intensity and unlit cells use ``empty_color``.  If both ``quota`` and
``progress`` are supplied, ``quota`` wins.

Robustness rules (graceful degradation instead of raising):

* ``led_count`` <= 0 or non-numeric -> ``[]``.
* unknown/non-string ``state`` -> ``idle``; state names are stripped and
  lower-cased.
* missing palette entries/keys fall back to :data:`DEFAULT_PALETTE`; a palette
  entry may also be a bare ``"#RRGGBB"`` string (colour shorthand) or any dict
  with a subset of the fields.
* unparsable hex falls back in this order: explicit ``color`` -> palette
  colour -> idle palette colour -> black.  A bad ``progress_color`` falls back
  to the state colour, a bad ``empty_color`` to black.
* ``brightness`` is clamped to 0..1; ``period`` <= 0 disables animation;
  non-finite ``tick`` / ``progress`` values behave like 0.

Quota tiers (by remaining fraction): ``> 0.5`` green ``#2ecc71``,
``0.2 < frac <= 0.5`` amber ``#ffb703``, ``frac <= 0.2`` red ``#ff4d4f``.
"""

from __future__ import annotations

import math

__all__ = ["DEFAULT_PALETTE", "STATES", "render", "parse_hex_color"]


DEFAULT_PALETTE = {
    "idle":     {"color": "#2a3a5a", "brightness": 0.25, "effect": "breathe", "period": 4.0},
    "thinking": {"color": "#3a86ff", "brightness": 0.85, "effect": "breathe", "period": 1.2},
    "working":  {"color": "#00d4ff", "brightness": 0.9,  "effect": "breathe", "period": 2.0},
    "waiting":  {"color": "#ffb703", "brightness": 0.9,  "effect": "pulse",   "period": 1.5},
    "done":     {"color": "#2ecc71", "brightness": 1.0,  "effect": "solid",   "period": 0},
    "error":    {"color": "#ff4d4f", "brightness": 1.0,  "effect": "blink",   "period": 0.5},
    "off":      {"color": "#000000", "brightness": 0.0,  "effect": "off",     "period": 0},
}

STATES = tuple(DEFAULT_PALETTE)

_EMPTY_DEFAULT_HEX = "#0a0a0f"

_QUOTA_GREEN_HEX = "#2ecc71"
_QUOTA_AMBER_HEX = "#ffb703"
_QUOTA_RED_HEX = "#ff4d4f"

_BLACK = (0, 0, 0)


def parse_hex_color(value: object) -> tuple[int, int, int] | None:
    """Parse ``"#RRGGBB"`` / ``"RRGGBB"`` into an ``(r, g, b)`` tuple.

    Returns ``None`` for anything that is not a 6-digit hex colour string
    (3-digit shorthand, named colours, bytes, numbers, ...).  Surrounding
    whitespace and a leading ``#`` are ignored.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) != 6:
        return None
    try:
        red = int(text[0:2], 16)
        green = int(text[2:4], 16)
        blue = int(text[4:6], 16)
    except ValueError:
        return None
    return (red, green, blue)


_QUOTA_GREEN = parse_hex_color(_QUOTA_GREEN_HEX)
_QUOTA_AMBER = parse_hex_color(_QUOTA_AMBER_HEX)
_QUOTA_RED = parse_hex_color(_QUOTA_RED_HEX)


def _number(value: object, default: float | None = None) -> float | None:
    """Best-effort finite float conversion (``None`` for anything unusable)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _led_count(value: object) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 0
    return count if count > 0 else 0


def _normalize_state(state: object) -> str:
    if isinstance(state, str):
        name = state.strip().lower()
        if name:
            return name
    return "idle"


def _entry(base: dict, override: object) -> dict:
    """Merge a palette override onto a default entry (copying both)."""
    if isinstance(override, dict):
        spec = dict(base)
        spec.update(override)
        return spec
    if isinstance(override, str):
        # Convenience shorthand: a palette entry may be just a colour string.
        spec = dict(base)
        spec["color"] = override
        return spec
    return dict(base)


def _resolve_palette(palette: object) -> dict:
    """Merge ``palette`` over :data:`DEFAULT_PALETTE`, copying every entry."""
    custom = palette if isinstance(palette, dict) else {}
    resolved: dict = {}
    for name, defaults in DEFAULT_PALETTE.items():
        resolved[name] = _entry(defaults, custom.get(name))
    for name, override in custom.items():
        if not isinstance(name, str):
            continue
        key = name.strip().lower()
        if not key:
            continue
        base = DEFAULT_PALETTE.get(key, DEFAULT_PALETTE["idle"])
        resolved[key] = _entry(base, override)
    return resolved


def _base_color(spec: dict, color_override: object) -> tuple[int, int, int]:
    """Resolve the lit colour: explicit override > palette > idle > black."""
    for candidate in (color_override, spec.get("color"), DEFAULT_PALETTE["idle"]["color"]):
        parsed = parse_hex_color(candidate)
        if parsed is not None:
            return parsed
    return _BLACK


def _pick_color(value: object, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    parsed = parse_hex_color(value)
    return fallback if parsed is None else parsed


def _brightness(spec: dict) -> float:
    value = _number(spec.get("brightness"))
    if value is None:
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _period(value: object) -> float:
    period = _number(value)
    if period is None or period <= 0.0:
        return 0.0
    return period


def _effect(spec: dict) -> str:
    value = spec.get("effect")
    if isinstance(value, str):
        return value.strip().lower()
    return "solid"


def _wave_factor(effect: str, tick: float, period: float) -> float:
    """Brightness multiplier in [0.0, 1.0] for ``effect`` at ``tick``."""
    if effect == "off":
        return 0.0
    if effect == "solid":
        return 1.0
    if period <= 0.0:
        # Time-based effects without a usable period render as solid.
        return 1.0

    phase = (tick % period) / period

    if effect == "blink":
        return 1.0 if phase < 0.5 else 0.0

    angle = 2.0 * math.pi * phase

    if effect == "breathe":
        return 0.55 + 0.45 * (0.5 + 0.5 * math.sin(angle))

    if effect == "pulse":
        # Smooth square pulse: fast rise/fall around a soft plateau.
        return 0.2 + 0.8 * (0.5 + 0.5 * math.tanh(2.5 * math.sin(angle)))

    # Unknown effect names degrade to a steady colour.
    return 1.0


def _channel(value: int, factor: float) -> int:
    scaled = int(round(value * factor))
    if scaled < 0:
        return 0
    if scaled > 255:
        return 255
    return scaled


def _scale_color(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    red, green, blue = color
    return (
        _channel(red, factor),
        _channel(green, factor),
        _channel(blue, factor),
    )


def _filled_count(fraction: float, led_count: int) -> int:
    clamped = 0.0 if fraction < 0.0 else 1.0 if fraction > 1.0 else fraction
    return int(round(clamped * led_count))


def _quota_fraction(quota: object) -> tuple[bool, float]:
    """Return ``(is_valid, remaining_fraction)`` for a ``(remaining, total)``."""
    try:
        remaining_raw, total_raw = quota  # type: ignore[misc]
    except (TypeError, ValueError):
        return False, 0.0
    remaining = _number(remaining_raw)
    total = _number(total_raw)
    if remaining is None or total is None:
        return False, 0.0
    if total <= 0.0:
        return True, 0.0
    fraction = remaining / total
    if fraction < 0.0:
        return True, 0.0
    if fraction > 1.0:
        return True, 1.0
    return True, fraction


def _quota_color(fraction: float) -> tuple[int, int, int]:
    if fraction > 0.5:
        return _QUOTA_GREEN
    if fraction > 0.2:
        return _QUOTA_AMBER
    return _QUOTA_RED


def _bar_pixels(led_count: int, filled: int,
                lit: tuple[int, int, int],
                empty: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    filled = 0 if filled < 0 else led_count if filled > led_count else filled
    return [lit] * filled + [empty] * (led_count - filled)


def render(state: str, led_count: int, tick: float = 0.0,
           palette: dict | None = None,
           color: str | None = None,
           progress: float | None = None,
           progress_color: str | None = None,
           empty_color: str = "#0a0a0f",
           quota: tuple[int, int] | None = None,
           ) -> list[tuple[int, int, int]]:
    """Render ``state`` into exactly ``led_count`` ``(r, g, b)`` pixels.

    ``progress`` (0..1) draws a progress bar; ``quota`` (remaining, total)
    draws a remaining-budget bar coloured green/amber/red.  Both override the
    state colour and animation (see module docstring).  Always returns a list
    of ``led_count`` tuples of ints in 0..255, or ``[]`` when ``led_count``
    is not positive.
    """
    count = _led_count(led_count)
    if count == 0:
        return []

    resolved = _resolve_palette(palette)
    name = _normalize_state(state)
    spec = resolved.get(name) or resolved["idle"]

    base = _base_color(spec, color)
    empty = _pick_color(empty_color, _BLACK)

    quota_valid, quota_fraction = _quota_fraction(quota)
    if quota_valid:
        filled = _filled_count(quota_fraction, count)
        return _bar_pixels(count, filled, _quota_color(quota_fraction), empty)

    if progress is not None:
        fraction = _number(progress, 0.0)
        filled = _filled_count(fraction, count)
        lit = _pick_color(progress_color, base)
        return _bar_pixels(count, filled, lit, empty)

    brightness = _brightness(spec)
    effect = _effect(spec)
    period = _period(spec.get("period"))
    tick_value = _number(tick, 0.0)
    factor = _wave_factor(effect, tick_value, period)
    lit = _scale_color(base, brightness * factor)
    return [lit] * count