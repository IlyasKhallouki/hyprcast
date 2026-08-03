"""
Streaming waybar module. `hyprcast waybar` prints one JSON object per line and
flushes every one -- a `custom` module with no `interval` and no `signal` reads
stdout continuously, and a bar that is not flushed after every line just sits
there showing stale text.

It subscribes to the control socket instead of polling it, so a state change
reaches the bar immediately, and the server's 1 s heartbeat carries a fresh
snapshot so the session timer keeps counting. With no session it emits `idle`
and retries the connection, forever. This process must never exit on its own.

Five states, each with its own `alt` (which picks the glyph out of
format-icons) and its own CSS class:

    idle  discovering  connecting  casting  error

plus two modifier classes, `muted` and `degraded`, appended to `class` when
they apply. `percentage` is only present while casting, where it is the share
of the target frame rate actually being delivered -- the one number here that
degrades for a real reason. Every other state omits it rather than pad it with
a zero the bar would happily draw.

Config snippet to paste: packaging/waybar-module.jsonc
"""

from __future__ import annotations

import json
import sys
import time

from .ctl import Client, CtlError

__all__ = ["run", "render", "peer_label", "duration", "rate"]

RETRY_S = 2.0

# The five states, in the order they normally happen.
STATES = ("idle", "discovering", "connecting", "casting", "error")

# Glyphs live in packaging/waybar-module.jsonc under format-icons, keyed by the
# `alt` below, so they can be changed without touching this file. The one
# exception is the mute marker: there is no second alt to hang it on.
# U+F026 is nf-fa-volume_off, Font Awesome 4 block, unmoved in Nerd Fonts v3.
MUTED_GLYPH = "\uf026"


def rate(bits_per_second: float) -> str:
    """A bit rate a human reads at a glance."""
    value = float(bits_per_second or 0.0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f} Mb/s"
    if value >= 1_000:
        return f"{value / 1_000:.0f} kb/s"
    return f"{value:.0f} b/s"


def duration(seconds: float) -> str:
    """m:ss, or h:mm:ss once it has been running that long."""
    total = int(seconds or 0)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def peer_label(state: dict) -> str:
    """What to call the sink: its name, its address, or neither."""
    name = str(state.get("peer_name") or "").strip()
    addr = str(state.get("peer") or "").strip()
    if name and addr and name != addr:
        return f"{name} ({addr})"
    return name or addr or "unknown sink"


def _mode_label(value) -> str:
    """`second-screen` is what the code calls it; `extend` is what people do."""
    return "extend" if str(value) == "second-screen" else str(value or "mirror")


def _escape(text: str) -> str:
    """waybar renders tooltips as pango markup, so a sink called `A & B` would
    otherwise silently blank the whole tooltip."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _tooltip(lines: list[str]) -> str:
    return _escape("\n".join(line for line in lines if line))


def _idle(reason: str = "") -> dict:
    return {
        "text": "",
        "alt": "idle",
        "tooltip": _tooltip([
            "hyprcast: idle",
            reason or "no session -- run `hyprcast cast`",
        ]),
        "class": ["idle"],
    }


def render(state: dict | None) -> dict:
    """Map a session snapshot onto waybar's text/alt/tooltip/class/percentage.

    Returns a dict with no `percentage` key at all unless the number would mean
    something, which is the only honest thing to do with a field the bar draws
    as a progress bar.
    """
    if not state:
        return _idle()

    name = str(state.get("state") or "idle")
    if name not in STATES:
        name = "idle"
    if name == "idle":
        return _idle()

    stats = state.get("stats") or {}
    width = int(state.get("width") or 0)
    height = int(state.get("height") or 0)
    target_fps = int(state.get("fps") or 0)
    live_fps = float(stats.get("fps") or 0.0)
    drops = int(stats.get("drops") or 0)
    muted = bool(state.get("muted"))
    peer = peer_label(state)
    classes = [name]
    payload: dict = {"alt": name}

    if name == "discovering":
        payload["text"] = "scanning"
        payload["tooltip"] = _tooltip([
            "hyprcast: looking for a sink",
            str(state.get("detail") or "the TV must be showing its Miracast screen"),
        ])
    elif name == "connecting":
        payload["text"] = peer if state.get("peer_name") or state.get("peer") else "connecting"
        payload["tooltip"] = _tooltip([
            f"hyprcast: connecting to {peer}",
            str(state.get("detail") or "forming the Wi-Fi Direct group"),
        ])
    elif name == "error":
        payload["text"] = "error"
        payload["tooltip"] = _tooltip([
            "hyprcast: error",
            str(state.get("error") or "no detail was reported"),
        ])
    else:  # casting
        payload["text"] = (f"{height}p{target_fps}" if height and target_fps
                           else "casting") + (f" {MUTED_GLYPH}" if muted else "")
        measured = rate(float(stats.get("kbps") or 0.0) * 1000.0)
        target = rate(float(state.get("bitrate") or 0.0))
        payload["tooltip"] = _tooltip([
            f"Casting to {peer}",
            f"{width}x{height} @ {target_fps} fps · {measured} of {target}"
            if width and height else f"{measured} of {target}",
            f"{_mode_label(state.get('mode'))} · "
            f"{state.get('capture_output') or 'unknown output'} · "
            f"{duration(state.get('duration', 0.0))}",
            "muted" if muted else f"volume {int(state.get('volume', 100))}%",
            f"drops {drops}" if drops else "",
        ])
        if target_fps and live_fps:
            payload["percentage"] = max(0, min(100, round(live_fps / target_fps * 100)))
        if muted:
            classes.append("muted")
        if drops:
            classes.append("degraded")

    payload["class"] = classes
    return payload


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
    _emit(_idle(), last)
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
        _emit(_idle(), last)
        time.sleep(RETRY_S)
