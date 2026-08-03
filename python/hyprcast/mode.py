"""
mirror / extend, switchable while a cast is running.

    mirror   capture the real output (eDP-1) and let the engine's VPP scale
             1920x1080 down to the 1280x720 wire. Nothing is created, nothing
             is destroyed. Deliberately NOT Hyprland's own mirror feature: a
             MIRRORED headless output disappears from `hyprctl monitors` and can
             never be removed again (measured -- see hypr.py).
    extend   create a headless output at the negotiated wire size, capture THAT,
             and leave eDP-1 alone, so the TV is a second desktop instead of a
             copy.

=== THE ORDERING RULE ===

Hyprland 0.55.4's CScreenshareFrame::transform() dereferences the session's
monitor with no null check, from inside the frame constructor. Destroying an
output that a capture session is bound to therefore aborts the compositor and
every client with it. So:

  * to START capturing a headless output: create it, wait until it really is in
    `hyprctl monitors`, and only THEN tell the engine to switch capture;
  * to STOP: tell the engine to switch capture AWAY first, confirm it took the
    change, and ONLY THEN destroy the output;
  * never destroy or reconfigure the output the engine is capturing right now.

switch() is written so every early return leaves the compositor as it found it.
A set_output that is not acknowledged aborts the switch and never falls through
to a destroy. If capture cannot even be moved back to a real output, the
headless output is deliberately LEAKED rather than removed: a phantom monitor is
survivable, destroying a captured output is not. hypr.py's atexit/signal hooks
and reap_orphans() clean it up once the engine is gone.

Acknowledgement, measured against engine/src/main.c: {"cmd":"output"} has no
positive reply. cmd_output() drops capture, rebuilds it, and on failure emits
{"ev":"error"} and stops the session (main.c:1148-1172). Failure is therefore
loud and observable through Engine.last_error / Engine.is_alive, and a stats
event that arrives afterwards with frames still flowing is the acknowledgement.
_confirm_output() waits for one of those two outcomes.

Windows on the headless output, measured on 0.55.4 (scratch experiment, one
kitty on the headless output's auto-created workspace 3): destroying the output
moves its workspaces -- windows and all -- to the remaining monitor. Nothing is
lost, but the destination is the compositor's choice and the workspace lands
behind whatever the user is actually looking at. teardown_extend() therefore
evacuates every workspace off the output ITSELF, to a monitor we picked, while
the output still exists.

Standard library only.
"""

from __future__ import annotations

import atexit
import re
import sys
import threading
import time
from dataclasses import dataclass

from . import hypr

__all__ = [
    "MODES", "ModeError", "ExtendConfig", "normalize", "from_config",
    "ensure_extend", "teardown_extend", "switch", "release",
    "current_mode", "current_output", "headless_name", "snapshot",
]

MODES = ("mirror", "extend")

# "second-screen" is session.py's spelling of the same thing; keep both working
# so `hyprcast ctl mode ...` means one thing to the user.
_ALIASES = {
    "mirror": "mirror", "clone": "mirror", "duplicate": "mirror",
    "extend": "extend", "extended": "extend", "second-screen": "extend",
    "second_screen": "extend", "secondscreen": "extend", "second": "extend",
}

DEFAULT_NAME = "hyprcast"

# Where the second desktop sits relative to the laptop panel. Measured on
# 0.55.4 with eDP-1 at 0,0 1920x1080 and a 1280x720 headless output:
#   auto / auto-right -> 1920,0     auto-left -> -1280,0
#   auto-up           -> 0,-720     auto-down -> 0,1080
# An explicit "1920x0" works too and is passed through. A position Hyprland does
# NOT understand is not passed through: `hyprctl keyword monitor ...,right,1`
# answers with undecodable bytes (measured -- it makes hyprctl's own reply blow
# up with a UnicodeDecodeError), so an unknown word is mapped or refused here.
_POSITIONS = {
    "auto": "auto", "right": "auto-right", "left": "auto-left",
    "above": "auto-up", "up": "auto-up", "below": "auto-down", "down": "auto-down",
    "auto-right": "auto-right", "auto-left": "auto-left",
    "auto-up": "auto-up", "auto-down": "auto-down",
}

