"""
Streaming waybar module. `hyprcast waybar` prints one JSON object per line and
flushes every time -- a waybar `custom` module with no `interval` and no
`signal` reads stdout continuously, and a bar that is not flushed after every
line simply freezes with stale text.

It subscribes to the control socket rather than polling it, so state changes
show up immediately; the server's 1 s heartbeat re-emits so the session timer
in the tooltip keeps counting. When no session is running it emits `idle` and
retries the connection, forever. This process must never exit on its own.

Config snippet to paste: packaging/waybar-module.jsonc
"""

from __future__ import annotations

import json
import sys
import time

from .ctl import Client, CtlError

__all__ = ["run", "render"]

RETRY_S = 2.0

_CLASSES = {
    "idle": "idle",
    "discovering": "discovering",
    "connecting": "connecting",
    "casting": "casting",
    "error": "error",
}

_IDLE = {
    "text": "hyprcast",
    "alt": "idle",
    "tooltip": "hyprcast: idle\nRun `hyprcast cast` to start.",
    "class": "idle",
    "percentage": 0,
}


def _human_rate(bps: float) -> str:
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mb/s"
    if bps >= 1_000:
        return f"{bps / 1_000:.0f} kb/s"
    return f"{bps:.0f} b/s"


def _duration(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def render(state: dict | None) -> dict:
    """Map a session snapshot onto waybar's text/alt/tooltip/class/percentage."""
    if not state:
        return dict(_IDLE)

    name = str(state.get("state", "idle"))
    css = _CLASSES.get(name, "idle")
    stats = state.get("stats") or {}
    width = int(state.get("width", 0))
    height = int(state.get("height", 0))
    target_fps = int(state.get("fps", 0)) or 1
    live_fps = float(stats.get("fps", 0.0))
    kbps = float(stats.get("kbps", 0.0))
    peer = state.get("peer_name") or state.get("peer") or "no sink"
    duration = float(state.get("duration", 0.0))

    if name == "casting":
        text = f"{height}p{target_fps} {live_fps:.0f}fps"
        percentage = max(0, min(100, round(live_fps / target_fps * 100)))
    elif name == "error":
        text = "hyprcast: error"
        percentage = 0
    elif name in ("connecting", "discovering"):
        text = f"hyprcast: {name}"
        percentage = 0
    else:
        return dict(_IDLE)

    lines = [f"hyprcast: {name}"]
    if name == "error" and state.get("error"):
        lines.append(str(state["error"]))
    lines.append(f"peer:       {peer}")
    if width and height:
        lines.append(f"resolution: {width}x{height} @ {target_fps} fps")
    if name == "casting":
        lines.append(f"measured:   {live_fps:.1f} fps, {_human_rate(kbps * 1000)}")
        lines.append(f"bitrate:    {_human_rate(float(state.get('bitrate', 0)))} target")
        lines.append(f"capture:    {state.get('capture_output') or '?'} "
                     f"({state.get('mode', '?')})")
        vol = state.get("volume", 100)
        lines.append(f"volume:     {'muted' if state.get('muted') else f'{vol}%'} "
                     f"({state.get('audio_mode', 'shared')})")
        lines.append(f"duration:   {_duration(duration)}")

    return {
        "text": text,
        "alt": css,
        "tooltip": "\n".join(lines),
        "class": css,
        "percentage": percentage,
    }


def _emit(payload: dict, last: list) -> None:
    if payload == last[0]:
        return
    last[0] = payload
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def run() -> int:
    client = Client(timeout=2.0)
    last: list = [None]
    latest: dict | None = None
    _emit(dict(_IDLE), last)
    while True:
        try:
            for update in client.subscribe():
                if update is not None:
                    latest = update
                _emit(render(latest), last)
        except CtlError:
            pass
        except (BrokenPipeError, KeyboardInterrupt):
            return 0
        except OSError:
            pass
        latest = None
        _emit(dict(_IDLE), last)
        time.sleep(RETRY_S)
