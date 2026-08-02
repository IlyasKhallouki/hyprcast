"""
hyprcast control plane -- Python 3.14, standard library only.

The media path is native C (engine/, contract in engine/src/hc.h). This package
is the part a human touches: the engine seam, Hyprland output management,
PipeWire routing, the control socket, the waybar module and the CLI.

Submodules are NOT imported here on purpose -- `hyprcast waybar` must start
without paying for hyprctl or pactl, and `import hyprcast.engine` must work on
a machine with no compositor at all.
"""

from .engine import Engine, EngineError, engine_binary

__version__ = "0.2.0"

__all__ = [
    "Engine",
    "EngineError",
    "engine_binary",
    "audio",
    "ctl",
    "engine",
    "hypr",
    "session",
    "waybar",
]
