#!/usr/bin/env python3
"""
mock-sink.py -- a loopback Wi-Fi Display (Miracast) sink for hyprcast.

Replays the sink half of reference/sink/wire-720p60-full-session.txt, the
byte-exact trace of a session that actually rendered on a Xiaomi box running
the Google TV Miracast app. It exists so every further RTSP / MPEG-TS change
can be developed and regression-tested without the TV in the room.

What it does, in the order the real sink did it:

  M1   receive OPTIONS from the source, answer 200 with
       "Public: org.wfa.wfd1.0, GET_PARAMETER, SET_PARAMETER"
  M2   send our own OPTIONS, expect 200
  M3   receive GET_PARAMETER, answer 200 with the captured capability body,
       read verbatim from reference/sink/m3-xiaomi-googletv.txt
  M4   receive SET_PARAMETER, log the negotiated video/audio formats, answer 200
  M5   receive SET_PARAMETER wfd_trigger_method: SETUP, answer 200, then send
       SETUP with Transport: RTP/AVP/UDP;unicast;client_port=<--rtp-port>
  M7   on the source's 200, send PLAY (immediately, or after --play-delay,
       which reproduces the real sink's measured 8.024 s stall)
  M16  answer GET_PARAMETER keepalives with 200, forever

and in parallel binds UDP on --rtp-port, strips the RTP header off every
datagram, and writes the MPEG-TS payload to --fifo (stdout by default) while
reporting throughput, RTP sequence gaps and timestamp regressions once a second.

Python 3.14, standard library only. Run it with:

    python3 tools/mock-sink.py --help
"""

from __future__ import annotations

import argparse
import errno
import os
import signal
import socket
import stat
import sys
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO, Optional

# ── constants ────────────────────────────────────────────────────────────────

CRLF = "\r\n"
RTSP_VERSION = "RTSP/1.0"

# The sink's OPTIONS response in the captured trace. Note it is a SHORTER list
# than the source's: a WFD sink implements GET_PARAMETER and SET_PARAMETER only.
SINK_PUBLIC = "org.wfa.wfd1.0, GET_PARAMETER, SET_PARAMETER"

# The trace's own capability body is 242 bytes; anything else means the file in
# reference/sink/ drifted and the mock is no longer replaying the real sink.
EXPECTED_M3_LENGTH = 242

DEFAULT_M3_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "reference", "sink", "m3-xiaomi-googletv.txt",
)

TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47

SEQ_MOD = 1 << 16
TS_MOD = 1 << 32


def log(msg: str) -> None:
    """Every diagnostic goes to stderr: stdout may be carrying MPEG-TS."""
    print(f"[mock-sink] {msg}", file=sys.stderr, flush=True)


def rtsp_date() -> str:
    """
    The captured sink emits 'Sun, Aug 02 2026 13:46:58 GMT' -- month before day,
    which is not RFC 1123. Reproduce the sink's format, not the RFC's.
    """
    t = time.gmtime()
    return time.strftime("%a, %b %d %Y %H:%M:%S GMT", t)


# ── RTSP message plumbing ────────────────────────────────────────────────────

@dataclass
class RTSPMessage:
    start: str
    headers: dict[str, str]
    raw_headers: list[str]
    body: str

    @property
    def is_response(self) -> bool:
        return self.start.startswith("RTSP/")

    @property
    def status(self) -> str:
        return self.start.partition(" ")[2] if self.is_response else ""

    @property
    def method(self) -> str:
        return self.start.partition(" ")[0].upper() if not self.is_response else ""

    @property
    def uri(self) -> str:
        if self.is_response:
            return ""
        parts = self.start.split()
        return parts[1] if len(parts) > 1 else ""

    @property
    def cseq(self) -> str:
        return self.headers.get("cseq", "")

    def wire(self) -> str:
        return CRLF.join([self.start, *self.raw_headers, "", self.body])


def read_rtsp_message(rfile: BinaryIO) -> Optional[RTSPMessage]:
    """Read one RTSP message. Returns None on a clean EOF."""
    lines: list[str] = []
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

    try:
        content_length = int(headers.get("content-length", "0"))
    except ValueError:
        content_length = 0

    body = ""
    if content_length > 0:
        raw_body = rfile.read(content_length)
        if raw_body is None:
            return None
        body = raw_body.decode("utf-8", errors="replace")

    return RTSPMessage(
        start=lines[0], headers=headers, raw_headers=lines[1:], body=body
    )