_SETTLE_TIMEOUT = 3.0       # how long an output may take to reach its mode
_POLL = 0.05
_ACK_TIMEOUT = 4.0          # hard deadline for "the engine took the new output"
_ACK_SOFT = 1.5             # after this, alive + silent counts as acknowledged
_LOCK_TIMEOUT = 2.0         # teardown must never block a signal handler forever


class ModeError(RuntimeError):
    """A mode switch could not be completed. Nothing was left half-done."""


@dataclass
class ExtendConfig:
    """Everything extend mode needs. width/height/refresh are the NEGOTIATED
    wire mode, not the laptop panel's: the point of extend is that the second
    desktop is exactly the size the sink agreed to, so nothing is rescaled."""

    width: int = 1280
    height: int = 720
    refresh: float = 60.0
    name: str = DEFAULT_NAME
    position: str = "auto"
    workspace: str = ""         # workspace to park on the TV; "" leaves it alone
    mirror_output: str = ""     # real output mirror mode captures; "" = discover


def _log(message: str) -> None:
    print(f"[hyprcast mode] {message}", file=sys.stderr, flush=True)


def normalize(value) -> str:
    mode = _ALIASES.get(str(value).strip().lower())
    if mode is None:
        raise ModeError(f"mode must be one of {' or '.join(MODES)}, not {value!r}")
    return mode


def _position(value: str) -> str:
    """Map a config word onto something hyprctl actually accepts."""
    text = str(value or "auto").strip().lower()
    if text in _POSITIONS:
        return _POSITIONS[text]
    if re.fullmatch(r"-?\d+x-?\d+", text):
        return text
    _log(f"ignoring unknown position {value!r}; using auto")
    return "auto"


# ------------------------------------------------------------------- state
#
# There is exactly one cast, so this is module state, guarded by a REENTRANT
# lock: teardown_extend() is reachable from a signal handler that may interrupt
# a thread already holding it.

_lock = threading.RLock()
_mode = "mirror"
_capture = ""       # what we last told the engine to capture
_headless = ""      # the headless output we own, "" when there is none
_home = ""          # the real output workspaces (and capture) fall back to
_moved: list[str] = []
_pipeline = None    # whoever is capturing our output, so release() can order itself
_atexit_armed = False


def current_mode() -> str:
    return _mode


def current_output() -> str:
    return _capture


def headless_name() -> str:
    return _headless


def snapshot() -> dict:
    with _lock:
        return {"mode": _mode, "capture_output": _capture, "headless": _headless}


# ------------------------------------------------------------------ hyprland
def _monitor(name: str):
    if not name:
        return None
    try:
        for mon in hypr.list_monitors(True):
            if mon.name == name:
                return mon
    except hypr.HyprError:
        return None
    return None


def _wait_for_output(name: str, width: int, height: int):
    """Block until the output is listed, then until it reaches its mode.

    hyprctl exits 0 even when it failed, so a re-list is the only real signal.
    A geometry that never settles is a warning, not an error: the engine's VPP
    rescales whatever it captures to the frozen wire size.
    """
    deadline = time.monotonic() + _SETTLE_TIMEOUT
    mon = None
    while time.monotonic() < deadline:
        mon = _monitor(name)
        if mon is not None and (mon.width, mon.height) == (width, height):
            return mon
        time.sleep(_POLL)
    return mon


def _real_outputs(exclude: str = "") -> list:
    try:
        monitors = hypr.list_monitors()
    except hypr.HyprError:
        return []
    return [m for m in monitors
            if m.name != exclude and m.name != _headless and not m.headless]


def _home_output(exclude: str = "") -> str:
    """The real output capture and workspaces fall back to."""
    if _home and _home != exclude and _monitor(_home) is not None:
        return _home
    candidates = _real_outputs(exclude)
    for mon in candidates:
        if mon.focused:
            return mon.name
    if candidates:
        return candidates[0].name
    # Everything left looks headless; take anything that is not the excluded one.
    try:
        for mon in hypr.list_monitors():
            if mon.name != exclude:
                return mon.name
    except hypr.HyprError:
        pass
    return ""


