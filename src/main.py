"""
hyprcast entry point — Wi-Fi Display (Miracast) only.

Private Hyprland-only fork of fluxcast (GPL-3.0). One machine, one sink:
the DLNA, Chromecast, HLS-server, tray and portal backends are gone, so
Wi-Fi Display is the only protocol and needs no --protocol flag.

Usage:
    python3 src/main.py [OPTIONS]

Streaming & Encoding Options:
    --fps N                  Frames per second (default: 60)
    --bitrate Xm             Video bitrate (default: 4M)
    --output-res WxH         Scale output (e.g. 1280x720); default: negotiated
    --monitor NAME           Pre-select monitor by name, e.g. eDP-1

Wi-Fi Display (Miracast) Options:
    --wfd-scan               Run active Wi-Fi Direct discovery and exit
    --wfd-peer PEER          Peer selector: index, MAC, or name
    --wfd-dry-run            Print D-Bus call without activating connection
    --wfd-test-pattern       Stream generated test video instead of the desktop
    --wfd-ffmpeg-stats       Show ffmpeg progress statistics
    --wfd-media-pipeline auto|ffmpeg|gst
                             RTP media sender (default: auto)
    --wfd-latency-log PATH   Write latency/session events to a JSONL log file
    --wfd-no-audio           Stream video only
    --wfd-audio-device DEV   PipeWire/Pulse monitor source for audio
    --wfd-rtsp-port PORT     RTSP port advertised in WFD IEs (default: 7236)
    --wfd-no-firewall        Do not auto-open the RTSP port via firewalld
    --wfd-rtp-source-port P  Local RTP source port (default: 19002)
    --wfd-interface IFACE    Wi-Fi interface to use, e.g. wlan0
    --wfd-timeout N          Wi-Fi Direct scan timeout in seconds (default: 8)
    --wfd-go-intent 0-15     P2P group-owner intent; 0 lets the TV be the owner
"""

import argparse
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hyprcast",
        description="hyprcast — stream this desktop to a Miracast sink over Wi-Fi Display",
    )

    # Streaming & Encoding Options
    stream_opts = parser.add_argument_group("Streaming & Encoding Options")
    stream_opts.add_argument("--fps", type=int, default=60,
                             help="Frames per second (default: 60)")
    stream_opts.add_argument("--bitrate", default="4M",
                             help="Video bitrate (default: 4M)")
    stream_opts.add_argument("--output-res", default=None, dest="output_res",
                             help="Scale output to WxH, e.g. 1280x720 "
                                  "(default: whatever the sink negotiates)")
    stream_opts.add_argument("--monitor", default=None, dest="monitor_name",
                             help="Pre-select monitor by name, e.g. eDP-1 (skips the picker)")

    # Wi-Fi Display (Miracast) Options
    wfd = parser.add_argument_group("Wi-Fi Display (Miracast) Options")
    wfd.add_argument("--wfd-scan", action="store_true", dest="wfd_scan",
                     help="Run active Wi-Fi Direct discovery and exit")
    wfd.add_argument("--wfd-peer", default=None, dest="wfd_peer",
                     help="Peer selector: index, MAC, or name")
    wfd.add_argument("--wfd-dry-run", action="store_true", dest="wfd_dry_run",
                     help="Print the connection D-Bus call without activating it")
    wfd.add_argument("--wfd-test-pattern", action="store_true", dest="wfd_test_pattern",
                     help="Stream generated test video instead of the desktop")
    wfd.add_argument("--wfd-ffmpeg-stats", action="store_true", dest="wfd_ffmpeg_stats",
                     help="Show ffmpeg progress statistics")
    wfd.add_argument("--wfd-media-pipeline", default="auto",
                     choices=["auto", "ffmpeg", "gst"], dest="wfd_media_pipeline",
                     help="RTP media sender: auto (default), ffmpeg, or gst")
    wfd.add_argument("--wfd-latency-log", nargs="?", const="/tmp/hyprcast-wfd-latency.jsonl",
                     default=None, dest="wfd_latency_log",
                     help="JSONL file path for latency/session logging "
                          "(default: /tmp/hyprcast-wfd-latency.jsonl)")
    wfd.add_argument("--wfd-no-audio", action="store_true", dest="wfd_no_audio",
                     help="Stream video only")
    wfd.add_argument("--wfd-audio-device", default=None, dest="wfd_audio_device",
                     help="PipeWire/Pulse monitor source for audio")
    wfd.add_argument("--wfd-rtsp-port", type=int, default=7236, dest="wfd_rtsp_port",
                     help="RTSP port advertised in WFD IEs (default: 7236)")
    wfd.add_argument("--wfd-no-firewall", action="store_true", dest="wfd_no_firewall",
                     help="Do not auto-open the WFD RTSP port via firewalld for the "
                          "session (use if you manage the firewall yourself)")
    wfd.add_argument("--wfd-rtp-source-port", type=int, default=19002,
                     dest="wfd_rtp_source_port",
                     help="Local RTP source port (default: 19002)")
    wfd.add_argument("--wfd-interface", default=None, dest="wfd_interface",
                     help="Wi-Fi interface to use, e.g. wlan0")
    wfd.add_argument("--wfd-timeout", type=int, default=8, dest="wfd_timeout",
                     help="Wi-Fi Direct scan timeout in seconds (default: 8)")
    wfd.add_argument("--wfd-go-intent", type=int, default=0, dest="wfd_go_intent",
                     choices=range(0, 16), metavar="0-15",
                     help="P2P group-owner intent (0-15); 0 forces the TV to be the "
                          "group owner, which this sink requires to start the "
                          "session (default: 0)")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    from wfd import WFDNotReady

    if args.wfd_scan:
        from wfd import active_scan, print_scan
        try:
            peers = active_scan(interface=args.wfd_interface, timeout=args.wfd_timeout)
        except WFDNotReady as exc:
            print(f"[hyprcast WFD] ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        print_scan(peers)
        return

    from wfd import start_experimental_backend
    try:
        start_experimental_backend(args)
    except WFDNotReady as exc:
        print(f"[hyprcast WFD] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[hyprcast] Stopped.")


if __name__ == "__main__":
    main()
