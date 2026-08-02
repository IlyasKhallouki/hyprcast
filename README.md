# hyprcast

Stream this desktop to a Miracast TV over Wi-Fi Display, with the GPU doing the
work and the CPU never touching a pixel.

**This is a private, single-purpose fork.** It targets exactly one machine and
exactly one sink, it is not distributed, and it deliberately deletes every
feature that machine does not use. If you want a general-purpose tool that
supports many desktops, TVs and protocols, use
[fluxcast](https://github.com/IlyaP358/fluxcast) upstream instead.

---

## What it is

fluxcast pushes frames through an `ffmpeg` subprocess and encodes H.264 in
software with x264. On the target laptop that costs **197% of one core** and
tops out at 84 fps for a 1080p job — which is why the picture stutters.

hyprcast replaces that path with a native C11 engine: Wayland
`ext-image-copy-capture-v1` hands out a dmabuf, the buffer is imported straight
into VA-API, colour-converted BGRX→NV12 by the GPU's VPP block, and encoded by
`h264_vaapi`. No copies, no pixel ever crossing the CPU.

The Python layer that remains does Wi-Fi Direct group formation and the WFD
RTSP/M1–M6 negotiation. Everything else is gone.

### Target

```
i5-8350U (4c/8t, Kaby Lake-R) · Intel UHD 620 (Gen9.5) · /dev/dri/renderD128
Hyprland 0.55.4 · eDP-1 1920x1080@60 · Intel 8265
PipeWire 1.6.7 · ffmpeg n8.1.2 · iHD 26.1.5 · Python 3.14
Sink: Xiaomi box running Google TV + its preinstalled Miracast app
```

The sink negotiates **1280x720p60**, H.264 Constrained Baseline level 3.2, AAC
48 kHz 2ch. Its maximum is 720p — 1080p is not on offer, so the engine captures
1080p and scales to a 720p60 wire.

---

## Measured numbers

Reproduced on the machine above, not copied from a design document. Load is a
realistic one (mpv playing a 1080p60 hardware-decoded clip); 1920x1080 capture →
1280x720p60 wire.

| | capture p50 | vpp p50 | encode p50 | total p50 | fps | CPU |
|---|---|---|---|---|---|---|
| VBR / `EncSlice` | 8.85 ms | 0.32 ms | 7.51 ms | 16.69 ms | **58.2** | 9.5% |
| CQP / `EncSliceLP` | 12.24 ms | 0.38 ms | **3.98 ms** | 16.79 ms | **57.8** | **6.6%** |

Against fluxcast's x264 path at 197% of one core, that is roughly a **20–30x
reduction in CPU**.

Other measurements worth keeping:

- `ext-image-copy-capture-v1` sustains 60.65 fps with 0 failures.
- commit→ready latency is 2.8–3.6 ms.
- VPP BGRX→NV12 costs 0.881 ms/frame, a 1135 fps ceiling.
- The complete M1–M6 RTSP negotiation costs **333 ms**. The sink then sits for
  **8.024 s** before it sends `PLAY` — that stall is entirely sink-side, so do
  not add a timeout that fires inside it.

Bitstream verified: Constrained Baseline, level 3.2, 1280x720, `has_b_frames=0`,
bt709/tv, decodes with zero errors. Colour checked against a `grim` screenshot
(mean RGB within 3/765), so the BGRX-vs-RGBX import is genuinely right rather
than accidentally symmetric.

Full detail, including the dmabuf modifier survey and the Wi-Fi regulatory
constraints on this card, lives in [STATUS.md](STATUS.md).

### Constraints that cost hours to rediscover

- `Y_TILED_CCS` cannot be imported by iHD. The GBM modifier list is pinned to
  `I915_FORMAT_MOD_Y_TILED` and `plane_count` is asserted `== 1`.
- DRM `XR24` is byte-order BGRX. `VA_FOURCC_RGBX` silently swaps R and B.
- VDEnc (`low_power=1`, `VAEntrypointEncSliceLP`) accepts **CQP only** on
  Gen9.5. CBR and VBR both fail `avcodec_open2` with `EINVAL`.
- The peer's IP and the `p2p-*` interface change between sessions. Resolve the
  peer by ARP-scanning `p2p-*`; never assume a subnet.
- Benchmark against a realistic load. `vkcube` saturates the same small GPU
  being measured and produces fake numbers.

---

## Build

The engine is C11, built with meson and ninja at `warning_level=3`, and compiles
with zero warnings.

```bash
cd engine
meson setup build
ninja -C build
```

Everything needed is already installed on the target machine — no `pacman -S`
required. Build dependencies are `wayland-client`, `gbm`, `libdrm`, `libva`,
`libva-drm` and the ffmpeg libraries (`libavcodec`, `libavformat`, `libavutil`,
`libavdevice`).

This produces:

| Binary | Purpose |
|---|---|
| `build/hyprcast-bench` | capture → import → VPP → encode, with per-stage histograms |
| `build/capture-probe` | capture rate and latency (`MOD=Y_TILED`, `OUTPUT=`, `VERIFY=1`) |
| `build/va-probe` | driver, dmabuf modifiers, encode entrypoints |
| `build/vpp-probe` | RGB→NV12 timing |

Capture is damage-driven, so the probes need something moving on screen to
measure anything meaningful.

## Run

The Python side is **standard library only** — there is no `requirements.txt`
and nothing to `pip install`.

```bash
python3 src/main.py
```

That scans for the sink, forms the Wi-Fi Direct group, negotiates the RTSP
session and starts streaming at 60 fps. Useful flags:

```bash
python3 src/main.py --wfd-scan            # discovery only, then exit
python3 src/main.py --fps 30 --bitrate 6M
python3 src/main.py --monitor eDP-1       # skip the monitor picker
python3 src/main.py --wfd-no-audio
python3 src/main.py --wfd-latency-log     # JSONL latency/session log
```

`python3 src/main.py --help` lists the rest.

System binaries still required: `ffmpeg`, `iw`, `wpa_cli`, `nmcli`, `gdbus`, and
`pactl` for audio monitor autodetection.

## Test without the TV

The loopback harness replays a byte-exact capture of a real session on
`127.0.0.1`, so the whole RTSP and MPEG-TS layer can be developed with the TV
switched off.

```bash
python3 tools/rtsp-loopback-source.py   # the WFD RTSP source
python3 tools/mock-sink.py              # replays the captured sink side
python3 tools/assert-ts.py              # validates the resulting MPEG-TS
```

Verified: 9,301 datagrams, 65,107 TS packets, 0 lost / 0 reordered /
0 duplicated / 0 malformed over 21.5 s at 4.6 Mbit/s, with all 12 `assert-ts`
checks passing.

---

## Layout

```
engine/            C11 native media path (meson + ninja)
  protocol/        vendored wayland-protocols XML + wayland-scanner output
  src/             hc.h is the contract; capture / dmabuf / vaapi / encode / hist / bench
reference/
  probes/          measured C probes — the numbers above came from these
  hyprland/        Hyprland 0.55.4 screenshare implementation
  aosp/            AOSP WFD source — the wire format is defined by these, not by specs
  sink/            byte-exact capture of a confirmed-working 720p60 session
  docs/            full design + fork analysis
src/               Wi-Fi Direct + WFD RTSP negotiation (stdlib only)
tools/             loopback harness: RTSP source, mock sink, MPEG-TS validator
```

## What was removed from fluxcast

DLNA (`upnpclient`), Chromecast (`pychromecast`), the HTTP/HLS bridge, the
system tray (`pystray`, `Pillow`), `--doctor` diagnostics, the UIBC touch
back-channel, the Microsoft-only LPCM muxer, the `xdg-desktop-portal` capture
path, and the `wf-recorder`/`x11grab` capture backends — about 3,700 lines. With
them went every third-party Python dependency.

The LPCM/TS muxer is preserved verbatim at `reference/mpegts_muxer.py` as a
dependency-free MPEG-TS reference.

---

## Credit and licence

hyprcast is a fork of **[fluxcast](https://github.com/IlyaP358/fluxcast)**.

**Author: IlyaP358 | Code licensed under GPL-3.0**

All of the Wi-Fi Direct group formation, the WFD RTSP state machine and the M1–M6
negotiation in `src/` are IlyaP358's work, carried over from upstream. This fork
adds the native capture and encode engine and removes what this one machine does
not need.

Licensed under **GPL-3.0-or-later**, the same licence as upstream. See
[LICENSE](LICENSE) for the full text.