def parse_parameters(body: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for line in body.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            params[key.strip().lower()] = value.strip()
    return params


def load_m3_body(path: str) -> str:
    """
    Read the captured M3 capability body verbatim. '#' lines are provenance
    comments added by the capture, not wire content; everything else is copied
    byte for byte and CRLF-terminated, which is what produced Content-Length: 242.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    lines = [
        line.rstrip("\r\n")
        for line in raw.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        raise ValueError(f"{path} contains no wfd_ parameter lines")
    return "".join(line + CRLF for line in lines)


def strip_uri_port(uri: str) -> str:
    """
    rtsp://192.168.168.115:7236/wfd1.0/streamid=0 -> rtsp://192.168.168.115/wfd1.0/streamid=0

    The real sink drops the port from wfd_presentation_URL before using it as
    the SETUP request URI. Reproduce that; some sources match on it literally.
    """
    scheme, sep, rest = uri.partition("://")
    if not sep:
        return uri
    authority, slash, tail = rest.partition("/")
    # Leave IPv6 literals ([::1]:7236) alone unless the port is outside the
    # brackets, and never mangle a bare host with no port.
    if authority.startswith("["):
        close = authority.find("]")
        if close != -1:
            authority = authority[: close + 1]
    elif ":" in authority:
        authority = authority.partition(":")[0]
    return f"{scheme}://{authority}{slash}{tail}"


# ── RTP ──────────────────────────────────────────────────────────────────────

@dataclass
class RTPPacket:
    seq: int
    timestamp: int
    ssrc: int
    payload_type: int
    marker: bool
    header_len: int
    payload: memoryview


def parse_rtp(datagram: bytes) -> Optional[RTPPacket]:
    """
    Strip the RTP header. WFD always uses the plain 12-byte form (no CSRCs, no
    extension, no padding), but handle the general case so a malformed or
    padded datagram is reported rather than silently corrupting the TS stream.
    """
    if len(datagram) < 12:
        return None
    b0 = datagram[0]
    if (b0 >> 6) != 2:
        return None
    padding = bool(b0 & 0x20)
    extension = bool(b0 & 0x10)
    csrc_count = b0 & 0x0F
    b1 = datagram[1]

    offset = 12 + 4 * csrc_count
    if extension:
        if len(datagram) < offset + 4:
            return None
        ext_words = int.from_bytes(datagram[offset + 2: offset + 4], "big")
        offset += 4 + 4 * ext_words

    end = len(datagram)
    if padding:
        if end <= offset:
            return None
        pad_len = datagram[-1]
        if pad_len == 0 or end - pad_len < offset:
            return None
        end -= pad_len
    if offset > end:
        return None

    return RTPPacket(
        seq=int.from_bytes(datagram[2:4], "big"),
        timestamp=int.from_bytes(datagram[4:8], "big"),
        ssrc=int.from_bytes(datagram[8:12], "big"),
        payload_type=b1 & 0x7F,
        marker=bool(b1 & 0x80),
        header_len=offset,
        payload=memoryview(datagram)[offset:end],
    )


@dataclass
class RTPStats:
    datagrams: int = 0
    bytes_payload: int = 0
    bytes_wire: int = 0
    lost: int = 0
    reordered: int = 0
    duplicates: int = 0
    ts_regressions: int = 0
    malformed: int = 0
    non_ts: int = 0

    def add(self, other: "RTPStats") -> None:
        self.datagrams += other.datagrams
        self.bytes_payload += other.bytes_payload
        self.bytes_wire += other.bytes_wire
        self.lost += other.lost
        self.reordered += other.reordered
        self.duplicates += other.duplicates
        self.ts_regressions += other.ts_regressions
        self.malformed += other.malformed
        self.non_ts += other.non_ts


# ── payload sink ─────────────────────────────────────────────────────────────

class PayloadWriter:
    """
    Writes de-RTP'd MPEG-TS to stdout, a regular file, or a FIFO.

    A FIFO has no reader until something opens the far end, so it is opened
    non-blocking and retried; datagrams that arrive before a reader shows up are
    counted and dropped rather than stalling the RTSP control thread.
    """

    def __init__(self, target: str, make_fifo: bool) -> None:
        self.target = target
        self.dropped_bytes = 0
        self._fd: Optional[int] = None
        self._stream: Optional[BinaryIO] = None
        self._is_fifo = False
        self._warned_no_reader = False
        self._last_retry = 0.0

        if target == "-":
            self._stream = sys.stdout.buffer
            log("MPEG-TS output: stdout")
            return

        if make_fifo and not os.path.exists(target):
            os.mkfifo(target, 0o644)
            log(f"created FIFO {target}")

        if os.path.exists(target) and stat.S_ISFIFO(os.stat(target).st_mode):
            self._is_fifo = True
            log(f"MPEG-TS output: FIFO {target} (waiting for a reader)")
            self._try_open_fifo()
        else:
            self._stream = open(target, "wb")
            log(f"MPEG-TS output: file {target}")

    def _try_open_fifo(self) -> None:
        now = time.monotonic()
        if self._fd is not None or now - self._last_retry < 0.5:
            return
        self._last_retry = now
        try:
            self._fd = os.open(self.target, os.O_WRONLY | os.O_NONBLOCK)
            log(f"FIFO {self.target}: reader attached")
        except OSError as exc:
            if exc.errno != errno.ENXIO:
                raise
            if not self._warned_no_reader:
                self._warned_no_reader = True
                log(f"FIFO {self.target}: no reader yet, dropping payload")

    def write(self, data: memoryview) -> None:
        if self._is_fifo:
            self._try_open_fifo()
            if self._fd is None:
                self.dropped_bytes += len(data)
                return
            try:
                os.write(self._fd, data)
            except BlockingIOError:
                self.dropped_bytes += len(data)
            except BrokenPipeError:
                log(f"FIFO {self.target}: reader went away")
                os.close(self._fd)
                self._fd = None
                self._warned_no_reader = False
                self.dropped_bytes += len(data)
            return

        assert self._stream is not None
        try:
            self._stream.write(data)
        except BrokenPipeError:
            self.dropped_bytes += len(data)

    def flush(self) -> None:
        if self._stream is not None:
            try:
                self._stream.flush()
            except (BrokenPipeError, ValueError):
                pass

    def close(self) -> None:
        self.flush()
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._stream is not None and self._stream is not sys.stdout.buffer:
            self._stream.close()
            self._stream = None


# ── RTP receiver ─────────────────────────────────────────────────────────────

class RTPReceiver:
    def __init__(self, bind_addr: str, port: int, writer: PayloadWriter,
                 stop: threading.Event) -> None:
        self.port = port
        self.writer = writer
        self.stop = stop
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        except OSError:
            pass
        self.sock.bind((bind_addr, port))
        self.sock.settimeout(0.5)

        self._lock = threading.Lock()
        self._window = RTPStats()
        self.total = RTPStats()
        self.first_packet_at: Optional[float] = None
        self.last_packet_at: Optional[float] = None
        self._ssrc: Optional[int] = None
        self._last_seq: Optional[int] = None
        self._last_ts: Optional[int] = None
        self._thread = threading.Thread(target=self._run, name="rtp-rx", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def take_window(self) -> RTPStats:
        with self._lock:
            window, self._window = self._window, RTPStats()
        return window

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                datagram = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            self._consume(datagram)

    def _consume(self, datagram: bytes) -> None:
        now = time.monotonic()
        pkt = parse_rtp(datagram)
        with self._lock:
            self._window.datagrams += 1
            self._window.bytes_wire += len(datagram)
            self.total.datagrams += 1
            self.total.bytes_wire += len(datagram)

            if pkt is None:
                self._window.malformed += 1
                self.total.malformed += 1
                return

            if self.first_packet_at is None:
                self.first_packet_at = now
                log(
                    f"RTP: first datagram on port {self.port} "
                    f"(ssrc=0x{pkt.ssrc:08x} pt={pkt.payload_type} "
                    f"seq={pkt.seq} ts={pkt.timestamp} "
                    f"header={pkt.header_len}B payload={len(pkt.payload)}B)"
                )
                if pkt.header_len != 12:
                    log(
                        f"RTP: WARNING non-standard {pkt.header_len}-byte header "
                        "(CSRC/extension present)"
                    )
            self.last_packet_at = now

            if self._ssrc is None or pkt.ssrc != self._ssrc:
                if self._ssrc is not None:
                    log(
                        f"RTP: SSRC changed 0x{self._ssrc:08x} -> 0x{pkt.ssrc:08x}; "
                        "resetting sequence tracking"
                    )
                self._ssrc = pkt.ssrc
                self._last_seq = None
                self._last_ts = None

            if self._last_seq is not None:
                delta = (pkt.seq - self._last_seq) % SEQ_MOD
                if delta == 0:
                    self._window.duplicates += 1
                    self.total.duplicates += 1
                elif delta == 1:
                    pass
                elif delta < SEQ_MOD // 2:
                    missing = delta - 1
                    self._window.lost += missing
                    self.total.lost += missing
                    log(
                        f"RTP: sequence gap -- {missing} datagram(s) lost "
                        f"({self._last_seq} -> {pkt.seq})"
                    )
                else:
                    self._window.reordered += 1
                    self.total.reordered += 1
                    log(f"RTP: out-of-order datagram ({self._last_seq} -> {pkt.seq})")
            # Track the highest sequence seen so a single reorder does not
            # report every following packet as a gap.
            if self._last_seq is None or (pkt.seq - self._last_seq) % SEQ_MOD < SEQ_MOD // 2:
                self._last_seq = pkt.seq

            if self._last_ts is not None:
                ts_delta = (pkt.timestamp - self._last_ts) % TS_MOD
                if ts_delta > TS_MOD // 2:
                    self._window.ts_regressions += 1
                    self.total.ts_regressions += 1
                    log(
                        f"RTP: timestamp regression {self._last_ts} -> {pkt.timestamp} "
                        f"(-{TS_MOD - ts_delta} ticks @90kHz)"
                    )
                else:
                    self._last_ts = pkt.timestamp
            else:
                self._last_ts = pkt.timestamp

            payload = pkt.payload
            if payload:
                if len(payload) % TS_PACKET_SIZE != 0 or payload[0] != TS_SYNC_BYTE:
                    self._window.non_ts += 1
                    self.total.non_ts += 1
                self._window.bytes_payload += len(payload)
                self.total.bytes_payload += len(payload)

        if pkt.payload:
            self.writer.write(pkt.payload)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ── the sink itself ──────────────────────────────────────────────────────────

@dataclass
class SinkConfig:
    host: str
    port: int
    rtp_port: int
    rtcp_port: int
    fifo: str
    mkfifo: bool
    play_delay: float
    m3_path: str
    connect_timeout: float
    wire_dump: Optional[str]
    bind_addr: str
    stats_interval: float


class MockSink:
    def __init__(self, cfg: SinkConfig) -> None:
        self.cfg = cfg
        self.stop = threading.Event()
        self.sock: Optional[socket.socket] = None
        self.rfile: Optional[BinaryIO] = None
        self.writer: Optional[PayloadWriter] = None
        self.rtp: Optional[RTPReceiver] = None

        self.m3_body = load_m3_body(cfg.m3_path)

        # The trace's sink numbers its own requests 2, 3, 4 -- it starts one
        # above the source's M1 CSeq rather than from 1. Replay that literally.
        self._cseq = 2
        self._pending: dict[str, str] = {}
        self._write_lock = threading.Lock()
        self._timers: list[threading.Timer] = []
        # shutdown() is reachable from the RTSP thread's finally block and from
        # the signal handler at the same time; without this it runs twice.
        self._shutdown_lock = threading.Lock()
        self._shut_down = False

        self.session: Optional[str] = None
        self.presentation_uri: Optional[str] = None
        self.stream_uri: Optional[str] = None
        self.play_sent = False
        self.play_sent_at: Optional[float] = None
        self._reporter: Optional[threading.Thread] = None
        self._negotiated: dict[str, str] = {}

    # ── wire I/O ────────────────────────────────────────────────────────────

    def _dump(self, direction: str, text: str) -> None:
        if not self.cfg.wire_dump:
            return
        path = os.path.expanduser(os.path.expandvars(self.cfg.wire_dump))
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
            log(f"wire dump DISABLED: cannot write {path!r}: {exc}")
            self.cfg.wire_dump = None

    def _send(self, text: str) -> None:
        sock = self.sock
        if sock is None:
            return
        with self._write_lock:
            self._dump("TX", text)
            try:
                sock.sendall(text.encode("utf-8"))
            except OSError as exc:
                log(f"send failed: {exc}")
                self.stop.set()

    def _respond(self, msg: RTSPMessage, status: str = "200 OK",
                 headers: Optional[list[tuple[str, str]]] = None,
                 body: str = "") -> None:
        lines = [f"{RTSP_VERSION} {status}", f"CSeq: {msg.cseq}"]
        for key, value in (headers or []):
            lines.append(f"{key}: {value}")
        if body:
            lines.append(f"Content-Length: {len(body.encode('utf-8'))}")
            lines.append("Content-Type: text/parameters")
        lines.append("")
        lines.append(body)
        self._send(CRLF.join(lines))
        label = msg.method or msg.status
        log(f"-> {status} for {label} (CSeq {msg.cseq})")

    def _request(self, name: str, method: str, uri: str,
                 headers: Optional[list[tuple[str, str]]] = None,
                 body: str = "") -> None:
        cseq = str(self._cseq)
        self._cseq += 1
        self._pending[cseq] = name

        lines = [f"{method} {uri} {RTSP_VERSION}", f"CSeq: {cseq}"]
        for key, value in (headers or []):
            lines.append(f"{key}: {value}")
        if body:
            lines.append("Content-Type: text/parameters")
            lines.append(f"Content-Length: {len(body.encode('utf-8'))}")
        lines.append("")
        lines.append(body)
        self._send(CRLF.join(lines))
        log(f"-> {name}: {method} {uri} (CSeq {cseq})")

    # ── connection ──────────────────────────────────────────────────────────

    def _connect(self) -> socket.socket:
        deadline = time.monotonic() + self.cfg.connect_timeout
        last: Optional[OSError] = None
        attempt = 0
        while not self.stop.is_set():
            attempt += 1
            try:
                sock = socket.create_connection(
                    (self.cfg.host, self.cfg.port), timeout=5.0
                )
                sock.settimeout(None)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                log(
                    f"RTSP connected to {self.cfg.host}:{self.cfg.port} "
                    f"from {sock.getsockname()[0]}:{sock.getsockname()[1]}"
                    + (f" (attempt {attempt})" if attempt > 1 else "")
                )
                return sock
            except OSError as exc:
                last = exc
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.25)
        raise SystemExit(
            f"[mock-sink] ERROR: could not connect to "
            f"{self.cfg.host}:{self.cfg.port} within "
            f"{self.cfg.connect_timeout:.1f}s: {last}"
        )

    # ── message handling ────────────────────────────────────────────────────

    def _handle_request(self, msg: RTSPMessage) -> None:
        method = msg.method

        if method == "OPTIONS":
            # M1. Answer with the sink's shorter Public list, then M2: our own
            # OPTIONS. The source only sends M3 after our M2 gets its 200.
            self._respond(
                msg,
                headers=[("Date", rtsp_date()), ("Public", SINK_PUBLIC)],
            )
            self._request(
                "M2_OPTIONS", "OPTIONS", "*",
                headers=[("Require", "org.wfa.wfd1.0")],
            )
            return

        if method == "GET_PARAMETER":
            if msg.body.strip():
                self._handle_m3(msg)
            else:
                # M16 keepalive: bodyless GET_PARAMETER, bare 200 forever.
                self._respond(msg)
            return

        if method == "SET_PARAMETER":
            params = parse_parameters(msg.body)
            if "wfd_trigger_method" in params:
                self._handle_m5(msg, params)
            else:
                self._handle_m4(msg, params)
            return

        if method == "TEARDOWN":
            self._respond(msg, headers=[("Connection", "close")])
            log("source sent TEARDOWN; shutting down")
            self.stop.set()
            return

        log(f"unhandled request {method!r}; answering 405")
        self._respond(msg, status="405 Method Not Allowed")

    def _handle_m3(self, msg: RTSPMessage) -> None:
        requested = [
            line.strip() for line in msg.body.splitlines() if line.strip()
        ]
        log(f"<- M3 GET_PARAMETER, source asked for: {', '.join(requested)}")
        body_len = len(self.m3_body.encode("utf-8"))
        if body_len != EXPECTED_M3_LENGTH:
            log(
                f"WARNING: M3 body is {body_len} bytes, the captured session "
                f"used {EXPECTED_M3_LENGTH}; {self.cfg.m3_path} has drifted"
            )
        self._respond(msg, body=self.m3_body)
        for line in self.m3_body.splitlines():
            log(f"   M3 {line}")
        if self.cfg.rtp_port != 19900:
            log(
                f"NOTE: M3 advertises wfd_client_rtp_ports 19900 verbatim from "
                f"the capture, but SETUP will request client_port="
                f"{self.cfg.rtp_port}; the source honours the SETUP value."
            )

    def _handle_m4(self, msg: RTSPMessage, params: dict[str, str]) -> None:
        log("<- M4 SET_PARAMETER, negotiated by the source:")
        for key in (
            "wfd_video_formats",
            "wfd_audio_codecs",
            "wfd_content_protection",
            "wfd_presentation_url",
            "wfd_client_rtp_ports",
            "wfd_uibc_capability",
            "wfd_uibc_setting",
        ):
            if key in params:
                log(f"   {key}: {params[key]}")
                self._negotiated[key] = params[key]

        video = params.get("wfd_video_formats", "")
        if video:
            self._describe_video_format(video)
        audio = params.get("wfd_audio_codecs", "")
        if audio:
            self._describe_audio_codec(audio)

        presentation = params.get("wfd_presentation_url", "")
        if presentation:
            self.presentation_uri = presentation.split()[0]
            self.stream_uri = strip_uri_port(self.presentation_uri)

        self._respond(msg)

    def _describe_video_format(self, value: str) -> None:
        """
        M4 selects exactly one mode. Its shape is
          <native> <pref-display> <profile> <level> <cea> <vesa> <hh> ...
        with the resolution carried as a one-hot bit in the CEA/VESA/HH mask.
        Decoding it here is what makes the log usable as a diagnostic.
        """
        fields = value.split()
        if len(fields) < 7:
            log(f"   (video format has {len(fields)} fields, cannot decode)")
            return
        try:
            profile = int(fields[2], 16)
            level = int(fields[3], 16)
            cea = int(fields[4], 16)
            vesa = int(fields[5], 16)
            hh = int(fields[6], 16)
        except ValueError:
            log("   (video format fields are not hex, cannot decode)")
            return

        profile_name = {1: "Constrained Baseline", 2: "Constrained High"}.get(
            profile, f"unknown(0x{profile:02x})"
        )
        level_name = {
            1: "3.1", 2: "3.2", 4: "4.0", 8: "4.1", 16: "4.2",
        }.get(level, f"unknown(0x{level:02x})")

        cea_modes = {
            0x0001: "640x480p60", 0x0002: "720x480p60", 0x0004: "720x480i60",
            0x0008: "720x576p50", 0x0010: "720x576i50", 0x0020: "1280x720p30",
            0x0040: "1280x720p60", 0x0080: "1920x1080p30", 0x0100: "1920x1080p60",
            0x0200: "1920x1080i60", 0x0400: "1280x720p25", 0x0800: "1280x720p50",
            0x1000: "1920x1080p25", 0x2000: "1920x1080p50", 0x4000: "1920x1080i50",
            0x8000: "1280x720p24", 0x10000: "1920x1080p24",
        }
        selected = []
        for mask, table, tag in ((cea, cea_modes, "CEA"), (vesa, {}, "VESA"), (hh, {}, "HH")):
            if not mask:
                continue
            bits = [1 << i for i in range(32) if mask & (1 << i)]
            for bit in bits:
                selected.append(table.get(bit, f"{tag} bit 0x{bit:x}"))
        log(
            f"   -> H.264 {profile_name}, level {level_name}, "
            f"mode(s): {', '.join(selected) if selected else 'none'}"
        )

    def _describe_audio_codec(self, value: str) -> None:
        for entry in value.split(","):
            fields = entry.split()
            if not fields:
                continue
            name = fields[0].upper()
            if name == "NONE":
                log("   -> audio disabled by the source")
                return
            if len(fields) < 2:
                continue
            try:
                modes = int(fields[1], 16)
            except ValueError:
                continue
            if name == "AAC":
                table = {
                    0x1: "48kHz 2ch", 0x2: "48kHz 4ch",
                    0x4: "48kHz 6ch", 0x8: "48kHz 8ch",
                }
            elif name == "LPCM":
                table = {0x1: "44.1kHz 2ch", 0x2: "48kHz 2ch"}
            else:
                table = {}
            bits = [
                table.get(1 << i, f"bit {i}")
                for i in range(32)
                if modes & (1 << i)
            ]
            log(f"   -> {name}: {', '.join(bits) if bits else 'no modes set'}")

    def _handle_m5(self, msg: RTSPMessage, params: dict[str, str]) -> None:
        trigger = params.get("wfd_trigger_method", "").strip().upper()
        log(f"<- M5 SET_PARAMETER wfd_trigger_method: {trigger}")
        self._respond(msg)

        if trigger != "SETUP":
            log(f"trigger {trigger!r} is not SETUP; not sending SETUP")
            return

        uri = self.stream_uri or f"rtsp://{self.cfg.host}/wfd1.0/streamid=0"
        if self.cfg.rtcp_port:
            transport = (
                "RTP/AVP/UDP;unicast;"
                f"client_port={self.cfg.rtp_port}-{self.cfg.rtcp_port}"
            )
        else:
            transport = f"RTP/AVP/UDP;unicast;client_port={self.cfg.rtp_port}"
        self._request(
            "M6_SETUP", "SETUP", uri, headers=[("Transport", transport)]
        )

    def _handle_response(self, msg: RTSPMessage) -> None:
        name = self._pending.pop(msg.cseq, "UNKNOWN")
        if not msg.status.startswith("200"):
            log(f"<- {name} FAILED: {msg.start}")
            if name in ("M6_SETUP", "M7_PLAY"):
                self.stop.set()
            return

        log(f"<- 200 OK for {name}")

        if name == "M6_SETUP":
            session = msg.headers.get("session", "")
            self.session = session.partition(";")[0].strip()
            transport = msg.headers.get("transport", "")
            log(f"   Session: {self.session or '(none)'}")
            log(f"   Transport: {transport or '(none)'}")
            if self.cfg.play_delay > 0:
                log(
                    f"   holding PLAY for {self.cfg.play_delay:.3f}s "
                    "(--play-delay: reproducing the real sink's stall)"
                )
                timer = threading.Timer(self.cfg.play_delay, self._send_play)
                timer.daemon = True
                self._timers.append(timer)
                timer.start()
            else:
                self._send_play()
            return

        if name == "M7_PLAY":
            self.play_sent_at = time.monotonic()
            log("   PLAY accepted; expecting RTP on udp/%d" % self.cfg.rtp_port)
            self._start_reporter()
            return

    def _send_play(self) -> None:
        if self.stop.is_set() or self.play_sent:
            return
        self.play_sent = True
        uri = self.stream_uri or f"rtsp://{self.cfg.host}/wfd1.0/streamid=0"
        headers = [("Session", self.session)] if self.session else []
        self._request("M7_PLAY", "PLAY", uri, headers=headers)

    # ── stats ───────────────────────────────────────────────────────────────

    def _start_reporter(self) -> None:
        if self._reporter is not None:
            return
        self._reporter = threading.Thread(
            target=self._report_loop, name="rtp-stats", daemon=True
        )
        self._reporter.start()

    def _report_loop(self) -> None:
        assert self.rtp is not None
        started = time.monotonic()
        interval = self.cfg.stats_interval
        next_tick = started + interval
        silent_ticks = 0
        while not self.stop.wait(max(0.0, next_tick - time.monotonic())):
            now = time.monotonic()
            next_tick += interval
            if next_tick < now:
                next_tick = now + interval
            window = self.rtp.take_window()
            mbits = (window.bytes_wire * 8) / (interval * 1_000_000.0)
            flags = []
            if window.lost:
                flags.append(f"lost={window.lost}")
            if window.reordered:
                flags.append(f"reorder={window.reordered}")
            if window.duplicates:
                flags.append(f"dup={window.duplicates}")
            if window.ts_regressions:
                flags.append(f"ts-regress={window.ts_regressions}")
            if window.malformed:
                flags.append(f"malformed={window.malformed}")
            if window.non_ts:
                flags.append(f"non-ts={window.non_ts}")
            if self.writer is not None and self.writer.dropped_bytes:
                flags.append(f"dropped={self.writer.dropped_bytes}B")
            suffix = ("  " + " ".join(flags)) if flags else ""
            log(
                f"RTP t=+{now - started:5.1f}s  "
                f"{window.datagrams:6d} dgram  "
                f"{window.bytes_payload / 1024.0:9.1f} KiB TS  "
                f"{mbits:7.3f} Mbit/s{suffix}"
            )
            if window.datagrams == 0:
                silent_ticks += 1
                if silent_ticks in (3, 10, 30):
                    log(
                        f"RTP: NO DATAGRAMS for {silent_ticks * interval:.0f}s on "
                        f"udp/{self.cfg.rtp_port} -- the source is not sending"
                    )
            else:
                silent_ticks = 0
            if self.writer is not None:
                self.writer.flush()

    def _final_report(self) -> None:
        if self.rtp is None:
            return
        t = self.rtp.total
        if t.datagrams == 0:
            log("RTP total: no datagrams received")
            return
        span = 0.0
        if self.rtp.first_packet_at is not None and self.rtp.last_packet_at is not None:
            span = self.rtp.last_packet_at - self.rtp.first_packet_at
        rate = (t.bytes_wire * 8) / (span * 1_000_000.0) if span > 0 else 0.0
        log(
            f"RTP total: {t.datagrams} dgram, "
            f"{t.bytes_payload / 1048576.0:.2f} MiB TS payload, "
            f"{t.bytes_wire / 1048576.0:.2f} MiB on the wire, "
            f"{span:.1f}s, {rate:.3f} Mbit/s avg"
        )
        log(
            f"RTP total: lost={t.lost} reorder={t.reordered} dup={t.duplicates} "
            f"ts-regress={t.ts_regressions} malformed={t.malformed} "
            f"non-ts-payload={t.non_ts}"
        )
        if t.bytes_payload and t.bytes_payload % TS_PACKET_SIZE == 0:
            log(f"RTP total: {t.bytes_payload // TS_PACKET_SIZE} MPEG-TS packets")

    # ── lifecycle ───────────────────────────────────────────────────────────

    def run(self) -> int:
        self.writer = PayloadWriter(self.cfg.fifo, self.cfg.mkfifo)
        self.rtp = RTPReceiver(
            self.cfg.bind_addr, self.cfg.rtp_port, self.writer, self.stop
        )
        self.rtp.start()
        log(f"RTP listening on {self.cfg.bind_addr}:{self.cfg.rtp_port}/udp")

        self.sock = self._connect()
        self.rfile = self.sock.makefile("rb")

        try:
            while not self.stop.is_set():
                msg = read_rtsp_message(self.rfile)
                if msg is None:
                    log("source closed the RTSP connection")
                    break
                self._dump("RX", msg.wire())
                if msg.is_response:
                    self._handle_response(msg)
                else:
                    self._handle_request(msg)
        except OSError as exc:
            if not self.stop.is_set():
                log(f"RTSP socket error: {exc}")
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._shut_down:
                self.stop.set()
                return
            self._shut_down = True
        self.stop.set()
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.rtp is not None:
            self.rtp.close()
        self._final_report()
        if self.writer is not None:
            self.writer.close()


# ── entry point ──────────────────────────────────────────────────────────────

def parse_args(argv: Optional[list[str]] = None) -> SinkConfig:
    parser = argparse.ArgumentParser(
        prog="mock-sink.py",
        description=(
            "Loopback Wi-Fi Display sink: replays the sink half of the captured "
            "Xiaomi/Google TV session so RTSP and MPEG-TS work can be done "
            "without the TV."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "The MPEG-TS payload goes to --fifo (stdout by default); every "
            "diagnostic goes to stderr, so `mock-sink.py | ffplay -` works."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="RTSP host of the WFD source to connect to")
    parser.add_argument("--port", type=int, default=7236,
                        help="RTSP port of the WFD source")
    parser.add_argument("--rtp-port", type=int, default=19900, dest="rtp_port",
                        help="UDP port to receive RTP on, sent as client_port in SETUP")
    parser.add_argument("--rtcp-port", type=int, default=0, dest="rtcp_port",
                        help="RTCP port to advertise in SETUP; 0 means none, "
                             "matching the captured session")
    parser.add_argument("--bind-addr", default="0.0.0.0", dest="bind_addr",
                        help="Local address to bind the RTP socket to")
    parser.add_argument("--fifo", default="-",
                        help="Where to write the de-RTP'd MPEG-TS: a path, or "
                             "'-' for stdout")
    parser.add_argument("--mkfifo", action="store_true",
                        help="Create --fifo as a named pipe if it does not exist")
    parser.add_argument("--play-delay", type=float, default=0.0, dest="play_delay",
                        help="Seconds to wait after the SETUP 200 before sending "
                             "PLAY; the real sink stalled 8.024s here")
    parser.add_argument("--m3-body", default=DEFAULT_M3_PATH, dest="m3_path",
                        help="File holding the captured M3 capability body")
    parser.add_argument("--connect-timeout", type=float, default=10.0,
                        dest="connect_timeout",
                        help="Seconds to keep retrying the TCP connect")
    parser.add_argument("--wire-dump", default=None, dest="wire_dump",
                        help="Append every RTSP message to this file, in the same "
                             "format as $HYPRCAST_RTSP_DUMP")
    parser.add_argument("--stats-interval", type=float, default=1.0,
                        dest="stats_interval",
                        help="Seconds between RTP throughput reports")
    args = parser.parse_args(argv)

    if args.stats_interval <= 0:
        parser.error("--stats-interval must be positive")
    if not 1 <= args.rtp_port <= 65535:
        parser.error("--rtp-port must be 1..65535")
    if args.play_delay < 0:
        parser.error("--play-delay must not be negative")

    return SinkConfig(
        host=args.host,
        port=args.port,
        rtp_port=args.rtp_port,
        rtcp_port=args.rtcp_port,
        fifo=args.fifo,
        mkfifo=args.mkfifo,
        play_delay=args.play_delay,
        m3_path=args.m3_path,
        connect_timeout=args.connect_timeout,
        wire_dump=args.wire_dump,
        bind_addr=args.bind_addr,
        stats_interval=args.stats_interval,
    )


def main(argv: Optional[list[str]] = None) -> int:
    cfg = parse_args(argv)
    try:
        sink = MockSink(cfg)
    except (OSError, ValueError) as exc:
        log(f"ERROR: {exc}")
        return 1

    def on_signal(signum, _frame):
        log(f"caught {signal.Signals(signum).name}; shutting down")
        sink.shutdown()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        return sink.run()
    except SystemExit as exc:
        sink.shutdown()
        if exc.code and isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 1
        return int(exc.code or 0)


if __name__ == "__main__":
    sys.exit(main())
