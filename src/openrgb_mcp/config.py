"""Configuration loading for openrgb-mcp.

Resolution order (later wins):

1. built-in defaults
2. ``config.json`` next to the repository root (or ``--config <path>``)
3. environment variables (``OPENRGB_PORT``, ``OPENRGB_DEVICE``)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_CONFIG: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": None,        # None = auto-discover
    "device": None,      # device name substring; None = first device
    "fps": 12,
    "brightness": 1.0,
    "animate": True,
    "done_hold_seconds": 8,
    # "global"  = whole strip as one colour/brightness (fans, RGB strips)
    # "per_led" = write every LED individually (addressable strips only)
    "control": "global",
    # hard ceiling for the brightness we ever ask the hardware for
    "max_brightness": 0.7,
    "palette": {},
}


def package_root() -> Path:
    """Repository root (three levels up from this file: src/openrgb_mcp/x.py)."""
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    env = os.environ.get("OPENRGB_MCP_CONFIG")
    if env:
        return Path(env)
    return package_root() / "config.json"


def load_config(path: Optional[str | os.PathLike] = None) -> Dict[str, Any]:
    """Load the effective configuration dictionary."""
    config = dict(DEFAULT_CONFIG)
    config_path = Path(path) if path else default_config_path()
    data: Dict[str, Any] = {}
    if config_path.is_file():
        try:
            with open(config_path, "r", encoding="utf-8-sig") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    for key, value in data.items():
        if value is not None or key == "port":
            config[key] = value

    env_port = os.environ.get("OPENRGB_PORT", "").strip()
    if env_port.isdigit():
        config["port"] = int(env_port)
    env_device = os.environ.get("OPENRGB_DEVICE", "").strip()
    if env_device:
        config["device"] = env_device
    config["config_path"] = str(config_path)
    return config


def state_path() -> Path:
    """Where the runtime state (port, device, original colours) is cached."""
    env = os.environ.get("OPENRGB_MCP_STATE")
    if env:
        return Path(env)
    return package_root() / ".state.json"