def _mirror_output(pipeline, cfg: "ExtendConfig", exclude: str = "") -> str:
    """The output mirror mode captures: config, then the pipeline's own choice,
    then whatever Hyprland says is real."""
    if cfg.mirror_output:
        return cfg.mirror_output
    name = getattr(getattr(getattr(pipeline, "config", None), "monitor", None),
                   "name", "")
    if name and name != exclude and name != _headless:
        return str(name)
    return _home_output(exclude)


def _park(workspace: str, output: str) -> None:
    """
    Move a workspace onto an output. Never fatal -- capture is already correct
    by the time this runs, so a workspace that will not move is a warning.

    Measured: `dispatch moveworkspacetomonitor 7 <output>` answers "Workspace not
    found" for a workspace that does not exist yet; Hyprland will not create one
    this way. It always gives a brand-new output its own empty workspace anyway
    (measured: workspace 3 appeared on the headless output the moment it was
    created), so an unknown name is skipped rather than forced.
    """
    global _moved
    workspace = str(workspace)
    try:
        known = hypr.workspace_monitors()
    except hypr.HyprError as exc:
        _log(f"could not list workspaces: {exc}")
        return
    if workspace not in known:
        _log(f"workspace {workspace} does not exist; leaving {output} with the "
             f"empty workspace Hyprland gave it")
        return
    try:
        hypr.move_workspace(workspace, output)
    except hypr.HyprError as exc:
        _log(f"could not move workspace {workspace} to {output}: {exc}")
        return
    if workspace not in _moved:
        _moved.append(workspace)


# --------------------------------------------------------------------- extend
def ensure_extend(width: int = 1280, height: int = 720, refresh: float = 60.0,
                  name: str = DEFAULT_NAME, position: str = "auto",
                  workspace: str = "") -> str:
    """
    Create the headless output and return its REAL name.

    Hyprland is documented to name headless outputs HEADLESS-N regardless of
    what is asked (0.55.4 happens to honour the name); hypr.create_headless
    diffs the monitor list around the call, so both behaviours work. This waits
    until the output is genuinely listed and has reached its mode before
    returning -- the caller may then, and only then, point capture at it.

    Idempotent: if we already own a live headless output, that one is returned
    untouched. It is never reconfigured, because it may be the output the engine
    is capturing right now.
    """
    global _headless
    with _lock:
        if _headless and _monitor(_headless) is not None:
            if workspace:
                _park(workspace, _headless)
            return _headless

        width, height, refresh = int(width), int(height), float(refresh)
        try:
            real = hypr.create_headless(name, width, height, refresh)
        except hypr.HyprError as exc:
            raise ModeError(f"could not create a headless output: {exc}") from None

        placement = _position(position)
        if placement != "auto":
            try:
                hypr.configure_monitor(real, width, height, refresh, position=placement)
            except Exception as exc:      # hyprctl can answer with undecodable bytes
                _log(f"could not place {real} at {placement}: {type(exc).__name__}: {exc}")

        mon = _wait_for_output(real, width, height)
        if mon is None:
            try:
                hypr.destroy_headless(real)
            except hypr.HyprError:
                pass
            raise ModeError(f"headless output {real!r} never appeared in hyprctl monitors")
        if (mon.width, mon.height) != (width, height):
            _log(f"warning: {real} settled at {mon.width}x{mon.height}@{mon.refresh:g}, "
                 f"asked for {width}x{height}@{refresh:g}; VPP will rescale")

        _headless = real
        if workspace:
            _park(workspace, real)
        return real


