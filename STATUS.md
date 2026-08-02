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

## ⚠️ Project-kill gate — not yet run

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
