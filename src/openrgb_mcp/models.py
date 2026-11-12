"""Typed output models for the openrgb-mcp tools.

Using Pydantic models (instead of bare dicts) lets the MCP SDK publish a real
``outputSchema`` for every tool and validate the ``structuredContent`` it
returns - the structured-output part of the MCP specification.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from pydantic import BaseModel, Field


class SegmentInfo(BaseModel):
    name: str
    start: int
    count: int


class ZoneInfo(BaseModel):
    index: int
    name: str
    type: str
    led_count: int
    start_idx: int
    matrix: Optional[List[int]] = Field(default=None, description="[height, width] for matrix zones")
    segments: List[SegmentInfo] = Field(default_factory=list)


class ModeInfo(BaseModel):
    index: int
    name: str
    value: int
    per_led: bool = Field(description="supports per-LED colour control")
    has_speed: bool
    has_brightness: bool
    color_mode: int


class DeviceInfo(BaseModel):
    index: int
    name: str
    type: str
    vendor: str
    description: str
    location: str
    led_count: int
    zone_count: int
    zones: List[ZoneInfo]
    modes: List[ModeInfo]
    active_mode: int
    active_mode_name: Optional[str]
    active_mode_per_led: bool


class DeviceListResult(BaseModel):
    port: int
    protocol_version: int
    selected: str
    led_count: int = Field(description="total LEDs of the selected device (progress bar length)")
    devices: List[DeviceInfo]


class StatusResult(BaseModel):
    state: str
    detail: Optional[str]
    led_count: Optional[int]
    device: Optional[str]
    port: Optional[int]
    protocol_version: Optional[int]
    animation_running: bool
    animate: bool
    fps: int
    brightness: float
    progress: Optional[float] = Field(default=None, description="0..1")
    quota: Optional[Tuple[int, int]] = Field(default=None, description="(remaining, total)")
    custom_color: Optional[str]
    state_age_s: float
    last_error: Optional[str]
    active_mode: Optional[str]
    zones: List[dict] = Field(default_factory=list)
    # progress / quota extras
    percent: Optional[float] = None
    filled_leds: Optional[int] = None
    remaining: Optional[float] = None
    total: Optional[float] = None
    ratio: Optional[float] = None
    restored: Optional[bool] = None