def teardown_extend(name: str = "") -> None:
    """
    Remove the headless output. Idempotent, safe when nothing exists, and safe
    from a signal handler: it never raises and never blocks forever.

    ONLY call this once nothing is capturing the output -- switch() does that
    ordering for a live cast. Workspaces still on the output are evacuated to a
    real monitor first, so windows land where we chose instead of wherever the
    compositor decides to drop them.
    """
    global _headless, _moved, _mode, _capture
    acquired = _lock.acquire(timeout=_LOCK_TIMEOUT)
    try:
        target = name or _headless
        if not target:
            return
        if _monitor(target) is not None:
            _evacuate(target, _home_output(exclude=target))
        try:
            hypr.destroy_headless(target)
        except hypr.HyprError as exc:
            _log(f"could not remove {target}: {exc}")
        if target == _headless:
            _headless = ""
            _moved = []
            if _mode == "extend":
                _mode, _capture = "mirror", _home_output()
    except Exception as exc:                      # a teardown must never escape
        _log(f"teardown failed: {type(exc).__name__}: {exc}")
    finally:
        if acquired:
            _lock.release()


def _evacuate(output: str, home: str) -> None:
    """Move every workspace off `output` before it is destroyed."""
    try:
        stragglers = [ws for ws, mon in hypr.workspace_monitors().items()
                      if mon == output]
    except hypr.HyprError as exc:
        _log(f"could not list workspaces before removing {output}: {exc}")
        return
    if not stragglers:
        return
    if not home:
        _log(f"no real output to move {', '.join(stragglers)} to; "
             f"Hyprland will place them itself")
        return
    mon = _monitor(output)
    if mon is not None and mon.focused:
        try:
            hypr.focus_monitor(home)
        except hypr.HyprError:
            pass
    for workspace in stragglers:
        try:
            hypr.move_workspace(workspace, home)
        except hypr.HyprError as exc:
            _log(f"could not move workspace {workspace} back to {home}: {exc}")


# --------------------------------------------------------------- the live switch
def _pipeline_alive(pipeline) -> bool:
    try:
        return bool(pipeline.is_alive())
    except Exception:
        return True


def _set_output_confirmed(pipeline, name: str, timeout: float = _ACK_TIMEOUT) -> None:
    """
    Tell the engine to capture `name` and wait until it demonstrably has.

    Engine.sync() is the real answer: the engine drains its whole control queue
    and emits stats from the same loop, so a stats event that lands after the
    send proves the send was consumed. Two of them cannot both have been in
    flight beforehand, which is why it waits for two.

    An engine too old to have sync() is handled the same way by hand: the only
    thing {"cmd":"output"} ever replies with is {"ev":"error"} followed by
    session_stop (main.c:1148-1172), so a NEW error or a dead engine is a
    failure, and frames still flowing afterwards are the acknowledgement.

    Raising means the switch is off. Every caller must then leave outputs alone.
    """
    engine = getattr(pipeline, "engine", None)
    base_error = getattr(engine, "last_error", None)
    base_stats = dict(getattr(engine, "stats", None) or {})

    try:
        pipeline.set_output(name)
    except Exception as exc:
        raise ModeError(f"the engine refused to capture {name!r}: {exc}") from None

    sync = getattr(engine, "sync", None)
    if callable(sync):
        confirmed = bool(sync(timeout=timeout))
        error = getattr(engine, "last_error", None)
        if error and error != base_error:
            raise ModeError(f"the engine rejected {name!r}: {error}")
        if not confirmed:
            raise ModeError(f"the engine did not confirm {name!r} within {timeout:g}s")
        return

    start = time.monotonic()
    while True:
        if engine is not None:
            error = engine.last_error
            if error and error != base_error:
                raise ModeError(f"the engine rejected {name!r}: {error}")
            if not engine.is_alive():
                raise ModeError(f"the engine died switching capture to {name!r}")
            stats = dict(engine.stats or {})
            if stats and stats != base_stats and float(stats.get("fps") or 0.0) > 0.0:
                return
        elif not _pipeline_alive(pipeline):
            raise ModeError(f"the media session died switching capture to {name!r}")

        waited = time.monotonic() - start
        if waited >= _ACK_SOFT and (engine is None or not base_stats):
            return
        if waited >= timeout:
            raise ModeError(f"the engine did not acknowledge {name!r} within {timeout:g}s")
        time.sleep(_POLL)


