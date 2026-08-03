"""
hyprcast command line.

    hyprcast cast [--second-screen] [--monitor NAME] [--fps N] [--bitrate N]
    hyprcast ctl fps 30 | bitrate 6M | volume 50 | mute | monitor NAME | mode X
    hyprcast ctl toggle-mode | toggle-mute | volume-up [N] | volume-down [N] | stop
    hyprcast config [--path | --init]
    hyprcast status
    hyprcast waybar
    hyprcast doctor
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading

from . import audio as audiomod
from . import config as configmod
from . import ctl as ctlmod
from . import hypr
from . import waybar as waybarmod
from .session import (DEFAULT_VOLUME_STEP, MODES, Session, SessionError,
                      engine_path, mode_label, normalize_mode, parse_bitrate)

PROG = "hyprcast"

# What the config file calls the two modes. session.MODES spells the second one
# "second-screen"; the file and the flag say "extend".
MODE_NAMES = ("mirror", "extend")

# argparse dest -> the config key it overrides. Every one of these arguments is
# declared with default=None (store_true included) so that "absent" is
# distinguishable from "happens to equal the default"; config.apply_flags then
# ignores the Nones and the file value survives.
_FLAG_MAP = {
    "fps":         ("cast", "fps"),
    "bitrate":     ("cast", "bitrate"),
    "mode":        ("cast", "mode"),
    "monitor":     ("cast", "monitor"),
    "audio":       ("cast", "audio"),
    "volume":      ("cast", "volume"),
    "low_power":   ("cast", "low_power"),
    "cursors":     ("cast", "cursors"),
    "qp":          ("cast", "qp"),
    "no_firewall": ("cast", "no_firewall"),
    "width":       ("cast", "width"),
    "height":      ("cast", "height"),
    "interface":   ("wifi", "interface"),
    "timeout":     ("wifi", "timeout"),
    "sink":        ("wifi", "sink"),
    "go_intent":   ("wifi", "go_intent"),
}


def _load_config(args) -> configmod.Config:
    """File + flags, in that order of increasing authority."""
    cfg = configmod.load()
    for warning in cfg.warnings:
        print(f"{PROG}: warning: {warning}", file=sys.stderr)
    # --second-screen is the older spelling of mode = "extend".
    if getattr(args, "second_screen", None) and getattr(args, "mode", None) is None:
        args.mode = "extend"
    configmod.apply_flags(
        cfg, {target: getattr(args, dest, None) for dest, target in _FLAG_MAP.items()})
    return cfg


def _merge_config(args) -> configmod.Config:
    """Write the merged values back onto `args`, so nothing downstream changes."""
    cfg = _load_config(args)
    for dest, (section, key) in _FLAG_MAP.items():
        setattr(args, dest, cfg.get(section, key))
    # "" and None are NOT the same peer selector: _scan_and_select() treats None
    # as "discover and pick" and anything else as a selector to match, so an
    # empty sink= would retry three scans and fail.
    args.sink = args.sink or None
    args.second_screen = (args.mode == "extend")
    return cfg


# ------------------------------------------------------------------- cast
def cmd_cast(args) -> int:
    """
    Two routes to a session:

      no --peer  -> the real Wi-Fi Display flow. P2P discovery, group
                    formation, the M1-M7 RTSP handshake, and only then the
                    engine, started with whatever the sink actually agreed to.
                    This is what you want.

      --peer     -> skip P2P and RTSP and stream straight at an address. Only
                    useful against tools/mock-sink.py, because a real sink will
                    not render anything it did not negotiate.
    """
    try:
        _merge_config(args)
    except configmod.ConfigError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2
    if not args.peer:
        return _cast_wfd(args)
    return _cast_direct(args)


def _cast_wfd(args) -> int:
    """Discover the sink over Wi-Fi Direct, negotiate, and cast to it."""
    from . import wfd

    ns = argparse.Namespace(
        fps=args.fps,
        bitrate=args.bitrate,
        output_res=(f"{args.width}x{args.height}" if args.width and args.height else None),
        monitor_name=args.monitor or None,
        wfd_interface=args.interface,
        wfd_timeout=args.timeout,
        wfd_peer=args.sink,
        wfd_rtsp_port=args.rtsp_port,
        wfd_rtp_source_port=args.src_port,
        wfd_no_audio=(args.audio == "none"),
        wfd_audio_mode=args.audio,
        wfd_audio_device=args.audio_device or None,
        wfd_volume=args.volume,
        wfd_low_power=args.low_power,
        cursors=args.cursors,
        wfd_qp=args.qp,
        wfd_no_firewall=args.no_firewall,
        wfd_go_intent=args.go_intent,
        wfd_latency_log=args.latency_log,
        wfd_dry_run=False,
        engine=None,
    )

    # Serve the ctl socket alongside, bridged to whichever pipeline is live, so
    # `hyprcast ctl fps 30` works while the WFD flow owns the main thread.
    server = None
    try:
        server = ctlmod.Server(
            lambda cmd, params: _ctl_bridge(wfd, ns, cmd, params),
            lambda: _wfd_snapshot(wfd, ns),
        )
        server.serve_in_background()
        print(f"{PROG}: control socket {server.path}")
    except ctlmod.CtlError as exc:
        print(f"{PROG}: control socket unavailable ({exc}); "
              f"casting anyway without runtime control", file=sys.stderr)

    try:
        wfd.start_experimental_backend(ns)
        return 0
    except KeyboardInterrupt:
        return 0
    except wfd.WFDNotReady as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2
    finally:
        if server is not None:
            server.server_close()


def _volume_step(params: dict) -> int:
    """The step of a volume-up/down. Bar scroll bindings send no argument."""
    raw = params.get("value")
    if raw in (None, ""):
        return DEFAULT_VOLUME_STEP
    try:
        return max(1, min(100, abs(int(raw))))
    except (TypeError, ValueError):
        raise ctlmod.CtlError(f"volume step must be an integer, not {raw!r}") from None


def _ctl_bridge(wfd, ns, cmd: str, params: dict):
    """Route a ctl command at the live WFD pipeline.

    Every relative command -- toggle-mode, toggle-mute, volume-up, volume-down
    -- is resolved on this side. waybar runs a click binding as a bare shell
    command with no way to read the current state first, so "the other mode"
    and "five points louder" have to mean something to the socket, not to the
    caller.
    """
    # Answerable at any point in the session, including before the media leg
    # exists. `status` in particular: it is what the bar and every script ask
    # first, and "no session is running" is not an answer to "what are you
    # doing" while a P2P scan is in progress.
    if cmd == "status":
        return {"ok": True, "state": _wfd_snapshot(wfd, ns)}

    if cmd in ("stop", "quit"):
        # The whole session, not just the engine: leaving the P2P group up and
        # the GO intent lowered makes the next cast fight a stale NetworkManager
        # activation. The main loop unwinds through its full teardown within 1 s.
        wfd.request_stop()
        return {"ok": True, "state": _wfd_snapshot(wfd, ns)}

    if cmd == "list-outputs":
        try:
            outputs = [m.as_dict() for m in hypr.list_monitors()]
        except hypr.HyprError as exc:
            raise ctlmod.CtlError(str(exc)) from None
        return {"ok": True, "outputs": outputs, "state": _wfd_snapshot(wfd, ns)}

    if cmd == "list-sinks":
        # Answerable while the P2P scan is still running: choosing the sink to
        # capture does not need a media session, and being told "no session is
        # running" when you only asked what exists is useless.
        try:
            sinks = [s.as_dict() for s in audiomod.list_sinks()]
        except audiomod.AudioError as exc:
            raise ctlmod.CtlError(str(exc)) from None
        return {"ok": True, "sinks": sinks, "state": _wfd_snapshot(wfd, ns)}

    p = wfd.current_pipeline()
    if p is None:
        raise ctlmod.CtlError("no session is running")
    if cmd == "fps":
        p.retune(fps=int(params["value"]))
    elif cmd == "bitrate":
        p.retune(bitrate=int(params["value"]))
    elif cmd == "qp":
        p.retune(qp=int(params["value"]))
    elif cmd == "volume":
        p.set_volume(params["value"])
    elif cmd == "volume-up":
        p.nudge_volume(_volume_step(params))
    elif cmd == "volume-down":
        p.nudge_volume(-_volume_step(params))
    elif cmd == "mute":
        p.set_muted(params.get("value", "toggle"))
    elif cmd == "toggle-mute":
        p.set_muted("toggle")
    elif cmd == "mode":
        p.set_mode(str(params["value"]))
    elif cmd == "toggle-mode":
        p.toggle_mode()
    elif cmd == "sink":
        # WFDNotReady is this module's "you cannot do that right now"; without
        # the translation the socket reports it as "WFDNotReady: ..." and the
        # class name ends up in front of the user.
        try:
            p.set_audio_source(str(params["value"]))
        except wfd.WFDNotReady as exc:
            raise ctlmod.CtlError(str(exc)) from None
    elif cmd == "idr":
        p.request_idr()
    elif cmd == "monitor":
        p.set_output(str(params["value"]))
    else:
        raise ctlmod.CtlError(f"unsupported while casting over WFD: {cmd}")
    return {"ok": True, "state": _wfd_snapshot(wfd, ns)}


def _wfd_snapshot(wfd, ns) -> dict:
    """Session state in exactly the shape session.Session.snapshot() returns.

    One shape, so `status`, `ctl` and the waybar module never have to know
    which of the two cast routes is live. Before the pipeline exists there is
    still something worth reporting -- scanning is not the same as
    handshaking -- and that comes from wfd's phase.
    """
    p = wfd.current_pipeline()
    if p is not None:
        return p.snapshot()
    phase = wfd.session_phase()
    return {
        "state": phase.get("state") or "idle",
        "error": "",
        "mode": "mirror",
        "monitor": getattr(ns, "monitor_name", "") or "",
        "capture_output": "",
        "peer": phase.get("peer") or (getattr(ns, "wfd_peer", "") or ""),
        "peer_name": phase.get("peer_name") or "",
        "detail": phase.get("detail") or "",
        "width": 0,
        "height": 0,
        "fps": getattr(ns, "fps", 0) or 0,
        "bitrate": 0,
        "volume": 100,
        "muted": False,
        "audio_mode": "none" if getattr(ns, "wfd_no_audio", False) else "shared",
        "audio_source": "",
        "duration": 0.0,
        "stats": {"fps": 0.0, "kbps": 0.0, "cpu": 0.0, "drops": 0, "idr": 0},
    }


def _cast_direct(args) -> int:
    stop = threading.Event()

    def _quit(signum, frame):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _quit)

    try:
        reaped = hypr.reap_orphans()
    except hypr.HyprError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    if reaped:
        print(f"{PROG}: reaped orphaned headless output(s): {', '.join(reaped)}",
              file=sys.stderr)
    audiomod.cleanup_stale_null_sink()

    session = Session()
    try:
        # on_quit wakes this thread when a client sends `ctl quit`; without it
        # the accept loop stops but the process keeps the socket bound.
        server = ctlmod.Server(session.dispatch, session.snapshot, on_quit=stop.set)
    except ctlmod.CtlError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    session._on_change = server.broadcast

    peer, dst_port = _split_peer(args.peer)
    try:
        session.configure(
            mode="second-screen" if args.second_screen else "mirror",
            monitor=args.monitor or "",
            fps=args.fps, bitrate=args.bitrate,
            width=args.width, height=args.height,
            audio_mode=args.audio, peer=peer, dst_port=dst_port,
            src_port=args.src_port, low_power=args.low_power,
            volume=args.volume,
        )
    except SessionError as exc:
        server.server_close()
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2

    server.serve_in_background()
    print(f"{PROG}: control socket {server.path}")

    rc = 0
    if not args.idle:
        try:
            session.start()
            snap = session.snapshot()
            print(f"{PROG}: casting {snap['width']}x{snap['height']}@{snap['fps']} "
                  f"from {snap['capture_output']} ({snap['mode']}) to {snap['peer']}")
        except (SessionError, hypr.HyprError, audiomod.AudioError) as exc:
            print(f"{PROG}: {exc}", file=sys.stderr)
            rc = 1
            stop.set()
    else:
        print(f"{PROG}: idle -- drive it with `{PROG} ctl start`")

    try:
        while not stop.is_set():
            stop.wait(0.5)
            if session.state == "error" and not args.idle:
                print(f"{PROG}: {session.error}", file=sys.stderr)
                rc = 1
                break
    finally:
        session.close()
        server.server_close()
    return rc


def _split_peer(value: str | None) -> tuple[str, int]:
    if not value:
        return "", 0
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        if port.isdigit():
            return host, int(port)
    return value, 0



# ------------------------------------------------------------------- scan
def cmd_scan(args) -> int:
    """Look for Wi-Fi Display sinks without starting a session.

    The single most useful diagnostic: it separates "the TV stopped
    advertising" from "hyprcast is broken". Android Miracast apps leave
    discovery mode after a short window, so a scan that finds nothing usually
    just means the app needs re-arming.
    """
    from . import wfd
    peers = wfd.active_scan(interface=args.interface, timeout=args.timeout)
    wfd.print_scan(peers)
    if not peers:
        print(f"\n{PROG}: no sink is advertising. On the TV, re-open the "
              f"Miracast / Screen Share app so it starts advertising again, "
              f"then run `{PROG} cast`.", file=sys.stderr)
        return 1
    return 0



# ------------------------------------------------------------------ probe
def cmd_probe(args) -> int:
    """Connect, read M3, print what the sink is offering, and disconnect.

    The P2P beacon a scan sees carries no mode table -- only device type,
    availability, RTSP port and max throughput. Whether you can have 60 fps is
    in the CEA mask, which arrives only in the M3 GET_PARAMETER response, after
    a connection exists. This forms the link, asks, and tears it down without
    ever starting the media path.
    """
    from . import wfd

    ns = argparse.Namespace(
        fps=args.fps, bitrate="8M", output_res=None, monitor_name=None,
        wfd_interface=args.interface, wfd_timeout=args.timeout,
        wfd_peer=args.sink, wfd_rtsp_port=7236, wfd_rtp_source_port=19002,
        wfd_no_audio=True, wfd_audio_device=None, wfd_low_power=False,
        wfd_qp=None, wfd_no_firewall=True, wfd_go_intent=None,
        wfd_latency_log=None, wfd_dry_run=False, engine=None,
        probe_only=True,
    )
    try:
        wfd.start_experimental_backend(ns)
        return 0
    except KeyboardInterrupt:
        return 0
    except wfd.WFDNotReady as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2


# -------------------------------------------------------------------- ctl
_CTL_VALUE_CMDS = {"fps", "bitrate", "volume", "monitor", "mode", "qp", "sink"}
# Optional argument: the step, defaulting to DEFAULT_VOLUME_STEP. A waybar
# on-scroll binding passes nothing at all.
_CTL_STEP_CMDS = {"volume-up", "volume-down"}
_CTL_NOARG_CMDS = {"status", "start", "stop", "idr", "toggle-mode", "toggle-mute",
                   "list-outputs", "list-sinks", "quit"}
_CTL_COMMANDS = sorted(_CTL_VALUE_CMDS | _CTL_STEP_CMDS | _CTL_NOARG_CMDS | {"mute"})


def cmd_ctl(args) -> int:
    command = args.command
    if command in _CTL_VALUE_CMDS and args.value is None:
        print(f"{PROG} ctl {command}: needs a value", file=sys.stderr)
        return 2
    fields = {}
    if command == "bitrate":
        try:
            fields["value"] = parse_bitrate(args.value)
        except SessionError as exc:
            print(f"{PROG}: {exc}", file=sys.stderr)
            return 2
    elif command in ("fps", "volume", "qp"):
        if not str(args.value).lstrip("-").isdigit():
            print(f"{PROG} ctl {command}: needs an integer", file=sys.stderr)
            return 2
        fields["value"] = int(args.value)
    elif command in _CTL_STEP_CMDS:
        if args.value is not None:
            if not str(args.value).lstrip("-").isdigit():
                print(f"{PROG} ctl {command}: the step must be an integer",
                      file=sys.stderr)
                return 2
            fields["value"] = abs(int(args.value))
    elif command == "mode":
        try:
            fields["value"] = normalize_mode(args.value)
        except SessionError as exc:
            print(f"{PROG} ctl mode: {exc}", file=sys.stderr)
            return 2
    elif command == "mute":
        fields["value"] = args.value or "toggle"
    elif command in _CTL_VALUE_CMDS:
        fields["value"] = args.value

    try:
        reply = ctlmod.Client().call(command, **fields)
    except ctlmod.NotRunning as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 3
    except ctlmod.CtlError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(reply, indent=2))
    elif command == "list-outputs":
        for out in reply.get("outputs", []):
            print(f"{out['name']:<14} {out['width']}x{out['height']}@{out['refresh']:g} "
                  f"scale={out['scale']:g} {'[focused] ' if out['focused'] else ''}"
                  f"{out['description']}")
    elif command == "list-sinks":
        for sink in reply.get("sinks", []):
            print(f"{'*' if sink['default'] else ' '} {sink['name']}\n"
                  f"    {sink['description']} | monitor: {sink['monitor']} | "
                  f"{sink['volume']}%{' muted' if sink['mute'] else ''}")
    else:
        _print_status(reply.get("state", {}))
    return 0


# ----------------------------------------------------------------- status
def cmd_status(args) -> int:
    try:
        reply = ctlmod.Client().call("status")
    except ctlmod.NotRunning:
        if args.json:
            print(json.dumps({"state": "idle", "running": False}))
        else:
            print("hyprcast: not running")
        return 3
    except ctlmod.CtlError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    state = reply.get("state", {})
    if args.json:
        print(json.dumps(state, indent=2))
    else:
        _print_status(state)
    return 0


def _print_status(state: dict) -> None:
    """What a person wants to read. `--json` is there for everything else."""
    if not state:
        print(f"{PROG}: no state")
        return

    name = str(state.get("state") or "idle")
    peer = waybarmod.peer_label(state)
    if name == "casting":
        headline = f"casting to {peer} for {waybarmod.duration(state.get('duration', 0))}"
    elif name == "connecting":
        headline = f"connecting to {peer}"
    elif name == "discovering":
        headline = state.get("detail") or "looking for a sink"
    elif name == "error":
        headline = f"error -- {state.get('error') or 'no detail'}"
    else:
        headline = "idle"
    print(f"{PROG}: {headline}")

    if name != "casting":
        detail = {"idle": f"start one with `{PROG} cast`",
                  "connecting": str(state.get("detail") or "")}.get(name, "")
        if detail:
            print(f"  {detail}")
        return

    stats = state.get("stats") or {}
    drops = int(stats.get("drops", 0) or 0)
    width, height = state.get("width") or 0, state.get("height") or 0
    audio = str(state.get("audio_mode") or "shared")
    rows = [
        ("wire", f"{width}x{height} @ {state.get('fps')} fps, "
                 f"{state.get('bitrate', 0) / 1e6:.1f} Mb/s target"),
        ("measured", f"{float(stats.get('fps', 0.0)):.1f} fps, "
                     f"{waybarmod.rate(float(stats.get('kbps', 0.0)) * 1000)}"
                     + (f", cpu {float(stats.get('cpu', 0.0)) * 100:.0f}%"
                        if stats.get("cpu") else "")
                     + (f", DROPS {drops}" if drops else "")
                     + (f", idr {stats.get('idr')}" if stats.get("idr") else "")),
        ("capture", f"{state.get('capture_output') or '?'} "
                    f"({mode_label(state.get('mode'))})"),
        ("audio", "off (video only)" if audio == "none" else
                  f"{audio}, " + ("MUTED" if state.get("muted")
                                  else f"volume {state.get('volume', 100)}%")),
    ]
    if state.get("error"):
        rows.append(("last error", str(state["error"])))
    for label, value in rows:
        print(f"  {label:<10} {value}")


# ----------------------------------------------------------------- doctor
def _check_hyprland() -> tuple[bool, str]:
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return False, "HYPRLAND_INSTANCE_SIGNATURE unset -- run inside a Hyprland session"
    try:
        version = hypr._hyprctl(["version"]).splitlines()[0]
        monitors = hypr.list_monitors()
    except hypr.HyprError as exc:
        return False, str(exc)
    names = ", ".join(f"{m.name} {m.width}x{m.height}@{m.refresh:.0f}" for m in monitors)
    return True, f"{version.split(' built')[0]}; outputs: {names}"


def _check_render_node() -> tuple[bool, str]:
    node = os.environ.get("HC_RENDER_NODE", "/dev/dri/renderD128")
    if not os.path.exists(node):
        return False, f"{node} does not exist"
    if not os.access(node, os.R_OK | os.W_OK):
        return False, f"{node} is not read/write for this user (need the 'render' group)"
    return True, node


def _check_vaapi() -> tuple[bool, str]:
    binary = shutil.which("vainfo")
    if not binary:
        return True, "vainfo not installed -- skipped (the engine asserts iHD itself)"
    env = dict(os.environ, LIBVA_DRIVER_NAME="iHD")
    try:
        out = subprocess.run([binary], capture_output=True, text=True, timeout=10,
                             env=env).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"vainfo failed: {exc}"
    if "iHD" not in out:
        return False, "the iHD driver did not load (i965 misreports RGB32 encode on Gen9.5)"
    has_lp = "VAEntrypointEncSliceLP" in out
    return True, "iHD" + (", VDEnc (EncSliceLP) available" if has_lp else ", no low-power entrypoint")


def _check_audio() -> tuple[bool, str]:
    try:
        sink = audiomod.default_sink()
        monitor = audiomod.resolve_monitor(sink)
    except audiomod.AudioError as exc:
        return False, str(exc)
    if not monitor:
        return False, f"default sink {sink} exposes no monitor source"
    return True, f"{sink}\n{'':>19}monitor {monitor}"


def _check_engine() -> tuple[bool, str]:
    path = engine_path()
    if not path:
        return False, ("engine/build/hyprcast-engine is not built "
                       "(run: meson setup engine/build && ninja -C engine/build)")
    return True, path


def _check_socket() -> tuple[bool, str]:
    path = ctlmod.socket_path()
    if ctlmod.Client(timeout=1.0).alive():
        return True, f"a session is already running on {path}"
    if os.path.exists(path):
        return True, f"{path} exists but is stale -- it will be replaced"
    return True, f"{path} is free"


def _check_p2p() -> tuple[bool, str]:
    binary = shutil.which("iw")
    if not binary:
        return False, "iw is not installed -- Wi-Fi Direct cannot be set up"
    try:
        out = subprocess.run([binary, "list"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"iw list failed: {exc}"
    if "P2P-GO" not in out and "P2P-client" not in out:
        return False, "no P2P-GO/P2P-client interface mode -- this card cannot do Wi-Fi Direct"
    # IR-CONCURRENT is a REGULATORY flag, so it is in `iw reg get`, not `iw list`.
    # Without it every 5 GHz channel is No-IR and a group silently drops to
    # 2.4 GHz, where 720p60 does not fit.
    try:
        reg = subprocess.run([binary, "reg", "get"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        reg = ""
    concurrent = "IR-CONCURRENT" in reg
    return True, ("P2P-GO supported"
                  + ("; 5 GHz IR-CONCURRENT present -- keep wlan0 associated on that channel"
                     if concurrent else "; no 5 GHz IR-CONCURRENT -- expect a 2.4 GHz group"))


def cmd_doctor(args) -> int:
    checks = (
        ("hyprland", _check_hyprland),
        ("render node", _check_render_node),
        ("va-api", _check_vaapi),
        ("audio", _check_audio),
        ("engine", _check_engine),
        ("ctl socket", _check_socket),
        ("wi-fi p2p", _check_p2p),
    )
    results = []
    failed = 0
    for name, fn in checks:
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append({"check": name, "ok": ok, "detail": detail})
        failed += not ok
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for item in results:
            print(f"[{'ok' if item['ok'] else 'FAIL'}] {item['check']:<12} {item['detail']}")
        print()
        print("all clear" if not failed else f"{failed} check(s) block a cast")
    return 0 if not failed else 1


# ----------------------------------------------------------------- waybar
def cmd_waybar(args) -> int:
    return waybarmod.run()


# ----------------------------------------------------------------- config
def cmd_config(args) -> int:
    path = configmod.config_path()
    if args.path:
        print(path)
        return 0
    if args.init:
        try:
            configmod.write_starter(path)
        except configmod.ConfigError as exc:
            print(f"{PROG}: {exc}", file=sys.stderr)
            return 1
        print(f"{PROG}: wrote {path}")
        return 0
    try:
        cfg = _load_config(args)
    except configmod.ConfigError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(
            {"path": cfg.path, "exists": cfg.exists,
             "config": {section: {key: {"value": value, "source": source}
                                  for s, key, value, source in cfg.rows() if s == section}
                        for section in configmod.SPEC}},
            indent=2))
    else:
        print("\n".join(configmod.describe(cfg)))
    return 0


# ------------------------------------------------------------------- main
def _config_flags() -> argparse.ArgumentParser:
    """The arguments that a config key can supply.

    Shared by `cast` and `config` so that `hyprcast config --fps 30` shows
    exactly the precedence `hyprcast cast --fps 30` would get. Every default is
    None: see _FLAG_MAP.
    """
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--mode", choices=MODE_NAMES, default=None,
                   help="mirror an existing output, or extend onto a headless one")
    p.add_argument("--second-screen", action="store_true", default=None,
                   help="alias for --mode extend")
    p.add_argument("--monitor", metavar="NAME", default=None,
                   help="output to mirror (name or description substring)")
    p.add_argument("--fps", type=int, default=None)
    p.add_argument("--bitrate", default=None, help="e.g. 8M, 6000k")
    p.add_argument("--width", type=int, default=None)
    p.add_argument("--height", type=int, default=None)
    p.add_argument("--audio", choices=("shared", "tv-only", "device", "none"), default=None,
                   help="tv-only routes playback through a null sink so the "
                        "laptop speakers stay silent")
    p.add_argument("--volume", type=int, default=None, metavar="0-100",
                   help="cast volume applied at start; 100 is unity and the "
                        "laptop's own volume is never touched")
    p.add_argument("--low-power", action="store_true", default=None,
                   help="VDEnc/EncSliceLP -- CQP only on Gen9.5, ignores --bitrate")
    p.add_argument("--qp", type=int, default=None,
                   help="CQP quantiser, only with --low-power")
    p.add_argument("--no-firewall", action="store_true", default=None,
                   help="do not touch firewalld (it can time out on this box)")
    p.add_argument("--sink", metavar="MAC", default=None,
                   help="skip discovery and connect to this P2P peer MAC")
    p.add_argument("--interface", default=None,
                   help="wpa_supplicant P2P control interface")
    p.add_argument("--timeout", type=int, default=None,
                   help="seconds to wait for the sink to appear")
    p.add_argument("--go-intent", type=int, default=None, metavar="0-15",
                   help="P2P group-owner intent; 15 makes us the GO")
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Cast a Hyprland output to a Miracast sink.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    flags = _config_flags()

    cast = sub.add_parser("cast", parents=[flags],
                          help="run a session in the foreground")
    cast.add_argument("--peer", metavar="IP[:PORT]",
                      help="stream straight at this address, skipping P2P+RTSP (mock-sink only)")
    cast.add_argument("--src-port", type=int, default=19002)
    cast.add_argument("--rtsp-port", type=int, default=7236)
    cast.add_argument("--audio-device", metavar="SOURCE", default=None,
                      help="capture this PipeWire source instead of the default "
                           "sink's monitor; skips --audio routing entirely")
    cast.add_argument("--latency-log", nargs="?", const=True, default=None,
                      metavar="PATH", help="write a session JSONL")
    cast.add_argument("--idle", action="store_true",
                      help="serve the control socket without starting the media path")
    cast.set_defaults(func=cmd_cast)

    conf = sub.add_parser("config", parents=[flags],
                          help="show or create the config file")
    conf.add_argument("--path", action="store_true",
                      help="print where the config file is looked for")
    conf.add_argument("--init", action="store_true",
                      help="write a commented starter file, never overwriting one")
    conf.add_argument("--json", action="store_true")
    conf.set_defaults(func=cmd_config)

    ctl = sub.add_parser("ctl", help="drive a running session")
    ctl.add_argument("command", choices=_CTL_COMMANDS, metavar="COMMAND",
                     help=" | ".join(_CTL_COMMANDS))
    ctl.add_argument("value", nargs="?",
                     help="the new value; the step for volume-up/volume-down")
    ctl.add_argument("--json", action="store_true")
    ctl.set_defaults(func=cmd_ctl)

    status = sub.add_parser("status", help="print the current session state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    wb = sub.add_parser("waybar", help="stream waybar JSON on stdout")
    wb.set_defaults(func=cmd_waybar)

    probe = sub.add_parser("probe",
                           help="ask the sink what it can do, without casting")
    probe.add_argument("--interface", default="p2p-dev-wlan0")
    probe.add_argument("--timeout", type=int, default=60)
    probe.add_argument("--sink", metavar="MAC", default=None)
    probe.add_argument("--fps", type=int, default=60)
    probe.set_defaults(func=cmd_probe)

    scan = sub.add_parser("scan", help="look for sinks without casting")
    scan.add_argument("--interface", default="p2p-dev-wlan0")
    scan.add_argument("--timeout", type=int, default=15)
    scan.set_defaults(func=cmd_scan)

    doc = sub.add_parser("doctor", help="check only what actually blocks a cast")
    doc.add_argument("--json", action="store_true")
    doc.set_defaults(func=cmd_doctor)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
