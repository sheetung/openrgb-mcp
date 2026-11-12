"""openrgb-mcp — AI session status lights for OpenRGB.

A standard Model Context Protocol server that mirrors an agent's conversation
state (thinking / working / waiting / done / error / idle) onto OpenRGB
hardware, and uses the device's LED count as a progress or remaining-budget
bar.

Public entry point: ``openrgb_mcp.server:main`` (console script
``openrgb-mcp``).
"""

__version__ = "0.2.0"
__all__ = ["__version__"]