def switch(pipeline, target_mode: str, cfg: "ExtendConfig | None" = None) -> str:
    """
    Switch a RUNNING cast between mirror and extend. Returns the output now
    being captured. Raises ModeError, with nothing changed, if it cannot.
    """
    global _mode, _capture, _home
    target = normalize(target_mode)
    cfg = cfg or ExtendConfig()
    if pipeline is None:
        raise ModeError("no session is running")

    with _lock:
        if target == _mode:
            if not _capture:
                _capture = (_headless if target == "extend"
                            else _mirror_output(pipeline, cfg))
            return _capture
        if target == "extend":
            return _to_extend(pipeline, cfg)
        return _to_mirror(pipeline, cfg)


def _to_extend(pipeline, cfg: "ExtendConfig") -> str:
    """create -> confirm it exists -> set_output -> confirm -> move workspace."""
    global _mode, _capture, _home
    _cancel_linger()   # do not let a pending removal fire onto the new output
    new = ensure_extend(cfg.width, cfg.height, cfg.refresh, cfg.name, cfg.position)
    fallback = _mirror_output(pipeline, cfg, exclude=new) or _capture

    try:
        _set_output_confirmed(pipeline, new)
    except ModeError as exc:
        # The engine may or may not have taken it, so the output cannot simply
        # be removed. Move capture back to a real output and confirm THAT first;
        # if even that fails, leave the headless output in place -- a phantom
        # monitor is survivable, destroying a captured one takes the compositor
        # down with it.
        moved_away = False
        if fallback:
            try:
                _set_output_confirmed(pipeline, fallback)
                moved_away = True
            except ModeError as back:
                raise ModeError(
                    f"{exc}; capture could not be moved back to {fallback} ({back}) -- "
                    f"leaving {new} in place, it is reaped when the engine exits"
                ) from None
        if not (moved_away or _engine_alive(pipeline) is False):
            # Nowhere to move capture to and the engine is still running: it may
            # be holding a session on this very output. Leave it.
            raise ModeError(
                f"{exc}; no real output to move capture back to -- leaving {new} "
                f"in place, it is reaped when the engine exits") from None
        teardown_extend(new)
        raise ModeError(f"{exc}; stayed on {fallback or 'the current output'}") from None

    _mode, _capture, _home = "extend", new, fallback or _home
    _remember(pipeline)
    if cfg.workspace:
        _park(cfg.workspace, new)
    _log(f"extend: capturing {new} at {cfg.width}x{cfg.height}@{cfg.refresh:g}; "
         f"{_home or 'the laptop panel'} keeps its own content")
    return new



# How long a headless output lingers after capture has moved off it.
#
# MEASURED ON THE REAL SINK, not on loopback: destroying the output the instant
# the capture switch confirms froze the TV on its last frame and the session
# died ~20 s later. Removing a monitor makes Hyprland reconfigure, which stalls
# compositing for a couple of hundred milliseconds -- exactly when the sink is
# trying to resync on the post-switch IDR. A decoder treats that as a timing
# fault and gives up. On loopback the same gap only tripped assert-ts's PAT/PMT
# and PCR checks, which is why it looked cosmetic.
#
# So the output is kept alive until the sink has settled, then removed. It is
# already empty and unfocused by then; it costs nothing but a phantom monitor
# for a few seconds.
_LINGER = 4.0
_linger_timer: "threading.Timer | None" = None


def _cancel_linger() -> None:
    global _linger_timer
    timer, _linger_timer = _linger_timer, None
    if timer is not None:
        timer.cancel()


def _teardown_later(name: str, delay: float = _LINGER) -> None:
    """Remove `name` once the sink has had time to resync. Never blocks."""
    global _linger_timer
    _cancel_linger()

    def _fire() -> None:
        global _linger_timer
        _linger_timer = None
        try:
            teardown_extend(name)
        except Exception as exc:                      # never kill the timer thread
            _log(f"deferred teardown of {name} failed: {type(exc).__name__}: {exc}")

    _log(f"{name} is free; removing it in {delay:g}s so the sink can resync first")
    timer = threading.Timer(delay, _fire)
    timer.daemon = True
    _linger_timer = timer
    timer.start()


