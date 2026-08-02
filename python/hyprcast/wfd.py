import ipaddress
import re
import random
import signal
import socket
import socketserver
import shutil
import subprocess
import sys
import threading
import time
import json
import os
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from typing import NamedTuple, Optional

# The media leg is hyprcast-engine, driven over a socketpair by engine.py.
from .engine import Engine, EngineError

WFD_RTSP_PORT = 7236

# The live WFDMediaPipeline, published so the ctl socket can drive it while the
# WFD flow owns the main thread. Set on engine start, cleared on stop.
_ACTIVE: "Optional[WFDMediaPipeline]" = None
_ACTIVE_LOCK = threading.Lock()


def current_pipeline():
    """The pipeline of the running session, or None."""
    with _ACTIVE_LOCK:
        return _ACTIVE


def _publish(pipeline) -> None:
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = pipeline

try:
    _DEVICE_NAME: str = re.sub(r"[^a-zA-Z0-9\-]", "", socket.gethostname().split(".")[0])[:32] or "FluxCast"
except OSError:
    _DEVICE_NAME = "FluxCast"

# WFD CEA resolution bitmask. The current backend intentionally negotiates
# only the !common! HD modes that Samsung TVs usually accept reliably.
WFD_CEA_640P60  = 0x00000001  # bit 0: 640x480p60
WFD_CEA_720P30  = 0x00000020  # bit 5: 1280x720p30, mandatory HD mode
WFD_CEA_720P60  = 0x00000040  # bit 6: 1280x720p60
WFD_CEA_1080P30 = 0x00000080  # bit 7: 1920x1080p30
WFD_CEA_1080P60 = 0x00000100  # bit 8: 1920x1080p60

# VESA resolution bitmasks (Table 5-11 / AOSP VideoFormats.cpp)
WFD_VESA_1200P30 = 0x10000000  # bit 28: 1920x1200p30
WFD_VESA_1200P60 = 0x20000000  # bit 29: 1920x1200p60

WFD_LEVEL_31 = 0x01
WFD_LEVEL_32 = 0x02
WFD_LEVEL_40 = 0x04
WFD_LEVEL_42 = 0x10
WFD_LEVEL_50 = 0x20
WFD_LEVEL_51 = 0x40
WFD_AUDIO_AAC = "AAC 00000001 00"
WFD_AUDIO_LPCM_48K  = "LPCM 00000002 00"
NM_DEST = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"


def _wfd_ie_device_info(rtsp_port: int) -> bytes:
    """
    WFD Subelement ID 0: WFD Device Information (6 bytes)
    Byte 0-1: Device Information bitmask
              (0x0010 = Source, 0x0000 = Coupled Sink not supported)
    Byte 2-3: Session Management Control Port (RTSP port)
    Byte 4-5: Device Throughput (max 100 Mbps)
    """
    return bytes([
        0x00, 0x00, 0x06,
        0x00, 0x10,  # Info: WFD Source, Session Available (No HDCP/Coupled Sink)
        (rtsp_port >> 8) & 0xff, rtsp_port & 0xff,
        0x00, 0xc8   # Throughput: 100 Mbps
    ])


def _wfd_ie_device_name(name: str) -> bytes:
    """
    WFD Subelement ID 10: WFD Device Name
    """
    encoded = name.encode("utf-8")
    length = len(encoded)
    return bytes([0x0a, (length >> 8) & 0xff, length & 0xff]) + encoded


@dataclass
class WFDPeer:
    address: str
    name: str = ""
    details: str = ""
    path: str = ""
    source: str = ""
    rtsp_port: int = 7236


def _parse_gdbus_byte_array(raw: str) -> list[int]:
    """Parse a gdbus @ay variant string into a list of integer byte values.

    NetworkManager returns WFD IEs via gdbus as a formatted string such as:
    ``<@ay [byte 0x00, byte 0x10, byte 0x1c, byte 0x00, byte 0x1c, ...]>`
    """
    return [int(h, 16) for h in re.findall(r"0x([0-9a-fA-F]+)", raw)]


def _parse_wfd_ies_rtsp_port(wfd_ies: list[int]) -> int:
    """
    Parse the WFD Information Element bytes to find the Sink's RTSP port.
    WFD Subelement ID 0: WFD Device Information (length 6)
    Bytes 3-4 of the subelement (offset 3 and 4 after ID and Length) 
    contain the RTSP port.
    """
    if not wfd_ies or len(wfd_ies) < 6:
        return 7236
    
    i = 0
    while i + 3 <= len(wfd_ies):
        sub_id = wfd_ies[i]
        sub_len = (wfd_ies[i+1] << 8) | wfd_ies[i+2]
        if sub_id == 0 and sub_len >= 6 and i + 3 + sub_len <= len(wfd_ies):
            # Port is at index i + 3 + 2 and i + 3 + 3
            port = (wfd_ies[i+5] << 8) | wfd_ies[i+6]
            return port if port > 0 else 7236
        i += 3 + sub_len
    return 7236


class WFDNotReady(RuntimeError):
    pass


@dataclass
class RTSPMessage:
    start: str
    headers: dict[str, str]
    raw_headers: list[str]
    body: str = ""

    @property
    def is_response(self) -> bool:
        return self.start.startswith("RTSP/")

    @property
    def method(self) -> str:
        if self.is_response:
            return ""
        return self.start.split(maxsplit=1)[0] if self.start else ""

    @property
    def cseq(self) -> str:
        return self.headers.get("cseq", "0")

    @property
    def status(self) -> str:
        if not self.is_response:
            return ""
        parts = self.start.split(maxsplit=2)
        return " ".join(parts[1:]) if len(parts) >= 2 else ""


class WFDProbeDone(Exception):
    """Raised to unwind a probe session cleanly once M3 has been read."""


@dataclass
class WFDMediaConfig:
    """Everything the media leg needs. Nine of these cross the fd-3 seam."""

    monitor: Optional["Monitor"]
    fps: int = 60
    probe_only: bool = False
    bitrate: str = "4M"
    output_resolution: Optional[str] = None
    audio_device: Optional[str] = None
    no_audio: bool = False
    source_port: int = 19002
    latency_log_path: Optional[str] = None
    peer_name: str = ""
    # VDEnc (VAEntrypointEncSliceLP) is measurably cheaper on Gen9.5 -- 3.98 ms
    # vs 7.51 ms encode p50 -- but it accepts CQP only: CBR and VBR both fail
    # avcodec_open2 with EINVAL. So low_power ignores `bitrate` and uses `qp`.
    low_power: bool = False
    qp: int = 0
    engine_path: Optional[str] = None


@dataclass
class WFDVideoFormat:
    native: str
    preferred: str
    profile: str
    level: str
    cea_mask: int
    vesa_mask: int
    hh_mask: int
    # Optional trailing fields. A sink may advertise modes in the CEA mask that
    # exceed its own stated maximum -- this Xiaomi offers 1080p60 in
    # 0x0001ffff while capping itself at 1280x720 -- so these are a hard filter,
    # not a hint. 0 means "not advertised", i.e. no dimension limit.
    max_hres: int = 0
    max_vres: int = 0


@dataclass(frozen=True)
class WFDCEAMode:
    name: str
    bit: int
    native: str
    width: int
    height: int
    fps: int
    table: str = "cea"  # "cea" or "vesa"

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"


WFD_CEA_MODES: dict[int, WFDCEAMode] = {
    WFD_CEA_640P60:  WFDCEAMode("640x480p60",    WFD_CEA_640P60,  "08", 640,  480, 60),
    WFD_CEA_720P30:  WFDCEAMode("1280x720p30",   WFD_CEA_720P30,  "28", 1280, 720, 30),
    WFD_CEA_720P60:  WFDCEAMode("1280x720p60",   WFD_CEA_720P60,  "30", 1280, 720, 60),
    WFD_CEA_1080P30: WFDCEAMode("1920x1080p30",  WFD_CEA_1080P30, "38", 1920, 1080, 30),
    WFD_CEA_1080P60: WFDCEAMode("1920x1080p60",  WFD_CEA_1080P60, "40", 1920, 1080, 60),
}

WFD_VESA_MODES: dict[int, WFDCEAMode] = {
    WFD_VESA_1200P30: WFDCEAMode("1920x1200p30", WFD_VESA_1200P30, "00", 1920, 1200, 30, table="vesa"),
    WFD_VESA_1200P60: WFDCEAMode("1920x1200p60", WFD_VESA_1200P60, "00", 1920, 1200, 60, table="vesa"),
}


