#!/usr/bin/env python3
"""assert-ts.py -- validate an MPEG-TS stream against what a Wi-Fi Display sink requires.

Reads a raw MPEG-TS byte stream from a file or stdin and checks the things that
an AOSP-derived Miracast sink (ATSParser.cpp / TSPacketizer.cpp) actually cares
about.  Everything checked here was cross-read against the vendored sink source
in reference/aosp/, not against the ISO 13818-1 text alone: the sink is a
narrower, crankier parser than the standard, and several of its CHECK_* macros
are hard aborts rather than error returns.

The input must be a raw 188-byte MPEG-TS byte stream. fluxcast's transmit path
uses ffmpeg's rtp_mpegts muxer, so a wire capture has to have its 12-byte RTP
headers stripped first; point this tool at the mpegts muxer's output, or at the
payload after de-RTP.

Exit status: 0 if every enabled check passed, 1 if any failed, 2 on usage or
I/O errors.

Usage:
    tools/assert-ts.py capture.ts
    ffmpeg ... -f mpegts - | tools/assert-ts.py -
    tools/assert-ts.py --pmt-pid 0x1000 --video-pid 0x1011 --level 3.2 capture.ts
"""

from __future__ import annotations

import argparse
import statistics
import sys

from dataclasses import dataclass, field
from typing import BinaryIO, Iterator, Optional

# --------------------------------------------------------------------------
# MPEG-TS constants
# --------------------------------------------------------------------------

TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47
PID_PAT = 0x0000
PID_NULL = 0x1FFF

TABLE_ID_PAT = 0x00
TABLE_ID_PMT = 0x02

# Stream types the sink understands (ATSParser::STREAMTYPE_*).
STREAMTYPE_H264 = 0x1B
STREAMTYPE_AAC_ADTS = 0x0F
STREAMTYPE_LPCM_WFD = 0x83  # the WIDI/WFD LPCM type, not Blu-ray 0x8B

STREAM_TYPE_NAMES = {
    0x0F: "AAC (ADTS)",
    0x11: "AAC (LATM)",
    0x1B: "H.264",
    0x24: "HEVC",
    0x81: "AC-3",
    0x83: "LPCM (WFD/WIDI)",
    0x8B: "LPCM (Blu-ray)",
}

# PID layout that AOSP's own TSPacketizer emits.  See the PID LAYOUT note at
# the bottom of this file for why these are advisory, not mandatory.
AOSP_PMT_PID = 0x0100
AOSP_PCR_PID = 0x1000
AOSP_VIDEO_PID = 0x1011
AOSP_AUDIO_PID = 0x1100

PCR_HZ = 27_000_000
PTS_HZ = 90_000
PCR_BASE_WRAP = 1 << 33
PTS_WRAP = 1 << 33

READ_CHUNK = 1 << 20

# H.264 NAL unit types (Rec. ITU-T H.264 Table 7-1).
NAL_SLICE = 1
NAL_IDR = 5
NAL_SEI = 6
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9
NAL_END_SEQ = 10
NAL_END_STREAM = 11
NAL_FILLER = 12
NAL_SPS_EXT = 13
NAL_PREFIX = 14
NAL_SUBSET_SPS = 15

# NAL types that may legitimately sit between the parameter sets and the IDR
# slice inside one access unit without breaking "SPS and PPS immediately
# precede the IDR".
NAL_ALLOWED_BEFORE_IDR = frozenset(
    {NAL_AUD, NAL_SEI, NAL_SPS, NAL_PPS, NAL_SPS_EXT, NAL_PREFIX,
     NAL_SUBSET_SPS, NAL_FILLER}
)


# --------------------------------------------------------------------------
# H.264 level limits -- Rec. ITU-T H.264 Table A-1.
# MaxBR/MaxCPB are given for the VCL HRD with cpbBrVclFactor = 1000, i.e. the
# Baseline / Constrained Baseline numbers, which is all a WFD sink negotiates.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LevelLimits:
    name: str
    level_idc: int
    max_mbps: int      # macroblocks per second
    max_fs: int        # frame size in macroblocks
    max_br_kbps: int   # VCL bitrate, kbit/s
    max_cpb_kbits: int
    min_cr: int        # minimum compression ratio

    @property
    def max_au_bytes(self) -> int:
        """Absolute cap on the byte size of one coded picture at this level.

        H.264 A.3.1(g): the number of bytes in an access unit shall not exceed
        384 * MaxFS / MinCR.  A PES packet on the video PID carries exactly one
        access unit, so this is the bound the 'no PES exceeds the level max
        frame size' check enforces.
        """
        return 384 * self.max_fs // self.min_cr


LEVELS: dict[str, LevelLimits] = {
    "1.0": LevelLimits("1.0", 10, 1485, 99, 64, 175, 2),
    "1b": LevelLimits("1b", 11, 1485, 99, 128, 350, 2),
    "1.1": LevelLimits("1.1", 11, 3000, 396, 192, 500, 2),
    "1.2": LevelLimits("1.2", 12, 6000, 396, 384, 1000, 2),
    "1.3": LevelLimits("1.3", 13, 11880, 396, 768, 2000, 2),
    "2.0": LevelLimits("2.0", 20, 11880, 396, 2000, 2000, 2),
    "2.1": LevelLimits("2.1", 21, 19800, 792, 4000, 4000, 2),
    "2.2": LevelLimits("2.2", 22, 20250, 1620, 4000, 4000, 2),
    "3.0": LevelLimits("3.0", 30, 40500, 1620, 10000, 10000, 2),
    "3.1": LevelLimits("3.1", 31, 108000, 3600, 14000, 14000, 4),
    "3.2": LevelLimits("3.2", 32, 216000, 5120, 20000, 20000, 4),
    "4.0": LevelLimits("4.0", 40, 245760, 8192, 20000, 25000, 4),
    "4.1": LevelLimits("4.1", 41, 245760, 8192, 50000, 62500, 2),
    "4.2": LevelLimits("4.2", 42, 522240, 8704, 50000, 62500, 2),
    "5.0": LevelLimits("5.0", 50, 589824, 22080, 135000, 135000, 2),
    "5.1": LevelLimits("5.1", 51, 983040, 36864, 240000, 240000, 2),
    "5.2": LevelLimits("5.2", 52, 2073600, 36864, 240000, 240000, 2),
}

# Levels a WFD sink can advertise (VideoFormats::kLevelIDC).
WFD_LEVELS = ("3.1", "3.2", "4.0", "4.1", "4.2")

LEVEL_BY_IDC = {lim.level_idc: name for name, lim in reversed(list(LEVELS.items()))}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def fmt_pid(pid: int) -> str:
    return f"0x{pid:04X}"


def fmt_bytes(n: int) -> str:
    return f"{n:,}"


def crc32_mpeg(data: bytes) -> int:
    """MPEG-2 section CRC-32: poly 0x04C11DB7, init 0xFFFFFFFF, no reflection.

    Byte-for-byte the algorithm in TSPacketizer::crc32().
    """
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def unwrap(prev_unwrapped: Optional[int], raw: int, modulus: int) -> int:
    """Extend a wrapping counter to a monotonically growing value.

    Only treats a large backwards jump as a wrap; a small backwards jump is a
    genuine ordering error and must stay visible to the monotonicity check.
    """
    if prev_unwrapped is None:
        return raw
    base = prev_unwrapped - (prev_unwrapped % modulus)
    candidate = base + raw
    if candidate < prev_unwrapped - modulus // 2:
        candidate += modulus
    return candidate


