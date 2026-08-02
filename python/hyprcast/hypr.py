"""
Hyprland integration for hyprcast.

Everything goes through `hyprctl -j` so we never parse human-formatted output.
Standard library only.

Measured against Hyprland 0.55.4 on this box -- these are not assumptions:

  * `hyprctl output create headless NAME` HONOURS the name in 0.55.4, but the
    documented behaviour is that the compositor picks `HEADLESS-N`. We diff the
    monitor list around the call and use whatever actually appeared, so both
    behaviours work.
  * `hyprctl` exits 0 even when it failed. `output remove nosuch` prints
    "output not found" and returns 0; `dispatch moveworkspacetomonitor 3 nosuch`
    prints "Monitor not found" and returns 0. The reply body is the only signal.
  * A headless output that has been put into `mirror` mode DISAPPEARS from
    `hyprctl monitors` (it is only in `monitors all`) and `output remove` then
    answers "output not found" -- the output is leaked for the life of the
    session. Un-mirror it first, then remove. `destroy_headless` does that.
    hyprcast therefore never mirrors a headless output: mirror mode captures
    the real monitor directly and lets the engine's VPP scale it.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

__all__ = [
    "HyprError",
    "Monitor",
    "list_monitors",
    "create_headless",
    "destroy_headless",
    "move_workspace",
    "workspace_monitors",
    "focus_monitor",
    "HeadlessOutput",
    "reap_orphans",
]

_CALL_TIMEOUT = 5.0
_SETTLE_TIMEOUT = 3.0
_SETTLE_POLL = 0.05


class HyprError(RuntimeError):
    """hyprctl was unreachable, or answered with something other than ok."""


def _state_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, "hyprcast-outputs.json")


def _hyprctl(args: list[str], want_json: bool = False):
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        raise HyprError("HYPRLAND_INSTANCE_SIGNATURE is unset -- not in a Hyprland session")
    cmd = ["hyprctl"] + (["-j"] if want_json else []) + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_CALL_TIMEOUT)
    except FileNotFoundError:
        raise HyprError("hyprctl not found in PATH") from None
    except subprocess.TimeoutExpired:
        raise HyprError(f"hyprctl timed out: {' '.join(args)}") from None
    if proc.returncode != 0:
        raise HyprError(f"hyprctl {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    out = proc.stdout.strip()
    if not want_json:
        return out
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise HyprError(f"hyprctl {' '.join(args)} returned non-JSON: {out[:200]!r}") from None


def _hyprctl_ok(args: list[str]) -> str:
    """Run a command whose only success reply is literally "ok"."""
    reply = _hyprctl(args)
    if reply != "ok":
        raise HyprError(f"hyprctl {' '.join(args)}: {reply or '(empty reply)'}")
    return reply


@dataclass(frozen=True)
class Monitor:
    name: str
    width: int
    height: int
    refresh: float
    scale: float
    transform: int
    description: str
    dpms: bool
    x: int
    y: int
    focused: bool
    mirror_of: str
    disabled: bool

    @property
    def headless(self) -> bool:
        return self.name.startswith("HEADLESS-") or self.description == ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "refresh": round(self.refresh, 3),
            "scale": self.scale,
            "transform": self.transform,
            "description": self.description,
            "dpms": self.dpms,
            "x": self.x,
            "y": self.y,
            "focused": self.focused,
            "mirror_of": self.mirror_of,
            "disabled": self.disabled,
        }


def _monitor(raw: dict) -> Monitor:
    # `mirrorOf` is "none" for a real monitor and "0" (an id) for a mirroring one.
    mirror = str(raw.get("mirrorOf", "none"))
    return Monitor(
        name=raw.get("name", ""),
        width=int(raw.get("width", 0)),
        height=int(raw.get("height", 0)),
        refresh=float(raw.get("refreshRate", 0.0)),
        scale=float(raw.get("scale", 1.0)),
        transform=int(raw.get("transform", 0)),
        description=raw.get("description", ""),
        dpms=bool(raw.get("dpmsStatus", True)),
        x=int(raw.get("x", 0)),
        y=int(raw.get("y", 0)),
        focused=bool(raw.get("focused", False)),
        mirror_of="" if mirror == "none" else mirror,
        disabled=bool(raw.get("disabled", False)),
    )


def list_monitors(include_inactive: bool = False) -> list[Monitor]:
    """Active outputs. `include_inactive` adds disabled and mirroring ones."""
    args = ["monitors", "all"] if include_inactive else ["monitors"]
    return [_monitor(m) for m in _hyprctl(args, want_json=True)]


def _names(include_inactive: bool = True) -> set[str]:
    return {m.name for m in list_monitors(include_inactive)}


def configure_monitor(name: str, width: int, height: int, refresh: float,
                      position: str = "auto", scale: float = 1.0) -> None:
    """Apply a mode to an existing output (headless or real)."""
    mode = f"{width}x{height}@{refresh:g}"
    _hyprctl_ok(["keyword", "monitor", f"{name},{mode},{position},{scale:g}"])


def create_headless(name: str, width: int, height: int, refresh: float = 60.0) -> str:
    """
    Create a headless output and drive it at the negotiated wire mode.

    Returns the output's REAL name, discovered by diffing the monitor list --
    Hyprland is documented to assign HEADLESS-N regardless of what we ask for.
    Raises HyprError if nothing appeared.
    """
    _record_intent(name)
    before = _names()
    _hyprctl_ok(["output", "create", "headless", name])

    deadline = time.monotonic() + _SETTLE_TIMEOUT
    real = ""
    while time.monotonic() < deadline:
        new = _names() - before
        if new:
            # Prefer the name we asked for if the compositor honoured it.
            real = name if name in new else sorted(new)[0]
            break
        time.sleep(_SETTLE_POLL)
    if not real:
        _forget_intent(name)
        raise HyprError(f"headless output {name!r} did not appear within {_SETTLE_TIMEOUT:g}s")

    _record_intent(real)
    try:
        configure_monitor(real, width, height, refresh)
    except HyprError:
        destroy_headless(real)
        raise
    return real


def destroy_headless(name: str) -> bool:
    """
    Remove a headless output. Idempotent: returns False if it was already gone.

    A mirroring output cannot be removed (measured: "output not found"), so drop
    mirroring first. Any error here is swallowed except the final verification --
    this runs from signal handlers and atexit.
    """
    if not name:
        return False
    try:
        present = _names()
    except HyprError:
        _forget_intent(name)
        return False
    if name not in present:
        _forget_intent(name)
        return False

    try:
        mon = next((m for m in list_monitors(True) if m.name == name), None)
        if mon is not None and mon.mirror_of:
            _hyprctl(["keyword", "monitor",
                      f"{name},{mon.width}x{mon.height}@{mon.refresh:g},auto,1"])
            time.sleep(_SETTLE_POLL)
        _hyprctl(["output", "remove", name])
    except HyprError:
        pass

    deadline = time.monotonic() + _SETTLE_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if name not in _names():
                _forget_intent(name)
                return True
        except HyprError:
            break
        time.sleep(_SETTLE_POLL)
    raise HyprError(f"failed to remove headless output {name!r}")


def workspace_monitors() -> dict[str, str]:
    """{workspace name: monitor name} for every workspace."""
    return {str(w["name"]): w["monitor"] for w in _hyprctl(["workspaces"], want_json=True)}


def move_workspace(workspace: str, output: str) -> None:
    reply = _hyprctl(["dispatch", "moveworkspacetomonitor", str(workspace), output])
    if reply != "ok":
        raise HyprError(f"moveworkspacetomonitor {workspace} {output}: {reply}")


def focus_monitor(output: str) -> None:
    reply = _hyprctl(["dispatch", "focusmonitor", output])
    if reply != "ok":
        raise HyprError(f"focusmonitor {output}: {reply}")


# --------------------------------------------------------------- crash safety
#
# A headless output that outlives its session leaves the user with a phantom
# monitor and windows stranded on it. Three layers guard against that:
#   1. every created name is written to $XDG_RUNTIME_DIR/hyprcast-outputs.json
#      BEFORE the output exists, so even SIGKILL leaves a breadcrumb;
#   2. atexit + SIGINT/SIGTERM/SIGHUP tear down anything still live;
#   3. reap_orphans() at startup cleans up after a previous crash.
# Every step is idempotent.

_lock = threading.Lock()
_live: set[str] = set()
_hooks_installed = False
_prev_handlers: dict[int, object] = {}


def _read_state() -> list[str]:
    try:
        with open(_state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [str(n) for n in data.get("outputs", [])]
    except (OSError, ValueError):
        return []


def _write_state(names: list[str]) -> None:
    path = _state_path()
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "outputs": sorted(set(names))}, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _record_intent(name: str) -> None:
    with _lock:
        names = _read_state()
        if name not in names:
            names.append(name)
            _write_state(names)
        _live.add(name)
    _install_hooks()


def _forget_intent(name: str) -> None:
    with _lock:
        _live.discard(name)
        names = [n for n in _read_state() if n != name]
        _write_state(names)


def _teardown_all() -> None:
    for name in list(_live):
        try:
            destroy_headless(name)
        except HyprError:
            _forget_intent(name)


def _on_signal(signum, frame):
    _teardown_all()
    prev = _prev_handlers.get(signum, signal.SIG_DFL)
    signal.signal(signum, prev if callable(prev) or prev in (signal.SIG_DFL, signal.SIG_IGN)
                  else signal.SIG_DFL)
    if callable(prev):
        prev(signum, frame)
    else:
        os.kill(os.getpid(), signum)


def _install_hooks() -> None:
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    atexit.register(_teardown_all)
    if threading.current_thread() is not threading.main_thread():
        return
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            _prev_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            _prev_handlers.pop(sig, None)


def reap_orphans() -> list[str]:
    """Destroy headless outputs recorded by a session that died. Returns names."""
    reaped = []
    for name in _read_state():
        try:
            if destroy_headless(name):
                reaped.append(name)
        except HyprError:
            pass
    return reaped


class HeadlessOutput:
    """
    Context manager around a headless output. `close()` is idempotent and is
    also reached via atexit and the SIGINT/SIGTERM/SIGHUP handlers.
    """

    def __init__(self, name: str, width: int, height: int, refresh: float = 60.0):
        self.requested = name
        self.width = width
        self.height = height
        self.refresh = refresh
        self.name = create_headless(name, width, height, refresh)

    def __enter__(self) -> "HeadlessOutput":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def close(self) -> None:
        if not self.name:
            return
        name, self.name = self.name, ""
        destroy_headless(name)
        if self.requested != name:
            destroy_headless(self.requested)