def _parse_resolution(value: Optional[str]) -> Optional[tuple[int, int]]:
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d+)x(\d+)\s*", value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _detect_audio_monitor() -> Optional[str]:
    """Resolve the default sink's monitor source.

    Returns None rather than the literal "default" when it cannot: "default"
    resolves to the default *source*, i.e. the built-in microphone, which is
    how fluxcast ended up streaming the room instead of the desktop.
    """
    try:
        sink = subprocess.check_output(
            ["pactl", "get-default-sink"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if sink and sink != "@DEFAULT_SINK@":
            return sink + ".monitor"
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        out = subprocess.check_output(
            ["pactl", "list", "short", "sinks"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if "RUNNING" in line:
                return line.split("\t")[1] + ".monitor"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _is_hyprland_session() -> bool:
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()
    session = (os.environ.get("XDG_SESSION_DESKTOP") or "").lower()
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) or "hyprland" in desktop or "hyprland" in session


class Monitor(NamedTuple):
    """One Hyprland output. Same shape capture.py's Monitor had, minus X11."""

    name: str            # e.g. 'eDP-1' -- also the engine's `output` selector
    width: int
    height: int
    x: int
    y: int
    refresh: float
    scale: float = 1.0
    focused: bool = False


def gather_monitors() -> list[Monitor]:
    """Enumerate outputs from `hyprctl monitors -j`.

    Sizes are the *pixel* mode, not the logical size: the engine captures
    pixels, so a scaled output must still be described by its real buffer.
    """
    if not shutil.which("hyprctl"):
        raise WFDNotReady("hyprctl not found; hyprcast is Hyprland-only.")
    try:
        result = _run(["hyprctl", "monitors", "-j"], timeout=3.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WFDNotReady(f"could not query Hyprland monitors: {exc}") from exc
    if result.returncode != 0:
        raise WFDNotReady(
            "hyprctl monitors failed: " + (result.stderr or result.stdout).strip()
        )
    try:
        raw = json.loads(result.stdout)
    except ValueError as exc:
        raise WFDNotReady(f"hyprctl monitors returned invalid JSON: {exc}") from exc

    monitors: list[Monitor] = []
    for entry in raw:
        if entry.get("disabled"):
            continue
        monitors.append(Monitor(
            name=str(entry.get("name", "")),
            width=int(entry.get("width", 0)),
            height=int(entry.get("height", 0)),
            x=int(entry.get("x", 0)),
            y=int(entry.get("y", 0)),
            refresh=float(entry.get("refreshRate", 0.0)),
            scale=float(entry.get("scale", 1.0)),
            focused=bool(entry.get("focused", False)),
        ))
    return monitors


def select_monitor(name: Optional[str]) -> Monitor:
    """Pick the named output, the focused one, or the only one."""
    monitors = gather_monitors()
    if not monitors:
        raise WFDNotReady("Hyprland reports no enabled monitors.")
    if name:
        for monitor in monitors:
            if monitor.name == name:
                return monitor
        available = ", ".join(m.name for m in monitors)
        raise WFDNotReady(f"Monitor '{name}' not found. Available: {available}")
    for monitor in monitors:
        if monitor.focused:
            return monitor
    return monitors[0]


def _bitrate_to_kbits(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)([kKmMgG]?)\s*", value)
    if not match:
        return 4000

    amount = float(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "g":
        amount *= 1_000_000
    elif suffix == "m":
        amount *= 1_000
    elif suffix == "k":
        amount *= 1
    else:
        amount /= 1_000
    return max(1, round(amount))


def _kbits_to_bitrate_text(value_kbits: int) -> str:
    if value_kbits % 1000 == 0:
        return f"{value_kbits // 1000}M"
    return f"{value_kbits}k"


def _quality_floor_kbits(width: int, height: int, fps: int) -> int:
    """
    Conservative quality floors for desktop readability at low latency.
    """
    pixels = width * height
    if pixels <= 1280 * 720:
        return 5000 if fps <= 30 else 7000
    if pixels <= 1920 * 1080:
        return 8000 if fps <= 30 else 14000
    # 1200p+ at ultrafast needs headroom to avoid compression artifacts
    return 14000 if fps <= 30 else 20000


def _calculate_gop(config: WFDMediaConfig) -> int:
    """
    Calculate Group of Pictures (GOP) size.
    LG TVs are strict and often require more frequent keyframes (IDR frames)
    to maintain a stable session, especially during initial buffering.
    """
    gop = max(1, config.fps)
    if "LG" in config.peer_name.upper():
        # For LG, use a 0.5s or 1s GOP but no more than 30 frames.
        return min(gop, 30)
    return gop


def _append_latency_log(path: Optional[str], event: str, **fields: object) -> None:
    if not path:
        return
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "mono": round(time.monotonic(), 6),
        "event": event,
        **fields,
    }
    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except OSError:
        pass


def _opt_hex(tokens: list[str], index: int) -> int:
    """Optional trailing hex field; 0 when absent or unparseable."""
    if index >= len(tokens):
        return 0
    try:
        return int(tokens[index], 16)
    except ValueError:
        return 0


def _parse_sink_video_format(value: str) -> Optional[WFDVideoFormat]:
    first_codec = value.split(",", 1)[0]
    tokens = first_codec.split()
    if len(tokens) < 11 or tokens[0].lower() == "none":
        return None
    try:
        return WFDVideoFormat(
            native=tokens[0],
            preferred=tokens[1],
            profile=tokens[2],
            level=tokens[3],
            cea_mask=int(tokens[4], 16),
            vesa_mask=int(tokens[5], 16),
            hh_mask=int(tokens[6], 16),
            max_hres=_opt_hex(tokens, 11),
            max_vres=_opt_hex(tokens, 12),
        )
    except ValueError:
        return None


def _choose_profile(profile_hex: str) -> str:
    try:
        profile_mask = int(profile_hex, 16)
    except ValueError:
        return profile_hex
    if profile_mask & 0x01:
        return "01"
    if profile_mask & 0x02:
        return "02"
    return f"{profile_mask & 0xff:02x}"


def _max_wfd_level(level_hex: str) -> Optional[int]:
    try:
        value = int(level_hex, 16)
    except ValueError:
        return None
    if value <= 0:
        return None
    highest = 1
    while highest << 1 <= value:
        highest <<= 1
    return highest


def _wfd_level_for_mode(mode: WFDCEAMode) -> int:
    if mode.width <= 1280 and mode.height <= 720:
        return WFD_LEVEL_31 if mode.fps <= 30 else WFD_LEVEL_32
    return WFD_LEVEL_40 if mode.fps <= 30 else WFD_LEVEL_42


def _desired_resolution(config: WFDMediaConfig) -> Optional[tuple[int, int]]:
    resolution = _parse_resolution(config.output_resolution)
    if resolution is not None:
        return resolution
    if config.monitor is not None:
        monitor = config.monitor
        return monitor.width, monitor.height
    return None


_mode_force_warned = False


def _choose_cea_mode(
    config: WFDMediaConfig,
    sink_format: Optional[WFDVideoFormat],
) -> WFDCEAMode:
    cea_supported = sink_format.cea_mask if sink_format else (
        WFD_CEA_720P30 | WFD_CEA_720P60 | WFD_CEA_1080P30 | WFD_CEA_1080P60
    )
    vesa_supported = sink_format.vesa_mask if sink_format else 0
    max_level = _max_wfd_level(sink_format.level) if sink_format else WFD_LEVEL_42
    resolution = _desired_resolution(config)
    wants_720 = resolution is None or (resolution[0] <= 1280 and resolution[1] <= 720)
    wants_1200 = resolution is not None and resolution[0] >= 1920 and resolution[1] > 1080
    wants_60 = config.fps > 30

    all_modes = {**WFD_CEA_MODES, **WFD_VESA_MODES}

    max_hres = sink_format.max_hres if sink_format else 0
    max_vres = sink_format.max_vres if sink_format else 0

    def supports(bit: int) -> bool:
        mode = all_modes[bit]
        if mode.table == "vesa":
            if not (vesa_supported & bit):
                return False
        else:
            if not (cea_supported & bit):
                return False
        # A sink can advertise modes in its mask that exceed its own stated
        # maximum. This Xiaomi offers 1080p60 inside CEA 0x0001ffff while
        # capping itself at max_hres/max_vres = 1280x720; sending 1080p30 to it
        # satisfies the mask and the level and still violates what it asked
        # for. Treat the stated maximum as binding.
        if max_hres and mode.width > max_hres:
            return False
        if max_vres and mode.height > max_vres:
            return False
        return max_level is None or _wfd_level_for_mode(mode) <= max_level

    # Build preference order: if monitor is 1200p, prefer VESA 1200p modes first
    if wants_1200:
        preferred = (
            [WFD_VESA_1200P60, WFD_VESA_1200P30,
             WFD_CEA_1080P60, WFD_CEA_1080P30, WFD_CEA_720P60, WFD_CEA_720P30]
            if wants_60 else [
                WFD_VESA_1200P30, WFD_VESA_1200P60,
                WFD_CEA_1080P30, WFD_CEA_1080P60,
                WFD_CEA_720P30, WFD_CEA_720P60,
            ]
        )
    elif wants_720:
        preferred = (
            [WFD_CEA_720P60, WFD_CEA_720P30]
            if wants_60 else [WFD_CEA_720P30, WFD_CEA_720P60]
        )
    else:
        preferred = (
            [WFD_CEA_1080P60, WFD_CEA_1080P30, WFD_CEA_720P60, WFD_CEA_720P30]
            if wants_60 else [
                WFD_CEA_1080P30,
                WFD_CEA_720P30,
                WFD_CEA_1080P60,
                WFD_CEA_720P60,
            ]
        )

    for bit in preferred:
        if supports(bit):
            return all_modes[bit]

    # Fallback: try any supported mode
    for bit in (
        WFD_CEA_720P30, WFD_CEA_1080P30, WFD_CEA_720P60, WFD_CEA_1080P60,
    ):
        if supports(bit):
            return all_modes[bit]

    # Nothing advertised — force the best mode for the source monitor.
    # Modern sinks (Samsung tablets etc.) accept modes beyond what they
    # advertise; Windows Miracast does the same.
    if wants_1200:
        forced = (
            [WFD_VESA_1200P60, WFD_VESA_1200P30,
             WFD_CEA_1080P60, WFD_CEA_1080P30]
            if wants_60 else [
                WFD_VESA_1200P30, WFD_VESA_1200P60,
                WFD_CEA_1080P30, WFD_CEA_1080P60,
            ]
        )
    elif wants_720:
        forced = (
            [WFD_CEA_720P60, WFD_CEA_720P30]
            if wants_60 else [WFD_CEA_720P30, WFD_CEA_720P60]
        )
    else:
        forced = (
            [WFD_CEA_1080P60, WFD_CEA_1080P30, WFD_CEA_720P60, WFD_CEA_720P30]
            if wants_60 else [
                WFD_CEA_1080P30, WFD_CEA_1080P60,
                WFD_CEA_720P30, WFD_CEA_720P60,
            ]
        )
    mode = all_modes[forced[0]]
    global _mode_force_warned
    if not _mode_force_warned:
        _mode_force_warned = True
        print(
            f"[hyprcast WFD RTSP] WARNING: Sink lacks advertised support for "
            f"{mode.name}; forcing it (most sinks accept it)."
        )
    return mode


def _selected_video_format(
    config: WFDMediaConfig,
    sink_format: Optional[WFDVideoFormat],
) -> str:
    mode = _choose_cea_mode(config, sink_format)

    profile = _choose_profile(sink_format.profile) if sink_format else "01"
    wfd_level = _wfd_level_for_mode(mode)
    # Bump level for resolutions exceeding standard 1080p limits
    if mode.width * mode.height > 1920 * 1080:
        wfd_level = WFD_LEVEL_50 if mode.fps <= 30 else WFD_LEVEL_51
    level = f"{wfd_level:02x}"

    # Place the mode bit in the correct mask field (CEA vs VESA)
    if mode.table == "vesa":
        cea_mask = 0
        vesa_mask = mode.bit
    else:
        cea_mask = mode.bit
        vesa_mask = 0

    return (
        f"{mode.native} 00 {profile} {level} {cea_mask:08x} "
        f"{vesa_mask:08x} 00000000 00 0000 0000 00 none none"
    )

def _udp_pair_free(port: int, local_ip: str = "") -> bool:
    """True if we can bind both `port` (RTP) and `port + 1` (RTCP) right now."""
    socks = []
    try:
        for candidate in (port, port + 1):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Deliberately NOT SO_REUSEADDR: we want to know whether ffmpeg's
            # avio will be able to take the port for real, and it does not set
            # it either. A permissive probe here would pass and then fail later,
            # after the port is already promised to the sink in SETUP.
            sock.bind((local_ip or "", candidate))
            socks.append(sock)
        return True
    except OSError:
        return False
    finally:
        for sock in socks:
            sock.close()


def _safe_source_port(requested: int, sink_port: int, sink_rtcp_port: int = 0,
                      local_ip: str = "") -> int:
    """Pick an RTP/RTCP source port pair that is free *and* not the sink's.

    This must settle before SETUP, because the source port goes out on the wire
    and the sink checks it -- the port cannot be moved afterwards. Previously
    this only avoided the sink's own ports, so a stale listener on 19002 was
    not noticed until hc_mux_open, by which point the engine had to fail, the
    session tore down, and the TV dropped the connection.
    """
    blocked = {sink_port}
    if sink_rtcp_port:
        blocked.add(sink_rtcp_port)
    else:
        blocked.add(sink_port + 1)

    port = requested
    if port % 2:
        port += 1

    first = port
    while port < 65000:
        if port not in blocked and port + 1 not in blocked and _udp_pair_free(port, local_ip):
            if port != first:
                print(f"[hyprcast WFD] Source RTP port {first} unavailable; using {port}.")
            return port
        port += 2

    raise WFDNotReady(
        f"No free UDP port pair for RTP from {first} upwards. Something is "
        f"holding the range -- check with: ss -ulpn | grep 19[0-9][0-9][0-9]"
    )


def _interface_for_ip(local_ip: str) -> Optional[str]:
    if not shutil.which("ip"):
        return None
    try:
        result = _run(["ip", "-o", "-4", "addr", "show"], timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    needle = f" {local_ip}/"
    for line in result.stdout.splitlines():
        if needle not in line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            return parts[1].split("@", 1)[0]
    return None


def _netdev_tx_bytes(interface: Optional[str]) -> Optional[int]:
    if not interface:
        return None
    try:
        with open("/proc/net/dev", "r", encoding="utf-8", errors="replace") as file:
            lines = file.read().splitlines()
    except OSError:
        return None

    prefix = interface + ":"
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        _, _, counters = stripped.partition(":")
        fields = counters.split()
        if len(fields) >= 16:
            try:
                return int(fields[8])
            except ValueError:
                return None
    return None


class NativeSender:
    """The media leg: one hyprcast-engine child per WFD session.

    This replaces fluxcast's four ffmpeg/gstreamer capture backends. Nothing
    here spawns a shell pipeline, and nothing here touches a pixel: Hyprland
    composites straight into the engine's gbm bo, VAAPI VPP converts it, and
    h264_vaapi encodes that same surface. The CPU only ever sees the Annex-B
    bitstream. Measured on this box: 58 fps at 6.6-9.5% of one core, against
    197% for the x264 path this deletes.

    Only plain scalars cross the seam, in one direction: destination and
    source ports, wire size, fps, bitrate, GOP, the output name and the audio
    source. Nothing below the seam ever calls back up.
    """

    def __init__(
        self,
        config: WFDMediaConfig,
        tv_ip: str,
        local_ip: str,
        sink_rtp_port: int,
    ) -> None:
        self.config = config
        self.tv_ip = tv_ip
        self.local_ip = local_ip
        self.sink_rtp_port = sink_rtp_port
        self.engine: Optional[Engine] = None
        self.tx_interface: Optional[str] = None
        self.tx_baseline: Optional[int] = None
        self.width = 0
        self.height = 0
        self.bitrate_kbits = 0
        self._lock = threading.Lock()
        self._last_idr = 0.0

    # ------------------------------------------------------------------ start

    def _wire_size(self) -> tuple[int, int]:
        """The negotiated wire size. Decoupled from the capture size: VPP
        scales, so retuning never renegotiates over RTSP."""
        resolution = _parse_resolution(self.config.output_resolution)
        if resolution is not None:
            return resolution
        monitor = self.config.monitor
        if monitor is not None:
            return monitor.width, monitor.height
        return 1280, 720

    def start(self) -> None:
        if self.engine is not None:
            return

        self.width, self.height = self._wire_size()
        gop = _calculate_gop(self.config)
        requested_kbits = _bitrate_to_kbits(self.config.bitrate)
        floor_kbits = _quality_floor_kbits(self.width, self.height, self.config.fps)
        self.bitrate_kbits = max(requested_kbits, floor_kbits)
        if self.bitrate_kbits > requested_kbits:
            print(
                "[hyprcast Media] Raising bitrate for desktop clarity: "
                f"{self.config.bitrate} -> {_kbits_to_bitrate_text(self.bitrate_kbits)}"
            )

        params: dict[str, object] = {
            "dst_ip": self.tv_ip,
            "dst_port": self.sink_rtp_port,
            "src_port": self.config.source_port,
            "width": self.width,
            "height": self.height,
            "fps": self.config.fps,
            "bitrate": self.bitrate_kbits * 1000,
            "gop": gop,
            "low_power": self.config.low_power,
        }
        if self.config.low_power and self.config.qp:
            params["qp"] = self.config.qp
        monitor = self.config.monitor
        if monitor is not None:
            params["output"] = monitor.name

        if not self.config.no_audio:
            audio = self.config.audio_device or _detect_audio_monitor()
            if audio:
                params["audio"] = audio
            else:
                print(
                    "[hyprcast Media] WARNING: no PipeWire monitor source found; "
                    "streaming video only (do NOT fall back to 'default' -- that "
                    "is the microphone)."
                )

        self.tx_interface = _interface_for_ip(self.local_ip)
        self.tx_baseline = _netdev_tx_bytes(self.tx_interface)

        print(
            f"[hyprcast Media] Capturing {monitor.name if monitor else 'primary output'} "
            f"-> {self.width}x{self.height}@{self.config.fps} "
            f"{'CQP low-power' if self.config.low_power else f'VBR {self.bitrate_kbits}k'}, "
            f"GOP {gop}"
        )
        print(
            f"[hyprcast Media] RTP target      : {self.tv_ip}:{self.sink_rtp_port} "
            f"from {self.local_ip}:{self.config.source_port}"
        )

        engine = Engine(
            binary=self.config.engine_path,
            log=lambda msg: print(f"[hyprcast Engine] {msg}", flush=True),
        )
        try:
            engine.start(**params)
        except EngineError as exc:
            engine.quit()
            raise WFDNotReady(f"hyprcast-engine failed to start: {exc}") from exc
        self.engine = engine
        _publish(self)
        _append_latency_log(
            self.config.latency_log_path,
            "engine_ready",
            pid=engine.pid,
            width=self.width,
            height=self.height,
            fps=self.config.fps,
            bitrate_kbits=self.bitrate_kbits,
        )

    # --------------------------------------------------------------- lifetime

    def is_alive(self) -> bool:
        engine = self.engine
        return engine is not None and engine.is_alive()

    def stop(self) -> None:
        _publish(None)
        with self._lock:
            engine, self.engine = self.engine, None
        if engine is None:
            return
        code = engine.quit()
        _append_latency_log(self.config.latency_log_path, "engine_stopped", code=code)

    # ------------------------------------------------------------ runtime knobs

    def request_idr(self) -> bool:
        """Honour the sink's wfd_idr_request. True if the engine was told.

        With gop == fps and scene-cut detection off, waiting for the next
        natural keyframe costs up to a full second of visible corruption. The
        sink only asks because it is showing garbage right now.
        """
        engine = self.engine
        if engine is None:
            return False
        now = time.monotonic()
        if now - self._last_idr < 0.5:      # one per 500 ms, per BUILD_PLAN M4
            return False
        self._last_idr = now
        try:
            engine.idr()
        except EngineError as exc:
            print(f"[hyprcast Media] IDR request not delivered: {exc}")
            return False
        return True

    def retune(self, **kw: object) -> None:
        """fps / bitrate / qp without restarting anything."""
        engine = self.engine
        if engine is None:
            raise WFDNotReady("no media session to retune")
        engine.retune(**kw)
        if "fps" in kw:
            self.config = replace(self.config, fps=int(kw["fps"]))  # type: ignore[arg-type]

    def volume(self, gain: Optional[float] = None, muted: Optional[bool] = None) -> None:
        engine = self.engine
        if engine is None:
            raise WFDNotReady("no media session")
        engine.volume(gain=gain, muted=muted)

    def set_output(self, name: str) -> None:
        """Move the capture to another output; the wire size stays frozen, so
        the sink never learns anything happened."""
        engine = self.engine
        if engine is None:
            raise WFDNotReady("no media session")
        engine.set_output(name)

    # ------------------------------------------------------------------ health

    def tx_summary(self) -> str:
        current = _netdev_tx_bytes(self.tx_interface)
        if self.tx_baseline is None or current is None:
            return "tx=unknown"
        delta = max(0, current - self.tx_baseline)
        return f"tx+{delta // 1024} KiB on {self.tx_interface}"

    def health_summary(self) -> str:
        engine = self.engine
        if engine is None:
            return "no engine"
        if not engine.is_alive():
            return f"pid={engine.pid}:exited={engine.returncode}"
        stats = engine.stats
        if not stats:
            return f"pid={engine.pid}:running"
        return (
            f"pid={engine.pid}:running fps={stats.get('fps')} "
            f"kbps={stats.get('kbps')} cpu={stats.get('cpu')} "
            f"drops={stats.get('drops')}"
        )


_WIRE_DUMP_BROKEN = False


def _wire_dump(direction: str, text: str) -> None:
    """
    Append a verbatim copy of one RTSP message to $HYPRCAST_RTSP_DUMP.

    The sink's M3 GET_PARAMETER response body is the authoritative definition of
    every format table in this fork -- AOSP defaults are not a substitute. Capture
    it byte-exactly once, then develop against tools/mock-sink.py instead of the TV.
    """
    raw = os.environ.get("HYPRCAST_RTSP_DUMP")
    if not raw:
        return
    # fish's `export VAR=~/x` does not expand the tilde; expand it ourselves
    # rather than silently failing to write the one artifact this exists for.
    path = os.path.expanduser(os.path.expandvars(raw))
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"===== {direction} {time.time():.6f} =====\n")
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
    except OSError as exc:
        global _WIRE_DUMP_BROKEN
        if not _WIRE_DUMP_BROKEN:
            _WIRE_DUMP_BROKEN = True
            print(f"[hyprcast] RTSP wire dump DISABLED: cannot write {path!r}: {exc}")


def _read_rtsp_message(rfile) -> Optional[RTSPMessage]:
    lines = []
    while True:
        raw = rfile.readline(8192)
        if not raw:
            return None
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            break
        lines.append(line)

    if not lines:
        return None

    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()

    content_length = 0
    try:
        content_length = int(headers.get("content-length", "0"))
    except ValueError:
        content_length = 0

    body = ""
    if content_length > 0:
        body = rfile.read(content_length).decode("utf-8", errors="replace")

    return RTSPMessage(
        start=lines[0],
        headers=headers,
        raw_headers=lines[1:],
        body=body,
    )


def _parse_parameters(body: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for line in body.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            params[key.strip().lower()] = value.strip()
    return params


def _parse_rtp_ports(value: str) -> Optional[tuple[int, int]]:
    match = re.search(
        r"RTP/AVP/(?:UDP|TCP);unicast\s+(\d+)\s+(\d+)\s+mode=play",
        value,
        re.IGNORECASE,
    )
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def _parse_transport_client_ports(value: str) -> Optional[tuple[int, int]]:
    match = re.search(r"client_port=(\d+)(?:-(\d+))?", value, re.IGNORECASE)
    if match:
        return int(match.group(1)), int(match.group(2) or "0")
    return None


class _WFDRTSPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        self.local_ip = self.request.getsockname()[0]
        self.next_cseq = 1
        self.pending: dict[str, str] = {}
        self.session_id = str(random.randint(1_000_000, 9_999_999))
        self._write_lock = threading.Lock()
        self._keepalive_active = True
        self.sink_rtp_port: Optional[int] = None
        self.sink_rtcp_port: int = 0
        self.source_rtp_port = self.media_config.source_port
        self.sink_video_format: Optional[WFDVideoFormat] = None
        self.negotiated_no_audio = False
        self.m3_sent = False
        self.media: Optional[NativeSender] = None
        self.connected_at = time.monotonic()
        self.play_accepted_at: Optional[float] = None
        self.setup_ms: Optional[float] = None
        self.first_tx_reported = False

        if hasattr(self.server, "parent_server"):
            self.server.parent_server.has_connected_client = True  # type: ignore[attr-defined]

        print(f"[hyprcast WFD RTSP] TV connected from {peer}; local={self.local_ip}")
        _append_latency_log(
            self.media_config.latency_log_path,
            "rtsp_connected",
            peer=peer,
            local_ip=self.local_ip,
        )
        try:
            self._send_m1_options()
            while True:
                msg = _read_rtsp_message(self.rfile)
                if msg is None:
                    print(f"[hyprcast WFD RTSP] TV disconnected from {peer}")
                    return
                _wire_dump(
                    "RX",
                    "\r\n".join([msg.start, *msg.raw_headers, "", msg.body]),
                )
                self._log_message(msg)
                if msg.is_response:
                    self._handle_response(msg)
                else:
                    self._handle_request(msg)
        except WFDProbeDone:
            target = getattr(self.server, "parent_server", self.server)
            target.probe_done = True
        except WFDNotReady as exc:
            print(f"[hyprcast WFD RTSP] ERROR: {exc}")
        except OSError as exc:
            print(f"[hyprcast WFD RTSP] Socket closed: {exc}")
        finally:
            self._keepalive_active = False
            self._stop_media()

    @property
    def media_config(self) -> WFDMediaConfig:
        return self.server.media_config  # type: ignore[attr-defined]

    @property
    def rtsp_port(self) -> int:
        return self.server.server_address[1]  # type: ignore[attr-defined]

    def _rtsp_control_uri(self) -> str:
        return "rtsp://localhost/wfd1.0"

    def _rtsp_presentation_uri(self) -> str:
        return f"rtsp://{self.local_ip}:{self.rtsp_port}/wfd1.0"

    def _cea_mode(self) -> WFDCEAMode:
        return _choose_cea_mode(self.media_config, self.sink_video_format)

    def _video_format(self) -> str:
        return _selected_video_format(self.media_config, self.sink_video_format)

    def _audio_codecs(self) -> str:
        if self.media_config.no_audio or self.negotiated_no_audio:
            return "none"
        if "microsoft" in self.media_config.peer_name.lower():
            return WFD_AUDIO_LPCM_48K
        return WFD_AUDIO_AAC

    def _send_bytes(self, text: str) -> None:
        with self._write_lock:
            _wire_dump("TX", text)
            self.wfile.write(text.encode("utf-8"))
            self.wfile.flush()

    def _send_request(
        self,
        name: str,
        method: str,
        uri: str,
        headers: Optional[dict[str, str]] = None,
        body: str = "",
    ) -> None:
        cseq = str(self.next_cseq)
        self.next_cseq += 1
        self.pending[cseq] = name

        output = [
            f"{method} {uri} RTSP/1.0",
            f"CSeq: {cseq}",
        ]
        if headers and "Session" in headers:
            output.append(f"Session: {headers['Session']}")
        for key, value in (headers or {}).items():
            if key == "Session":
                continue
            output.append(f"{key}: {value}")
        if body:
            output.append("Content-Type: text/parameters")
            output.append(f"Content-Length: {len(body.encode('utf-8'))}")
        output.append("")
        output.append(body)
        self._send_bytes("\r\n".join(output))
        print(f"[hyprcast WFD RTSP] -> {name}: {method} (CSeq {cseq})")
        if body:
            for line in body.splitlines():
                if line.startswith("wfd_"):
                    print(f"[hyprcast WFD RTSP]   {line}")

    def _send_response(
        self,
        msg: RTSPMessage,
        status: str = "200 OK",
        headers: Optional[dict[str, str]] = None,
        body: str = "",
    ) -> None:
        output = [
            f"RTSP/1.0 {status}",
            f"CSeq: {msg.cseq}",
        ]
        if headers and "Session" in headers:
            output.append(f"Session: {headers['Session']}")
        # Wire value: this exact string is in the confirmed-working
        # session capture (reference/sink/wire-720p60-full-session.txt).
        output.append("Server: FluxCast-WFD/0.1")
        for key, value in (headers or {}).items():
            if key == "Session":
                continue
            output.append(f"{key}: {value}")
        if body:
            output.append("Content-Type: text/parameters")
        output.append(f"Content-Length: {len(body.encode('utf-8'))}")
        output.append("")
        output.append(body)
        self._send_bytes("\r\n".join(output))
        print(f"[hyprcast WFD RTSP] -> response {status} for {msg.method or msg.status}")

    def _send_m1_options(self) -> None:
        self._send_request(
            "M1_OPTIONS",
            "OPTIONS",
            "*",
            headers={"Require": "org.wfa.wfd1.0"},
        )

    def _send_m3_get_parameters(self) -> None:
        if self.m3_sent:
            return
        self.m3_sent = True
        body = (
            "wfd_content_protection\r\n"
            "wfd_video_formats\r\n"
            "wfd_audio_codecs\r\n"
            "wfd_client_rtp_ports\r\n"
        )
        self._send_request(
            "M3_GET_PARAMETER",
            "GET_PARAMETER",
            self._rtsp_control_uri(),
            body=body,
        )

    def _send_m4_set_parameters(self) -> None:
        if not self.sink_rtp_port:
            raise WFDNotReady("TV did not provide a valid RTP port in M3.")
        sink_rtcp_port = self.sink_rtcp_port if self.sink_rtcp_port > 0 else 0
        body = (
            "wfd_content_protection: none\r\n"
            f"wfd_video_formats: {self._video_format()}\r\n"
            f"wfd_audio_codecs: {self._audio_codecs()}\r\n"
            f"wfd_presentation_URL: {self._rtsp_presentation_uri()}/streamid=0 none\r\n"
            "wfd_client_rtp_ports: RTP/AVP/UDP;unicast "
            f"{self.sink_rtp_port} {sink_rtcp_port} mode=play\r\n"
        )
        self._send_request(
            "M4_SET_PARAMETER",
            "SET_PARAMETER",
            self._rtsp_control_uri(),
            body=body,
        )

    def _send_m5_trigger_setup(self) -> None:
        body = "wfd_trigger_method: SETUP\r\n"
        self._send_request(
            "M5_TRIGGER_SETUP",
            "SET_PARAMETER",
            self._rtsp_control_uri(),
            body=body,
        )

    def _handle_response(self, msg: RTSPMessage) -> None:
        name = self.pending.pop(msg.cseq, "UNKNOWN")
        if not msg.status.startswith("200"):
            if name == "M16_KEEPALIVE":
                # LG (or any TV) rejected our keepalive, STOP RESCHEDULING
                # but keep the stream alive
                print(
                    f"[hyprcast WFD RTSP] M16 keepalive rejected: {msg.status} "
                    "— disabling keepalive, stream continues."
                )
                self._keepalive_active = False
                return
            raise WFDNotReady(f"RTSP {name} failed: {msg.start}")

        print(f"[hyprcast WFD RTSP] <- response for {name}: {msg.status}")
        if name == "M3_GET_PARAMETER":
            params = _parse_parameters(msg.body)
            ports = _parse_rtp_ports(params.get("wfd_client_rtp_ports", ""))
            if not ports or ports[0] <= 0:
                raise WFDNotReady(
                    "TV M3 response did not include a usable wfd_client_rtp_ports value."
                )
            self.sink_rtp_port, self.sink_rtcp_port = ports
            self.source_rtp_port = _safe_source_port(
                self.media_config.source_port,
                self.sink_rtp_port,
                self.sink_rtcp_port,
                self.local_ip,
            )
            self.sink_video_format = _parse_sink_video_format(
                params.get("wfd_video_formats", "")
            )
            audio = params.get("wfd_audio_codecs", "")
            _is_microsoft = "microsoft" in self.media_config.peer_name.lower()
            if (
                audio
                and not self.media_config.no_audio
                and "AAC" not in audio.upper()
                and not _is_microsoft
            ):
                self.negotiated_no_audio = True
                print(
                    "[hyprcast WFD RTSP] TV did not advertise AAC; "
                    "falling back to video-only WFD."
                )
            if _is_microsoft and audio:
                print(f"[hyprcast WFD RTSP] Microsoft adapter audio caps: {audio}")
            mode = self._cea_mode()
            print(
                f"[hyprcast WFD RTSP] TV RTP port: {self.sink_rtp_port}; "
                f"source port: {self.source_rtp_port}; audio={audio or 'unknown'}"
            )
            print(f"[hyprcast WFD RTSP] Negotiated media mode: {mode.name}")
            print(f"[hyprcast WFD RTSP] Selected video format: {self._video_format()}")
            if self.media_config.probe_only:
                print()
                print("=== what this sink is offering RIGHT NOW ===")
                print(f"  wfd_video_formats: {params.get('wfd_video_formats', '(none)')}")
                print(f"  wfd_audio_codecs : {params.get('wfd_audio_codecs', '(none)')}")
                fmt = self.sink_video_format
                if fmt is not None:
                    modes = [m.name for bit, m in sorted(WFD_CEA_MODES.items())
                             if fmt.cea_mask & bit]
                    print(f"  CEA mask 0x{fmt.cea_mask:08x} -> "
                          f"{', '.join(modes) if modes else 'nothing usable'}")
                    sixty = any(m.endswith("p60") for m in modes)
                    print()
                    print(f"  60 fps available : {'YES' if sixty else 'NO'}")
                    print(f"  would negotiate  : {mode.name}")
                    if not sixty:
                        print("  -> The TV is advertising a reduced set. Fully quit and")
                        print("     re-open the Miracast app on the TV, then probe again.")
                raise WFDProbeDone()
            self._send_m4_set_parameters()
        elif name == "M4_SET_PARAMETER":
            self._send_m5_trigger_setup()

    def _handle_request(self, msg: RTSPMessage) -> None:
        method = msg.method
        if method == "OPTIONS":
            self._send_response(
                msg,
                headers={
                    "Public": (
                        "org.wfa.wfd1.0, SETUP, TEARDOWN, PLAY, PAUSE, "
                        "GET_PARAMETER, SET_PARAMETER"
                    )
                },
            )
            self._send_m3_get_parameters()
            return

        if method == "GET_PARAMETER":
            requested = msg.body.lower()
            lines = []
            if "wfd_video_formats" in requested:
                lines.append(f"wfd_video_formats: {self._video_format()}\r\n")
            if "wfd_audio_codecs" in requested:
                lines.append(f"wfd_audio_codecs: {self._audio_codecs()}\r\n")
            if "wfd_content_protection" in requested:
                lines.append("wfd_content_protection: none\r\n")
            body = "".join(lines)
            self._send_response(msg, headers=self._session_header(), body=body)
            return

        if method == "SET_PARAMETER":
            if "wfd_idr_request" in msg.body:
                # The sink only asks because it has visible corruption RIGHT NOW.
                # gop == fps with sc_threshold 0 means the next natural keyframe
                # is up to a full second away, so forward it to the engine, which
                # sets pict_type = AV_PICTURE_TYPE_I on the very next frame.
                if self.media is not None:
                    forced = self.media.request_idr()
                    print(
                        "[hyprcast WFD RTSP] Sink requested IDR; "
                        + ("forcing one now." if forced
                           else "rate-limited, one is already on its way.")
                    )
                    _append_latency_log(
                        self.media_config.latency_log_path,
                        "idr_requested", forced=forced,
                    )
                else:
                    print("[hyprcast WFD RTSP] Sink requested IDR before media started.")
            self._send_response(msg, headers=self._session_header())
            return

        if method == "SETUP":
            ports = _parse_transport_client_ports(msg.headers.get("transport", ""))
            if ports:
                self.sink_rtp_port, self.sink_rtcp_port = ports
            if not self.sink_rtp_port:
                self.sink_rtp_port = 19000
                self.sink_rtcp_port = 0

            self.source_rtp_port = _safe_source_port(
                self.media_config.source_port,
                self.sink_rtp_port,
                self.sink_rtcp_port,
            )
            source_port = self.source_rtp_port
            if self.sink_rtcp_port:
                transport = (
                    "RTP/AVP/UDP;unicast;"
                    f"client_port={self.sink_rtp_port}-{self.sink_rtcp_port};"
                    f"server_port={source_port}-{source_port + 1}"
                )
            else:
                transport = (
                    "RTP/AVP/UDP;unicast;"
                    f"client_port={self.sink_rtp_port};"
                    f"server_port={source_port}"
                )
            self._send_response(
                msg,
                headers={
                    "Transport": transport,
                    "Session": f"{self.session_id};timeout=30",
                },
            )
            print(f"[hyprcast WFD RTSP] SETUP complete; RTP sink port={self.sink_rtp_port}")
            return

        if method == "PLAY":
            self._send_response(
                msg,
                headers={
                    **self._session_header(),
                    "Range": "npt=now-",
                },
            )
            # Schedule the M16 keepalive NOW, before _start_media() waits on
            # the engine's ready event. The first keepalive must be in flight
            # regardless of how long VAAPI init takes; sinks reset the TCP
            # connection ~40-45 s after PLAY without one.
            # Microsoft adapter sends TEARDOWN in response to M16 GET_PARAMETER.
            if "microsoft" not in self.media_config.peer_name.lower():
                self._schedule_rtsp_keepalive(20.0)
            else:
                print("[hyprcast WFD RTSP] Microsoft adapter detected — M16 keepalive disabled.")
            self._start_media()
            return

        if method == "PAUSE":
            self._send_response(msg, headers=self._session_header())
            self._stop_media()
            return

        if method == "TEARDOWN":
            self._send_response(
                msg,
                headers={
                    **self._session_header(),
                    "Connection": "close",
                },
            )
            self._stop_media()
            return

        self._send_response(msg, status="405 Method Not Allowed")

    def _session_header(self) -> dict[str, str]:
        return {"Session": f"{self.session_id};timeout=30"}

    def _start_media(self) -> None:
        if not self.sink_rtp_port:
            raise WFDNotReady("Cannot start media before the TV RTP port is known.")
        if self.media is None:
            mode = self._cea_mode()
            effective_config = replace(
                self.media_config,
                source_port=self.source_rtp_port,
                output_resolution=mode.resolution,
                fps=mode.fps,
                no_audio=self.media_config.no_audio or self.negotiated_no_audio,
            )
            print(
                f"[hyprcast WFD RTSP] Starting media as {mode.name}; "
                f"RTP source port {self.source_rtp_port}"
            )
            _append_latency_log(
                self.media_config.latency_log_path,
                "media_starting",
                mode=mode.name,
                tv_ip=self.client_address[0],
                sink_rtp_port=self.sink_rtp_port,
                source_rtp_port=self.source_rtp_port,
            )
            self.media = NativeSender(
                effective_config,
                tv_ip=self.client_address[0],
                local_ip=self.local_ip,
                sink_rtp_port=self.sink_rtp_port,
            )
            if hasattr(self.server, "parent_server"):
                self.server.parent_server._register_media(self.media)  # type: ignore[attr-defined]
            self.media.start()
            print("[hyprcast WFD RTSP] PLAY accepted; media stream started.")
            self.play_accepted_at = time.monotonic()
            self.setup_ms = round((self.play_accepted_at - self.connected_at) * 1000.0, 1)
            _append_latency_log(
                self.media_config.latency_log_path,
                "play_accepted",
                setup_ms=self.setup_ms,
            )
            self._schedule_probe(0.7)

    def _schedule_probe(self, delay: float) -> None:
        probe = threading.Timer(delay, self._probe_tx)
        probe.daemon = True
        probe.start()

    def _schedule_rtsp_keepalive(self, delay: float = 25.0) -> None:
        """Schedule the next RTSP M16 GET_PARAMETER keepalive."""
        t = threading.Timer(delay, self._send_rtsp_keepalive)
        t.daemon = True
        t.start()

    def _send_rtsp_keepalive(self) -> None:
        """Send RTSP GET_PARAMETER (M16) on the existing TCP connection."""
        if not self._keepalive_active:
            return
        media = self.media
        # Only stop the chain once the engine has actually exited. While media
        # is still None the session is mid-setup, so keep the keepalives going.
        if media is not None and not media.is_alive():
            return
        try:
            self._send_request(
                "M16_KEEPALIVE",
                "GET_PARAMETER",
                self._rtsp_presentation_uri(),
                headers={"Session": f"{self.session_id};timeout=30"},
            )
            print("[hyprcast WFD RTSP] M16 keepalive sent")
            self._schedule_rtsp_keepalive(25.0)
        except OSError:
            pass  # Socket dead -> DONT RESCHEDULE

    def _probe_tx(self) -> None:
        media = self.media
        if media is None:
            return

        states = [media.health_summary()]

        if media.is_alive():
            current = _netdev_tx_bytes(media.tx_interface)
            delta = None
            if media.tx_baseline is not None and current is not None:
                delta = max(0, current - media.tx_baseline)
            if (
                not self.first_tx_reported
                and delta is not None
                and delta > 0
                and self.play_accepted_at is not None
            ):
                self.first_tx_reported = True
                sender_startup_ms = round((time.monotonic() - self.play_accepted_at) * 1000.0, 1)
                print(
                    f"[hyprcast Media] Latency probe: first RTP bytes after PLAY in "
                    f"{sender_startup_ms} ms"
                )
                sender_path_latency_ms = None
                if self.setup_ms is not None:
                    sender_path_latency_ms = round(self.setup_ms + sender_startup_ms, 1)
                    print(
                        "[hyprcast Media] Latency probe: sender-path latency "
                        f"(RTSP connect -> first RTP) {sender_path_latency_ms} ms"
                    )
                _append_latency_log(
                    self.media_config.latency_log_path,
                    "latency_probe",
                    sender_startup_ms=sender_startup_ms,
                    setup_ms=self.setup_ms,
                    sender_path_latency_ms=sender_path_latency_ms,
                )
            print(
                f"[hyprcast Media] Sender health: "
                f"{', '.join(states)}; {media.tx_summary()}"
            )
            _append_latency_log(
                self.media_config.latency_log_path,
                "sender_health",
                processes=states,
                tx_summary=media.tx_summary(),
            )
            self._schedule_probe(5.0)
            return

        detail = ", ".join(states) if states else "no engine"
        print(
            f"[hyprcast Media] WARNING: RTP sender is not healthy "
            f"({detail}; {media.tx_summary()})"
        )
        _append_latency_log(
            self.media_config.latency_log_path, "sender_died", detail=detail,
        )
        # Reap it and drop the session's reference rather than leaving a dead
        # engine registered and the sink staring at a frozen frame forever.
        self._stop_media()

    def _stop_media(self) -> None:
        if self.media is not None:
            print("[hyprcast Media] Stopping RTP stream...")
            if hasattr(self.server, "parent_server"):
                self.server.parent_server._unregister_media(self.media)  # type: ignore[attr-defined]
            self.media.stop()
            self.media = None

    def _log_message(self, msg: RTSPMessage) -> None:
        arrow = "<- response" if msg.is_response else "<- request"
        print(f"[hyprcast WFD RTSP] {arrow}: {msg.start}")
        for line in msg.raw_headers:
            lower = line.lower()
            if lower.startswith(("cseq:", "transport:", "session:", "content-type:", "content-length:")):
                print(f"[hyprcast WFD RTSP]   {line}")
        if msg.body:
            for line in msg.body.splitlines():
                if line.startswith("wfd_"):
                    print(f"[hyprcast WFD RTSP]   {line}")


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    # Set by WFDRTSPServer.start(); None means "accept anybody", which is only
    # ever right for an explicitly bound loopback harness.
    allowed_network: Optional[ipaddress.IPv4Network] = None

    def verify_request(self, request, client_address) -> bool:
        """Drop anyone who is not on the Wi-Fi Direct link.

        Binding to the p2p interface is not enough on its own: any host that
        can route to that address would still be served a full desktop mirror,
        and this daemon answers M1-M5 to whoever asks first.
        """
        network = self.allowed_network
        if network is None:
            return True
        try:
            peer = ipaddress.ip_address(client_address[0])
        except ValueError:
            return False
        if peer in network:
            return True
        print(
            f"[hyprcast WFD RTSP] Refused connection from {client_address[0]}: "
            f"not on the P2P link ({network})"
        )
        return False


class WFDRTSPServer:
    def __init__(
        self,
        media_config: WFDMediaConfig,
        host: str = "0.0.0.0",
        port: int = WFD_RTSP_PORT,
        allowed_network: Optional[ipaddress.IPv4Network] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.media_config = media_config
        self.allowed_network = allowed_network
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.has_connected_client = False
        self.probe_done = False
        self._media_lock = threading.Lock()
        self._active_media: list[NativeSender] = []

    def _register_media(self, media: NativeSender) -> None:
        with self._media_lock:
            self._active_media.append(media)

    def _unregister_media(self, media: NativeSender) -> None:
        with self._media_lock:
            try:
                self._active_media.remove(media)
            except ValueError:
                pass

    def stop_all_media(self) -> None:
        with self._media_lock:
            pipelines = list(self._active_media)
        for pipeline in pipelines:
            pipeline.stop()

    def start(self) -> None:
        try:
            self._server = _ThreadingTCPServer((self.host, self.port), _WFDRTSPHandler)
        except OSError as exc:
            raise WFDNotReady(
                f"could not bind RTSP on {self.host}:{self.port}: {exc}"
            ) from exc
        self._server.media_config = self.media_config  # type: ignore[attr-defined]
        self._server.parent_server = self  # type: ignore[attr-defined]
        self._server.allowed_network = self.allowed_network  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        scope = f"peers on {self.allowed_network}" if self.allowed_network else "any peer"
        print(f"[hyprcast WFD RTSP] Server listening on {self.host}:{self.port} ({scope})")

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def _run(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _firewalld_active() -> bool:
    if not shutil.which("firewall-cmd"):
        return False
    try:
        result = _run(["systemctl", "is-active", "firewalld"], timeout=3.0)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "active"


_FIREWALL_AUTH_TIMEOUT = 60.0
_FIREWALL_QUERY_TIMEOUT = 3.0

_WFD_FIREWALL_ZONE = "nm-shared"

def _print_firewall_manual_hint(port: int, reason: str) -> None:
    print(
        f"[hyprcast WFD] Could not open firewalld port {port}/tcp automatically "
        f"({reason}).\n"
        "  Open it once yourself, then re-run FluxCast:\n"
        f"    sudo firewall-cmd --permanent --zone={_WFD_FIREWALL_ZONE} --add-port={port}/tcp "
        "&& sudo firewall-cmd --reload\n"
        "  or pass --wfd-no-firewall if you manage the firewall yourself."
    )


def _open_wfd_firewall_port(port: int) -> bool:
    """
    Runtime-only (no ``--permanent``): cleared on reload/reboot, and removed on
    exit. Returns True only if WE opened it, so the caller knows to undo it; a
    port the user already had open is left untouched.
    """
    if not _firewalld_active():
        return False

    try:
        query = _run(
            [
                "firewall-cmd",
                f"--zone={_WFD_FIREWALL_ZONE}",
                f"--query-port={port}/tcp",
            ],
            timeout=_FIREWALL_QUERY_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _print_firewall_manual_hint(port, f"could not check existing rule: {exc}")
        return False

    # A real query result is the literal ``yes``/``no`` output. Authorization
    # failures can use the same non-zero status as a closed port, so do not
    # infer the result from the return code alone.
    query_status = query.stdout.strip().lower()
    query_output = (query.stdout + query.stderr).strip()
    if query_status == "yes":
        return False  # The user already had it open; leave it untouched.
    if query_status != "no":
        _print_firewall_manual_hint(
            port,
            query_output or "firewall-cmd could not verify the existing rule",
        )
        return False

    print(f"[hyprcast WFD] Opening firewalld port {port}/tcp ({_WFD_FIREWALL_ZONE} zone) "
          "for this session; approve the authorization prompt if one appears.")
    try:
        result = _run(["firewall-cmd", f"--zone={_WFD_FIREWALL_ZONE}",
                       f"--add-port={port}/tcp"],
                      timeout=_FIREWALL_AUTH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _print_firewall_manual_hint(port, f"authorization did not complete: {exc}")
        return False
    output = result.stdout + result.stderr
    if result.returncode != 0:
        _print_firewall_manual_hint(port, output.strip() or "firewall-cmd refused the request")
        return False
    if "ALREADY_ENABLED" in output.upper():
        return False  # user already had it open; leave it exactly as-is
    print(f"[hyprcast WFD] Opened firewalld port {port}/tcp for this session "
          "(removed on exit).")
    return True


def _close_wfd_firewall_port(port: int) -> None:
    """Undo _open_wfd_firewall_port (best-effort)."""
    try:
        _run(["firewall-cmd", f"--zone={_WFD_FIREWALL_ZONE}",
              f"--remove-port={port}/tcp"], timeout=15.0)
        print(f"[hyprcast WFD] Closed firewalld port {port}/tcp.")
    except (OSError, subprocess.TimeoutExpired):
        pass


def _object_paths(text: str) -> list[str]:
    return re.findall(r"'(/[^']+)'", text)


def _variant_string(text: str) -> str:
    match = re.search(r"<\'(.*)\' >", text)
    if match:
        return match.group(1)
    match = re.search(r"<\'(.*)\'", text)
    if match:
        return match.group(1)
    match = re.search(r"<\"(.*)\"", text)
    if match:
        return match.group(1)
    return ""


def _variant_uint(text: str) -> Optional[int]:
    matches = re.findall(r"(?:uint32\s+)?(\d+)", text)
    if not matches:
        return None
    return int(matches[-1])


def _variant_uint_tuple(text: str) -> tuple[Optional[int], Optional[int]]:
    matches = re.findall(r"(?:uint32\s+)?(\d+)", text)
    if len(matches) < 2:
        return None, None
    return int(matches[-2]), int(matches[-1])


NM_ACTIVE_STATE_NAMES = {
    0: "unknown",
    1: "activating",
    2: "activated",
    3: "deactivating",
    4: "deactivated",
}

NM_DEVICE_STATE_NAMES = {
    0: "unknown",
    10: "unmanaged",
    20: "unavailable",
    30: "disconnected",
    40: "prepare",
    50: "config",
    60: "need-auth",
    70: "ip-config",
    80: "ip-check",
    90: "secondaries",
    100: "activated",
    110: "deactivating",
    120: "failed",
}

NM_DEVICE_REASON_NAMES = {
    0: "none",
    1: "unknown",
    2: "now-managed",
    3: "now-unmanaged",
    4: "config-failed",
    5: "ip-config-unavailable",
    6: "ip-config-expired",
    7: "no-secrets",
    8: "supplicant-disconnect",
    9: "supplicant-config-failed",
    10: "supplicant-failed",
    11: "supplicant-timeout",
    15: "dhcp-start-failed",
    16: "dhcp-error",
    17: "dhcp-failed",
    18: "shared-start-failed",
    19: "shared-failed",
    38: "external-disconnect",
    39: "assume-failed",
    40: "supplicant-available",
    41: "modem-not-found",
    42: "bt-failed",
    53: "peer-not-found",
    54: "device-handler-failed",
}


def _gdbus_call(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    if not shutil.which("gdbus"):
        raise WFDNotReady("gdbus is required for NetworkManager Wi-Fi P2P discovery.")
    return _run(["gdbus", "call", "--system", *args], timeout=timeout)


def _nm_get_property(path: str, interface: str, prop: str) -> str:
    result = _gdbus_call([
        "--dest", NM_DEST,
        "--object-path", path,
        "--method", "org.freedesktop.DBus.Properties.Get",
        interface,
        prop,
    ])
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _nm_get_string(path: str, interface: str, prop: str) -> str:
    return _variant_string(_nm_get_property(path, interface, prop))


def _nm_device_summary(path: str) -> str:
    iface = _nm_get_string(path, "org.freedesktop.NetworkManager.Device", "Interface")
    ip_iface = _nm_get_string(path, "org.freedesktop.NetworkManager.Device", "IpInterface")
    state = _variant_uint(_nm_get_property(path, "org.freedesktop.NetworkManager.Device", "State"))
    _, reason = _variant_uint_tuple(
        _nm_get_property(path, "org.freedesktop.NetworkManager.Device", "StateReason")
    )
    state_text = NM_DEVICE_STATE_NAMES.get(state or -1, str(state))
    reason_text = NM_DEVICE_REASON_NAMES.get(reason or -1, str(reason))
    if ip_iface and ip_iface != iface:
        return f"{iface}/{ip_iface}:{state_text}:{reason_text}"
    return f"{iface}:{state_text}:{reason_text}"


def _nm_active_devices(active_path: str) -> list[str]:
    raw = _nm_get_property(
        active_path,
        "org.freedesktop.NetworkManager.Connection.Active",
        "Devices",
    )
    return _object_paths(raw)


def _wait_for_nm_activation(active_path: str, timeout: float = 35.0) -> None:
    print("[hyprcast WFD] Waiting for NetworkManager P2P activation...")
    deadline = time.monotonic() + timeout
    last_status = ""

    while time.monotonic() < deadline:
        state_raw = _nm_get_property(
            active_path,
            "org.freedesktop.NetworkManager.Connection.Active",
            "State",
        )
        state = _variant_uint(state_raw)
        state_text = NM_ACTIVE_STATE_NAMES.get(state or -1, str(state))
        devices = _nm_active_devices(active_path)
        device_status = ", ".join(_nm_device_summary(path) for path in devices) or "no-device"
        status = f"{state_text}; {device_status}"

        if status != last_status:
            print(f"[hyprcast WFD] NM active connection: {status}")
            last_status = status

        if state == 2:
            print("[hyprcast WFD] P2P link is activated; waiting for RTSP session...")
            return
        if state == 4:
            raise WFDNotReady(
                "NetworkManager deactivated the Wi-Fi Direct connection before RTSP. "
                f"Last status: {status}"
            )

        time.sleep(0.5)

    raise WFDNotReady(
        "Timed out waiting for NetworkManager Wi-Fi Direct activation. "
        f"Last status: {last_status or 'unknown'}"
    )


def _nm_p2p_device_path(interface: Optional[str] = None) -> Optional[str]:
    result = _gdbus_call([
        "--dest", NM_DEST,
        "--object-path", NM_PATH,
        "--method", "org.freedesktop.DBus.Properties.Get",
        "org.freedesktop.NetworkManager",
        "Devices",
    ])
    if result.returncode != 0:
        raise WFDNotReady((result.stderr or result.stdout).strip())

    requested = interface or ""
    for path in _object_paths(result.stdout):
        iface = _nm_get_string(path, "org.freedesktop.NetworkManager.Device", "Interface")
        if not iface or "p2p" not in iface.lower():
            continue
        if requested and requested not in iface:
            continue
        return path
    return None


def _nm_start_find(path: str, timeout: int) -> None:
    result = _gdbus_call([
        "--dest", NM_DEST,
        "--object-path", path,
        "--method", "org.freedesktop.NetworkManager.Device.WifiP2P.StartFind",
        f"{{'timeout': <int32 {timeout}>}}",
    ])
    if result.returncode != 0:
        raise WFDNotReady((result.stderr or result.stdout).strip())


def _nm_stop_find(path: str) -> None:
    try:
        _gdbus_call([
            "--dest", NM_DEST,
            "--object-path", path,
            "--method", "org.freedesktop.NetworkManager.Device.WifiP2P.StopFind",
        ], timeout=3.0)
    except Exception:
        pass


def _nm_collect_peers(path: str) -> list[WFDPeer]:
    """Snapshot NetworkManager's current P2P peer list."""
    peers_raw = _nm_get_property(path, "org.freedesktop.NetworkManager.Device.WifiP2P", "Peers")
    peers = []
    for peer_path in _object_paths(peers_raw):
        name = _nm_get_string(peer_path, "org.freedesktop.NetworkManager.WifiP2PPeer", "Name")
        address = _nm_get_string(peer_path, "org.freedesktop.NetworkManager.WifiP2PPeer", "HwAddress")
        model = _nm_get_string(peer_path, "org.freedesktop.NetworkManager.WifiP2PPeer", "Model")
        manufacturer = _nm_get_string(peer_path, "org.freedesktop.NetworkManager.WifiP2PPeer", "Manufacturer")
        wfd_ies_raw = _nm_get_property(peer_path, "org.freedesktop.NetworkManager.WifiP2PPeer", "WfdIEs")
        
        # wfd_ies_raw is the raw gdbus stdout string (e.g. "<@ay [byte 0x00, ...]>").
        # Parse it into a byte list, then extract the RTSP port from subelement 0.
        wfd_ies_list = _parse_gdbus_byte_array(wfd_ies_raw)
        sink_rtsp_port = _parse_wfd_ies_rtsp_port(wfd_ies_list)

        details = "; ".join(
            part for part in [
                f"model={model}" if model else "",
                f"manufacturer={manufacturer}" if manufacturer else "",
                f"wfd_ies={wfd_ies_raw}" if wfd_ies_raw else "",
                f"sink_rtsp_port={sink_rtsp_port}",
            ]
            if part
        )
        peers.append(WFDPeer(
            address=address or peer_path.rsplit("/", 1)[-1],
            name=name,
            details=details,
            path=peer_path,
            source="NetworkManager",
            rtsp_port=sink_rtsp_port,
        ))
    return peers


def _has_wfd_sink(peers: list[WFDPeer]) -> bool:
    """True once a peer is advertising Wi-Fi Display capability."""
    return any("wfd_ies=" in (p.details or "") or p.rtsp_port for p in peers)


def _nm_scan(interface: Optional[str], timeout: int) -> list[WFDPeer]:
    path = _nm_p2p_device_path(interface)
    if not path:
        raise WFDNotReady("NetworkManager did not expose a Wi-Fi P2P device.")

    iface = _nm_get_string(path, "org.freedesktop.NetworkManager.Device", "Interface") or path
    print(f"[hyprcast WFD] Scanning for Wi-Fi Display sinks on {iface} "
          f"(up to {timeout}s, stops as soon as one answers)...", flush=True)
    _nm_start_find(path, timeout)

    # Poll rather than sleeping the whole timeout. Discovery hops the social
    # channels 1/6/11, so a sink usually appears within a few seconds; sitting
    # mute for the full 60 looks exactly like a hang, which is what it was
    # mistaken for.
    peers: list[WFDPeer] = []
    deadline = time.monotonic() + max(1, timeout)
    last_note = 0.0
    try:
        while True:
            peers = _nm_collect_peers(path)
            if _has_wfd_sink(peers):
                break
            now = time.monotonic()
            if now >= deadline:
                break
            if now - last_note >= 5.0:
                last_note = now
                left = int(deadline - now)
                seen = f", {len(peers)} non-WFD peer(s) so far" if peers else ""
                print(f"[hyprcast WFD]   ...still looking, {left}s left{seen}", flush=True)
            time.sleep(1.0)
    finally:
        _nm_stop_find(path)

    return peers


def _variant_byte_array(data: bytes) -> str:
    return "@ay [" + ", ".join(f"byte 0x{byte:02x}" for byte in data) + "]"


def _wfd_source_ie(rtsp_port: int) -> bytes:
    if rtsp_port <= 0 or rtsp_port > 65535:
        raise WFDNotReady(f"Invalid WFD RTSP port: {rtsp_port}")
    # Build WFD IE with Device Info and Device Name subelements.
    return _wfd_ie_device_info(rtsp_port) + _wfd_ie_device_name(_DEVICE_NAME)


def _connection_settings(peer: WFDPeer, rtsp_port: int) -> str:
    peer_address = peer.address
    return (
        "{"
        "'connection': {"
        "'id': <'FluxCast WFD'>, "
        "'type': <'wifi-p2p'>, "
        "'autoconnect': <false>"
        "}, "
        "'wifi-p2p': {"
        f"'peer': <'{peer_address}'>, "
        f"'wfd-ies': <{_variant_byte_array(_wfd_source_ie(rtsp_port))}>"
        "}, "
        "'ipv4': {'method': <'auto'>, 'never-default': <true>}, "
        "'ipv6': {'method': <'auto'>, 'never-default': <true>, 'may-fail': <true>}"
        "}"
    )


def _connect_peer(
    device_path: str,
    peer: WFDPeer,
    rtsp_port: int = WFD_RTSP_PORT,
    dry_run: bool = False,
) -> str:
    if not peer.path:
        raise WFDNotReady("NetworkManager peer object path is required for P2P connection.")

    settings = _connection_settings(peer, rtsp_port)
    # !!!Do not use bind-activation here!!!: the gdbus CLI process exits right after
    # the method call, and NetworkManager would tear the P2P link down with it.
    options = "{'persist': <'volatile'>}"
    args = [
        "--dest", NM_DEST,
        "--object-path", NM_PATH,
        "--method", "org.freedesktop.NetworkManager.AddAndActivateConnection2",
        settings,
        device_path,
        peer.path,
        options,
    ]
    if dry_run:
        print("[hyprcast WFD] Dry-run AddAndActivateConnection2:")
        print("gdbus call --system " + " ".join(args))
        return "/"

    print(f"[hyprcast WFD] Connecting to {peer.name or peer.address} via NetworkManager...")
    result = _gdbus_call(args, timeout=30.0)
    text = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise WFDNotReady(text)

    paths = _object_paths(text)
    active = paths[-1] if paths else "/"
    print(f"[hyprcast WFD] NetworkManager activation started: {text}")
    return active


def _p2p_device_iface_paths(iface: Optional[str]) -> list[str]:
    """Return wpa_supplicant interface object paths, best P2P candidate first.

    The p2p-dev-<iface> control interface is preferred, then the physical
    interface, then anything else. Returns [] if wpa_supplicant can't be
    queried, so callers degrade to a warning instead of raising.
    """
    wpa_dest = "fi.w1.wpa_supplicant1"
    wpa_root = "/fi/w1/wpa_supplicant1"
    wpa_iface = "fi.w1.wpa_supplicant1.Interface"

    try:
        list_result = _gdbus_call([
            "--dest", wpa_dest,
            "--object-path", wpa_root,
            "--method", "org.freedesktop.DBus.Properties.Get",
            wpa_dest, "Interfaces",
        ], timeout=3.0)
    except Exception:
        return []

    if list_result.returncode != 0:
        return []

    iface_paths = _object_paths(list_result.stdout)
    if not iface_paths:
        return []

    physical = iface or _default_wifi_interface()
    p2p_dev = f"p2p-dev-{physical}" if physical and not physical.startswith("p2p-dev-") else physical

    def _priority(path: str) -> int:
        ifname = _nm_get_string(path, wpa_iface, "Ifname")
        if ifname == p2p_dev:
            return 0
        if ifname == physical:
            return 1
        return 2

    return sorted(iface_paths, key=_priority)


def _set_p2p_device_name(iface: Optional[str], name: str = _DEVICE_NAME) -> None:
    wpa_dest = "fi.w1.wpa_supplicant1"
    wpa_iface = "fi.w1.wpa_supplicant1.Interface"

    paths = _p2p_device_iface_paths(iface)
    if not paths:
        print("[hyprcast WFD] Warning: could not set P2P device name (cosmetic, connection will proceed).")
        return

    for iface_path in paths:
        try:
            result = _gdbus_call([
                "--dest", wpa_dest,
                "--object-path", iface_path,
                "--method", "org.freedesktop.DBus.Properties.Set",
                f"{wpa_iface}.P2PDevice", "P2PDeviceConfig",
                f"<{{'DeviceName': <'{name}'>}}>",
            ], timeout=3.0)
            if result.returncode == 0:
                print(f"[hyprcast WFD] P2P device name set to '{name}'.")
                return
        except Exception:
            pass

    print("[hyprcast WFD] Warning: could not set P2P device name (cosmetic, connection will proceed).")


def _read_p2p_go_intent(iface_path: str) -> Optional[int]:
    """Read the current P2P GO intent from a wpa_supplicant interface, or None."""
    wpa_dest = "fi.w1.wpa_supplicant1"
    wpa_iface = "fi.w1.wpa_supplicant1.Interface"
    try:
        result = _gdbus_call([
            "--dest", wpa_dest,
            "--object-path", iface_path,
            "--method", "org.freedesktop.DBus.Properties.Get",
            f"{wpa_iface}.P2PDevice", "P2PDeviceConfig",
        ], timeout=3.0)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"'GOIntent':\s*<uint32\s+(\d+)>", result.stdout)
    return int(match.group(1)) if match else None


def _set_p2p_go_intent(iface: Optional[str], value: int,
                       restoring: bool = False) -> Optional[int]:
    #Set the wpa_supplicant P2P group-owner intent (0-15)
    
    wpa_dest = "fi.w1.wpa_supplicant1"
    wpa_iface = "fi.w1.wpa_supplicant1.Interface"

    paths = _p2p_device_iface_paths(iface)
    if not paths:
        if not restoring:
            print("[hyprcast WFD] Warning: could not set P2P GO intent (connection will proceed with the default).")
        return None

    for iface_path in paths:
        previous = _read_p2p_go_intent(iface_path)
        try:
            result = _gdbus_call([
                "--dest", wpa_dest,
                "--object-path", iface_path,
                "--method", "org.freedesktop.DBus.Properties.Set",
                f"{wpa_iface}.P2PDevice", "P2PDeviceConfig",
                f"<{{'GOIntent': <uint32 {value}>}}>",
            ], timeout=3.0)
            if result.returncode == 0:
                if restoring:
                    print(f"[hyprcast WFD] Restored P2P GO intent to {value}.")
                else:
                    print(f"[hyprcast WFD] P2P GO intent set to {value} "
                          f"(lower intent lets the TV be the group owner).")
                return previous
        except Exception:
            pass

    if not restoring:
        print("[hyprcast WFD] Warning: could not set P2P GO intent (connection will proceed with the default).")
    return None


def _disconnect_device(device_path: str) -> None:
    result = _gdbus_call([
        "--dest", NM_DEST,
        "--object-path", device_path,
        "--method", "org.freedesktop.NetworkManager.Device.Disconnect",
    ], timeout=10.0)
    text = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        print("[hyprcast WFD] NetworkManager P2P device disconnected.")
    elif text and "Device.NotActive" not in text:
        print(f"[hyprcast WFD] NetworkManager disconnect warning: {text}")


def _deactivate_connection(active_path: str) -> None:
    if not active_path or active_path == "/":
        return
    result = _gdbus_call([
        "--dest", NM_DEST,
        "--object-path", NM_PATH,
        "--method", "org.freedesktop.NetworkManager.DeactivateConnection",
        active_path,
    ], timeout=10.0)
    text = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        print("[hyprcast WFD] NetworkManager P2P connection deactivated.")
    elif text:
        print(f"[hyprcast WFD] NetworkManager deactivate warning: {text}")


def _cleanup_step(label: str, action) -> None:
    """Run one teardown step without letting it skip the ones after it.

    Pressing Ctrl+C again while a session is being torn down used to abort the
    rest of the cleanup, which left the P2P connection up and the GO intent
    still lowered, so the next run needed a NetworkManager restart (#86).
    """
    try:
        action()
    except KeyboardInterrupt:
        print(f"[hyprcast WFD] Interrupted during {label}; finishing cleanup anyway.")
    except Exception as exc:
        print(f"[hyprcast WFD] Cleanup step '{label}' failed: {exc}")


def _select_peer(peers: list[WFDPeer], selector: Optional[str]) -> WFDPeer:
    if not peers:
        raise WFDNotReady("No Wi-Fi Direct peers found. Put the TV into Screen Share/Wireless Display mode.")
    if selector is None and len(peers) == 1:
        # One sink is the normal case on a personal machine. Prompting for it
        # makes `hyprcast cast` un-scriptable and hangs when stdin is not a tty.
        only = peers[0]
        print(f"[hyprcast WFD] Using the only peer: {only.address}"
              f"{'  ' + only.name if only.name else ''}")
        return only
    if selector is None:
        print_scan(peers)
        try:
            raw = input("Select WFD peer [0]: ").strip()
        except EOFError:
            raw = ""
        selector = raw or "0"

    if selector.isdigit():
        index = int(selector)
        if 0 <= index < len(peers):
            return peers[index]
        raise WFDNotReady(f"Peer index out of range: {selector}")

    normalized = selector.lower()
    for peer in peers:
        if normalized in peer.address.lower() or normalized in peer.name.lower():
            return peer
    raise WFDNotReady(f"No peer matched selector: {selector}")


def _scan_and_select(interface: Optional[str], selector: Optional[str],
                     timeout: int, attempts: int = 3) -> WFDPeer:
    """Scans and resolves peer. If no selector, does one scan and opens prompt.
    With selector, retries non-deterministic scans
    until resolved or raises original error.
    """
    if selector is None:
        peers = active_scan(interface=interface, timeout=timeout)
        return _select_peer(peers, None)

    last_error: Optional[WFDNotReady] = None
    for attempt in range(1, attempts + 1):
        peers = active_scan(interface=interface, timeout=timeout)
        try:
            return _select_peer(peers, selector)
        except WFDNotReady as exc:
            last_error = exc
            if attempt < attempts:
                print(f"[hyprcast WFD] peer '{selector}' not in scan "
                      f"{attempt}/{attempts}; rescanning...")
    assert last_error is not None
    raise last_error


def _default_wifi_interface() -> Optional[str]:
    if not shutil.which("iw"):
        return None

    try:
        result = _run(["iw", "dev"], timeout=3.0)
    except (OSError, subprocess.TimeoutExpired):
        return None

    current_iface = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Interface "):
            current_iface = stripped.split(maxsplit=1)[1]
        elif stripped == "type managed" and current_iface:
            return current_iface
    return None


def _parse_peer_name(details: str) -> str:
    for line in details.splitlines():
        stripped = line.strip()
        if stripped.startswith("device_name="):
            return stripped.partition("=")[2]
    return ""


def active_scan(interface: Optional[str] = None, timeout: int = 8) -> list[WFDPeer]:
    """Run an active Wi-Fi Direct peer scan.
    """
    try:
        return _nm_scan(interface=interface, timeout=timeout)
    except WFDNotReady as nm_error:
        print(f"[hyprcast WFD] NetworkManager scan unavailable: {nm_error}")

    if not shutil.which("wpa_cli"):
        raise WFDNotReady("wpa_cli is required for active Wi-Fi Direct scans.")

    iface = interface or _default_wifi_interface()
    if not iface:
        raise WFDNotReady("Could not detect a managed Wi-Fi interface for wpa_cli.")

    print(f"[hyprcast WFD] Starting Wi-Fi Direct scan on {iface} for {timeout}s...")
    try:
        start = _run(["wpa_cli", "-i", iface, "p2p_find", str(timeout)], timeout=5.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WFDNotReady(f"Could not start p2p_find: {exc}") from exc
    if start.returncode != 0:
        error = (start.stderr or start.stdout).strip()
        if "Permission denied" in error:
            raise WFDNotReady(
                "wpa_cli cannot access the supplicant control interface. "
                "This usually needs root, a ctrl_interface group, or a "
                "NetworkManager D-Bus connection path. Raw error: " + error
            )
        raise WFDNotReady(error)

    time.sleep(max(1, timeout))

    try:
        peers_result = _run(["wpa_cli", "-i", iface, "p2p_peers"], timeout=5.0)
    finally:
        try:
            _run(["wpa_cli", "-i", iface, "p2p_stop_find"], timeout=3.0)
        except Exception:
            pass

    if peers_result.returncode != 0:
        raise WFDNotReady((peers_result.stderr or peers_result.stdout).strip())

    peers = []
    for raw in peers_result.stdout.splitlines():
        address = raw.strip()
        if not re.fullmatch(r"[0-9a-fA-F:]{17}", address):
            continue

        details = ""
        try:
            details_result = _run(["wpa_cli", "-i", iface, "p2p_peer", address], timeout=5.0)
            if details_result.returncode == 0:
                details = details_result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        peers.append(WFDPeer(
            address=address,
            name=_parse_peer_name(details),
            details=details,
            source="wpa_cli",
        ))

    return peers


def print_scan(peers: list[WFDPeer]) -> None:
    if not peers:
        print("[hyprcast WFD] No Wi-Fi Direct peers found.")
        return

    print("[hyprcast WFD] Wi-Fi Direct peer(s):")
    for idx, peer in enumerate(peers):
        name = f"  {peer.name}" if peer.name else ""
        source = f" via {peer.source}" if peer.source else ""
        print(f"  [{idx}] {peer.address}{name}{source}")
        if "wfd_subelems" in peer.details or "wfd_dev_info" in peer.details:
            print("      WFD capability data detected")
        elif "wfd_ies=" in peer.details:
            print("      WFD capability data detected")


def _get_peer_ip_from_arp(peer_mac: str) -> Optional[str]:
    """Return the IP for peer_mac from the kernel ARP/neighbour table."""
    if not shutil.which("ip"):
        return None
    try:
        result = _run(["ip", "neigh", "show"], timeout=3.0)
        if result.returncode != 0:
            return None
        mac = peer_mac.lower().replace("-", ":")
        for line in result.stdout.splitlines():
            if mac in line.lower():
                parts = line.split()
                if parts and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parts[0]):
                    return parts[0]
    except Exception:
        pass
    return None


def _get_peer_ip_from_p2p_iface() -> Optional[str]:
    """Fallback: find TV IP from ARP on any active P2P group interface.

    Some TVs (LG webOS in issue #44) randomize their MAC between the P2P discovery phase and
    the actual group connection, so. the scanned MAC never matches the ARP entry.
    Scanning the p2p-* group interface directly avoids the MAC comparison entirely.
    """
    try:
        result = _run(["ip", "neigh", "show"], timeout=3.0)
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            # Format: IP dev IFACE lladdr MAC STATE
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "dev":
                iface = parts[2]
                if iface.startswith("p2p-") and not iface.startswith("p2p-dev-"):
                    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parts[0]):
                        return parts[0]
    except Exception:
        pass
    return None


def _wait_for_peer_ip(peer_mac: str, timeout: float = 12.0) -> Optional[str]:
    """Poll ARP until the peer's IP appears (DHCP may take a few seconds)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ip = _get_peer_ip_from_arp(peer_mac) or _get_peer_ip_from_p2p_iface()
        if ip:
            return ip
        time.sleep(0.75)
    return None


def _p2p_local_address() -> Optional[tuple[str, str, int]]:
    """Our own IPv4 on the Wi-Fi Direct group interface: (iface, ip, prefix).

    Both the interface name and the subnet change between sessions -- observed
    p2p-wlan0-0 / 192.168.13.x one run and p2p-wlan0-1 / 192.168.168.x the
    next -- so nothing here may be hardcoded or cached across sessions.
    """
    if not shutil.which("ip"):
        return None
    try:
        result = _run(["ip", "-o", "-4", "addr", "show"], timeout=3.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        iface = parts[1].split("@", 1)[0]
        if not iface.startswith("p2p-") or iface.startswith("p2p-dev-"):
            continue
        address, _, prefix = parts[3].partition("/")
        if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", address):
            continue
        try:
            return iface, address, int(prefix or "24")
        except ValueError:
            return iface, address, 24
    return None


def _wait_for_p2p_local_address(timeout: float = 20.0) -> tuple[str, str, int]:
    """Block until the group interface has an address, or explain why not.

    NetworkManager reports the connection activated before DHCP has finished,
    so the interface can exist for a second or two with no IPv4 on it.
    """
    deadline = time.monotonic() + timeout
    while True:
        found = _p2p_local_address()
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            raise WFDNotReady(
                "No IPv4 address on any p2p-* interface after "
                f"{timeout:g}s; the group formed but DHCP never completed."
            )
        time.sleep(0.25)


def _active_rtsp_probe(
    rtsp_server: WFDRTSPServer,
    peer: WFDPeer,
    media_config: WFDMediaConfig,
) -> None:
    # 4s: give passive-RTSP TVs a window to connect first, without burning too
    # much of the cca 15 s timeout that some TVs enforce after P2P activation.
    time.sleep(4.0)
    if rtsp_server.has_connected_client:
        return

    print("[hyprcast WFD RTSP] No passive connection; trying Source-initiated RTSP probe...")

    tv_ip = _wait_for_peer_ip(peer.address, timeout=10.0)
    if not tv_ip:
        print(
            f"[hyprcast WFD RTSP] Active probe: TV IP not found for MAC {peer.address} "
            "— ARP table empty; is the P2P link still up?"
        )
        return

    if rtsp_server.has_connected_client:
        return
    
    tv_port = peer.rtsp_port if 0 < peer.rtsp_port <= 65535 else 7236
    print(f"[hyprcast WFD RTSP] Active probe: TV={tv_ip}; connecting to RTSP port {tv_port}...")

    try:
        sock = socket.create_connection((tv_ip, tv_port), timeout=5.0)
    except ConnectionRefusedError:
        print(
            f"[hyprcast WFD RTSP] Active probe: TV port {tv_port} refused "
            "— Sink-only device; waiting for its passive connection to us."
        )
        return
    except OSError as exc:
        print(f"[hyprcast WFD RTSP] Active probe: connect error: {exc}")
        return

    print(f"[hyprcast WFD RTSP] Active probe: connected to TV RTSP at {tv_ip}:{tv_port}")
    # NOT 8.0: the captured session shows the sink sitting for 8.024 s between
    # our SETUP 200 and its PLAY. An 8 s read timeout fires inside that stall
    # and kills a session that was about to work.
    sock.settimeout(60.0)
    rfile = sock.makefile("rb")
    wfile = sock.makefile("wb")
    local_ip: str = sock.getsockname()[0]
    local_uri = f"rtsp://{local_ip}:{rtsp_server.port}/wfd1.0"
    session_id = str(random.randint(1_000_000, 9_999_999))

    # Mutable session state shared by the nested helpers below.
    st: dict = {
        "cseq": 1,
        "pending": {},
        "sink_rtp_port": 0,
        "sink_rtcp_port": 0,
        "src_port": media_config.source_port,
        "sink_vfmt": None,
        "no_audio": media_config.no_audio,
    }

    def _send(name: str, method: str, uri: str, hdrs: Optional[dict] = None, body: str = "") -> None:
        cseq = str(st["cseq"])
        st["cseq"] += 1
        st["pending"][cseq] = name
        lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {cseq}"]
        for k, v in (hdrs or {}).items():
            lines.append(f"{k}: {v}")
        if body:
            lines += ["Content-Type: text/parameters", f"Content-Length: {len(body.encode())}"]
        lines += ["", body]
        _wire_dump("TX", "\r\n".join(lines))
        wfile.write("\r\n".join(lines).encode())
        wfile.flush()
        print(f"[hyprcast WFD RTSP] Active probe -> {name}")

    def _reply(msg: RTSPMessage, status: str = "200 OK", extra: Optional[dict] = None, body: str = "") -> None:
        lines = [f"RTSP/1.0 {status}", f"CSeq: {msg.cseq}", f"Session: {session_id};timeout=30"]
        for k, v in (extra or {}).items():
            lines.append(f"{k}: {v}")
        lines += [f"Content-Length: {len(body.encode())}", "", body]
        _wire_dump("TX", "\r\n".join(lines))
        wfile.write("\r\n".join(lines).encode())
        wfile.flush()

    media: Optional[NativeSender] = None
    try:
        with sock:
            _send("M1_OPTIONS", "OPTIONS", "*", {"Require": "org.wfa.wfd1.0"})
            while True:
                msg = _read_rtsp_message(rfile)
                if msg is None:
                    print("[hyprcast WFD RTSP] Active probe: TV closed connection.")
                    break
                _wire_dump("RX", "\r\n".join([msg.start, *msg.raw_headers, "", msg.body]))

                if msg.is_response:
                    name = st["pending"].pop(msg.cseq, "UNKNOWN")
                    if not msg.status.startswith("200"):
                        print(f"[hyprcast WFD RTSP] Active probe: {name} failed: {msg.status}")
                        break
                    print(f"[hyprcast WFD RTSP] Active probe <- OK for {name}")

                    if name == "M1_OPTIONS":
                        _send("M3_GET_PARAMETER", "GET_PARAMETER", local_uri,
                              body="wfd_content_protection\r\nwfd_video_formats\r\nwfd_audio_codecs\r\nwfd_client_rtp_ports\r\n")

                    elif name == "M3_GET_PARAMETER":
                        params = _parse_parameters(msg.body)
                        ports = _parse_rtp_ports(params.get("wfd_client_rtp_ports", ""))
                        if not ports or ports[0] <= 0:
                            print("[hyprcast WFD RTSP] Active probe: no valid RTP ports in M3.")
                            break
                        st["sink_rtp_port"], st["sink_rtcp_port"] = ports
                        st["sink_vfmt"] = _parse_sink_video_format(params.get("wfd_video_formats", ""))
                        audio = params.get("wfd_audio_codecs", "")
                        _probe_microsoft = "microsoft" in media_config.peer_name.lower()
                        if (
                            audio
                            and not media_config.no_audio
                            and "AAC" not in audio.upper()
                            and not _probe_microsoft
                        ):
                            st["no_audio"] = True
                        st["src_port"] = _safe_source_port(
                            media_config.source_port, st["sink_rtp_port"], st["sink_rtcp_port"])
                        vfmt = _selected_video_format(media_config, st["sink_vfmt"])
                        if st["no_audio"]:
                            afmt = "none"
                        elif _probe_microsoft:
                            afmt = WFD_AUDIO_LPCM_48K
                        else:
                            afmt = WFD_AUDIO_AAC
                        rtcp = st["sink_rtcp_port"] if st["sink_rtcp_port"] > 0 else 0
                        m4 = (
                            "wfd_content_protection: none\r\n"
                            f"wfd_video_formats: {vfmt}\r\n"
                            f"wfd_audio_codecs: {afmt}\r\n"
                            f"wfd_presentation_URL: {local_uri}/streamid=0 none\r\n"
                            f"wfd_client_rtp_ports: RTP/AVP/UDP;unicast "
                            f"{st['sink_rtp_port']} {rtcp} mode=play\r\n"
                        )
                        _send("M4_SET_PARAMETER", "SET_PARAMETER", local_uri, body=m4)

                    elif name == "M4_SET_PARAMETER":
                        _send("M5_TRIGGER_SETUP", "SET_PARAMETER", local_uri,
                              body="wfd_trigger_method: SETUP\r\n")

                    elif name == "M5_TRIGGER_SETUP":
                        print("[hyprcast WFD RTSP] Active probe: M5 sent — awaiting TV SETUP...")
                        # TV should now send SETUP on this same TCP connection.

                else:
                    method = msg.method
                    print(f"[hyprcast WFD RTSP] Active probe <- TV request: {method}")

                    if method in ("GET_PARAMETER", "SET_PARAMETER"):
                        _reply(msg)

                    elif method == "OPTIONS":
                        _reply(msg, extra={
                            "Public": (
                                "org.wfa.wfd1.0, SETUP, TEARDOWN, PLAY, PAUSE, "
                                "GET_PARAMETER, SET_PARAMETER"
                            )
                        })

                    elif method == "SETUP":
                        ports = _parse_transport_client_ports(msg.headers.get("transport", ""))
                        if ports:
                            st["sink_rtp_port"], st["sink_rtcp_port"] = ports
                        if not st["sink_rtp_port"]:
                            st["sink_rtp_port"] = 19000
                        st["src_port"] = _safe_source_port(
                            media_config.source_port, st["sink_rtp_port"], st["sink_rtcp_port"])
                        sp = st["src_port"]
                        sr = st["sink_rtp_port"]
                        sc = st["sink_rtcp_port"]
                        transport = (
                            f"RTP/AVP/UDP;unicast;client_port={sr}-{sc};server_port={sp}-{sp + 1}"
                            if sc else
                            f"RTP/AVP/UDP;unicast;client_port={sr};server_port={sp}"
                        )
                        _reply(msg, extra={
                            "Transport": transport,
                            "Session": f"{session_id};timeout=30",
                        })
                        print(f"[hyprcast WFD RTSP] Active probe: SETUP OK; sink RTP={sr}")

                    elif method == "PLAY":
                        _reply(msg, extra={"Range": "npt=now-"})
                        mode = _choose_cea_mode(media_config, st["sink_vfmt"])
                        eff_cfg = replace(
                            media_config,
                            source_port=st["src_port"],
                            output_resolution=mode.resolution,
                            fps=mode.fps,
                            no_audio=st["no_audio"],
                        )
                        media = NativeSender(
                            eff_cfg,
                            tv_ip=tv_ip,
                            local_ip=local_ip,
                            sink_rtp_port=st["sink_rtp_port"],
                        )
                        rtsp_server.has_connected_client = True
                        print(
                            f"[hyprcast WFD RTSP] Active probe: PLAY — "
                            f"starting media ({mode.name})"
                        )
                        media.start()
                        # Keep-alive: respond to GET_PARAMETER/SET_PARAMETER heartbeats.
                        while True:
                            ka = _read_rtsp_message(rfile)
                            if ka is None:
                                break
                            if ka.method in ("GET_PARAMETER", "SET_PARAMETER"):
                                if "wfd_idr_request" in ka.body and media is not None:
                                    forced = media.request_idr()
                                    print("[hyprcast WFD RTSP] Active probe: sink "
                                          "requested IDR; "
                                          + ("forced." if forced else "rate-limited."))
                                _reply(ka)
                            elif ka.method == "TEARDOWN":
                                _reply(ka, extra={"Connection": "close"})
                                break
                        return

                    elif method == "TEARDOWN":
                        _reply(msg, extra={"Connection": "close"})
                        break

                    else:
                        _reply(msg, status="405 Method Not Allowed")

    except OSError as exc:
        print(f"[hyprcast WFD RTSP] Active probe: I/O error: {exc}")
    finally:
        if media is not None:
            media.stop()
    print("[hyprcast WFD RTSP] Active probe: session ended.")


class _SessionStop(Exception):
    """SIGTERM/SIGINT arrived; unwind through the teardown block."""


def _install_stop_handlers() -> list[tuple[int, object]]:
    """Make SIGTERM unwind exactly like Ctrl+C does.

    The default SIGTERM disposition kills the process outright, which skips the
    finally block below -- leaving the P2P connection up, the GO intent still
    lowered and the engine orphaned, so the next run needs a NetworkManager
    restart. Signal handlers can only be installed from the main thread; the
    loopback harness calls this from its own, so failure is not fatal.
    """
    previous: list[tuple[int, object]] = []

    def _stop(signum, _frame):
        raise _SessionStop(signal.Signals(signum).name)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous.append((signum, signal.signal(signum, _stop)))
        except (ValueError, OSError):
            pass
    return previous


def _restore_stop_handlers(previous: list[tuple[int, object]]) -> None:
    for signum, handler in previous:
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):
            pass


def start_experimental_backend(args) -> None:
    if not _is_hyprland_session():
        print(
            "[hyprcast WFD] WARNING: this does not look like a Hyprland session "
            "(no HYPRLAND_INSTANCE_SIGNATURE); the engine needs "
            "ext-image-copy-capture-v1."
        )

    for dead_flag, label in (
        ("wfd_test_pattern", "--wfd-test-pattern"),
        ("wfd_ffmpeg_stats", "--wfd-ffmpeg-stats"),
        ("wfd_capture_backend", "--wfd-capture-backend"),
        ("wfd_uibc", "--wfd-uibc"),
    ):
        value = getattr(args, dead_flag, None)
        if value and value != "auto":
            print(
                f"[hyprcast WFD] {label} is gone: the media leg is "
                "hyprcast-engine now, not an ffmpeg/gstreamer subprocess."
            )
    if getattr(args, "wfd_media_pipeline", "auto") not in ("auto", None):
        print("[hyprcast WFD] --wfd-media-pipeline is gone; there is one pipeline.")

    monitor: Optional[Monitor] = None
    if not getattr(args, "wfd_dry_run", False):
        monitor = select_monitor(getattr(args, "monitor_name", None))
        print(
            f"[hyprcast WFD] Capture source: {monitor.name} "
            f"{monitor.width}x{monitor.height}@{monitor.refresh:g} "
            f"(scale {monitor.scale:g})"
        )

    _set_p2p_device_name(args.wfd_interface)
    peer = _scan_and_select(
        args.wfd_interface, getattr(args, "wfd_peer", None), args.wfd_timeout
    )
    device_path = _nm_p2p_device_path(args.wfd_interface)
    if not device_path:
        raise WFDNotReady("NetworkManager P2P device disappeared before connection.")

    if getattr(args, "wfd_dry_run", False):
        _connect_peer(
            device_path,
            peer,
            rtsp_port=getattr(args, "wfd_rtsp_port", WFD_RTSP_PORT),
            dry_run=True,
        )
        return

    media_config = WFDMediaConfig(
        monitor=monitor,
        fps=args.fps,
        bitrate=args.bitrate,
        output_resolution=args.output_res,
        probe_only=bool(getattr(args, "probe_only", False)),
        audio_device=getattr(args, "wfd_audio_device", None),
        no_audio=getattr(args, "wfd_no_audio", False),
        source_port=getattr(args, "wfd_rtp_source_port", 19002),
        latency_log_path=getattr(args, "wfd_latency_log", None),
        peer_name=peer.name,
        low_power=getattr(args, "wfd_low_power", False),
        qp=getattr(args, "wfd_qp", 0),
        engine_path=getattr(args, "engine", None),
    )
    if media_config.latency_log_path:
        print(f"[hyprcast WFD] Latency log file: {media_config.latency_log_path}")

    rtsp_port = getattr(args, "wfd_rtsp_port", WFD_RTSP_PORT)
    rtsp: Optional[WFDRTSPServer] = None
    firewall_opened = False
    active_path = ""
    previous_go_intent = None
    previous_signals = _install_stop_handlers()
    try:
        # Clear stale P2P device state from previous runs before new activation.
        try:
            _disconnect_device(device_path)
        except Exception:
            pass
        # Lower our GO intent before negotiation so the TV becomes the group
        # owner; most Miracast sinks only start the RTSP session in that role.
        previous_go_intent = _set_p2p_go_intent(
            args.wfd_interface, getattr(args, "wfd_go_intent", 0)
        )
        active_path = _connect_peer(
            device_path,
            peer,
            rtsp_port=rtsp_port,
        )
        _wait_for_nm_activation(active_path)

        # Only now does the group interface exist, and only now do we know
        # which subnet it landed on. Bind RTSP to that address and refuse
        # everyone else: the old default bound 0.0.0.0 with no peer check,
        # which on shared Wi-Fi is a desktop-mirror-on-request service.
        p2p_iface, local_ip, prefix = _wait_for_p2p_local_address()
        allowed = ipaddress.ip_network(f"{local_ip}/{prefix}", strict=False)
        print(
            f"[hyprcast WFD] P2P link up on {p2p_iface} "
            f"({local_ip}/{prefix}); serving RTSP to {allowed} only."
        )
        rtsp = WFDRTSPServer(
            media_config=media_config,
            host=local_ip,
            port=rtsp_port,
            allowed_network=allowed,
        )
        rtsp.start()

        if not getattr(args, "wfd_no_firewall", False):
            firewall_opened = _open_wfd_firewall_port(rtsp_port)

        # Active probe for sinks that expect the source to connect to them.
        # It runs in a background thread to not block the main loop.
        probe_thread = threading.Thread(
            target=_active_rtsp_probe,
            args=(rtsp, peer, media_config),
            daemon=True,
        )
        probe_thread.start()

        if media_config.probe_only:
            print("[hyprcast WFD] Probing: waiting for the sink to answer M3...")
        else:
            print("[hyprcast WFD] Waiting for TV RTSP/WFD session. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
            if media_config.probe_only and getattr(rtsp, "probe_done", False):
                print("[hyprcast WFD] Probe complete; tearing the link down.")
                break
    except (KeyboardInterrupt, _SessionStop) as exc:
        reason = str(exc) or "Ctrl+C"
        print(f"\n[hyprcast WFD] Stopping WFD session ({reason})...")
    finally:
        _restore_stop_handlers(previous_signals)
        if rtsp is not None:
            _cleanup_step("media shutdown", rtsp.stop_all_media)
            _cleanup_step("RTSP server shutdown", rtsp.stop)
        if firewall_opened:
            _cleanup_step("firewall close", lambda: _close_wfd_firewall_port(rtsp_port))
        if active_path:
            _cleanup_step("connection deactivate",
                          lambda: _deactivate_connection(active_path))
        _cleanup_step("P2P device disconnect", lambda: _disconnect_device(device_path))
        if previous_go_intent is not None:
            _cleanup_step(
                "GO intent restore",
                lambda: _set_p2p_go_intent(
                    args.wfd_interface, previous_go_intent, restoring=True
                ),
            )
