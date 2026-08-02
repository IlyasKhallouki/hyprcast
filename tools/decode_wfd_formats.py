#!/usr/bin/env python3
"""
Decode a sink's wfd_video_formats / wfd_audio_codecs M3 response.

The sink's M3 body is the authoritative definition of what we may send.
Cross-check any surprise against reference/aosp/VideoFormats.cpp -- the sink
parses with a fixed-stride scanner guarded by CHECK_LE, so a malformed
wfd_video_formats aborts the sink process rather than erroring.
"""
import sys

CEA = [
    "640x480p60", "720x480p60", "720x480i60", "720x576p50", "720x576i50",
    "1280x720p30", "1280x720p60", "1920x1080p30", "1920x1080p60", "1920x1080i60",
    "1280x720p25", "1280x720p50", "1920x1080p25", "1920x1080p50", "1920x1080i50",
    "1280x720p24", "1920x1080p24",
]
VESA = [
    "800x600p30", "800x600p60", "1024x768p30", "1024x768p60", "1152x864p30",
    "1152x864p60", "1280x768p30", "1280x768p60", "1280x800p30", "1280x800p60",
    "1360x768p30", "1360x768p60", "1366x768p30", "1366x768p60", "1280x1024p30",
    "1280x1024p60", "1400x1050p30", "1400x1050p60", "1440x900p30", "1440x900p60",
    "1600x900p30", "1600x900p60", "1600x1200p30", "1600x1200p60", "1680x1024p30",
    "1680x1024p60", "1680x1050p30", "1680x1050p60", "1920x1200p30",
]
HH = [
    "800x480p30", "800x480p60", "854x480p30", "854x480p60", "864x480p30",
    "864x480p60", "640x360p30", "640x360p60", "960x540p30", "960x540p60",
    "848x480p30", "848x480p60",
]
LEVELS = {0: "3.1", 1: "3.2", 2: "4.0", 3: "4.1", 4: "4.2"}
# H.264 Annex A: MaxMBPS (macroblocks/sec) per level
MAX_MBPS = {"3.1": 108000, "3.2": 216000, "4.0": 245760, "4.1": 245760, "4.2": 522240}

LPCM_MODES = ["44.1kHz/16b/2ch", "48kHz/16b/2ch"]
AAC_MODES = ["48kHz/16b/2ch", "48kHz/16b/4ch", "48kHz/16b/6ch", "48kHz/16b/8ch"]


def bits(mask, table):
    return [n for i, n in enumerate(table) if mask & (1 << i)]


def mbs(mode):
    """Macroblocks per frame and per second for a 'WxHpF'/'WxHiF' mode name."""
    res, sep, fps = mode.replace("i", "p").partition("p")
    w, h = (int(x) for x in res.split("x"))
    per_frame = ((w + 15) // 16) * ((h + 15) // 16)
    return per_frame, per_frame * int(fps)


def decode_video(spec):
    f = spec.split()
    if len(f) < 13:
        print(f"  ! expected >=13 fields, got {len(f)}: {spec}")
        return
    native, pdms = f[0], f[1]
    profile, level = int(f[2], 16), int(f[3], 16)
    cea, vesa, hh = int(f[4], 16), int(f[5], 16), int(f[6], 16)
    latency, minslice, sliceenc, frc = f[7], f[8], f[9], int(f[10], 16)
    maxh, maxv = int(f[11], 16), int(f[12], 16)

    ni, nt = int(native, 16) >> 3, int(native, 16) & 0x7
    tbl = [CEA, VESA, HH][nt] if nt < 3 else []
    print(f"  native                : 0x{native} -> table={['CEA','VESA','HH'][nt] if nt<3 else nt} "
          f"index={ni} ({tbl[ni] if ni < len(tbl) else '?'})")
    print(f"  preferred-display-mode: 0x{pdms}")
    prof = [n for b, n in [(1, "CBP"), (2, "CHP"), (4, "CHP-444")] if profile & b]
    print(f"  profile               : 0x{profile:02x} -> {', '.join(prof) or '?'}")
    lv = [LEVELS[i] for i in LEVELS if level & (1 << i)]
    print(f"  level                 : 0x{level:02x} -> {', '.join(lv) or '?'}")
    print(f"  latency/min-slice/enc : {latency} {minslice} {sliceenc}")
    print(f"  frame-rate-control    : 0x{frc:02x} (0b{frc:08b})")
    print(f"  max-hres x max-vres   : {maxh} x {maxv}")
    print()
    for name, mask, table in (("CEA", cea, CEA), ("VESA", vesa, VESA), ("HH", hh, HH)):
        got = bits(mask, table)
        print(f"  {name}-support 0x{mask:08x}: {len(got)} modes")
        if name == "CEA":
            for m in got:
                print(f"      {m}")
    print()

    best = max(lv, key=lambda x: MAX_MBPS[x]) if lv else None
    if not best:
        return
    budget = MAX_MBPS[best]
    print(f"  Feasible CEA modes within level {best} (MaxMBPS={budget}) AND max-hres/vres:")
    for m in bits(cea, CEA):
        if "i" in m:
            continue
        per_frame, per_sec = mbs(m)
        w, h = (int(x) for x in m.split("p")[0].split("x"))
        fits_level = per_sec <= budget
        fits_dims = w <= maxh and h <= maxv
        flag = "OK " if (fits_level and fits_dims) else "no "
        why = []
        if not fits_level:
            why.append(f"needs {per_sec} MB/s > {budget}")
        if not fits_dims:
            why.append(f"exceeds {maxh}x{maxv}")
        print(f"      {flag}{m:<16} {per_sec:>7} MB/s  {'; '.join(why)}")


def decode_audio(spec):
    for part in spec.split(","):
        p = part.split()
        if len(p) < 2:
            continue
        codec, mask = p[0], int(p[1], 16)
        table = LPCM_MODES if codec == "LPCM" else AAC_MODES
        print(f"  {codec:<5} 0x{mask:08x}: {', '.join(bits(mask, table)) or '?'}")


if __name__ == "__main__":
    body = sys.stdin.read()
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("wfd_video_formats:"):
            print("wfd_video_formats")
            decode_video(line.split(":", 1)[1].strip())
        elif line.startswith("wfd_audio_codecs:"):
            print("wfd_audio_codecs")
            decode_audio(line.split(":", 1)[1].strip())
            print()
        elif line.startswith(("wfd_content_protection:", "wfd_client_rtp_ports:")):
            k, v = line.split(":", 1)
            print(f"{k}\n  {v.strip()}\n")
