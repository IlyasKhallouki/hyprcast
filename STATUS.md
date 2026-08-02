# hyprcast — status

Private Hyprland-only fork of [fluxcast](https://github.com/IlyaP358/fluxcast) (GPL-3.0).
Not distributed. Target: one machine, one sink.

```
i5-8350U (4c/8t, Kaby Lake-R) · UHD 620 (Gen9.5) · Hyprland 0.55.4
eDP-1 1920x1080@60 · Intel 8265 · PipeWire 1.6.7 · ffmpeg n8.1.2 · iHD 26.1.5
Sink: Xiaomi box running Google TV + a Miracast app
```

Goal: replace fluxcast's software media path with a zero-copy native one, and add
runtime control of fps / bitrate / volume / display / mirror-vs-second-screen.

---

## Measured on this box

All figures reproduced locally, not taken from the design doc.

| Metric | Measured | Note |
|---|---|---|
| `ext-image-copy-capture-v1` sustained | **60.65 fps, 0 failures** | under `vkcube` |
| commit→ready latency | **2.8 – 3.6 ms** | beats the 4.5 ms design estimate |
| VPP BGRX→NV12 (GPU) | **0.881 ms/frame** | 1135 fps ceiling, CPU never touches pixels |
| H.264 Constrained Baseline encode | `VAEntrypointEncSlice` + **`EncSliceLP`** | low-power VDEnc path available |
| VAAPI encode cost | **45% of one core** | and that includes the `hwupload` we are deleting |
| x264 veryfast (what fluxcast uses at 1080p) | **197% of one core**, 84 fps ceiling | the reason it is choppy |

Frame budget at 60 fps is 16.67 ms. Capture ~3 + VPP ~0.9 + encode ~3.6 ≈ **7.5 ms**.

### dmabuf modifier support (`va-probe`)

```
Y_TILED      planes=1 fourcc=BGRX -> IMPORT OK
Y_TILED_CCS  planes=2 fourcc=BGRX -> resource allocation failed   <-- must be excluded
LINEAR       planes=1 fourcc=BGRX -> IMPORT OK
X_TILED      planes=1 fourcc=BGRX -> IMPORT OK
```

Pin the GBM modifier list to `I915_FORMAT_MOD_Y_TILED` and assert
`gbm_bo_get_plane_count(bo) == 1`. `gbm_bo_create_with_modifiers2()` fed Hyprland's
full advertised list picks `Y_TILED_CCS`, which iHD refuses to import.

### Corrections to the design doc

- **Y_TILED vs LINEAR is not a 3 ms difference.** Measured under load they are
  indistinguishable (2.83–3.64 vs 2.79–3.12 ms). The design doc's 4.5-vs-7.43 ms
  figure did not reproduce, so "LINEAR costs 3 ms" is *not* a valid reason to reject
  Rust / wl-screenrec. The C choice rests only on: working C probes already exist,
  and rust/cargo are not installed.
- Idle-desktop measurements are meaningless here — capture is damage-driven, so an
  idle screen yields ~19–26 fps with 100 ms+ gaps. Always benchmark under `vkcube`.
- `presentation_time` is stamped at atomic commit, not page flip
  (`reference/hyprland/hypr_ScreenshareFrame.cpp:146-147`, and Hyprland's own TODO
  says so). Call the metric **commit→bitstream**, not glass→bitstream.

---

## Milestone 1B: the native path works

`engine/build/hyprcast-bench` runs capture → VA import → VPP → h264_vaapi → Annex-B
with the CPU never touching a pixel. Measured against a realistic load (mpv playing
a 1080p60 hardware-decoded clip in a window), 1920x1080 capture → 1280x720p60 wire:

| | capture p50 | vpp p50 | encode p50 | total p50 | fps | CPU |
|---|---|---|---|---|---|---|
| VBR / `EncSlice`   | 8.85 ms | 0.32 ms | 7.51 ms | 16.69 ms | **58.2** | 9.5% |
| CQP / `EncSliceLP` | 12.24 ms | 0.38 ms | **3.98 ms** | 16.79 ms | **57.8** | **6.6%** |

For comparison, fluxcast's current x264 path costs **197% of one core** for the same
job. This is roughly a 20-30x reduction in CPU.

Bitstream verified correct: Constrained Baseline, level 3.2, 1280x720,
`has_b_frames=0`, bt709/tv, decodes with zero errors, 18 keyframes at the
configured GOP. Colour checked against a `grim` screenshot (mean RGB within
3/765) so the BGRX-vs-RGBX import is genuinely right, not accidentally symmetric.

### VDEnc supports CQP only

`--low-power` selects `VAEntrypointEncSliceLP`. On Gen9.5 that path accepts **CQP
only** -- both CBR and VBR fail `avcodec_open2` with EINVAL, verified directly with
ffmpeg for constrained_baseline and for main. That is a genuine trade-off, not a
bug: VDEnc is a separate fixed-function block from the one hardware *decode* uses,
so it stays fast while a video plays, but it cannot hold a bitrate target -- which
is exactly what a Wi-Fi Direct link wants. Expose both, pick per session.

### Still short of 59.5 fps

Both paths land at ~58. The remaining cost is `capture` p50 8.8-12.2 ms: the
compositor's own GL blit into our bo, which we do not control. Total p50 sits
right at the 16.67 ms budget, so the loop is marginal rather than comfortable.

Benchmarking note: `vkcube` is the WRONG load to measure against -- it saturates
the same small GPU we are measuring, and produced misleading 39-52 fps numbers.
Use a realistic load.

### Pipelining

The loop keeps one capture outstanding while the previous frame converts and
encodes (`hc_capture_submit` / `hc_capture_wait`). The buffer being read by VPP is
explicitly re-reserved before the next submit, or the compositor picks the same bo
and overwrites it mid-read.

`vaSyncSurface` after VPP is off by default (`HC_VPP_SYNC=1` restores it): it forces
a CPU-GPU round-trip and VA-API already orders the encode behind the VPP.

## Loopback harness: the TV is no longer needed

`tools/rtsp-loopback-source.py` + `tools/mock-sink.py` replay the captured session
locally. Verified: 9,301 datagrams, 65,107 TS packets, **0 lost / 0 reordered /
0 duplicated / 0 malformed** over 21.5 s at 4.6 Mbit/s.

`tools/assert-ts.py` validates the result -- all 12 checks pass, including
"SPS+PPS immediately precede every IDR" (23 IDRs, all in band) and IDR interval
1000.0 ms with zero jitter.

It also answers the open PID question from the design doc:

> ATSParser reads the PMT PID out of the PAT and the elementary PIDs out of the
> PMT; it hardcodes nothing but PID 0. Any self-consistent layout parses.

So the `wfd.py:880-882` warning about PID layout does not bind for Android sinks.

---

## Milestone 2: end to end, verified against loopback

`hyprcast` is a working command. Everything below was measured on this box after
the milestone-2 integration, entirely through the loopback harness -- no TV.

### Full stack: RTSP -> engine -> wire

```
tools/rtsp-loopback-source.py + tools/mock-sink.py + tools/assert-ts.py

  15,915 datagrams   111,405 TS packets   26.7 s   6.33 Mbit/s
  lost=0  reorder=0  dup=0  ts-regress=0  malformed=0
  assert-ts.py: OVERALL PASS (12 of 12 checks)
  IDR interval: min 1000.0  max 1000.0  mean 1000.0 ms  -- zero jitter
```

### The CFR pacer works

The engine holds **60.0 fps on a completely idle desktop**, ~7-8 Mbit/s at
5-11% of one core, 0 drops, 0 capture failures. Stats report 36-50 `repeats`
per second: the pacer refilling grid slots that damage-driven capture left
empty. `hyprcast-muxtest` (no pacer) fails PAT/PMT/PCR on an idle desktop with
~160 ms gaps -- exactly the failure the pacer exists to prevent.

### Runtime control, live, with zero drops

```
baseline               fps=60.0  kbps=7484  drops=0
retune fps=30          fps=30.0  kbps=4474  drops=0
retune bitrate=3M      fps=30.0  kbps=2944  drops=0
+ idr + volume + mute  fps=30.0  kbps=3064  drops=0
```

The stream still validates 12/12 **after** the mid-stream encoder rebuild, so
SPS/PPS are being spliced ahead of the next IDR correctly.

### Audio opens the monitor, not the microphone

`alsa_output.pci-0000_00_1f.3.analog-stereo.monitor`. fluxcast's fallback to
Pulse device `"default"` opened the mic -- a silent privacy failure. Fixed.

---

## We crashed Hyprland once. Read this before touching outputs.

2026-08-02 16:09:30, Hyprland 0.55.4 aborted with SIGABRT (14.6 MB coredump),
taking awww-daemon, hyprsunset and xdg-desktop-portal-hyprland with it.

```
#5  Screenshare::CScreenshareFrame::transform() const
#6  CImageCopyCaptureFrame::CImageCopyCaptureFrame(...)
#12 libwayland-server   <- dispatching our create_frame
```

`CScreenshareFrame::transform()` returns `m_session->monitor()->m_transform`
with no null check (`reference/hyprland/hypr_ScreenshareFrame.cpp:487-494`),
reached from the frame constructor via `nextFrame()`
(`hypr_ImageCopyCapture.cpp:355`). Once the captured output is gone,
`monitor()` is null and `create_frame` aborts the entire compositor.

Trigger: headless outputs were being created and destroyed on the live session
while capture sessions were bound to them.

Guarded in `capture.c` (`hc_capture_submit` refuses once `output_gone` latches).
**Never test output create/remove against the live compositor** -- use a nested
Hyprland instance.

This is also a real Hyprland bug: any unprivileged client can abort the
compositor. Worth reporting upstream, hand-written -- hyprwm bans AI-authored
issues.

---


## The TV's Fluency/Definition setting decides what you can have

Measured with `hyprcast probe`, same TV, one setting changed between runs:

| TV setting | CEA mask | level | audio | best mode |
|---|---|---|---|---|
| Fluency | `0x00000020` | 3.1 | `AAC 00000001` | 1280x720p30 |
| **Definition** | `0x0001ffff` | 4.1 | `LPCM 3` + `AAC 7` | **1280x720p60** |

Counter-intuitive naming: **Definition** is the one that unlocks 60 fps.
Set it there and `hyprcast cast` negotiates 720p60 with no flags.

### max_hres/max_vres are binding, and fluxcast never parsed them

In Definition mode the sink advertises `CEA 0x0001ffff`, which *includes*
1080p30 and 1080p60 -- and then caps itself with `max_hres 0x0500 = 1280`,
`max_vres 0x02D0 = 720` in the same string.

hyprcast originally picked **1920x1080p30**: inside the CEA mask, inside
level 4.1 at 244800 of 245760 MB/s, and in violation of the sink's own stated
maximum. `grep max_hres` over the inherited fluxcast tree returned nothing --
the fields were never parsed. Now they are, and they filter mode selection:

```
definition + --fps 60 -> 1280x720p60   (was 1920x1080p30)
definition + --fps 30 -> 1280x720p30
fluency    + any fps  -> 1280x720p30   (only mode offered)
```

1080p60 is impossible regardless: 489600 MB/s against level 4.1's 245760.

---

## The sink's advertised capability VARIES between sessions

Two M3 responses from the same Xiaomi box, hours apart:

```
full:     00 01 01 08 0001ffff 0f3fffff 00000fff 00 0000 0000 13 0500 02D0
degraded: 00 00 01 01 00000020 00000000 00000000 00 0000 0000 00 none none
```

The degraded one offers **CEA mask 0x00000020 -- bit 5 only, 1280x720p30 and
nothing else**, level 3.1 instead of 4.1, VESA and HH tables empty, and no
max_hres/max_vres at all. hyprcast correctly negotiates 720p30 against it,
because that is the only mode on the table.

If you get 30 fps when you wanted 60, it is the TV, not hyprcast. Fully quit
and re-open the Miracast app on the TV -- a session that ended badly leaves it
advertising a reduced set. Check what you are being offered without starting a
session:

```bash
hyprcast scan
```

and read the negotiated mode in the cast log:

```
[hyprcast WFD RTSP] Negotiated media mode: 1280x720p60
```

---

## Using it

```bash
ln -sf ~/projects/hyprcast/packaging/hyprcast ~/.local/bin/hyprcast

hyprcast doctor                      # checks only what blocks a cast
hyprcast cast                        # discover the sink and start
hyprcast cast --fps 60 --bitrate 6M
hyprcast ctl fps 30
hyprcast ctl bitrate 6M
hyprcast ctl volume 50
hyprcast ctl mute
hyprcast status
hyprcast waybar                      # see packaging/waybar-module.jsonc
```

Sink ceiling is 1280x720p60 -- 1080p is not available (level 4.1 and
max_hres/max_vres both forbid it).

---

## ✅ Project-kill gate — PASSED

Confirmed on 2026-08-02 with a real session that rendered on screen. The concern
below was real but did not apply to this device -- Xiaomi's preinstalled app IS a
genuine WFD sink.

Discovery returned `wfd_dev_info=0x00111c440032` -> WFD **Primary Sink**, available
for session, P2P preferred, RTSP control port 7236, 50 Mbps max throughput.

Negotiated and confirmed working: **1280x720p60**, H.264 Constrained Baseline
level 3.2, AAC 48 kHz 2ch, `wfd_content_protection: none` accepted.

Session timeline from `reference/sink/wire-720p60-full-session.txt`: the complete
M1-M6 negotiation costs **333 ms**; the sink then sits for **8.024 s** before
sending PLAY. That stall is entirely sink-side.

Original concern, kept because it explains why this had to be checked first:

**Google TV ships no built-in Miracast sink.** AOSP dropped the
`libstagefright/wifi-display` sink, and `WifiP2pManager.setWfdInfo()` is gated behind
`android.permission.CONFIGURE_WIFI_DISPLAY` (`signature|privileged`) — so **no Play
Store app can advertise the WFD IE**. Apps sold as "Miracast receivers" (AirScreen,
PigeonCast) are AirPlay/DLNA/Cast over infrastructure Wi-Fi, not WFD.

Only Xiaomi's *preinstalled* app can be a real sink. Until that is confirmed,
everything else in this repo is speculative.

```bash
# Put the Xiaomi Miracast app into discovery mode first, then:
sudo wpa_cli -i p2p-dev-wlan0
> set wifi_display 1
> p2p_find
```

Pass = a peer appears **with a WFD IE** and a display `pri_dev_type`.
Fail = the premise is wrong; Google Cast is the only fallback and it has a ~400 ms
design floor, which defeats the entire point.

### Wi-Fi constraint discovered on this card

```
#{managed}<=1, #{P2P-client,P2P-GO}<=1, #{P2P-device}<=1, total<=3, #channels<=2
```

Concurrent STA + P2P works. But `phy#0` is **self-managed** (firmware LAR), every
5 GHz channel is flagged `No IR`, and only **5170–5250 (ch 36/40/44/48)** carries
`IR-CONCURRENT` — legal *only while wlan0 stays associated on that channel*.
Disconnect from the home AP and a 5 GHz GO silently drops to 2.4 GHz, where 1080p60
is not happening. Re-read `iw reg get` at group-formation time; force
`p2p_group_add freq=<wlan0's freq>`.

---

## Layout

```
engine/            C11 native media path (meson + ninja)
  protocol/        vendored wayland-protocols XML + wayland-scanner output
  src/             milestone 1B sources land here
reference/
  probes/          measured C probes — all of the above numbers came from these
  hyprland/        Hyprland 0.55.4 screenshare implementation, for behaviour questions
  aosp/            AOSP WFD source — the wire format is defined by these, not by specs
  docs/            full design + fork analysis
  mpegts_muxer.py  fluxcast's LPCM/TS muxer, kept as a dependency-free TS reference
src/               inherited fluxcast Python (8,834 lines, 71.5% slated for deletion)
```

## Build

```bash
cd engine && meson setup build && ninja -C build
```

Everything needed is already installed — no `pacman -S` required.

```bash
vkcube &                                   # capture is damage-driven; you need motion
MOD=Y_TILED ./build/capture-probe          # capture rate + latency
./build/va-probe                           # driver, modifiers, encode entrypoints
./build/vpp-probe                          # RGB->NV12 timing
pkill vkcube
```

## Next

1. **Run the WFD gate above.** 90 minutes. Nothing else matters until it passes.
2. Milestone 1B: `hyprcast-bench` — capture → import → VPP → encode → `/dev/null`
   with per-stage histograms. Exit criteria in `reference/docs/BUILD_PLAN.md`.
3. Milestone 2: `rtp_mpegts` on the wire + `tools/mock-sink.py`, so the RTSP/TS
   layer can be developed without the TV.

Full milestone list, deletion inventory and risk analysis:
`reference/docs/BUILD_PLAN.md`.
