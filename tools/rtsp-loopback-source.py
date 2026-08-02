#!/usr/bin/env python3
"""
rtsp-loopback-source.py -- start hyprcast's WFD RTSP source on localhost only.

main.py has no direct-IP escape hatch: start_experimental_backend() always runs
the Wi-Fi Direct scan and waits for a NetworkManager P2P activation before it
will serve anybody, and it now binds RTSP to the p2p-* interface address, so it
cannot be pointed at 127.0.0.1. This harness starts the very same
WFDRTSPServer with the very same WFDMediaConfig, and skips only the P2P half.
Nothing in wfd.py is patched or monkeypatched -- _WFDRTSPHandler and
NativeSender run verbatim, including the fd-3 handshake with the engine.

Pair it with tools/mock-sink.py for a full loopback session:

    python3 tools/rtsp-loopback-source.py 35 &
    python3 tools/mock-sink.py --fifo /tmp/out.ts

Set HYPRCAST_RTSP_DUMP to capture the source side of the wire for diffing
against reference/sink/wire-720p60-full-session.txt. Set HYPRCAST_ENGINE (or
pass --engine) to run against something other than engine/build/hyprcast-engine.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
    ),
)

from wfd import WFDMediaConfig, WFDRTSPServer, select_monitor  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the hyprcast WFD RTSP source without Wi-Fi Direct.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("seconds", nargs="?", type=float, default=0.0,
                        help="Stop after N seconds; 0 runs until SIGINT")
    parser.add_argument("--port", type=int, default=7236, help="RTSP listen port")
    parser.add_argument("--host", default="127.0.0.1", help="RTSP listen address")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--bitrate", default="4M")
    parser.add_argument("--no-audio", action="store_true", dest="no_audio")
    parser.add_argument("--peer-name", default="TV", dest="peer_name",
                        help="Peer name the source uses for its sink quirk checks")
    parser.add_argument("--source-port", type=int, default=19002, dest="source_port")
    parser.add_argument("--latency-log", default=None, dest="latency_log")
    parser.add_argument("--engine", default=None,
                        help="Engine binary to drive (default: $HYPRCAST_ENGINE "
                             "or engine/build/hyprcast-engine)")
    parser.add_argument("--monitor", default=None, dest="monitor_name",
                        help="Capture this Hyprland output; omit to let the "
                             "engine pick, which is what a headless test wants")
    parser.add_argument("--low-power", action="store_true", dest="low_power",
                        help="VAEntrypointEncSliceLP (CQP only on Gen9.5)")
    parser.add_argument("--qp", type=int, default=0, help="CQP quantiser for --low-power")
    args = parser.parse_args()

    config = WFDMediaConfig(
        monitor=select_monitor(args.monitor_name) if args.monitor_name else None,
        fps=args.fps,
        bitrate=args.bitrate,
        output_resolution=None,
        audio_device=None,
        no_audio=args.no_audio,
        source_port=args.source_port,
        latency_log_path=args.latency_log,
        peer_name=args.peer_name,
        low_power=args.low_power,
        qp=args.qp,
        engine_path=args.engine,
    )

    rtsp = WFDRTSPServer(media_config=config, host=args.host, port=args.port)
    rtsp.start()

    stopping = False

    def on_signal(signum, _frame):
        nonlocal stopping
        print(f"[harness] caught {signal.Signals(signum).name}", flush=True)
        stopping = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    deadline = time.monotonic() + args.seconds if args.seconds > 0 else None
    while not stopping and (deadline is None or time.monotonic() < deadline):
        time.sleep(0.2)

    print("[harness] stopping", flush=True)
    rtsp.stop_all_media()
    rtsp.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
