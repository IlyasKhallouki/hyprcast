"""
Session orchestration: the object every `hyprcast ctl` command ends up talking to.

It owns, in this order:

    display   mirror        -> capture the real monitor; the engine's VPP scales
                               1920x1080 -> the negotiated 1280x720 wire mode.
                               Deliberately NOT a mirrored headless output:
                               Hyprland cannot remove one of those again
                               (measured -- see hypr.py).
              second-screen -> a headless output created at exactly the wire
                               mode, so no scaling happens at all.
    audio     shared | tv-only          (audio.py)
    engine    engine/build/hyprcast-engine on fd 3, supervised by engine.py.

The engine seam itself lives in engine.py; this module only decides WHAT to ask
it for. Blocking engine calls are made without the session lock held so a
`hyprcast status` during a slow start still answers.
"""

from __future__ import annotations

import os
import re
import threading
import time

from . import audio as audiomod
from . import hypr
from .engine import Engine, EngineError, engine_binary

__all__ = ["Session", "SessionError", "parse_bitrate", "REPO_ROOT",
           "MODES", "normalize_mode", "other_mode", "mode_label"]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STATES = ("idle", "discovering", "connecting", "casting", "error")
MODES = ("mirror", "second-screen")

# "extend" is what the waybar tooltip and every other desktop calls it; it is
# spelled second-screen internally because that is what the mode has always
# been called here. Both spellings are accepted everywhere a mode is taken.
MODE_ALIASES = {
    "mirror": "mirror",
    "clone": "mirror",
    "second-screen": "second-screen",
    "second_screen": "second-screen",
    "secondscreen": "second-screen",
    "extend": "second-screen",
    "extended": "second-screen",
}

DEFAULT_WIRE = (1280, 720)      # the Xiaomi sink's maximum; 1080p is not offered
DEFAULT_FPS = 60
DEFAULT_BITRATE = 8_000_000
DEFAULT_GOP = 60
HEADLESS_NAME = "hyprcast"


class SessionError(RuntimeError):
    pass


