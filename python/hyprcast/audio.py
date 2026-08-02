"""
PipeWire audio control for hyprcast, via `pactl` (pipewire-pulse). Stdlib only.

Two routing modes, both measured on this box (PipeWire 1.6.7, pactl 17.0-98):

  shared  -- capture the default sink's `.monitor`. Sound plays on the laptop
             AND goes to the TV. Nothing is reconfigured; nothing to restore.

  tv-only -- `pactl load-module module-null-sink sink_name=hyprcast`, move every
             existing playback stream onto it, and make it the default sink so
             new streams follow. The engine captures `hyprcast.monitor`, and the
             speakers stay silent because nothing feeds them any more.

tv-only was verified end to end: the null sink appears as a normal sink with a
`hyprcast.monitor` source at 48000 Hz / 2ch, `pactl move-sink-input` relocates a
live stream (checked with pw-play), and `unload-module` removes the sink and
its monitor cleanly.

Two honest caveats, neither flaky but both worth knowing:

  * PipeWire does not tell us WHY a stream sits on a sink, so a stream the user
    deliberately pinned elsewhere is indistinguishable from a default-routed
    one. `--audio tv-only` moves all of them, and on teardown moves whatever is
    still on the null sink back to the sink that was default when we started.
    A stream the user pinned to a third sink mid-cast is restored to the
    original default, not to their pin. That is the only fidelity loss.
  * Object indices are recycled across module loads (measured: sink index went
    876 -> 886 for the same sink_name). Everything here resolves by NAME.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

__all__ = [
    "AudioError",
    "Sink",
    "list_sinks",
    "default_sink",
    "default_monitor_source",
    "resolve_monitor",
    "NullSinkRoute",
]

NULL_SINK_NAME = "hyprcast"
_CALL_TIMEOUT = 5.0


class AudioError(RuntimeError):
    """pactl was unreachable or refused a command."""


def _pactl(args: list[str]) -> str:
    try:
        proc = subprocess.run(["pactl"] + args, capture_output=True, text=True,
                              timeout=_CALL_TIMEOUT)
    except FileNotFoundError:
        raise AudioError("pactl not found in PATH (install libpulse / pipewire-pulse)") from None
    except subprocess.TimeoutExpired:
        raise AudioError(f"pactl {' '.join(args)} timed out") from None
    if proc.returncode != 0:
        raise AudioError(f"pactl {' '.join(args)}: {proc.stderr.strip() or 'failed'}")
    return proc.stdout.strip()


def _pactl_json(args: list[str]):
    out = _pactl(["-f", "json"] + args)
    try:
        return json.loads(out) if out else []
    except json.JSONDecodeError:
        raise AudioError(f"pactl {' '.join(args)} returned non-JSON") from None


@dataclass(frozen=True)
class Sink:
    index: int
    name: str
    description: str
    monitor_source: str
    state: str
    mute: bool
    volume_pct: int
    is_default: bool

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "description": self.description,
            "monitor": self.monitor_source,
            "state": self.state,
            "mute": self.mute,
            "volume": self.volume_pct,
            "default": self.is_default,
        }


def _volume_pct(raw: dict) -> int:
    vols = raw.get("volume") or {}
    if not isinstance(vols, dict) or not vols:
        return 0
    total = 0
    n = 0
    for chan in vols.values():
        try:
            total += int(str(chan.get("value_percent", "0%")).rstrip("%"))
            n += 1
        except (ValueError, AttributeError):
            continue
    return total // n if n else 0


def default_sink() -> str:
    return _pactl(["get-default-sink"])


def list_sinks() -> list[Sink]:
    try:
        current = default_sink()
    except AudioError:
        current = ""
    out = []
    for raw in _pactl_json(["list", "sinks"]):
        out.append(Sink(
            index=int(raw.get("index", -1)),
            name=raw.get("name", ""),
            description=raw.get("description", ""),
            # pactl's JSON source objects do not carry monitor_of_sink on this
            # build, so the sink's own monitor_source is the only reliable link.
            monitor_source=raw.get("monitor_source", ""),
            state=raw.get("state", ""),
            mute=bool(raw.get("mute", False)),
            volume_pct=_volume_pct(raw),
            is_default=raw.get("name", "") == current,
        ))
    return out


def resolve_monitor(sink_name: str) -> str:
    """Monitor source for a sink name. Empty string if the sink is unknown."""
    for sink in list_sinks():
        if sink.name == sink_name:
            return sink.monitor_source
    return ""


def default_monitor_source() -> str:
    """The `.monitor` of the current default sink -- what `shared` mode captures."""
    name = default_sink()
    monitor = resolve_monitor(name)
    if not monitor:
        raise AudioError(f"default sink {name!r} has no monitor source")
    return monitor


def set_sink_volume(sink_name: str, percent: int) -> None:
    percent = max(0, min(150, int(percent)))
    _pactl(["set-sink-volume", sink_name, f"{percent}%"])


def set_sink_mute(sink_name: str, muted: bool) -> None:
    _pactl(["set-sink-mute", sink_name, "1" if muted else "0"])


def _sink_inputs() -> list[tuple[int, int]]:
    return [(int(s["index"]), int(s["sink"])) for s in _pactl_json(["list", "sink-inputs"])]


def _sink_name_by_index(index: int) -> str:
    for sink in list_sinks():
        if sink.index == index:
            return sink.name
    return ""


class NullSinkRoute:
    """
    A dedicated null sink so cast audio reaches the TV ONLY.

    Every mutation is recorded so close() puts the system back. close() is
    idempotent and never raises -- it runs from teardown paths.
    """

    def __init__(self, name: str = NULL_SINK_NAME, rate: int = 48000, channels: int = 2):
        self.name = name
        self._module_id = ""
        self._prev_default = ""
        self._moved: list[int] = []
        self.monitor_source = ""

        try:
            self._prev_default = default_sink()
        except AudioError:
            self._prev_default = ""

        # A stale sink from a crashed session would shadow ours; drop it first.
        self._unload_stale()

        args = [
            "load-module", "module-null-sink",
            f"sink_name={name}",
            "media.class=Audio/Sink",
            f"audio.rate={rate}",
            f"audio.channels={channels}",
            "audio.position=FL,FR",
            f"sink_properties=device.description=hyprcast",
        ]
        module_id = _pactl(args)
        if not module_id.isdigit():
            raise AudioError(f"module-null-sink did not return a module id: {module_id!r}")
        self._module_id = module_id

        self.monitor_source = resolve_monitor(name)
        if not self.monitor_source:
            self.close()
            raise AudioError(f"null sink {name!r} loaded but exposes no monitor source")

    def _unload_stale(self) -> None:
        for sink in list_sinks():
            if sink.name != self.name:
                continue
            owner = None
            for raw in _pactl_json(["list", "sinks"]):
                if raw.get("name") == self.name:
                    owner = raw.get("owner_module")
            if isinstance(owner, int) and 0 <= owner < 0xFFFFFFFF:
                try:
                    _pactl(["unload-module", str(owner)])
                except AudioError:
                    pass

    def capture_all(self, set_default: bool = True) -> int:
        """Move every current playback stream onto the null sink. Returns count."""
        target_index = -1
        for sink in list_sinks():
            if sink.name == self.name:
                target_index = sink.index
        moved = 0
        for stream, sink_index in _sink_inputs():
            if sink_index == target_index:
                continue
            try:
                _pactl(["move-sink-input", str(stream), self.name])
            except AudioError:
                continue
            self._moved.append(stream)
            moved += 1
        if set_default:
            try:
                _pactl(["set-default-sink", self.name])
            except AudioError:
                pass
        return moved

    def close(self) -> None:
        if not self._module_id:
            return
        module_id, self._module_id = self._module_id, ""

        if self._prev_default:
            try:
                _pactl(["set-default-sink", self._prev_default])
            except AudioError:
                pass

        # Anything still parked on the null sink goes back to the old default,
        # otherwise unloading the module would silently strand it.
        if self._prev_default:
            try:
                target_index = -1
                for sink in list_sinks():
                    if sink.name == self.name:
                        target_index = sink.index
                for stream, sink_index in _sink_inputs():
                    if sink_index == target_index:
                        _pactl(["move-sink-input", str(stream), self._prev_default])
            except AudioError:
                pass

        try:
            _pactl(["unload-module", module_id])
        except AudioError:
            pass
        self.monitor_source = ""

    def __enter__(self) -> "NullSinkRoute":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


def cleanup_stale_null_sink(name: str = NULL_SINK_NAME) -> bool:
    """Drop a hyprcast null sink left behind by a crashed session."""
    try:
        for raw in _pactl_json(["list", "sinks"]):
            if raw.get("name") != name:
                continue
            owner = raw.get("owner_module")
            if isinstance(owner, int) and 0 <= owner < 0xFFFFFFFF:
                _pactl(["unload-module", str(owner)])
                return True
    except AudioError:
        pass
    return False


def runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