class BitReader:
    """Just enough bit reading for an H.264 SPS."""

    __slots__ = ("data", "pos", "nbits")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.nbits = len(data) * 8

    def u(self, n: int) -> int:
        if self.pos + n > self.nbits:
            raise ValueError("SPS truncated")
        value = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]
            bit = (byte >> (7 - (self.pos & 7))) & 1
            value = (value << 1) | bit
            self.pos += 1
        return value

    def ue(self) -> int:
        leading = 0
        while self.u(1) == 0:
            leading += 1
            if leading > 32:
                raise ValueError("SPS Exp-Golomb runaway")
        if leading == 0:
            return 0
        return (1 << leading) - 1 + self.u(leading)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k % 2 else -(k // 2)


def rbsp(data: bytes) -> bytes:
    """Strip emulation prevention bytes (00 00 03 -> 00 00)."""
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0x00 else 0
    return bytes(out)


@dataclass
class SPSInfo:
    profile_idc: int
    constraint_flags: int
    level_idc: int
    width: int
    height: int
    width_mbs: int
    height_mbs: int

    @property
    def pic_size_in_mbs(self) -> int:
        return self.width_mbs * self.height_mbs

    @property
    def is_constrained_baseline(self) -> bool:
        # Constrained Baseline == profile_idc 66 with constraint_set1_flag, or
        # 77/88/100 with constraint_set1_flag set (Annex A.2.1.1).
        return bool(self.constraint_flags & 0x40) or self.profile_idc == 66


def parse_sps(nal_payload: bytes) -> Optional[SPSInfo]:
    """Parse an SPS NAL (payload starts at profile_idc, NAL header removed)."""
    data = rbsp(nal_payload)
    if len(data) < 4:
        return None
    profile_idc = data[0]
    constraint_flags = data[1]
    level_idc = data[2]
    try:
        br = BitReader(data[3:])
        br.ue()  # seq_parameter_set_id
        chroma_format_idc = 1
        if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            chroma_format_idc = br.ue()
            if chroma_format_idc == 3:
                br.u(1)  # separate_colour_plane_flag
            br.ue()  # bit_depth_luma_minus8
            br.ue()  # bit_depth_chroma_minus8
            br.u(1)  # qpprime_y_zero_transform_bypass_flag
            if br.u(1):  # seq_scaling_matrix_present_flag
                count = 8 if chroma_format_idc != 3 else 12
                for i in range(count):
                    if br.u(1):
                        size = 16 if i < 6 else 64
                        last = 8
                        next_scale = 8
                        for _ in range(size):
                            if next_scale != 0:
                                delta = br.se()
                                next_scale = (last + delta + 256) % 256
                            last = next_scale if next_scale != 0 else last
        br.ue()  # log2_max_frame_num_minus4
        poc_type = br.ue()
        if poc_type == 0:
            br.ue()
        elif poc_type == 1:
            br.u(1)
            br.se()
            br.se()
            for _ in range(br.ue()):
                br.se()
        br.ue()  # max_num_ref_frames
        br.u(1)  # gaps_in_frame_num_value_allowed_flag
        width_mbs = br.ue() + 1
        height_map_units = br.ue() + 1
        frame_mbs_only = br.u(1)
        if not frame_mbs_only:
            br.u(1)  # mb_adaptive_frame_field_flag
        br.u(1)  # direct_8x8_inference_flag
        crop_l = crop_r = crop_t = crop_b = 0
        if br.u(1):  # frame_cropping_flag
            crop_l = br.ue()
            crop_r = br.ue()
            crop_t = br.ue()
            crop_b = br.ue()
    except (ValueError, IndexError):
        return None

    height_mbs = height_map_units * (2 - frame_mbs_only)
    sub_w = 2 if chroma_format_idc in (1, 2) else 1
    sub_h = 2 if chroma_format_idc == 1 else 1
    crop_unit_x = sub_w if chroma_format_idc != 0 else 1
    crop_unit_y = (sub_h if chroma_format_idc != 0 else 1) * (2 - frame_mbs_only)
    width = width_mbs * 16 - (crop_l + crop_r) * crop_unit_x
    height = height_mbs * 16 - (crop_t + crop_b) * crop_unit_y
    return SPSInfo(profile_idc, constraint_flags, level_idc,
                   width, height, width_mbs, height_mbs)


def iter_nals(es: bytes) -> Iterator[tuple[int, int, int, int]]:
    """Yield (nal_unit_type, nal_ref_idc, start, end) for each Annex-B NAL."""
    n = len(es)
    i = es.find(b"\x00\x00\x01")
    while i >= 0:
        start = i + 3
        nxt = es.find(b"\x00\x00\x01", start)
        end = n if nxt < 0 else nxt
        # A 4-byte start code (00 00 00 01) shows up here as a trailing zero on
        # the previous NAL; trim trailing zero bytes so sizes stay honest.
        while end > start and es[end - 1] == 0x00:
            end -= 1
        if start < n:
            header = es[start]
            yield (header & 0x1F, (header >> 5) & 0x03, start, end)
        i = nxt


# --------------------------------------------------------------------------
# Packet-level reader with resync
# --------------------------------------------------------------------------


def iter_ts(fh: BinaryIO) -> Iterator[tuple[str, int, int, bytes]]:
    """Yield ('pkt', file_offset, packet_index, data) and
    ('lost', file_offset, byte_count, b'') sync-loss events.

    Resync requires three consecutive 0x47 bytes at 188-byte stride so a random
    0x47 inside a PES payload cannot fake a packet boundary.
    """
    buf = bytearray()
    base = 0
    pos = 0
    synced = False
    index = 0
    eof = False

    def compact() -> None:
        nonlocal base, pos
        if pos:
            del buf[:pos]
            base += pos
            pos = 0

    while True:
        if not eof:
            chunk = fh.read(READ_CHUNK)
            if chunk:
                buf += chunk
            else:
                eof = True

        while True:
            available = len(buf) - pos
            if synced:
                if available >= TS_PACKET_SIZE:
                    if buf[pos] == TS_SYNC_BYTE:
                        yield ("pkt", base + pos, index, bytes(buf[pos:pos + TS_PACKET_SIZE]))
                        index += 1
                        pos += TS_PACKET_SIZE
                        continue
                    synced = False
                    continue
                if eof:
                    if available:
                        yield ("lost", base + pos, available, b"")
                        pos = len(buf)
                    return
                break
            else:
                found = _find_sync(buf, pos, eof)
                if found == -2:
                    if eof:
                        found = -1
                    else:
                        break
                if found < 0:
                    # Nothing usable; drop everything but a packet of tail so a
                    # boundary straddling the chunk edge is not lost.
                    keep = min(len(buf) - pos, 2 * TS_PACKET_SIZE)
                    drop = len(buf) - pos - keep
                    if drop > 0:
                        yield ("lost", base + pos, drop, b"")
                        pos += drop
                    if eof:
                        if len(buf) - pos:
                            yield ("lost", base + pos, len(buf) - pos, b"")
                        return
                    break
                if found > pos:
                    yield ("lost", base + pos, found - pos, b"")
                    pos = found
                synced = True
                continue
        compact()
        if eof and len(buf) == 0:
            return


def _find_sync(buf: bytearray, start: int, eof: bool) -> int:
    """Return the offset of a confirmed packet boundary, -1 none, -2 need data."""
    n = len(buf)
    i = start
    while i < n:
        j = buf.find(TS_SYNC_BYTE, i)
        if j < 0:
            return -1
        confirmed = 0
        k = j
        ok = True
        while confirmed < 3:
            k2 = k + TS_PACKET_SIZE
            if k2 >= n:
                if eof:
                    break
                return -2
            if buf[k2] != TS_SYNC_BYTE:
                ok = False
                break
            k = k2
            confirmed += 1
        if ok:
            return j
        i = j + 1
    return -1


# --------------------------------------------------------------------------
# PSI section reassembly
# --------------------------------------------------------------------------


class SectionAssembler:
    __slots__ = ("buf", "active")

    def __init__(self) -> None:
        self.buf = bytearray()
        self.active = False

    def feed(self, payload: bytes, pusi: bool) -> list[bytes]:
        out: list[bytes] = []
        if pusi:
            if not payload:
                return out
            pointer = payload[0]
            tail = payload[1:1 + pointer]
            if self.active and tail:
                self._absorb(tail, out)
            self.buf.clear()
            self.active = True
            rest = payload[1 + pointer:]
        else:
            if not self.active:
                return out
            rest = payload
        self._absorb(rest, out)
        return out

    def _absorb(self, data: bytes, out: list[bytes]) -> None:
        self.buf += data
        while len(self.buf) >= 3:
            if self.buf[0] == 0xFF:  # stuffing to end of packet
                self.buf.clear()
                return
            section_length = ((self.buf[1] & 0x0F) << 8) | self.buf[2]
            total = section_length + 3
            if len(self.buf) < total:
                return
            out.append(bytes(self.buf[:total]))
            del self.buf[:total]


@dataclass
class PMTInfo:
    pcr_pid: int
    streams: list[tuple[int, int]]  # (stream_type, elementary_pid)
    version: int


def parse_pat(section: bytes) -> Optional[list[tuple[int, int]]]:
    if len(section) < 12 or section[0] != TABLE_ID_PAT:
        return None
    section_length = ((section[1] & 0x0F) << 8) | section[2]
    end = 3 + section_length - 4
    programs: list[tuple[int, int]] = []
    i = 8
    while i + 4 <= end:
        program_number = (section[i] << 8) | section[i + 1]
        pid = ((section[i + 2] & 0x1F) << 8) | section[i + 3]
        programs.append((program_number, pid))
        i += 4
    return programs


def parse_pmt(section: bytes) -> Optional[PMTInfo]:
    if len(section) < 16 or section[0] != TABLE_ID_PMT:
        return None
    section_length = ((section[1] & 0x0F) << 8) | section[2]
    end = 3 + section_length - 4
    version = (section[5] >> 1) & 0x1F
    pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    i = 12 + program_info_length
    streams: list[tuple[int, int]] = []
    while i + 5 <= end:
        stream_type = section[i]
        pid = ((section[i + 1] & 0x1F) << 8) | section[i + 2]
        es_info_length = ((section[i + 3] & 0x0F) << 8) | section[i + 4]
        streams.append((stream_type, pid))
        i += 5 + es_info_length
    return PMTInfo(pcr_pid, streams, version)


# --------------------------------------------------------------------------
# PES
# --------------------------------------------------------------------------

PES_NO_HEADER_IDS = frozenset({0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xFF, 0xF2, 0xF8})


@dataclass
class PESRecord:
    pid: int
    offset: int             # file offset of the packet carrying the PES header
    index: int              # packet index of that packet
    stream_id: int
    length_field: int
    pts: Optional[int]      # 90 kHz, raw 33-bit
    dts: Optional[int]
    es_len: int
    errors: list[str] = field(default_factory=list)
    # video-only
    nal_types: list[int] = field(default_factory=list)
    has_idr: bool = False
    sps_before_idr: bool = False
    pps_before_idr: bool = False
    interlopers: list[int] = field(default_factory=list)
    params_only: bool = False
    sps: Optional[SPSInfo] = None
    # audio-only
    adts_ok: bool = True


def _read_timestamp(b: bytes) -> tuple[int, bool]:
    """Decode a 5-byte PTS/DTS field; returns (value, marker_bits_ok)."""
    ok = bool(b[0] & 0x01) and bool(b[2] & 0x01) and bool(b[4] & 0x01)
    value = (((b[0] >> 1) & 0x07) << 30) | (b[1] << 22) | (((b[2] >> 1) & 0x7F) << 15) \
        | (b[3] << 7) | ((b[4] >> 1) & 0x7F)
    return value, ok


def parse_pes(pid: int, offset: int, index: int, buf: bytes) -> PESRecord:
    rec = PESRecord(pid, offset, index, 0, 0, None, None, 0)
    if len(buf) < 6:
        rec.errors.append(f"PES fragment only {len(buf)} bytes, no header")
        return rec
    if buf[0:3] != b"\x00\x00\x01":
        # ATSParser::parsePES returns ERROR_MALFORMED here, then CHECK_EQ aborts
        # in the version that reaches the CHECK first -- either way the stream
        # is unusable for the sink.
        rec.errors.append(
            f"missing PES start code (got {buf[0]:02x} {buf[1]:02x} {buf[2]:02x})")
        return rec
    rec.stream_id = buf[3]
    rec.length_field = (buf[4] << 8) | buf[5]
    if rec.stream_id in PES_NO_HEADER_IDS:
        rec.es_len = max(0, len(buf) - 6)
        return rec
    if len(buf) < 9:
        rec.errors.append("PES header truncated before flags")
        return rec
    if (buf[6] & 0xC0) != 0x80:
        # ATSParser: CHECK_EQ(br->getBits(2), 2u) -- a hard abort in the sink.
        rec.errors.append(f"PES flag marker bits are {(buf[6] >> 6) & 3}, must be 2 "
                          "(ATSParser CHECK_EQ aborts the sink process)")
    pts_dts_flags = (buf[7] >> 6) & 0x03
    header_data_length = buf[8]
    es_start = 9 + header_data_length
    if es_start > len(buf):
        rec.errors.append("PES_header_data_length runs past the packet payload")
        es_start = len(buf)
    cursor = 9
    if pts_dts_flags in (2, 3):
        if cursor + 5 <= len(buf):
            pts, ok = _read_timestamp(buf[cursor:cursor + 5])
            if (buf[cursor] >> 4) != pts_dts_flags:
                rec.errors.append(
                    f"PTS prefix nibble is {buf[cursor] >> 4:#x}, must equal "
                    f"PTS_DTS_flags {pts_dts_flags}")
            if not ok:
                rec.errors.append("PTS marker bits not all 1 "
                                  "(ATSParser CHECK_EQ aborts the sink process)")
            rec.pts = pts
            cursor += 5
        else:
            rec.errors.append("PTS field truncated")
    elif pts_dts_flags == 1:
        rec.errors.append("PTS_DTS_flags == 1 is forbidden")
    if pts_dts_flags == 3:
        if cursor + 5 <= len(buf):
            dts, ok = _read_timestamp(buf[cursor:cursor + 5])
            if (buf[cursor] >> 4) != 1:
                rec.errors.append(f"DTS prefix nibble is {buf[cursor] >> 4:#x}, must be 1")
            if not ok:
                rec.errors.append("DTS marker bits not all 1")
            rec.dts = dts
        else:
            rec.errors.append("DTS field truncated")
    if rec.length_field != 0 and rec.length_field < header_data_length + 3:
        # ATSParser: CHECK_GE(PES_packet_length, PES_header_data_length + 3)
        rec.errors.append(
            f"PES_packet_length {rec.length_field} < PES_header_data_length+3 "
            f"{header_data_length + 3} (ATSParser CHECK_GE aborts the sink process)")
    rec.es_len = len(buf) - es_start
    rec._es = buf[es_start:]  # type: ignore[attr-defined]
    return rec


# --------------------------------------------------------------------------
# Analysis state
# --------------------------------------------------------------------------


@dataclass
class SyncLoss:
    offset: int
    count: int


@dataclass
class CCGap:
    offset: int
    index: int
    pid: int
    expected: int
    got: int
    duplicate: bool


@dataclass
class PCRSample:
    index: int
    offset: int
    pid: int
    raw: int          # 27 MHz, base*300+ext
    unwrapped: int


@dataclass
class TableSighting:
    index: int
    offset: int
    crc_ok: bool


@dataclass
class State:
    total_packets: int = 0
    total_bytes: int = 0
    input_bytes: int = 0
    sync_losses: list[SyncLoss] = field(default_factory=list)
    tei_packets: list[tuple[int, int, int]] = field(default_factory=list)
    scrambled: list[tuple[int, int, int]] = field(default_factory=list)
    bad_afc: list[tuple[int, int, int]] = field(default_factory=list)
    pid_counts: dict[int, int] = field(default_factory=dict)
    cc_gaps: list[CCGap] = field(default_factory=list)
    pcr_samples: list[PCRSample] = field(default_factory=list)
    pcr_nonmonotonic: list[tuple[int, int, int, int]] = field(default_factory=list)
    pat_sightings: list[TableSighting] = field(default_factory=list)
    pmt_sightings: list[TableSighting] = field(default_factory=list)
    pat_programs: list[tuple[int, int]] = field(default_factory=list)
    pmt: Optional[PMTInfo] = None
    pmt_pid: Optional[int] = None
    pmt_versions: set[int] = field(default_factory=set)
    pes_video: list[PESRecord] = field(default_factory=list)
    pes_audio: list[PESRecord] = field(default_factory=list)
    pes_other: dict[int, int] = field(default_factory=dict)
    sps_seen: Optional[SPSInfo] = None
    sps_variants: set[tuple[int, int, int, int, int]] = field(default_factory=set)


def analyze(fh: BinaryIO, max_records: int) -> State:
    st = State()
    pat_asm = SectionAssembler()
    pmt_asm: dict[int, SectionAssembler] = {}
    cc_expect: dict[int, int] = {}
    last_packet: dict[int, bytes] = {}
    pes_buf: dict[int, bytearray] = {}
    pes_start: dict[int, tuple[int, int]] = {}
    pcr_prev_unwrapped: Optional[int] = None
    orphan: list[tuple[int, int, int, bytes]] = []
    orphan_bytes = 0
    prev_video_params_only = False

    def finish_pes(pid: int) -> None:
        nonlocal orphan_bytes
        buf = pes_buf.pop(pid, None)
        if buf is None:
            return
        offset, index = pes_start.pop(pid, (0, 0))
        raw = bytes(buf)
        role = classify(pid)
        if role is None:
            if orphan_bytes < 8 << 20 and len(orphan) < 4096:
                orphan.append((pid, offset, index, raw))
                orphan_bytes += len(raw)
            return
        _record_pes(role, pid, offset, index, raw)

    def classify(pid: int) -> Optional[str]:
        if st.pmt is None:
            return None
        for stream_type, epid in st.pmt.streams:
            if epid == pid:
                if stream_type == STREAMTYPE_H264:
                    return "video"
                if stream_type in (STREAMTYPE_AAC_ADTS, 0x11):
                    return "audio-aac"
                if stream_type in (STREAMTYPE_LPCM_WFD, 0x8B, 0x81):
                    return "audio-other"
                return "other"
        return "other"

    def _record_pes(role: str, pid: int, offset: int, index: int, raw: bytes) -> None:
        nonlocal prev_video_params_only
        rec = parse_pes(pid, offset, index, raw)
        es: bytes = getattr(rec, "_es", b"")
        if role == "video":
            _scan_video(rec, es)
            if len(st.pes_video) < max_records:
                st.pes_video.append(rec)
            prev_video_params_only = rec.params_only
        elif role.startswith("audio"):
            if role == "audio-aac":
                rec.adts_ok = len(es) >= 2 and es[0] == 0xFF and (es[1] & 0xF0) == 0xF0
            if len(st.pes_audio) < max_records:
                st.pes_audio.append(rec)
        else:
            st.pes_other[pid] = st.pes_other.get(pid, 0) + 1

    def _scan_video(rec: PESRecord, es: bytes) -> None:
        seen_sps = False
        seen_pps = False
        slice_nals = 0
        for nal_type, _ref_idc, start, end in iter_nals(es):
            rec.nal_types.append(nal_type)
            if nal_type == NAL_SPS:
                seen_sps = True
                info = parse_sps(es[start + 1:end])
                if info is not None:
                    rec.sps = info
                    st.sps_variants.add((info.profile_idc, info.constraint_flags,
                                         info.level_idc, info.width, info.height))
                    if st.sps_seen is None:
                        st.sps_seen = info
            elif nal_type == NAL_PPS:
                seen_pps = True
            elif nal_type == NAL_IDR:
                slice_nals += 1
                if not rec.has_idr:
                    rec.has_idr = True
                    # "Immediately precede" is evaluated at the first IDR NAL of
                    # the access unit: the parameter sets must already have been
                    # seen, and nothing but AUD/SEI/filler may sit between them.
                    rec.sps_before_idr = seen_sps
                    rec.pps_before_idr = seen_pps
            elif nal_type == NAL_SLICE:
                slice_nals += 1
            if not rec.has_idr and nal_type not in NAL_ALLOWED_BEFORE_IDR:
                rec.interlopers.append(nal_type)
        if not rec.has_idr:
            # Only meaningful for access units that contain an IDR.
            rec.interlopers.clear()
        rec.params_only = slice_nals == 0 and (seen_sps or seen_pps)
        # A parameter-set-only PES immediately in front of the IDR PES is a
        # legitimate layout (some muxers emit the CSD as its own access unit),
        # so credit it rather than raising a false alarm.
        if rec.has_idr and prev_video_params_only:
            rec.sps_before_idr = True
            rec.pps_before_idr = True

    for kind, offset, arg, data in iter_ts(fh):
        if kind == "lost":
            st.sync_losses.append(SyncLoss(offset, arg))
            st.input_bytes += arg
            continue

        index = arg
        st.total_packets += 1
        st.total_bytes += TS_PACKET_SIZE
        st.input_bytes += TS_PACKET_SIZE

        tei = (data[1] >> 7) & 1
        pusi = (data[1] >> 6) & 1
        pid = ((data[1] & 0x1F) << 8) | data[2]
        scrambling = (data[3] >> 6) & 0x03
        afc = (data[3] >> 4) & 0x03
        cc = data[3] & 0x0F

        st.pid_counts[pid] = st.pid_counts.get(pid, 0) + 1

        if tei:
            st.tei_packets.append((offset, index, pid))
            continue
        if scrambling:
            st.scrambled.append((offset, index, pid))
        if afc == 0:
            st.bad_afc.append((offset, index, pid))
            continue

        payload_start = 4
        if afc in (2, 3):
            af_len = data[4]
            payload_start = 5 + af_len
            if af_len > 0 and payload_start <= TS_PACKET_SIZE:
                flags = data[5]
                if flags & 0x10 and af_len >= 7:
                    pcr_base = (data[6] << 25) | (data[7] << 17) | (data[8] << 9) \
                        | (data[9] << 1) | ((data[10] >> 7) & 1)
                    pcr_ext = ((data[10] & 0x01) << 8) | data[11]
                    unwrapped_base = unwrap(
                        None if pcr_prev_unwrapped is None else pcr_prev_unwrapped // 300,
                        pcr_base, PCR_BASE_WRAP)
                    raw = pcr_base * 300 + pcr_ext
                    unwrapped = unwrapped_base * 300 + pcr_ext
                    if st.pcr_samples and unwrapped < st.pcr_samples[-1].unwrapped:
                        st.pcr_nonmonotonic.append(
                            (offset, index, st.pcr_samples[-1].raw, raw))
                    st.pcr_samples.append(PCRSample(index, offset, pid, raw, unwrapped))
                    pcr_prev_unwrapped = unwrapped
            if payload_start > TS_PACKET_SIZE:
                payload_start = TS_PACKET_SIZE

        has_payload = afc in (1, 3)
        payload = data[payload_start:] if has_payload else b""

        # Continuity counter.  Per ISO 13818-1 2.4.3.3 the counter increments
        # only for packets carrying payload, so adaptation-only packets (the
        # AOSP PCR carrier, afc == 2) are exempt.
        if pid != PID_NULL and has_payload:
            expected = cc_expect.get(pid)
            if expected is not None and cc != expected:
                previous = (expected - 1) & 0x0F
                duplicate = cc == previous and last_packet.get(pid) == data
                # A duplicate packet is legal in ISO 13818-1 but ATSParser
                # treats ANY counter that is not the expected one as a
                # discontinuity: it drops the partially assembled PES and waits
                # for the next payload_unit_start.  So duplicates are reported
                # too, they just get labelled.
                st.cc_gaps.append(CCGap(offset, index, pid, expected, cc, duplicate))
            cc_expect[pid] = (cc + 1) & 0x0F
            last_packet[pid] = data

        if not has_payload or not payload:
            continue

        if pid == PID_PAT:
            for section in pat_asm.feed(payload, bool(pusi)):
                crc_ok = crc32_mpeg(section) == 0
                st.pat_sightings.append(TableSighting(index, offset, crc_ok))
                programs = parse_pat(section)
                if programs:
                    st.pat_programs = programs
                    for _pn, pmt_pid in programs:
                        if pmt_pid != PID_PAT and pmt_pid not in pmt_asm:
                            pmt_asm[pmt_pid] = SectionAssembler()
            continue

        if pid in pmt_asm:
            for section in pmt_asm[pid].feed(payload, bool(pusi)):
                crc_ok = crc32_mpeg(section) == 0
                st.pmt_sightings.append(TableSighting(index, offset, crc_ok))
                info = parse_pmt(section)
                if info is not None:
                    st.pmt = info
                    st.pmt_pid = pid
                    st.pmt_versions.add(info.version)
                    if orphan:
                        for opid, ooff, oidx, oraw in orphan:
                            role = classify(opid)
                            if role is not None:
                                _record_pes(role, opid, ooff, oidx, oraw)
                        orphan.clear()
                        orphan_bytes = 0
            continue

        if pid == PID_NULL:
            continue

        if pusi:
            finish_pes(pid)
            pes_buf[pid] = bytearray(payload)
            pes_start[pid] = (offset, index)
        elif pid in pes_buf:
            pes_buf[pid] += payload

    for pid in list(pes_buf):
        finish_pes(pid)
    for opid, ooff, oidx, oraw in orphan:
        role = classify(opid)
        if role is not None:
            _record_pes(role, opid, ooff, oidx, oraw)

    return st


# --------------------------------------------------------------------------
# Timing model -- mirrors ATSParser::updatePCR (piecewise-linear over PCR
# samples keyed by byte offset from the start of the stream).
# --------------------------------------------------------------------------


class Timeline:
    def __init__(self, samples: list[PCRSample]) -> None:
        self.samples = samples
        self.indices = [s.index for s in samples]
        self.seconds = [s.unwrapped / PCR_HZ for s in samples]

    @property
    def usable(self) -> bool:
        return len(self.samples) >= 2

    @property
    def duration(self) -> float:
        if not self.usable:
            return 0.0
        return self.seconds[-1] - self.seconds[0]

    def at(self, index: int) -> Optional[float]:
        if not self.usable:
            return None
        idx = self.indices
        sec = self.seconds
        if index <= idx[0]:
            lo, hi = 0, 1
        elif index >= idx[-1]:
            lo, hi = len(idx) - 2, len(idx) - 1
        else:
            lo, hi = 0, len(idx) - 1
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if idx[mid] <= index:
                    lo = mid
                else:
                    hi = mid
        span = idx[hi] - idx[lo]
        if span <= 0:
            return sec[lo]
        t = (index - idx[lo]) / span
        return sec[lo] + t * (sec[hi] - sec[lo])


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Check:
    def __init__(self, key: str, title: str) -> None:
        self.key = key
        self.title = title
        self.status = PASS
        self.summary = ""
        self.details: list[str] = []
        self.total_failures = 0

    def fail(self, message: str) -> None:
        self.status = FAIL
        self.total_failures += 1
        self.details.append(message)

    def skip(self, reason: str) -> None:
        self.status = SKIP
        self.summary = reason

    def note(self, message: str) -> None:
        self.details.append(message)


def build_checks(st: State, tl: Timeline, args: argparse.Namespace) -> list[Check]:
    checks: list[Check] = []
    limit = args.max_errors

    def trim(check: Check, extra: int) -> None:
        if extra > 0:
            check.details.append(f"... and {extra} more")

    # ---- sync ------------------------------------------------------------
    c = Check("sync", "every 188-byte packet starts with 0x47")
    lost_total = sum(s.count for s in st.sync_losses)
    if st.sync_losses:
        for loss in st.sync_losses[:limit]:
            c.fail(f"sync lost at byte offset {loss.offset} ({fmt_bytes(loss.count)} "
                   f"bytes discarded before resync)")
        trim(c, len(st.sync_losses) - limit)
        c.summary = (f"{len(st.sync_losses)} sync loss(es), "
                     f"{fmt_bytes(lost_total)} bytes unusable")
    else:
        c.summary = f"{fmt_bytes(st.total_packets)} packets, no sync loss"
    if st.total_packets == 0:
        c.fail("no MPEG-TS packets found at all")
    checks.append(c)

    # ---- transport error indicator / scrambling / afc --------------------
    c = Check("header", "transport header sanity (TEI, scrambling, adaptation_field_control)")
    for off, idx, pid in st.tei_packets[:limit]:
        c.fail(f"transport_error_indicator set, packet {idx} at offset {off}, PID {fmt_pid(pid)}")
    trim(c, len(st.tei_packets) - limit)
    for off, idx, pid in st.scrambled[:max(0, limit - len(st.tei_packets))]:
        c.fail(f"transport_scrambling_control != 0, packet {idx} at offset {off}, "
               f"PID {fmt_pid(pid)} (the sink does not descramble)")
    for off, idx, pid in st.bad_afc[:max(0, limit - len(st.tei_packets))]:
        c.fail(f"adaptation_field_control == 0 (reserved), packet {idx} at offset {off}, "
               f"PID {fmt_pid(pid)}")
    if c.status == PASS:
        c.summary = "no TEI, no scrambling, no reserved adaptation_field_control"
    checks.append(c)

    # ---- PAT presence and cadence ---------------------------------------
    c = Check("pat", f"PAT present and repeated at <= {args.table_period_ms:.0f} ms")
    if not st.pat_sightings:
        c.fail("no PAT section on PID 0x0000 -- the sink can never find the program")
    else:
        bad_crc = [s for s in st.pat_sightings if not s.crc_ok]
        for s in bad_crc[:limit]:
            c.fail(f"PAT CRC-32 mismatch, packet {s.index} at offset {s.offset}")
        trim(c, len(bad_crc) - limit)
        worst = _check_cadence(c, st.pat_sightings, tl, args.table_period_ms,
                               args.period_tolerance_ms, "PAT", limit)
        if c.status == PASS:
            c.summary = (f"{len(st.pat_sightings)} PATs, worst interval "
                         f"{worst * 1000:.1f} ms" if worst is not None
                         else f"{len(st.pat_sightings)} PATs")
        if st.pat_programs:
            c.note("programs: " + ", ".join(
                f"program_number={pn} -> program_map_PID={fmt_pid(p)}"
                for pn, p in st.pat_programs))
    checks.append(c)

    # ---- PMT presence, PID and cadence ----------------------------------
    c = Check("pmt", f"PMT on the expected PID and repeated at <= {args.table_period_ms:.0f} ms")
    if not st.pmt_sightings or st.pmt is None:
        c.fail("no parsable PMT section")
    else:
        assert st.pmt_pid is not None
        expected_pmt = args.pmt_pid
        if expected_pmt is not None and st.pmt_pid != expected_pmt:
            c.fail(f"PMT is on PID {fmt_pid(st.pmt_pid)}, expected {fmt_pid(expected_pmt)}")
        advertised = {p for _pn, p in st.pat_programs}
        if advertised and st.pmt_pid not in advertised:
            c.fail(f"PMT arrived on PID {fmt_pid(st.pmt_pid)} which the PAT never advertised")
        bad_crc = [s for s in st.pmt_sightings if not s.crc_ok]
        for s in bad_crc[:limit]:
            c.fail(f"PMT CRC-32 mismatch, packet {s.index} at offset {s.offset}")
        trim(c, len(bad_crc) - limit)
        if len(st.pmt_versions) > 1:
            c.fail(f"PMT version_number changed mid-stream {sorted(st.pmt_versions)} -- "
                   "ATSParser tears the streams down and rebuilds them on a PID change")
        worst = _check_cadence(c, st.pmt_sightings, tl, args.table_period_ms,
                               args.period_tolerance_ms, "PMT", limit)
        if c.status == PASS:
            c.summary = (f"PID {fmt_pid(st.pmt_pid)}, {len(st.pmt_sightings)} PMTs, "
                         f"worst interval {worst * 1000:.1f} ms" if worst is not None
                         else f"PID {fmt_pid(st.pmt_pid)}, {len(st.pmt_sightings)} PMTs")
    checks.append(c)

    video_pid = _find_stream_pid(st, STREAMTYPE_H264)
    audio_pid = _find_stream_pid(st, args.audio_stream_type)

    # ---- video stream type ----------------------------------------------
    c = Check("video-type", "video stream_type 0x1B (H.264) present on its PID")
    if st.pmt is None:
        c.fail("no PMT, cannot resolve the video PID")
    elif video_pid is None:
        listed = ", ".join(f"{fmt_pid(p)}=0x{t:02X}" for t, p in st.pmt.streams) or "none"
        c.fail(f"no stream_type 0x1B in the PMT (declared: {listed})")
    else:
        if args.video_pid is not None and video_pid != args.video_pid:
            c.fail(f"H.264 is on PID {fmt_pid(video_pid)}, expected {fmt_pid(args.video_pid)}")
        count = st.pid_counts.get(video_pid, 0)
        if count == 0:
            c.fail(f"PMT declares H.264 on {fmt_pid(video_pid)} but no packet ever "
                   "arrived on that PID")
        if c.status == PASS:
            c.summary = (f"stream_type 0x1B on PID {fmt_pid(video_pid)}, "
                         f"{fmt_bytes(count)} packets, {len(st.pes_video)} access units")
    checks.append(c)

    # ---- audio stream type ----------------------------------------------
    c = Check("audio-type",
              f"audio stream_type 0x{args.audio_stream_type:02X} present on its PID")
    if args.no_audio:
        c.skip("audio checks disabled with --no-audio")
    elif st.pmt is None:
        c.fail("no PMT, cannot resolve the audio PID")
    elif audio_pid is None:
        listed = ", ".join(f"{fmt_pid(p)}=0x{t:02X}" for t, p in st.pmt.streams) or "none"
        c.fail(f"no stream_type 0x{args.audio_stream_type:02X} in the PMT (declared: {listed})")
    else:
        if args.audio_pid is not None and audio_pid != args.audio_pid:
            c.fail(f"audio is on PID {fmt_pid(audio_pid)}, expected {fmt_pid(args.audio_pid)}")
        count = st.pid_counts.get(audio_pid, 0)
        if count == 0:
            c.fail(f"PMT declares audio on {fmt_pid(audio_pid)} but no packet ever "
                   "arrived on that PID")
        if args.audio_stream_type == STREAMTYPE_AAC_ADTS:
            bad = [r for r in st.pes_audio if not r.adts_ok]
            for r in bad[:limit]:
                c.fail(f"audio PES at offset {r.offset} (packet {r.index}) does not start "
                       "with an ADTS syncword; stream_type 0x0F means ADTS and the sink's "
                       "ESQueue will reject the access unit")
            trim(c, len(bad) - limit)
        if c.status == PASS:
            c.summary = (f"stream_type 0x{args.audio_stream_type:02X} on PID "
                         f"{fmt_pid(audio_pid)}, {fmt_bytes(count)} packets, "
                         f"{len(st.pes_audio)} frames")
    checks.append(c)

    # ---- PCR --------------------------------------------------------------
    c = Check("pcr", "PCR present, monotonic, and within 100 ms of its PTS")
    if not st.pcr_samples:
        c.fail("no PCR anywhere in the stream -- the sink has no clock to lock to")
    else:
        pcr_pids = {s.pid for s in st.pcr_samples}
        if st.pmt is not None and st.pmt.pcr_pid not in pcr_pids:
            c.fail(f"PMT declares PCR_PID {fmt_pid(st.pmt.pcr_pid)} but PCR was only seen "
                   f"on {', '.join(fmt_pid(p) for p in sorted(pcr_pids))}")
        for off, idx, prev, now in st.pcr_nonmonotonic[:limit]:
            c.fail(f"PCR went backwards at offset {off} (packet {idx}): "
                   f"{prev / PCR_HZ:.6f}s -> {now / PCR_HZ:.6f}s")
        trim(c, len(st.pcr_nonmonotonic) - limit)
        gaps = []
        for a, b in zip(st.pcr_samples, st.pcr_samples[1:]):
            dt = (b.unwrapped - a.unwrapped) / PCR_HZ
            if dt > (args.pcr_period_ms + args.period_tolerance_ms) / 1000.0:
                gaps.append((b, dt))
        for sample, dt in gaps[:limit]:
            c.fail(f"PCR gap of {dt * 1000:.1f} ms ending at offset {sample.offset} "
                   f"(packet {sample.index}), limit {args.pcr_period_ms:.0f} ms")
        trim(c, len(gaps) - limit)

        # PCR vs PTS.
        worst_delta = 0.0
        violations = []
        if tl.usable:
            for rec in st.pes_video + st.pes_audio:
                if rec.pts is None:
                    continue
                pcr_sec = tl.at(rec.index)
                if pcr_sec is None:
                    continue
                pts_sec = _unwrapped_pts(rec.pts, pcr_sec)
                delta = pts_sec - pcr_sec
                if abs(delta) > abs(worst_delta):
                    worst_delta = delta
                if abs(delta) > args.pts_pcr_ms / 1000.0:
                    violations.append((rec, delta))
            for rec, delta in violations[:limit]:
                where = "behind" if delta < 0 else "ahead of"
                c.fail(f"PTS on PID {fmt_pid(rec.pid)} is {abs(delta) * 1000:.1f} ms "
                       f"{where} the PCR at offset {rec.offset} (packet {rec.index}), "
                       f"limit {args.pts_pcr_ms:.0f} ms")
            trim(c, len(violations) - limit)
        else:
            c.fail("fewer than two PCR samples, cannot build a timeline")
        if c.status == PASS:
            c.summary = (f"{fmt_bytes(len(st.pcr_samples))} PCRs on "
                         f"{', '.join(fmt_pid(p) for p in sorted(pcr_pids))}, "
                         f"span {tl.duration:.3f} s, worst PTS-PCR {worst_delta * 1000:+.1f} ms")
    checks.append(c)

    # ---- continuity counter ----------------------------------------------
    c = Check("continuity", "continuity_counter increments per PID with no gaps")
    for gap in st.cc_gaps[:limit]:
        label = "duplicate packet" if gap.duplicate else "gap"
        c.fail(f"CC {label} on PID {fmt_pid(gap.pid)} at offset {gap.offset} "
               f"(packet {gap.index}): expected {gap.expected}, got {gap.got}")
    trim(c, len(st.cc_gaps) - limit)
    if c.status == PASS:
        c.summary = f"clean across {len(st.pid_counts)} PID(s)"
    checks.append(c)

    # ---- SPS/PPS before every IDR ----------------------------------------
    #
    # WHY THIS CHECK EXISTS: it catches the bitrate-retune bug before the TV
    # does.
    #
    # A WFD session renegotiates while it is running.  The sink asks for a new
    # bitrate over RTSP SET_PARAMETER, or asks for an IDR (wfd_idr_request), and
    # hyprcast retunes or restarts the encoder.  Every encoder emits a fresh IDR
    # at that moment.  Whether it also re-emits SPS and PPS in front of that IDR
    # is a separate setting -- x264's repeat-headers, VAAPI's
    # VAEncPackedHeaderSequence, GStreamer's config-interval, ffmpeg's
    # global-header handling -- and it is exactly the setting that gets lost
    # when a pipeline is reconfigured rather than rebuilt.
    #
    # The bug is invisible locally.  A local decoder has been holding the
    # parameter sets from the very first frame, so it decodes the new IDR fine
    # and the preview window looks perfect.  The sink is in a different
    # position:
    #
    #   * ATSParser hands each access unit to ElementaryStreamQueue, which
    #     extracts csd-0/csd-1 from the SPS/PPS it finds in the bitstream and
    #     configures a MediaCodec from them.  An IDR that arrives with new
    #     coding parameters and no new SPS is decoded against the stale
    #     parameter set: wrong crop, wrong reference list size, green blocks, a
    #     frozen picture, or a hard decoder reset.
    #
    #   * ATSParser::Stream::parse() (ATSParser.cpp:555) treats any
    #     continuity_counter it did not expect as a discontinuity and throws
    #     away the partially assembled PES.  Over RTP/UDP that happens for real.
    #     After such a drop the only way back to a picture is an IDR that is
    #     self-contained -- one that carries its own SPS and PPS.  An IDR
    #     without them is not a recovery point at all, and the screen stays
    #     black until the next one that has them.
    #
    #   * A sink that joins late (or reconnects) has never seen the parameter
    #     sets that were sent once at session start.
    #
    # AOSP's source side guards against this explicitly: MediaSender.cpp:493
    # sets PREPEND_SPS_PPS_TO_IDR_FRAMES, and TSPacketizer::packetize()
    # (TSPacketizer.cpp:473) prepends the codec-specific data to every access
    # unit for which IsIDR() is true -- every IDR, not just the first.  This
    # check asserts the same property on the wire.  It costs one NAL scan per
    # access unit here; catching it on the TV costs an afternoon with a phone
    # camera pointed at a screen.
    c = Check("sps-pps-idr", "SPS and PPS immediately precede every IDR")
    idr_records = [r for r in st.pes_video if r.has_idr]
    if not st.pes_video:
        c.fail("no video access units to inspect")
    elif not idr_records:
        c.fail("no IDR access unit anywhere in the stream -- a sink that tunes in "
               "late (or loses a UDP packet) has no recovery point")
    else:
        offenders = [r for r in idr_records if not (r.sps_before_idr and r.pps_before_idr)]
        for r in offenders[:limit]:
            missing = []
            if not r.sps_before_idr:
                missing.append("SPS")
            if not r.pps_before_idr:
                missing.append("PPS")
            c.fail(f"IDR at offset {r.offset} (packet {r.index}, PID {fmt_pid(r.pid)}) "
                   f"has no preceding {'/'.join(missing)}; NALs in this access unit: "
                   f"{r.nal_types}")
        trim(c, len(offenders) - limit)
        interlopers = [r for r in idr_records if r.interlopers]
        for r in interlopers[:max(0, limit - len(offenders))]:
            c.fail(f"IDR at offset {r.offset} (packet {r.index}) has NAL type(s) "
                   f"{sorted(set(r.interlopers))} between the parameter sets and the "
                   "IDR slice")
        if c.status == PASS:
            c.summary = f"{len(idr_records)} IDR access units, all carry SPS+PPS in band"
    checks.append(c)

    # ---- level max frame size --------------------------------------------
    level_name = args.level
    if level_name == "auto":
        if st.sps_seen is not None and st.sps_seen.level_idc in LEVEL_BY_IDC:
            level_name = LEVEL_BY_IDC[st.sps_seen.level_idc]
        else:
            level_name = args.default_level
    limits = LEVELS[level_name]
    c = Check("frame-size", f"no PES packet exceeds the level {limits.name} max frame size")
    if not st.pes_video:
        c.fail("no video access units to size")
    else:
        cap = limits.max_au_bytes
        offenders = [r for r in st.pes_video if r.es_len > cap]
        for r in offenders[:limit]:
            c.fail(f"video access unit at offset {r.offset} (packet {r.index}) is "
                   f"{fmt_bytes(r.es_len)} bytes, level {limits.name} caps a coded "
                   f"picture at 384*MaxFS/MinCR = 384*{limits.max_fs}/{limits.min_cr} "
                   f"= {fmt_bytes(cap)} bytes")
        trim(c, len(offenders) - limit)
        largest = max(r.es_len for r in st.pes_video)
        if c.status == PASS:
            c.summary = (f"largest access unit {fmt_bytes(largest)} bytes, "
                         f"cap {fmt_bytes(cap)} bytes "
                         f"({100.0 * largest / cap:.1f}% of the level limit)")
        # Tighter, picture-accurate bound; advisory only because A.3.1(g) allows
        # the MaxMBPS-derived allowance for non-first pictures.
        if st.sps_seen is not None:
            tight = 384 * st.sps_seen.pic_size_in_mbs // limits.min_cr
            over = [r for r in st.pes_video if r.es_len > tight]
            if over:
                c.note(f"advisory: {len(over)} access unit(s) exceed the picture-accurate "
                       f"bound 384*PicSizeInMbs/MinCR = {fmt_bytes(tight)} bytes "
                       f"(largest {fmt_bytes(max(r.es_len for r in over))})")
    checks.append(c)

    # ---- PES structural conformance --------------------------------------
    c = Check("pes", "PES headers conform to what ATSParser accepts")
    bad = [r for r in st.pes_video + st.pes_audio if r.errors]
    for r in bad[:limit]:
        for message in r.errors:
            c.fail(f"PID {fmt_pid(r.pid)} PES at offset {r.offset} (packet {r.index}): {message}")
    trim(c, len(bad) - limit)
    missing_pts = [r for r in st.pes_video + st.pes_audio if r.pts is None and not r.errors]
    for r in missing_pts[:max(0, limit - len(bad))]:
        c.fail(f"PID {fmt_pid(r.pid)} PES at offset {r.offset} (packet {r.index}) "
               "carries no PTS")
    if c.status == PASS:
        c.summary = (f"{len(st.pes_video) + len(st.pes_audio)} PES packets, "
                     "all with start code, marker bits and PTS")
    checks.append(c)

    # ---- profile / level advertised in the SPS (WFD negotiation) ---------
    c = Check("h264-profile", "SPS profile/level match what a WFD sink negotiates")
    if args.no_profile_check:
        c.skip("disabled with --no-profile-check")
    elif st.sps_seen is None:
        c.fail("no parsable SPS found in the video elementary stream")
    else:
        sps = st.sps_seen
        if not sps.is_constrained_baseline and args.profile == "cbp":
            c.fail(f"SPS advertises profile_idc {sps.profile_idc} constraint_set 0x"
                   f"{sps.constraint_flags:02X}; the sink negotiated Constrained "
                   "Baseline (profile_idc 66, constraint_set1_flag set)")
        if sps.level_idc > limits.level_idc:
            c.fail(f"SPS level_idc {sps.level_idc} exceeds the negotiated level "
                   f"{limits.name} (level_idc {limits.level_idc})")
        if len(st.sps_variants) > 1:
            c.fail(f"{len(st.sps_variants)} distinct SPS variants in one session "
                   f"{sorted(st.sps_variants)}; a sink that latched the first one "
                   "decodes every later picture against a stale parameter set")
        if c.status == PASS:
            c.summary = (f"profile_idc {sps.profile_idc} constraint_set 0x"
                         f"{sps.constraint_flags:02X} level_idc {sps.level_idc}, "
                         f"{sps.width}x{sps.height}")
    checks.append(c)

    return checks


def _unwrapped_pts(raw_pts: int, near_seconds: float) -> float:
    """Map a 33-bit PTS onto the (unwrapped) PCR timeline near `near_seconds`."""
    period = PTS_WRAP / PTS_HZ
    base = raw_pts / PTS_HZ
    k = round((near_seconds - base) / period)
    return base + k * period


def _check_cadence(check: Check, sightings: list[TableSighting], tl: Timeline,
                   period_ms: float, tolerance_ms: float, label: str,
                   limit: int) -> Optional[float]:
    if len(sightings) < 2:
        check.fail(f"only {len(sightings)} {label} section(s) in the whole stream; "
                   f"WFD requires one every {period_ms:.0f} ms "
                   "(MediaSender.cpp: mPrevTimeUs + 100000 <= timeUs)")
        return None
    if not tl.usable:
        check.fail(f"cannot time the {label} interval without at least two PCR samples")
        return None
    worst = 0.0
    violations: list[tuple[TableSighting, float]] = []
    for a, b in zip(sightings, sightings[1:]):
        ta, tb = tl.at(a.index), tl.at(b.index)
        if ta is None or tb is None:
            continue
        dt = tb - ta
        worst = max(worst, dt)
        if dt > (period_ms + tolerance_ms) / 1000.0:
            violations.append((b, dt))
    for sighting, dt in violations[:limit]:
        check.fail(f"{label} interval {dt * 1000:.1f} ms ending at offset "
                   f"{sighting.offset} (packet {sighting.index}), limit {period_ms:.0f} ms")
    if len(violations) > limit:
        check.details.append(f"... and {len(violations) - limit} more")
    return worst


def _find_stream_pid(st: State, stream_type: int) -> Optional[int]:
    if st.pmt is None:
        return None
    for t, pid in st.pmt.streams:
        if t == stream_type:
            return pid
    return None


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def print_report(st: State, tl: Timeline, checks: list[Check], args: argparse.Namespace,
                 source: str, out) -> bool:
    write = out.write
    write("assert-ts.py -- MPEG-TS conformance for a Wi-Fi Display sink\n")
    write(f"input: {source}\n")
    write(f"       {fmt_bytes(st.input_bytes)} bytes, "
          f"{fmt_bytes(st.total_packets)} TS packets\n\n")

    write("CHECKS\n")
    for c in checks:
        write(f"  [{c.status}] {c.title}\n")
        if c.summary:
            write(f"         {c.summary}\n")
        for line in c.details:
            write(f"         - {line}\n")
    write("\n")

    write("SUMMARY\n")
    write(f"  packets              {fmt_bytes(st.total_packets)}\n")
    write(f"  bytes                {fmt_bytes(st.total_bytes)} in packets, "
          f"{fmt_bytes(st.input_bytes)} read\n")
    if tl.usable:
        write(f"  duration (PCR span)  {tl.duration:.3f} s\n")
        if tl.duration > 0:
            write(f"  bitrate              "
                  f"{st.total_bytes * 8 / tl.duration / 1e6:.3f} Mbit/s (TS gross)\n")
    else:
        write("  duration (PCR span)  n/a (need >= 2 PCR samples)\n")

    video_pid = _find_stream_pid(st, STREAMTYPE_H264)
    audio_pid = _find_stream_pid(st, args.audio_stream_type)
    roles: dict[int, str] = {
        PID_PAT: "PAT", 0x0001: "CAT", 0x0010: "NIT", 0x0011: "SDT/BAT",
        0x0012: "EIT", 0x0013: "RST", 0x0014: "TDT/TOT", PID_NULL: "null",
    }
    if st.pmt_pid is not None:
        roles[st.pmt_pid] = "PMT"
    if st.pmt is not None:
        for t, pid in st.pmt.streams:
            roles[pid] = STREAM_TYPE_NAMES.get(t, f"stream_type 0x{t:02X}")
        roles.setdefault(st.pmt.pcr_pid, "PCR")
        if st.pmt.pcr_pid in roles and "PCR" not in roles[st.pmt.pcr_pid]:
            roles[st.pmt.pcr_pid] += " + PCR"

    write("  per-PID packet counts\n")
    for pid in sorted(st.pid_counts):
        count = st.pid_counts[pid]
        share = 100.0 * count / st.total_packets if st.total_packets else 0.0
        role = roles.get(pid, "unreferenced")
        write(f"    {fmt_pid(pid)}  {count:>10,}  {share:5.1f}%  {role}\n")

    if st.pmt is not None:
        write("  PMT contents\n")
        write(f"    PCR_PID            {fmt_pid(st.pmt.pcr_pid)}\n")
        for t, pid in st.pmt.streams:
            write(f"    stream_type 0x{t:02X}   {fmt_pid(pid)}  "
                  f"{STREAM_TYPE_NAMES.get(t, 'unknown')}\n")

    if st.sps_seen is not None:
        s = st.sps_seen
        write(f"  H.264                {s.width}x{s.height} "
              f"({s.width_mbs}x{s.height_mbs} MBs, {s.pic_size_in_mbs} total), "
              f"profile_idc {s.profile_idc}, level_idc {s.level_idc}\n")

    # IDR interval statistics.
    idrs = [r for r in st.pes_video if r.has_idr and r.pts is not None]
    write(f"  video access units   {fmt_bytes(len(st.pes_video))}\n")
    write(f"  IDR access units     {fmt_bytes(len(idrs))}\n")
    if len(idrs) >= 2:
        anchor = tl.at(idrs[0].index) or (idrs[0].pts / PTS_HZ)
        times = []
        prev = anchor
        for rec in idrs:
            near = tl.at(rec.index) or prev
            t = _unwrapped_pts(rec.pts, near)
            times.append(t)
            prev = t
        intervals = [(b - a) * 1000.0 for a, b in zip(times, times[1:])]
        write(f"  IDR interval (ms)    min {min(intervals):.1f}  "
              f"max {max(intervals):.1f}  mean {statistics.fmean(intervals):.1f}  "
              f"median {statistics.median(intervals):.1f}  n={len(intervals)}\n")
        if tl.duration > 0:
            write(f"  IDR rate             {len(idrs) / tl.duration:.2f} /s\n")
    elif idrs:
        write("  IDR interval (ms)    n/a (only one IDR)\n")
    if st.pes_video:
        sizes = [r.es_len for r in st.pes_video]
        write(f"  access unit bytes    min {min(sizes):,}  max {max(sizes):,}  "
              f"mean {statistics.fmean(sizes):,.0f}\n")
    if st.pes_audio:
        write(f"  audio frames         {fmt_bytes(len(st.pes_audio))}\n")

    # The PID layout note -- see the long comment at the bottom of this file.
    if st.pmt_pid is not None:
        write("  PID layout\n")
        write(f"    observed           PMT {fmt_pid(st.pmt_pid)}, "
              f"video {fmt_pid(video_pid) if video_pid is not None else '-'}, "
              f"audio {fmt_pid(audio_pid) if audio_pid is not None else '-'}, "
              f"PCR {fmt_pid(st.pmt.pcr_pid) if st.pmt else '-'}\n")
        write(f"    AOSP TSPacketizer  PMT {fmt_pid(AOSP_PMT_PID)}, "
              f"video {fmt_pid(AOSP_VIDEO_PID)}, audio {fmt_pid(AOSP_AUDIO_PID)}, "
              f"PCR {fmt_pid(AOSP_PCR_PID)}\n")
        write("    ATSParser reads the PMT PID out of the PAT and the elementary PIDs "
              "out of the PMT;\n"
              "    it hardcodes nothing but PID 0. Any self-consistent layout parses.\n")

    failed = [c for c in checks if c.status == FAIL]
    write("\n")
    if failed:
        write(f"OVERALL: FAIL ({len(failed)} of "
              f"{len([c for c in checks if c.status != SKIP])} checks failed: "
              f"{', '.join(c.key for c in failed)})\n")
    else:
        write(f"OVERALL: PASS ({len([c for c in checks if c.status == PASS])} checks)\n")
    return not failed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def auto_int(text: str) -> int:
    return int(text, 0)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="assert-ts.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Validate an MPEG-TS stream against what a Wi-Fi Display sink requires.",
        epilog="""\
examples:
  # validate a capture
  tools/assert-ts.py capture.ts

  # validate straight off an ffmpeg pipe
  ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=60 -t 5 -c:v libx264 \\
      -profile:v baseline -f mpegts - | tools/assert-ts.py -

  # assert the exact PID layout fluxcast's working ffmpeg path produces
  tools/assert-ts.py --pmt-pid 0x1000 --video-pid 0x1011 capture.ts

exit status:
  0  every enabled check passed
  1  at least one check failed
  2  usage or I/O error
""")
    p.add_argument("input", nargs="?", default="-",
                   help="MPEG-TS file, or '-' for stdin (default: stdin)")
    p.add_argument("--pmt-pid", type=auto_int, default=None, metavar="PID",
                   help="assert the PMT is on this PID (e.g. 0x1000)")
    p.add_argument("--video-pid", type=auto_int, default=None, metavar="PID",
                   help="assert H.264 is on this PID (e.g. 0x1011)")
    p.add_argument("--audio-pid", type=auto_int, default=None, metavar="PID",
                   help="assert audio is on this PID (e.g. 0x1100)")
    p.add_argument("--audio-stream-type", type=auto_int, default=STREAMTYPE_AAC_ADTS,
                   metavar="TYPE",
                   help="expected audio stream_type (default 0x0F = AAC ADTS; "
                        "use 0x83 for the WFD LPCM path)")
    p.add_argument("--no-audio", action="store_true",
                   help="video-only stream; skip the audio checks")
    p.add_argument("--level", default="auto", choices=("auto",) + tuple(LEVELS),
                   help="H.264 level for the max frame size check "
                        "(default: auto, taken from the SPS)")
    p.add_argument("--default-level", default="3.2", choices=tuple(LEVELS),
                   help="level to assume when --level auto finds no SPS "
                        "(default 3.2, what the Xiaomi/Google TV sink negotiates)")
    p.add_argument("--profile", default="cbp", choices=("cbp", "any"),
                   help="expected H.264 profile (default cbp = Constrained Baseline)")
    p.add_argument("--no-profile-check", action="store_true",
                   help="skip the SPS profile/level check")
    p.add_argument("--table-period-ms", type=float, default=100.0, metavar="MS",
                   help="maximum PAT/PMT repetition interval (default 100)")
    p.add_argument("--pcr-period-ms", type=float, default=100.0, metavar="MS",
                   help="maximum PCR repetition interval (default 100)")
    p.add_argument("--period-tolerance-ms", type=float, default=5.0, metavar="MS",
                   help="slack allowed on the PAT/PMT/PCR interval limits (default 5). "
                        "AOSP's own scheduler fires when the interval REACHES 100 ms "
                        "(MediaSender.cpp: mPrevTimeUs + 100000 <= timeUs), so an "
                        "interval of exactly 100 ms is conformant, and PCR "
                        "interpolation adds a little noise on top")
    p.add_argument("--pts-pcr-ms", type=float, default=100.0, metavar="MS",
                   help="maximum |PTS - PCR| at the moment a PES header is sent "
                        "(default 100)")
    p.add_argument("--max-errors", type=int, default=10, metavar="N",
                   help="per-check cap on printed failures (default 10)")
    p.add_argument("--max-records", type=int, default=1_000_000, metavar="N",
                   help="cap on retained PES records (default 1000000)")
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    if args.input == "-":
        source = "<stdin>"
        fh: BinaryIO = sys.stdin.buffer
        close = False
    else:
        source = args.input
        try:
            fh = open(args.input, "rb")
        except OSError as exc:
            print(f"assert-ts.py: cannot open {args.input}: {exc}", file=sys.stderr)
            return 2
        close = True

    try:
        st = analyze(fh, args.max_records)
    except KeyboardInterrupt:
        print("assert-ts.py: interrupted", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"assert-ts.py: read error: {exc}", file=sys.stderr)
        return 2
    finally:
        if close:
            fh.close()

    tl = Timeline(st.pcr_samples)
    checks = build_checks(st, tl, args)
    ok = print_report(st, tl, checks, args, source, sys.stdout)
    return 0 if ok else 1