def parse_bitrate(value) -> int:
    """8000000, "8M", "8000k", "8 Mbit" -> bits per second."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower().replace("bit/s", "").replace("bps", "").replace("bit", "")
    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([kmg]?)\s*", text)
    if not match:
        raise SessionError(f"cannot parse bitrate {value!r}")
    scale = {"": 1, "k": 1_000, "m": 1_000_000, "g": 1_000_000_000}[match.group(2)]
    return int(float(match.group(1)) * scale)


def normalize_mode(value) -> str:
    """Accept either spelling of a mode; raise on anything else."""
    key = str(value or "").strip().lower()
    if key not in MODE_ALIASES:
        raise SessionError(f"mode must be one of {'|'.join(MODES)} (or 'extend')")
    return MODE_ALIASES[key]


def other_mode(value) -> str:
    """The mode a toggle lands on."""
    return "mirror" if normalize_mode(value) == "second-screen" else "second-screen"


def mode_label(value) -> str:
    """How a mode is spoken about in the UI."""
    return "extend" if str(value) == "second-screen" else str(value or "mirror")


def engine_path() -> str:
    """The native engine binary if it is built and executable, else ""."""
    candidate = engine_binary()
    return candidate if os.access(candidate, os.X_OK) else ""


DEFAULT_VOLUME_STEP = 5


def _step(msg: dict) -> int:
    """The step of a volume-up/down, defaulting to 5 points."""
    try:
        value = int(msg.get("value") or DEFAULT_VOLUME_STEP)
    except (TypeError, ValueError):
        raise SessionError("volume step must be an integer") from None
    return max(1, min(100, abs(value)))


class Session:
    """Thread-safe: every public method takes the lock."""

    def __init__(self, on_change=None):
        self._lock = threading.RLock()
        self._on_change = on_change

        self.state = "idle"
        self.error = ""
        self.mode = "mirror"
        self.monitor = ""            # user-requested source monitor ("" = focused)
        self.capture_output = ""     # what the engine is actually capturing
        self.peer = ""
        self.peer_name = ""
        self.dst_port = 0
        self.src_port = 19002
        self.width, self.height = DEFAULT_WIRE
        self.fps = DEFAULT_FPS
        # The headless output's refresh rate is the NEGOTIATED wire rate, not
        # the live encode rate: `ctl fps 30` must not reprogram the compositor's
        # output, it just makes the engine emit fewer frames.
        self.wire_fps = DEFAULT_FPS
        self.bitrate = DEFAULT_BITRATE
        self.gop = DEFAULT_GOP
        self.volume = 100
        self.muted = False
        self.audio_mode = "shared"
        self.audio_source = ""
        self.low_power = False
        self.started_at = 0.0
        self.stats = {"fps": 0.0, "kbps": 0, "cpu": 0.0, "drops": 0, "idr": 0}

        self._headless: hypr.HeadlessOutput | None = None
        self._null_sink: audiomod.NullSinkRoute | None = None
        self._engine: Engine | None = None
        self._pump: threading.Thread | None = None
        self._starting = False

    # ------------------------------------------------------------- observable
    def snapshot(self) -> dict:
        with self._lock:
            duration = time.time() - self.started_at if self.started_at else 0.0
            return {
                "state": self.state,
                "error": self.error,
                "mode": self.mode,
                "monitor": self.monitor,
                "capture_output": self.capture_output,
                "peer": self.peer,
                "peer_name": self.peer_name,
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "bitrate": self.bitrate,
                "volume": self.volume,
                "muted": self.muted,
                "audio_mode": self.audio_mode,
                "audio_source": self.audio_source,
                "low_power": self.low_power,
                "duration": round(duration, 1),
                "engine": engine_path(),
                "stats": dict(self.stats),
            }

    def _changed(self) -> None:
        if self._on_change:
            try:
                self._on_change(self.snapshot())
            except Exception:
                pass

    def _fail(self, message: str) -> None:
        self.state = "error"
        self.error = message
        self._changed()

    # ------------------------------------------------------------- dispatcher
    def dispatch(self, cmd: str, msg: dict) -> dict:
        handlers = {
            "status": lambda: None,
            "start": lambda: self.start(
                peer=msg.get("peer") or self.peer,
                dst_port=int(msg.get("dst_port") or self.dst_port or 0),
                mode=msg.get("mode") or self.mode,
                monitor=msg.get("monitor") or self.monitor,
            ),
            "stop": self.stop,
            "fps": lambda: self.set_fps(msg.get("value")),
            "bitrate": lambda: self.set_bitrate(msg.get("value")),
            "qp": lambda: self._retune(qp=int(msg.get("value") or 0)),
            "idr": lambda: self._send({"cmd": "idr"}),
            "volume": lambda: self.set_volume(msg.get("value")),
            "mute": lambda: self.set_muted(msg.get("value", "toggle")),
            "monitor": lambda: self.set_monitor(msg.get("value")),
            "mode": lambda: self.set_mode(msg.get("value")),
            # The bar-button set: no argument to look up, no state to guess.
            "toggle-mode": lambda: self.set_mode(other_mode(self.mode)),
            "toggle-mute": lambda: self.set_muted("toggle"),
            "volume-up": lambda: self.set_volume(self.volume + _step(msg)),
            "volume-down": lambda: self.set_volume(self.volume - _step(msg)),
            "quit": self.stop,
        }
        if cmd == "list-outputs":
            return {"ok": True, "outputs": [m.as_dict() for m in hypr.list_monitors()],
                    "state": self.snapshot()}
        if cmd == "list-sinks":
            return {"ok": True, "sinks": [s.as_dict() for s in audiomod.list_sinks()],
                    "state": self.snapshot()}
        handler = handlers.get(cmd)
        if handler is None:
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        try:
            handler()
        except (SessionError, hypr.HyprError, audiomod.AudioError) as exc:
            return {"ok": False, "error": str(exc), "state": self.snapshot()}
        return {"ok": True, "state": self.snapshot()}

    # ------------------------------------------------------------------ setup
    def configure(self, *, mode: str | None = None, monitor: str | None = None,
                  fps: int | None = None, bitrate=None, width: int | None = None,
                  height: int | None = None, audio_mode: str | None = None,
                  peer: str | None = None, dst_port: int | None = None,
                  src_port: int | None = None, low_power: bool | None = None,
                  volume: int | None = None) -> None:
        with self._lock:
            if mode is not None:
                self.mode = normalize_mode(mode)
            if monitor is not None:
                self.monitor = monitor
            if fps is not None:
                self.fps = max(1, min(60, int(fps)))
                if self.state != "casting":
                    self.wire_fps = self.fps
            if bitrate is not None:
                self.bitrate = parse_bitrate(bitrate)
            if width:
                self.width = int(width)
            if height:
                self.height = int(height)
            if audio_mode is not None:
                if audio_mode not in ("shared", "tv-only", "none"):
                    raise SessionError("audio must be shared, tv-only or none")
                self.audio_mode = audio_mode
            if volume is not None:
                # The configured default. start() applies it once the engine
                # is up, because a fresh audio leg always opens at unity.
                self.volume = max(0, min(100, int(volume)))
            if peer is not None:
                self.peer = peer
            if dst_port is not None:
                self.dst_port = int(dst_port)
            if src_port is not None:
                self.src_port = int(src_port)
            if low_power is not None:
                self.low_power = bool(low_power)

    def _source_monitor(self) -> str:
        """The real monitor mirror mode captures."""
        monitors = hypr.list_monitors()
        if not monitors:
            raise SessionError("Hyprland reports no active outputs")
        if self.monitor:
            for mon in monitors:
                if self.monitor.lower() in (mon.name.lower(), mon.description.lower()):
                    return mon.name
            raise SessionError(f"no output matching {self.monitor!r}")
        for mon in monitors:
            if mon.focused:
                return mon.name
        return monitors[0].name

    def _bring_up_display(self) -> str:
        if self.mode == "second-screen":
            self._headless = hypr.HeadlessOutput(HEADLESS_NAME, self.width, self.height,
                                                 float(self.wire_fps))
            return self._headless.name
        return self._source_monitor()

    def _bring_up_audio(self) -> str:
        if self.audio_mode == "none":
            return ""
        if self.audio_mode == "tv-only":
            audiomod.cleanup_stale_null_sink()
            self._null_sink = audiomod.NullSinkRoute()
            self._null_sink.capture_all(set_default=True)
            return self._null_sink.monitor_source
        return audiomod.default_monitor_source()

    # ------------------------------------------------------------- lifecycle
    def start(self, *, peer: str = "", dst_port: int = 0,
              mode: str | None = None, monitor: str | None = None) -> None:
        """
        Bring the display, the audio route and the engine up.

        The engine handshake blocks (Engine.start waits for {"ev":"ready"}), so
        it runs WITHOUT the session lock -- otherwise a `hyprcast status` from
        waybar would stall for the whole handshake. `_starting` is the mutual
        exclusion instead.
        """
        with self._lock:
            if self.state == "casting" or self._starting:
                raise SessionError("already casting")
            self.configure(mode=mode, monitor=monitor, peer=peer or None,
                           dst_port=dst_port or None)
            if not self.peer:
                raise SessionError(
                    "no sink address. Pass --peer IP[:PORT]; automatic WFD discovery "
                    "arrives with the RTSP milestone")
            if not engine_path():
                raise SessionError(
                    f"{engine_binary()} is not built. "
                    "Build it (ninja -C engine/build) or set HYPRCAST_ENGINE")
            self._starting = True
            self.state = "connecting"
            self.error = ""
        self._changed()

        try:
            capture_output = self._bring_up_display()
            audio_source = self._bring_up_audio()
            engine = Engine(log=lambda msg: None)
            engine.start(
                dst_ip=self.peer, dst_port=self.dst_port, src_port=self.src_port,
                width=self.width, height=self.height, fps=self.fps,
                bitrate=self.bitrate, gop=self.gop, qp=0,
                output=capture_output, audio=audio_source,
                low_power=self.low_power,
            )
        except (EngineError, SessionError, hypr.HyprError, audiomod.AudioError, OSError) as exc:
            with self._lock:
                self._starting = False
                self._teardown_locked()
                self._fail(str(exc))
            raise SessionError(str(exc)) from None

        with self._lock:
            self._engine = engine
            self._starting = False
            self.capture_output = capture_output
            self.audio_source = audio_source
            self.started_at = time.time()
            self.state = "casting"
            self._pump = threading.Thread(target=self._pump_events, args=(engine,),
                                          name="hyprcast-events", daemon=True)
            self._pump.start()
        self._changed()
        # Apply anything the user changed while we were idle.
        if self.volume != 100 or self.muted:
            try:
                engine.volume(gain=self.volume / 100.0, muted=self.muted)
            except EngineError:
                pass

    def _pump_events(self, engine: Engine) -> None:
        """
        Mirror engine events into session state. The stream always terminates
        with {"ev":"exited"}, so an engine that dies unexpectedly still reaches
        the teardown path and never leaves a headless output behind.
        """
        for event in engine.events():
            kind = event.get("ev")
            with self._lock:
                if self._engine is not engine:
                    return
                if kind == "stats":
                    for key in self.stats:
                        if key in event:
                            self.stats[key] = event[key]
                elif kind == "ready":
                    self.state = "casting"
                    self.error = ""
                elif kind == "error":
                    self.error = str(event.get("msg", "engine error"))
                elif kind == "exited":
                    expected = self.state in ("idle", "error")
                    reason = self.error or f"engine exited (code {event.get('code')})"
                    self._teardown_locked()
                    if expected:
                        self.state = "idle"
                        self._changed()
                    else:
                        self._fail(f"engine exited unexpectedly: {reason}")
                    return
            self._changed()

    def _send(self, obj: dict) -> None:
        """Legacy shim for callers that still speak raw hc.h control messages."""
        engine = self._engine
        if engine is None:
            raise SessionError("engine is not running")
        cmd = obj.get("cmd")
        try:
            if cmd == "retune":
                engine.retune(**{k: v for k, v in obj.items() if k != "cmd"})
            elif cmd == "volume":
                engine.volume(gain=obj.get("gain"), muted=obj.get("muted"))
            elif cmd == "output":
                engine.set_output(str(obj.get("name", "")))
            elif cmd == "idr":
                engine.idr()
            elif cmd == "stop":
                engine.stop()
            else:
                raise SessionError(f"unsupported engine command {cmd!r}")
        except (EngineError, ValueError) as exc:
            raise SessionError(str(exc)) from None

    def stop(self) -> None:
        with self._lock:
            self.state = "idle"
            self._teardown_locked()
            self.error = ""
        self._changed()

    def _teardown_locked(self) -> None:
        engine, self._engine = self._engine, None
        self._pump = None
        if engine is not None:
            engine.quit()
        if self._null_sink is not None:
            self._null_sink.close()
            self._null_sink = None
        if self._headless is not None:
            try:
                self._headless.close()
            except hypr.HyprError:
                pass
            self._headless = None
        self.capture_output = ""
        self.audio_source = ""
        self.started_at = 0.0
        self.stats = {"fps": 0.0, "kbps": 0, "cpu": 0.0, "drops": 0, "idr": 0}

    def close(self) -> None:
        with self._lock:
            self.state = "idle"
            self._teardown_locked()

    # ---------------------------------------------------------- live controls
    def _retune(self, **fields) -> None:
        if self.state == "casting":
            self._send(dict(cmd="retune", **fields))

    def set_fps(self, value) -> None:
        with self._lock:
            fps = int(value)
            if not 1 <= fps <= 60:
                raise SessionError("fps must be 1..60 (the sink negotiated 720p60)")
            self.fps = fps
            self._retune(fps=fps)
            self._changed()

    def set_bitrate(self, value) -> None:
        with self._lock:
            bitrate = parse_bitrate(value)
            if not 200_000 <= bitrate <= 50_000_000:
                raise SessionError("bitrate must be 200k..50M")
            self.bitrate = bitrate
            self._retune(bitrate=bitrate)
            self._changed()

    def set_volume(self, value) -> None:
        with self._lock:
            volume = max(0, min(100, int(value)))
            self.volume = volume
            if self.state == "casting":
                self._send({"cmd": "volume", "gain": round(volume / 100.0, 4)})
            self._changed()

    def set_muted(self, value) -> None:
        with self._lock:
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in ("toggle", ""):
                    muted = not self.muted
                elif lowered in ("on", "true", "yes", "1", "mute"):
                    muted = True
                elif lowered in ("off", "false", "no", "0", "unmute"):
                    muted = False
                else:
                    raise SessionError("mute takes on, off or toggle")
            else:
                muted = bool(value)
            self.muted = muted
            if self.state == "casting":
                self._send({"cmd": "volume", "muted": muted})
            self._changed()

    def set_monitor(self, name) -> None:
        with self._lock:
            if not name:
                raise SessionError("monitor needs an output name")
            self.monitor = str(name)
            if self.state == "casting" and self.mode == "mirror":
                target = self._source_monitor()
                self._send({"cmd": "output", "name": target})
                self.capture_output = target
            self._changed()

    def set_mode(self, mode) -> None:
        with self._lock:
            mode = normalize_mode(mode)
            if mode == self.mode:
                return
            self.mode = mode
            if self.state != "casting":
                self._changed()
                return
            old_headless, self._headless = self._headless, None
            try:
                self.capture_output = self._bring_up_display()
                self._send({"cmd": "output", "name": self.capture_output})
            except Exception:
                # Capture may still be bound to the old output, so it must
                # survive. Anything created on the way here is left registered
                # in hypr's live set and reaped at exit instead.
                self.mode = "second-screen" if mode == "mirror" else "mirror"
                self._headless = old_headless
                raise
            self._retire_headless(old_headless)
            self._changed()

    def _retire_headless(self, output) -> None:
        """Destroy an output the engine has just been moved OFF of.

        Never before the engine confirms the move. Destroying an output that
        still has a capture session bound does not fail, it kills the
        compositor: CScreenshareFrame::transform() dereferences
        m_session->monitor() with no null check. If the engine will not
        confirm, the output is left alone -- a phantom monitor until exit is a
        nuisance, a dead Hyprland is the whole desktop.
        """
        if output is None:
            return
        engine = self._engine
        if engine is not None and engine.is_alive() and not engine.sync():
            print(f"hyprcast: engine did not confirm the capture switch; "
                  f"leaving {output.name} in place until exit")
            return
        try:
            output.close()
        except hypr.HyprError:
            pass
