"""
hyprcast command line.

    hyprcast cast [--second-screen] [--monitor NAME] [--fps N] [--bitrate N]
    hyprcast ctl fps 30 | bitrate 6M | volume 50 | mute | monitor NAME | mode X
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
from . import ctl as ctlmod
from . import hypr
from . import waybar as waybarmod
from .session import DEFAULT_WIRE, MODES, Session, SessionError, engine_path, parse_bitrate

PROG = "hyprcast"


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
        wfd_audio_device=None,
        wfd_low_power=args.low_power,
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
            lambda cmd, params: _ctl_bridge(wfd, cmd, params),
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


def _ctl_bridge(wfd, cmd: str, params: dict):
    """Route a ctl command at the live WFD pipeline."""
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
        p.volume(gain=float(params["value"]) / 100.0)
    elif cmd == "mute":
        p.volume(muted=bool(params.get("value", True)))
    elif cmd == "idr":
        p.request_idr()
    elif cmd == "monitor":
        p.set_output(str(params["value"]))
    elif cmd == "stop":
        p.stop()
    else:
        raise ctlmod.CtlError(f"unsupported while casting over WFD: {cmd}")
    return {"ok": True}


def _wfd_snapshot(wfd, ns) -> dict:
    p = wfd.current_pipeline()
    if p is None:
        return {"state": "discovering", "peer": ns.wfd_peer or "", "fps": ns.fps}
    return {
        "state": "casting" if p.is_alive() else "error",
        "peer": p.tv_ip,
        "width": p.width,
        "height": p.height,
        "fps": p.config.fps,
        "bitrate_kbits": p.bitrate_kbits,
        "mode": "mirror",
        "capture_output": (p.config.monitor.name if p.config.monitor else ""),
        "health": p.health_summary(),
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


# -------------------------------------------------------------------- ctl
_CTL_VALUE_CMDS = {"fps", "bitrate", "volume", "monitor", "mode"}
_CTL_NOARG_CMDS = {"status", "start", "stop", "list-outputs", "list-sinks", "quit"}


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
    elif command in ("fps", "volume"):
        if not str(args.value).lstrip("-").isdigit():
            print(f"{PROG} ctl {command}: needs an integer", file=sys.stderr)
            return 2
        fields["value"] = int(args.value)
    elif command == "mode":
        if args.value not in MODES:
            print(f"{PROG} ctl mode: use {' or '.join(MODES)}", file=sys.stderr)
            return 2
        fields["value"] = args.value
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
    if not state:
        print("hyprcast: no state")
        return
    stats = state.get("stats", {})
    print(f"state      {state.get('state')}"
          + (f"  ({state['error']})" if state.get("error") else ""))
    print(f"mode       {state.get('mode')}  capture={state.get('capture_output') or '-'}")
    print(f"wire       {state.get('width')}x{state.get('height')}@{state.get('fps')} "
          f"target {state.get('bitrate', 0) / 1e6:.1f} Mb/s")
    print(f"peer       {state.get('peer') or '-'}")
    print(f"audio      {state.get('audio_mode')}  {state.get('audio_source') or '-'}  "
          f"vol={state.get('volume')}%{' muted' if state.get('muted') else ''}")
    if state.get("state") == "casting":
        print(f"measured   {stats.get('fps', 0):.1f} fps  {stats.get('kbps', 0)} kb/s  "
              f"drops={stats.get('drops', 0)}  idr={stats.get('idr', 0)}")
        print(f"duration   {state.get('duration', 0):.0f}s")


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


# ------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Cast a Hyprland output to a Miracast sink.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    cast = sub.add_parser("cast", help="run a session in the foreground")
    cast.add_argument("--second-screen", action="store_true",
                      help="create a headless output at the wire mode instead of mirroring")
    cast.add_argument("--monitor", metavar="NAME",
                      help="output to mirror (name or description substring)")
    cast.add_argument("--fps", type=int, default=60)
    cast.add_argument("--bitrate", default="8M", help="e.g. 8M, 6000k")
    cast.add_argument("--width", type=int, default=DEFAULT_WIRE[0])
    cast.add_argument("--height", type=int, default=DEFAULT_WIRE[1])
    cast.add_argument("--audio", choices=("shared", "tv-only", "none"), default="shared",
                      help="tv-only routes playback through a null sink so the "
                           "laptop speakers stay silent")
    cast.add_argument("--peer", metavar="IP[:PORT]",
                      help="stream straight at this address, skipping P2P+RTSP (mock-sink only)")
    cast.add_argument("--src-port", type=int, default=19002)
    cast.add_argument("--low-power", action="store_true",
                      help="VDEnc/EncSliceLP -- CQP only on Gen9.5, ignores --bitrate")
    cast.add_argument("--sink", metavar="MAC",
                      help="skip discovery and connect to this P2P peer MAC")
    cast.add_argument("--interface", default="p2p-dev-wlan0",
                      help="wpa_supplicant P2P control interface")
    cast.add_argument("--timeout", type=int, default=60,
                      help="seconds to wait for the sink to appear")
    cast.add_argument("--rtsp-port", type=int, default=7236)
    cast.add_argument("--go-intent", type=int, default=None, metavar="0-15",
                      help="P2P group-owner intent; 15 makes us the GO")
    cast.add_argument("--qp", type=int, default=None,
                      help="CQP quantiser, only with --low-power")
    cast.add_argument("--no-firewall", action="store_true",
                      help="do not touch firewalld (it can time out on this box)")
    cast.add_argument("--latency-log", nargs="?", const=True, default=None,
                      metavar="PATH", help="write a session JSONL")
    cast.add_argument("--idle", action="store_true",
                      help="serve the control socket without starting the media path")
    cast.set_defaults(func=cmd_cast)

    ctl = sub.add_parser("ctl", help="drive a running session")
    ctl.add_argument("command", choices=sorted(_CTL_VALUE_CMDS | _CTL_NOARG_CMDS | {"mute"}))
    ctl.add_argument("value", nargs="?")
    ctl.add_argument("--json", action="store_true")
    ctl.set_defaults(func=cmd_ctl)

    status = sub.add_parser("status", help="print the current session state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    wb = sub.add_parser("waybar", help="stream waybar JSON on stdout")
    wb.set_defaults(func=cmd_waybar)

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