def flush_linger() -> None:
    """Run any pending deferred teardown now. Safe to call repeatedly."""
    global _linger_timer
    timer, _linger_timer = _linger_timer, None
    if timer is None:
        return
    timer.cancel()
    try:
        teardown_extend()
    except Exception as exc:
        _log(f"flushing deferred teardown failed: {type(exc).__name__}: {exc}")


def _to_mirror(pipeline, cfg: "ExtendConfig") -> str:
    """set_output(real) -> confirm -> move workspaces back -> THEN destroy."""
    global _mode, _capture, _home, _pipeline
    old = _headless
    target = _mirror_output(pipeline, cfg, exclude=old)
    if not target:
        raise ModeError("no real output to mirror; refusing to leave capture nowhere")

    # No destroy anywhere before this line, and none at all if it raises.
    _set_output_confirmed(pipeline, target)

    _mode, _capture, _home = "mirror", target, target
    if old:
        _teardown_later(old)
    _pipeline = None
    _log(f"mirror: capturing {target}")
    return target


# ------------------------------------------------------------- end of session
def _remember(pipeline) -> None:
    """Keep the capturer, and make sure OUR teardown runs before hypr.py's.

    hypr.py registers its atexit teardown when the output is created; atexit is
    LIFO, so registering here -- strictly afterwards -- guarantees release()
    runs first and hypr's hook finds nothing left to remove. That is what keeps
    process exit from destroying an output the engine is still capturing.
    """
    global _pipeline, _atexit_armed
    _pipeline = pipeline
    if not _atexit_armed:
        _atexit_armed = True
        atexit.register(release)


def _engine_alive(pipeline) -> bool:
    if pipeline is None:
        return False
    engine = getattr(pipeline, "engine", None)
    if engine is not None:
        try:
            return bool(engine.is_alive())
        except Exception:
            return False
    return _pipeline_alive(pipeline)


def release(pipeline=None) -> str:
    """
    Give the headless output back at the end of a session, in the right order.

    If something is still capturing it, capture is moved to a real output and
    confirmed BEFORE the destroy; if that cannot be confirmed, the output is
    left in place rather than pulled out from under a live capture session.
    With nothing capturing (the usual case -- the engine has already exited)
    this is just teardown_extend(). Never raises.
    """
    global _pipeline
    acquired = _lock.acquire(timeout=_LOCK_TIMEOUT)
    try:
        target = pipeline if pipeline is not None else _pipeline
        if _headless and _engine_alive(target):
            try:
                out = _to_mirror(target, ExtendConfig())
                flush_linger()      # the session is ending; do not linger
                return out
            except ModeError as exc:
                _log(f"leaving {_headless} in place: {exc}")
                return _capture
        _cancel_linger()
        teardown_extend()
        return _capture
    except Exception as exc:
        _log(f"release failed: {type(exc).__name__}: {exc}")
        return _capture
    finally:
        _pipeline = None
        if acquired:
            _lock.release()


# --------------------------------------------------------------------- config
def from_config(cfg, width: int = 0, height: int = 0, refresh: float = 0.0) -> ExtendConfig:
    """
    Build an ExtendConfig from a config.Config plus the NEGOTIATED wire mode.

    config.py owns the file; this only maps [extend] and [cast] onto the fields
    this module uses. The wire mode wins over [cast] width/height when it is
    known, because the whole point of extend is a second desktop that is exactly
    the size the sink agreed to, so nothing is rescaled.
    """
    def get(section, key, fallback):
        try:
            value = cfg.get(section, key)
        except (KeyError, AttributeError):
            return fallback
        return fallback if value in (None, "") else value

    base = ExtendConfig()
    return ExtendConfig(
        width=int(width or get("cast", "width", base.width)),
        height=int(height or get("cast", "height", base.height)),
        refresh=float(refresh or get("cast", "fps", base.refresh)),
        name=str(get("extend", "name", base.name)),
        position=str(get("extend", "position", base.position)),
        workspace=str(get("extend", "workspace", "")),
        mirror_output=str(get("cast", "monitor", "")),
    )