# --------------------------------------------------------------------------
# PID LAYOUT -- the open question in src/wfd.py:880-882.
#
# src/wfd.py shouts that "PMT/video/audio PID values must stay aligned with the
# working gst path!!!" and pins PMT 0x1000, video 0x1011, audio 0x1012.
# reference/mpegts_muxer.py pins PMT 0x0100, PCR 0x1000, video 0x1000, audio
# 0x1100.  Those two cannot both be "required".
#
# Reading the sink settles it: neither is required.
#
#   ATSParser::parsePID() (reference/aosp/ATSParser.cpp:1090) looks the incoming
#   PID up in mPSISections, which is seeded with exactly one entry -- PID 0
#   (line 974).  parseProgramAssociationTable() (line 1024) reads
#   program_map_PID out of the PAT and adds a PSISection for whatever value it
#   finds (line 1082).  Program::parseProgramMap() (line 259) then reads PCR_PID
#   (line 275) and each elementary_PID (line 301) out of the PMT and builds a
#   Stream per PID (line 419).  There is not one hardcoded PID constant anywhere
#   in the parser besides 0.  Any self-consistent PAT -> PMT -> ES chain works.
#
# So the "!!!" comment is superstition about the sink, though it may still be
# true about a specific *source*: it was written against a GStreamer pipeline,
# and what actually had to stay aligned was ffmpeg's PID allocation matching
# what that pipeline produced, so that a downstream tool comparing captures did
# not see a diff.  Nothing in the sink cares.
#
# The layout worth copying, if you want to look exactly like a real Miracast
# source, is AOSP's own TSPacketizer (reference/aosp/TSPacketizer.cpp:390-393,
# 696, 766, 856): video elementary PID starts at 0x1011, audio at 0x1100, the
# PMT sits on kPID_PMT and the PCR gets a PID of its own, kPID_PCR, carried in
# adaptation-only packets with continuity_counter frozen at 0.  The two
# constants live in TSPacketizer.h, which is vendored here truncated to two
# bytes; upstream AOSP defines kPID_PMT = 0x100 and kPID_PCR = 0x1000.  That
# makes reference/mpegts_muxer.py's PID_PMT = 0x0100 and PID_PCR = 0x1000 an
# exact match for AOSP, with its only deviation being that it puts video on
# 0x1000 (AOSP's PCR-only PID) instead of 0x1011.  wfd.py's ffmpeg flags match
# AOSP on the video PID and put the PMT where AOSP puts the PCR.
#
# Practical recommendation for hyprcast: keep wfd.py's values, because they are
# the ones measured working against this Xiaomi/Google TV sink, and drop the
# "!!!"; but if a new sink ever misbehaves, moving PMT to 0x0100 and video to
# 0x1011 makes the stream byte-layout-identical to what that sink's own vendor
# stack emits, which is the cheapest possible thing to try.
# --------------------------------------------------------------------------


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
