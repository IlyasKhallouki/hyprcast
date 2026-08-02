# hyprcast — Build Plan

**Target:** i5-8350U / UHD 620 (Gen9.5) / Hyprland 0.55.4 (a0136d8c) / eDP-1 1920×1080@60 / Intel 8265 / PipeWire 1.6.7 / ffmpeg n8.1.2 / iHD 26.1.5. Private fork, never distributed. Priorities: (1) latency, (2) runtime control, (3) Hyprland-only.

Where the seven investigations disagreed with their own verification blocks, I took the verification. Where investigations disagreed with each other, I say so below and pick.

---

## 1. ARCHITECTURE

### Two processes, one seam, nine scalars

```
┌─────────────────────────────── hyprcast (Python 3.14, stdlib only) ────────────────────────────────┐
│  wfd.py        RTSP server :7236  M1/M3/M4/M5/SETUP/PLAY/M16 keepalive, CEA mode negotiation        │
│  p2p.py        NetworkManager + wpa_supplicant P2P over gdbus subprocess, WFD IE construction       │
│  hypr.py       hyprctl: monitors -j, output create headless hyprcast-0, keyword monitor, remove     │
│  engine.py     owns the child + the socketpair. ~120 lines. THIS IS THE SEAM.                       │
│  ctl.py        $XDG_RUNTIME_DIR/hyprcast.sock — hyprcastctl / waybar / TUI clients                  │
└──────────────────────────────────────────┬──────────────────────────────────────────────────────────┘
                                           │  fd 3, socketpair(AF_UNIX, SOCK_STREAM)
                                           │  newline-delimited JSON, both directions
                                           │  verbs: prepare start retune idr source stop quit
                                           │  events: ready stats error
┌──────────────────────────────────────────┴─── hyprcast-engine (C11, ~2600 lines) ────────────────────┐
│  thread A  wayland: ext-image-copy-capture-v1 → GBM dmabuf                                          │
│  thread B  VAAPI VPP + libavcodec h264_vaapi + libavformat rtp_mpegts + sendmsg                     │
│  thread C  libavdevice pulse → AAC-LC (same AVFormatContext as B)                                   │
│  main      epoll{ fd3, timerfd(CFR grid), timerfd(staleness watchdog) }                             │
└──────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### The pixel path — where the bytes physically live

| Stage | Memory | Who owns it | CPU touches pixels? |
|---|---|---|---|
| Hyprland composites eDP-1 | GPU, `RENDER_MODE_TO_BUFFER` GL pass into **our** dmabuf (`hypr_ScreenshareFrame.cpp:391`) | us (GBM) | no |
| Capture buffer | GBM bo, **XR24 / `I915_FORMAT_MOD_Y_TILED` (0x0100000000000002), 1 plane**, 8,388,608 B | us, pool of 3 | no |
| Same bo as VA surface | imported once at pool build via `VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2`, `VA_FOURCC_BGRX`, `VA_RT_FORMAT_RGB32` | us | no |
| RGB→NV12 | VAAPI VPP (VEBOX/SFC), `VAProfileNone`/`VAEntrypointVideoProc`, **0.88 ms** measured | ffmpeg's `AVHWFramesContext` allocates the NV12 target | no |
| NV12 surface → encoder | `AVFrame.data[3] = VASurfaceID`, `format = AV_PIX_FMT_VAAPI`. Same `VADisplay`. **No copy, no `hwupload`.** | ffmpeg pool, depth 6 | no |
| H.264 Annex-B | `AVPacket`, ~1–2 MB/s | ffmpeg | **yes — bitstream only** |
| MPEG-TS + RTP | `libavformat rtp_mpegts`, 1328 B datagrams | ffmpeg | yes — bitstream only |
| `sendmsg()` → UDP → P2P link | kernel | — | — |

**Confirmation:** the CPU never reads or writes a pixel. At 1080p60 the pixel rate is 1920·1080·4·60 = **497 MB/s of BGRX**, and none of it crosses the PCIe-to-DRAM-to-CPU boundary. The CPU handles the H.264 bitstream (~1–2 MB/s) and the audio PCM (48 kHz × 2ch × 4 B = 384 KB/s). fluxcast today pushes 186 MB/s of raw NV12 through a pipe between two processes (`wfd.py:1417-1532`); that pipe is deleted.

The measured 45%-of-a-core figure in the brief was paying for `format=nv12,hwupload` — a CPU colour convert plus an 8.3 MB/frame upload. **That entire cost disappears.** Expect single-digit percent of one core for the whole engine.

### The control path

- **Session setup (~30 s, once per cast):** Python discovers the peer over NM/wpa_supplicant D-Bus, forms the P2P group, binds RTSP on the p2p interface IP, exchanges M1–M5 + SETUP + PLAY, then sends `start` down fd 3 with the nine negotiated scalars.
- **Runtime (milliseconds):** everything else. `retune {fps}` re-times the CFR grid — free, no encoder touch, because PTS comes from a 90 kHz grid we own. `retune {bitrate}` rebuilds only the `AVCodecContext`, keeps the hwframes pool, the `AVFormatContext`, the socket and the RTP sequence space, and splices fresh SPS/PPS ahead of the next IDR. `idr` sets `pict_type = AV_PICTURE_TYPE_I` on the next frame. `source` destroys the capture session and rebuilds it against a different `wl_output`; the encoder never notices because the wire size is frozen.
- **Nothing renegotiates over RTSP.** This is the load-bearing insight from investigation 6 and it holds up: `wfd_video_formats` carries native/profile/level/CEA/VESA/HH masks and **no bitrate field at all** (`wfd.py:656-681`). Sending fewer frames than the negotiated mode is legal — timing lives in RTP/PTS and the TS PCR. Source resolution is decoupled from wire resolution by VPP scaling. So `stop`+`start` is the only ~30 s operation in the system.

---

## 2. THE DECISIONS

| # | Decision | Verdict | Reason |
|---|---|---|---|
| 1 | **Language** | **C11**, meson + ninja | The working, measured reference implementations for *this exact box* are already C: `scratchpad/hyprcast-probe/probe.c` (60.03 fps sustained, 4 modifiers, buffer-depth sweep, headless, constraint renegotiation), `vatest.c`, `vpptest.c`, `encbench.c`, `rev/ccs.c`, `rev/curs.c`, `rev/vppscale.c`. gcc 16.1.1 / meson 1.11.1 / ninja 1.13.2 / wayland-scanner 1.25.0 are **already installed**; rust, cargo and clang are **not** (verified: `which cargo` fails) and cost ~560 MiB. |
| 2 | **Not Rust / not wl-screenrec** | reject | wl-screenrec is not liftable as-is: it inverts the allocator (VAAPI allocates, compositor fills) and therefore **requires `DRM_FORMAT_MOD_LINEAR`** — `avhw.rs:201-205` literally says *"unknown how to request non-linear frames in vaapi"* and `main.rs:1500` gates on it. LINEAR measures **7.43 ms** present→ready vs **4.5 ms** for Y_TILED. Paying 3 ms/frame of latency to reuse someone else's 236 lines is backwards when priority #1 is latency and we already have working code. Read it for reference; don't depend on it. |
| 3 | **Capture protocol** | `ext-image-copy-capture-v1` **only** | Confirmed live: `ext_image_copy_capture_manager_v1` v1 name 62, `ext_output_image_capture_source_manager_v1` v1 name 60, `zwp_linux_dmabuf_v1` v5 name 67. Delete the `zwlr_screencopy` fallback entirely — it has no session, so it costs a full round-trip per frame, and it has no constraint-change signal. |
| 4 | **Buffer allocator** | **GBM allocates**, 3 bos, modifier list of **exactly one entry: `I915_FORMAT_MOD_Y_TILED`** | `gbm_bo_create_with_modifiers2()` fed Hyprland's advertised list picks `I915_Y_TILED_CCS`, which is 2 planes, and **iHD refuses to import it** ("resource allocation failed") — reproduced at every object size and with the aux plane dropped (`rev2/ccs2.c`, `ccs3.c`). Strip `DRM_FORMAT_MOD_INVALID`. Assert `gbm_bo_get_plane_count()==1` and abort loudly. |
| 5 | **VA import** | `VADRMPRIMESurfaceDescriptor` + `VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2` (`va_drmcommon.h:89,130`), fourcc **`VA_FOURCC_BGRX`** | The legacy `VASurfaceAttribExternalBuffers` has no modifier field. DRM `XR24` is byte-order BGRX — `VA_FOURCC_RGBX` imports without error and silently swaps R and B. Import **once at pool build**, and **`close()` the dmabuf fd after `vaCreateSurfaces`** (iHD dups it) or you leak an fd per surface per renegotiation. |
| 6 | **VAAPI driver** | **iHD, pinned.** `vaSetDriverName(dpy, "iHD")` (`va.h:458`) before `vaInitialize`; assert the vendor string contains `iHD` and abort otherwise. | i965 returns `VA_STATUS_ERROR_INVALID_PARAMETER` for **every** PRIME_2 import — it doesn't implement the memory type at all. i965 also has no intra-refresh and only 4 L0 refs. It is already the default; pinning is insurance against a libva update reshuffling fallback order. |
| 7 | **RGB→NV12** | VAAPI VPP, `VAProfileNone` / `VAEntrypointVideoProc` | The session advertises **only AR24 and XR24** — confirmed at source, `CScreenshareSession::calculateConstraints` pushes `getPreferredReadFormat()` + its alpha variant and nothing else. There is no YUV path out of the compositor. VPP costs **0.88 ms** (1132 fps ceiling), zero CPU, and gives free scaling (1080p→720p in the same pass: 0.815 ms). |
| 8 | **Colour** | `output_color_standard = VAProcColorStandardBT709` **and** `output_color_properties.color_range = VA_SOURCE_RANGE_REDUCED` (`va_vpp.h:559-561`). Signal both in the H.264 VUI. | Range is a *different knob* from standard and every investigation except one forgot it. RGB capture is full range; H.264 to a WFD sink is limited range. Leaving `color_range` at UNKNOWN is the real cause of crushed blacks — fluxcast's x264 path already forces `out_range=tv` (`wfd.py:1473`). |
| 9 | **Encoder** | **libavcodec `h264_vaapi`**, not direct libva | Direct libva measures p50 3.58 ms vs ffmpeg's structurally-identical path, so the delta is ~1 ms. What you'd buy that 1 ms with is hand-writing SPS/PPS/VUI/AUD/level/cropping — and the one thing that was *never validated* in the direct-libva work is exactly that: `encbench.c` reads `seg->size` and discards the bitstream, never writes a file, never decodes one. Hand-written parameter sets are the classic "works on my laptop, black screen on the TV" failure. ffmpeg gets level_idc, VUI, `has_emulation_bytes` and `VAConfigAttribEncPackedHeaders` right for free. |
| 10 | **Entrypoint** | `VAEntrypointEncSlice`. **`low_power=0` always.** | Measured on this box: `-low_power 1` + CBR/VBR/QVBR all fail with *"Driver does not support any RC mode compatible with selected options (supported modes: CQP)"*. LP's BRC lives in HuC firmware which isn't loaded. The claimed LP latency win doesn't reproduce (EncSlice p50 3.58 / p99 9.61 vs LP p50 3.42 / p99 7.09 — noise, and the LP run emitted a 17-byte "keyframe", i.e. it wasn't configured validly). Do not chase `i915.enable_guc=2`; it buys nothing measured and risks GuC hangs on KBL. |
| 11 | **Rate control** | **VBR, `maxrate == bitrate`, `bufsize == bitrate/fps`.** NOT CBR. | This is my call and it overrides investigation 2. Measured **today** on this box: <br>• VBR + 1-frame bufsize on `testsrc2` 1080p30 @10M: **peak packet 40,484 B vs 41,667 B budget = 0.97×**. That is the full issue-#19 burst fix. <br>• VBR on 150 frames of pure black: **13,445 bytes total**. CBR on the identical source: **6,250,254 bytes** — **465× more**, i.e. 10 Mbit/s of pure padding on an idle desktop. <br>With a duplicate-frame CFR pacer (mandatory, see #13), CBR would saturate the Wi-Fi Direct link with stuffing whenever nothing moves. VBR gets both properties. |
| 12 | **Encoder flags** | `-profile:v constrained_baseline -coder cavlc -bf 0 -async_depth 1 -aud 1 -sei 0 -level <negotiated> -g <fps> -idr_interval 0 -rc_mode VBR -b:v N -maxrate N -bufsize N/fps -max_frame_size <2×budget>` | `-bf 0` kills reorder delay (default is **on**: measured `IBBPBBP`, `max_num_reorder_frames=1`). `-async_depth 1` stops the encoder holding a frame (default 2). Default GOP is effectively infinite — measured **one** I-frame in 120 frames. `-sei` default is `identifier+timing+recovery_point+a53_cc`; kill it. WFD has only two profile bits ever (CBP=0, CHP=1 — AOSP `VideoFormats.h`), so Main is not even representable; CBP is universally safe. |
| 13 | **Frame pacing** | **CFR repeat-pacer on a 90 kHz grid, mandatory.** Plus a **550 ms staleness watchdog** that destroys and re-creates the frame object. | Capture is purely damage-driven — measured 60.03 fps under vkcube, but a single **2030 ms gap** on an idle desktop. A WFD sink needs continuous CFR. On a timer tick with no new frame, re-encode the held NV12 surface: a near-empty all-skip P-frame, a few hundred bytes under VBR. The watchdog exploits a Hyprland-specific lever: `m_shareStopTimer` fires 500 ms after the last copy and sets `m_sharing=false`, so the *next* `create_frame` gets `m_isFirst=true` and triggers `scheduleFrameForMonitor(AQ_SCHEDULE_NEEDS_FRAME) + damageMonitor()` (`hypr_ScreenshareFrame.cpp:121-125`). That bounds staleness at ~550 ms instead of 2 s. |
| 14 | **Buffer depth** | 3 RGB capture bos, ffmpeg NV12 pool `initial_pool_size = 6`, **plus one dedicated hold surface outside the rotation** | The protocol forbids >1 live frame object per session (`duplicate_frame`, enforced at `hypr_ImageCopyCapture.cpp:23-28`), so captures cannot be pipelined and depth does nothing for throughput. Depth exists only to stop capture N+1 overwriting a buffer VPP is still reading. **The hold surface is not optional:** the pacer re-encodes "the last good NV12", and if that is a pool member, VPP re-targets it three frames later and the duplicate tears. |
| 15 | **Muxer** | `libavformat rtp_mpegts`, in-process, with fluxcast's exact tuned parameters | `wfd.py:872-891`: `mpegts_pmt_start_pid 4096` (0x1000), `mpegts_start_pid 4113` (0x1011), `streamid 1:4352` (0x1100 audio), `mpegts_flags resend_headers+pat_pmt_at_frames`, `pat_period 0.1`, `pcr_period 20`, `muxdelay 0`, `muxpreload 0`, `flush_packets 1`. URL from `wfd.py:709-721` including `pkt_size=1328` (12 RTP + 7×188) and `local_rtpport`. **Do not "clean these up"** — the source comment says so and they were found by trial against real sinks. Hand-rolled TS is a phase-2 option only if measurement shows burst loss. |
| 16 | **Audio** | libavdevice `pulse` on `<default-sink>.monitor` → native `aac` `-profile:a aac_low` 48 kHz 2ch 128k, **second stream on the same `AVFormatContext`** | Stream type **0x0F**. Delete LPCM: stock Android maps stream_type **0x83 → `ElementaryStreamQueue::AC3`** (`ATSParser.cpp:525-528`), so LPCM to this sink is decoded as AC-3. The LPCM muxer exists only for the Microsoft Wireless Display Adapter (its own docstring). Same `AVFormatContext` is what gets you correct interleaving and a shared PCR with no cross-process clock reconciliation. Derive audio PTS from **cumulative sample count**, not wall clock (`wfd_lpcm_mux.py:501-502` gets this wrong). |
| 17 | **What stays Python** | RTSP M1–M7, P2P bring-up, CEA negotiation, Hyprland orchestration, control socket. **~1,650 lines, stdlib only.** | RTSP is ~15 messages per session plus a 25 s keepalive — nothing per-frame. The P2P layer isn't protocol code at all: it's `gdbus` subprocess orchestration of NetworkManager with a timeout state machine (`wfd.py:2633-2758, 2761-2805, 2877-3044`). Porting it to C means adopting a D-Bus library and reimplementing several hundred lines of the flakiest code in the system for **zero** latency gain. All six `requirements.txt` deps die with the deleted files. |
| 18 | **The seam** | `_WFDRTSPHandler._start_media()` (`wfd.py:2204-2248`). Replace `WFDMediaPipeline` with `NativeSender` exposing `__init__ / start / stop / is_alive / tx_summary`. | Nine scalars cross, one direction, as plain data: `tv_ip, local_ip, sink_rtp_port, source_port, W, H, fps, bitrate, no_audio` — plus `audio_device` and `monitor.name`. Nothing below calls back up. **Two leaks to seal first** (M5 step 1): the RTSP layer reads `media.processes` at `wfd.py:2298, 2318, 2322`; and `_active_rtsp_probe` builds a second pipeline at `wfd.py:3451-3462`, bypassing `_start_media` entirely. |
| 19 | **IPC** | newline-JSON on an **inherited socketpair, fd 3** | Lifetime is exactly the child's lifetime — engine dies, Python's read returns EOF, no timeouts, no stale socket in `$XDG_RUNTIME_DIR`, no permissions. Drivable by hand with `socat`. Seven verbs, hard cap. |
| 20 | **Engine lifecycle** | Spawn **once** at hyprcast start, keep idle. `prepare` on M3, `start` on PLAY. | VAAPI device init + hwframes pool allocation dominates PLAY→first-frame. `wfd.py:2304-2330` already instruments "first RTP bytes after PLAY", so this is measurable rather than assumed. |
| 21 | **Testing** | ~250-line loopback mock sink, `tools/mock-sink.py` | **fluxcast is the RTSP *server*** (`wfd.py:1814` subclasses `StreamRequestHandler`, `wfd.py:2390` `WFDRTSPServer`); the sink is the TCP *client*. So a mock sink needs no Wi-Fi, no P2P, no radio, no TV. Invert the existing 130-line client walker at `wfd.py:3318-3447`. |
| 22 | **Build/package** | meson + ninja for the engine, plain Python package alongside, `justfile` as the only entry point, local PKGBUILD + `makepkg -si` for install | One compiled artifact. systemd `--user` unit `PartOf=graphical-session.target` (verified active under uwsm), installed **but not enabled** — a laptop should not hold port 7236 and a P2P device open all day. |

---

## 3. BUILD ORDER

Ordered by risk-reduction × learning. Total ≈ 13–15 working days.

---

### **MILESTONE 1 — Gate + Bench** · 2 days · *run 1A and 1B in parallel*

#### **1A — The hardware gate (90 minutes, do this first, tonight)**

This is a **project-kill check**. Everything downstream is worthless if it fails.

1. Put the Xiaomi box's Miracast app into discovery. Run:
   ```
   sudo wpa_cli -i p2p-dev-wlan0
   > set wifi_display 1
   > p2p_find
   ```
   Confirm a peer appears **with a WFD IE** and a display `pri_dev_type`. Google TV ships **no built-in WFD sink**, and `WifiP2pManager.setWfdInfo()` is gated by `android.permission.CONFIGURE_WIFI_DISPLAY`, which is `signature|privileged` — so no Play Store app (AirScreen, PigeonCast) can be a real sink. Only Xiaomi's preinstalled app can. **If no WFD peer appears, stop. The premise is wrong.**
2. Form the group pinned to wlan0's channel: `p2p_go_intent 15`, then `p2p_group_add freq=5200`. Verify with `iw dev` that both interfaces coexist and the GO landed on 40.
3. **`iperf3 -c <peer> -t 30` over the P2P link.** Write the number down. This sets the achievable resolution/fps before you tune a single encoder flag.
4. Dump the sink's raw M3 `GET_PARAMETER` response body verbatim to a file. Every format table in the fork derives from that string, not from AOSP defaults. Check whether it sends `wfdx_video_formats` (Windows-influenced, different stride).

**Measured state as of today, which you must re-check because it drifts:** `phy#0 (self-managed)`, reg domain now `country 00` (it was MA), every 5 GHz channel flagged `No IR`, `IR-CONCURRENT` present only on 5170–5250 (ch 36–48), wlan0 associated on **ch 40**. Interface combos: `#{managed}<=1, #{P2P-client,P2P-GO}<=1, #{P2P-device}<=1, total<=3, #channels<=2`. A 5 GHz GO is legal **only while wlan0 stays associated on that same channel**.

**Exit criteria:** peer discovered with WFD IE ✓ · P2P group formed on 5 GHz ✓ · iperf3 ≥ 25 Mbit/s ✓ · M3 body captured ✓

---

#### **1B — `hyprcast-bench`: the media-path number (1–2 days)**

One binary, no networking, no Python, no muxer. Capture → import → VPP → encode → `/dev/null`, with a per-stage histogram. This is the number that justifies the whole project.

Assembled from code that already works: `scratchpad/hyprcast-probe/probe.c` (capture, 14.5 KB, has `MOD=`/`OUTPUT=`/`VERIFY=`/`CURSORS=` env knobs), `vatest.c` (import), `vpptest.c` (VPP), `encbench.c` (encode), `rev/vppscale.c` (scaling).

Prints, over a 60-second run under `vkcube`:
```
capture: n=3600  fps=59.98  commit→ready p50=4.5 p90=5.8 p99=8.9 max=14.2 ms
vpp:     p50=0.88 p90=1.02 p99=1.9 ms
encode:  p50=3.6  p90=4.2  p99=6.2 max=12.0 ms
total (commit→bitstream): p50=9.1 p99=16.4 ms
dup-frame ratio: 0.3%   bitrate: 7.8 Mbit/s   cpu: 6.2% of one core
```

**Exit criteria:** ≥59.5 fps sustained under animation ✓ · total p99 < 16.67 ms ✓ · CPU < 15% of one core ✓ · `gbm_bo_get_plane_count()==1` assert never fires ✓ · output file plays in `ffplay` with correct colours (no R/B swap, no crushed blacks) ✓

**Note the honest caveat that every investigation glossed over:** `presentation_time` is **not** the presentation instant. Hyprland stamps it with `Time::steadyNow()` at the top of `CScreenshareFrame::copy()` (`hypr_ScreenshareFrame.cpp:146-147`), driven from `onOutputCommit` — i.e. at atomic commit, not page flip. Hyprland's own comment says *"TODO: is this right? this is right after we commit to aq, not when page flip happens.."*. So label the metric **commit→bitstream**, not glass→bitstream, and expect up to one extra scanout period in reality.

---

### **MILESTONE 2 — Bitstream on the wire + loopback mock sink** · 2 days

- Add the in-process `rtp_mpegts` `AVFormatContext` with the exact parameters from `wfd.py:872-891` and the URL form from `wfd.py:709-721`.
- Run the muxer write on **its own thread behind a bounded ring**, dropping **whole frames only** when it fills. `libavformat`'s UDP writer can block; it must never stall the encode thread.
- `tools/mock-sink.py` (~250 lines): connect to `127.0.0.1:7236`, answer M1 OPTIONS, answer M3 with the **byte-exact string captured in 1A**, ACK M4, on M5 `wfd_trigger_method: SETUP` send SETUP then PLAY, bind the UDP port, strip 12-byte RTP headers, write 1316-byte TS payloads to a fifo.
- `tools/assert-ts.py`: PAT period ≤ 100 ms · PMT on 0x1000 · stream_type 0x1B on 0x1011 · audio 0x0F on 0x1100 · **SPS+PPS immediately precede every IDR** (this last one is what catches the bitrate-retune bug before the TV does).
- `ffplay -f mpegts -fflags nobuffer -flags low_delay -analyzeduration 0 -probesize 32 -` reading the fifo.

**Critical wire-format gotcha:** AOSP's `VideoFormats::parseFormatSpec` is a **fixed-stride** scanner — skip 6 chars, then `offset += 60` per codec block — guarded by `CHECK_LE(offset + 58, size)`, which is an **abort**, not a soft error. A malformed `wfd_video_formats` **kills the sink process**. Emit exactly `%02x %02x %02x %02x %08x %08x %08x 00 0000 0000 00 none none` with `native=0x00` in M4, one codec block, literal `none none`.

**Exit criteria:** desktop visible in ffplay over loopback ✓ · `assert-ts.py` clean ✓ · glass-to-wire p99 measured ✓

---

### **MILESTONE 3 — Correctness core: pacer, watchdog, state machine** · 2 days

This is the milestone that decides whether hyprcast is reliable. Four things, all of which the naive implementation gets wrong:

1. **CFR pacer.** 90 kHz PTS grid at the configured fps. `epoll` over `{wl_display fd, timerfd, fd 3}`. Use `wl_display_prepare_read`/`read_events`/`dispatch_pending` — **never `wl_display_dispatch`**, which blocks inside libwayland and will not wake for the timerfd, silently converting CFR back into damage-driven output. On `ready`: VPP + encode immediately, stamp the current grid slot, re-arm. On timer with no frame: re-encode the **hold surface**, stamp, re-arm.

2. **Unified failure rule — this is where the design was wrong.** The stated rule ("on `failed(reason=1)`, issue no `create_frame` until the next `done`") **deadlocks**: `headless.log` shows `done (constraints round #2)` arriving **before** the first `failed(reason=1)`, and there is no third `done` among the 394 failures. Also `reason=0` does **not** mean transient here — `hypr_ImageCopyCapture.cpp:349-353` sends `sendConstraints()` **and** `failed(FAILURE_REASON_UNKNOWN)` when the cached `m_bufferSize` differs from live. Correct rule:

   > On **any** `failed(reason)`: destroy the frame object. Compare the last-committed constraints against the current pool. If they differ → tear down pool (closing every dmabuf fd), reallocate, resume. If identical → back off exactly one frame interval, then retry.

   Never retry without that gate: failures do **not** wait for damage, and a naive loop burns **394 failures in ~0.4 s** — a pegged core the moment the user changes resolution.

3. **Staleness watchdog.** No `ready` within 550 ms → destroy + recreate the frame object to trip Hyprland's `m_shareStopTimer` path.

4. **Permission watchdog — the silent-hang killer nobody mentioned.** `CScreenshareFrame::copy()` checks `clientPermissionMode(PERMISSION_TYPE_SCREENCOPY)` and, under `PERMISSION_RULE_ALLOW_MODE_PENDING`, **returns without copying and without invoking the callback** (`hypr_ScreenshareFrame.cpp:150-157`) — no `ready`, no `failed`, forever. `perms.md` says the default for `screencopy` is **ASK**. `ecosystem:enforce_permissions` is `0` on this box today, but a new unsigned binary called `hyprcast` is exactly what that system targets. So: hard timeout on `capture`→`ready|failed`, and document the config:
   ```lua
   hl.permission({ binary = "/usr/bin/hyprcast-engine", type = "screencopy", mode = "allow" })
   ```
   (requires a Hyprland restart to take effect).

**Exit criteria:** static desktop for 60 s still emits a valid, decodable CFR stream ✓ · `hyprctl keyword monitor eDP-1,1280x720@60` mid-stream recovers within 2 frames and CPU never spikes ✓ · `hyprctl output remove` produces a clean `stopped` and rebuild ✓ · no fd leak across 20 renegotiations (`ls /proc/PID/fd | wc -l` flat) ✓

---

### **MILESTONE 4 — Control channel + runtime knobs** · 1 day

fd-3 JSON. Verbs `prepare start retune idr source stop quit`; events `ready stats error`. Drive it by hand with `socat` before Python touches it.

- `retune {fps}` — pure grid retiming. **Free.** Gate the `create_frame` call, not the output, so the compositor does less work and the frame you get is fresher.
- `retune {bitrate}` — rebuild only the `AVCodecContext`. Keep hwframes pool, `AVFormatContext`, socket, RTP sequence space. **Splice the fresh `extradata` (SPS+PPS) in front of the next IDR** or the sink freezes on the last good frame with no error anywhere.
- `idr` — `frame->pict_type = AV_PICTURE_TYPE_I` + `AV_FRAME_FLAG_KEY`. Rate-limit to one per 500 ms.
- `source {output|toplevel}` — destroy capture session, rebuild against the new `wl_output`. Encoder untouched.
- `stats` at 1 Hz: fps, tx kbps, dropped, encode p50/p99, **duplicate-frame ratio** (the single most useful tuning number — it tells you instantly whether you're latency-bound or damage-starved).

---

### **MILESTONE 5 — Fork and strip fluxcast; wire the seam** · 2 days

Fork from `https://github.com/IlyaP358/fluxcast.git` (the checkout at `scratchpad/fluxcast/` is 8,834 lines and matches the inventory in §5; `scratchpad/fluxcast-src/` is a **different, older** 8,798-line tree — do not use it).

Order matters:
1. **Seal the seam first, before deleting anything.** Add `WFDMediaPipeline.is_alive()`, replace the three `media.processes` reads at `wfd.py:2298, 2318, 2322`. Merge `_active_rtsp_probe` (`wfd.py:3258-3487`) into the handler as a transport variant so `_start_media` is the single entry point, deleting the duplicate pipeline construction at `wfd.py:3451-3462`. Fix `sock.settimeout(8.0)` at `wfd.py:3298` while you're there.
2. Whole-file `git rm` (§5).
3. **Rip vendor quirks as one commit:** delete `peer_name` at `wfd.py:163` and `wfd.py:3566`, then fix the nine `NameError`s at `417, 471, 1095, 1150, 1317, 1896, 2065, 2176, 3368`. **Careful at 2176-2179:** that block is `if "microsoft" not in peer_name: self._schedule_rtsp_keepalive(20.0)` — it is the **only** call site that starts M16. Unwrap the conditional, keep line 2177. Deleting the range kills the keepalive and the sink drops the TCP connection.
4. Rip UIBC (20 call sites), firewalld (`wfd.py:2458-2547`), the diagnostics gate (`wfd.py:3493-3501`), all four `_start_desktop_*` backends (`wfd.py:1034-1745`, 712 lines in one block).
5. **Read `wfd.py:1475-1504` into a scratch note before deleting** — that ffmpeg arg list is the reference encoder config.
6. Replace `capture.py` (568 lines) with a ~30-line `monitors.py` parsing `hyprctl monitors -j`. Keep the `Monitor` NamedTuple shape.
7. `engine.py`: ~120 lines owning the socketpair.

**Keep the wf-recorder path alive behind `--legacy-encoder` (116 lines) until Milestone 6 passes.** Otherwise you have a multi-day window with no working stream and no A/B baseline against the real sink.

---

### **MILESTONE 6 — First real cast to the Xiaomi** · 1 day

Everything meets the actual TV. Set the Xiaomi app to **Fluency Priority** before measuring anything — it's a sink-side buffer knob that will move the number more than most sender-side work.

Measure glass-to-glass **once**, manually: phone camera at 240 fps against an on-screen millisecond counter. Expect **150–400 ms total** with ~35–60 ms of it yours. Published Miracast sink numbers land at ~250 ms tuned, ~400 ms for an Android-class receiver.

**Wire the IDR request properly here.** `wfd.py:2118-2123` currently prints *"Sink requested IDR; next keyframe satisfies it."* and discards it. The sink is asking **because it has visible corruption right now**. Forward it to the engine. Ten lines, biggest perceived-quality win available.

**Also fix the security hole:** `wfd.py:2402` defaults `host="0.0.0.0"` and `_WFDRTSPHandler` does no peer check — on coffee-shop Wi-Fi, that is a desktop-mirror-on-request service. Bind to the p2p interface IP; `_get_peer_ip_from_p2p_iface` (`wfd.py:3223-3244`) already resolves it.

---

### **MILESTONE 7 — Audio** · 1 day

libavdevice `pulse` → AAC-LC → second stream on the same `AVFormatContext`. **Fix the microphone bug:** `wfd.py:239` and `capture.py:66/75/79` return the literal string `"default"`, which `ffmpeg -f pulse -i default` resolves to the default **source** — the built-in mic. Hardcode `alsa_output.pci-0000_00_1f.3.analog-stereo.monitor` and fail loudly if absent. Derive audio PTS from cumulative 48 kHz sample count.

---

### **MILESTONE 8 — Second screen** · 1 day

`hyprctl output create headless hyprcast-0` — **you can name it** (`hctl.md:98`), which deletes the entire "HEADLESS-N increments, always enumerate and match" complexity. Then **immediately**:
```
hyprctl keyword monitor hyprcast-0,1920x1080@60,auto,1
```
The trailing `,1` is **mandatory**: a fresh headless output lands at **scale 2.0**, i.e. 960×540 logical, and every window on it is comically large. Poll `hyprctl monitors -j` until `scale == 1.0` before opening the capture session. `availableModes` lies on headless outputs — never validate against it.

Move workspaces with `dispatch moveworkspacetomonitor` **only** — `keyword workspace N,monitor:` becomes a persistent config rule that drags the workspace back the next time an output with that name appears.

Teardown: workspaces home first, **then** `output remove`. Crash safety: journal state to `$XDG_RUNTIME_DIR/hyprcast.state` before touching anything, and on daemon start unconditionally sweep every `hyprcast-*` monitor. (Verified: `output remove` is clean — windows survive, workspaces migrate home, the empty auto-created workspace is destroyed.)

---

### **MILESTONE 9 — Control surface** · 1 day

`hyprcastctl` (thin JSON client) + waybar module. Waybar uses `"exec": "hyprcastctl waybar"` with **no `interval`** and `"restart-interval": 2` — setting both `interval` and a long-running `exec` makes waybar kill and respawn the process on every tick. Drops into `~/.config/waybar/modules/custom-hyprcast.jsonc`, picked up by the existing glob. Right-click → `float-tui.sh hyprcast-tui` matching the existing `wb-float` kitty class.

---

### **DEFERRED — only if measurement demands it**

Direct libva encode · intra-refresh via `VAEncMiscParameterTypeRIR` · hand-rolled TS+RTP with per-packet pacing · multi-slice · PipeWire in-process virtual sink for TV-only audio · ratatui TUI · `ext_foreign_toplevel_image_capture_source_manager_v1` single-window casting (it's advertised, one extra match arm).

---

## 4. THE THREE THINGS MOST LIKELY TO KILL THIS

### **#1 — The Xiaomi box may not have a real WFD sink at all**

**Why it's #1:** it invalidates 100% of the project, and it is the single cheapest thing to check. Google TV ships **no built-in Miracast sink** — AOSP dropped it around Android 6/8 and the `libstagefright/wifi-display` sink was only ever a command-line test harness (`wfd.cpp`). `WifiP2pManager.setWfdInfo()` is gated by `android.permission.CONFIGURE_WIFI_DISPLAY`, which is `signature|privileged`, so **no Play Store app can advertise the WFD IE** and therefore none can be discovered as a Miracast sink. Apps marketed as "Miracast receivers" (AirScreen, PigeonCast) are AirPlay/DLNA/Cast over infrastructure Wi-Fi. That leaves exactly one candidate: Xiaomi's preinstalled app.

**Detect:** Milestone 1A, 30 minutes. `wpa_cli p2p_find` with `wifi_display 1` — either a peer appears with a WFD IE and a display `pri_dev_type`, or it doesn't.

**Fallback:** none within WFD. Google Cast is the only alternative and it has a **400 ms design floor** (Chromium's target playout delay is documented as the total allowed capture-to-presentation window, not a bug), routes two hops through the router instead of a direct P2P link, and gives up encoder control. If 1A fails, this project should be cancelled rather than redirected.

---

### **#2 — Intel 8265 P2P: legal 5 GHz, and enough airtime**

**Why it's #2:** it doesn't kill the project, it caps it — potentially at 720p30, potentially at "unusable jitter". Measured today:

- `phy#0` is **self-managed** (firmware LAR). Reg domain reads `country 00: DFS-UNSET` right now — it was `MA` when investigation 4 ran. **This changes underneath you when you roam.** Re-read `iw reg get` at group-formation time, not at startup.
- Every 5 GHz channel is flagged **`No IR`** (may not initiate radiation). Only **5170–5250 (ch 36/40/44/48)** carries **`IR-CONCURRENT`** — the GO-concurrent relaxation, legal *only while another interface is already associated as a client on that channel*. wlan0 is on **ch 40** right now. **That is load-bearing state, not a coincidence.** Disconnect from the home AP and your 5 GHz GO becomes illegal and silently drops to 2.4 GHz, where 1080p60 is not happening.
- Interface combos: `#{managed}<=1, #{P2P-client,P2P-GO}<=1, #{P2P-device}<=1, total<=3, **#channels<=2**`. On a single-radio 2×2 part, `#channels<=2` means **time-division** multi-channel concurrency — halved airtime and multi-millisecond jitter. Always force same-channel: `p2p_group_add freq=5200`.
- P2P discovery always uses social channels 1/6/11 regardless of operating channel, so it yanks the radio to 2.4 GHz and stalls the STA link for a few hundred ms. Discover once, cache the peer, stay in the group.

**Detect:** Milestone 1A step 3 — `iperf3` over the formed P2P link. One number, 30 seconds.

**Fallback ladder, in order:** (a) refuse to start a 5 GHz GO unless wlan0 is on 36/40/44/48, with an explicit error rather than a silent 2.4 GHz downgrade; (b) if the sink only supports 2.4 GHz P2P (many Android sinks do), target **720p60 or 1080p30** — the architecture is unchanged, only the negotiated CEA mode moves; (c) let the sink be GO (`p2p_go_intent 0`) and join as client — the source is still the RTSP server, so the role swap costs nothing protocol-wise. Log the P2P operating channel and negotiated PHY rate in every session's telemetry; no amount of encoder work fixes halved airtime.

---

### **#3 — Building the whole thing and discovering the latency floor is in the sink**

This subsumes and reframes the issue-#19 risk. Let me be blunt about #19 first:

**Issue #19 is not a real risk anymore. It was never a VAAPI problem and it was never a hardware problem.** The measured facts:

- ffmpeg's `h264_vaapi` ships a **1.000-second VBV** by default under CBR — re-derived arithmetically from the emitted SPS twice: `bit_rate_scale=1, bit_rate_value_minus1=62499 → 8,000,000 bps`; `cpb_size_scale=3, cpb_size_value_minus1=62499 → 62500 × 2^7 = 8,000,000 bits = exactly 1.000 s`.
- **B-frames are ON by default** — measured `IBBPBBP`, `max_num_reorder_frames=1`, `max_dec_frame_buffering=2`.
- The default GOP is effectively infinite — **one** I-frame in 120 frames.
- And the contributor's topology inserted a whole extra encode + container-on-a-pipe + demux + decode + re-encode stage (`wf-recorder -c h264_vaapi` → nut container → `ffmpeg` → `libx264 -tune zerolatency` → RTP), because `_start_desktop_wf_recorder` (`wfd.py:1417-1532`) kept the downstream x264 stage.

The silicon does a full VPP + encode + sync in **p50 3.6 ms**. hyprcast's design deletes the pipe, the container, the second encoder and every one of those defaults. **The verification also correctly notes that the 1-second VBV cannot have been #19's mechanism** — a large VBV adds zero encoder delay in a one-pass encoder, and in that topology the intermediate stream was re-decoded by x264 before reaching the wire. B-frames and the extra generation are the real culprits. Don't cite the VBV as "the #19 fix"; cite it as correct advice for *our* pipeline, where the encoder output does go straight to the wire.

**So the residual risk is not #19. It's this: you do all the work, hit 40 ms sender-side, and glass-to-glass is still 300 ms because the Xiaomi app's jitter buffer owns the rest.** You cannot reach it. Published Miracast sink measurements land at ~250 ms tuned and ~400 ms for an Android-class receiver, and those are firmware sinks with tighter control than a Java app.

**Detect:** two instruments, both from day one.
1. **Milestone 1B's histogram** tells you your own budget exactly. If sender-side p99 is already under 20 ms, further sender optimisation is theatre.
2. **The camera measurement in Milestone 6.** If total is 300 ms and yours is 40 ms, **stop optimising the sender**. Set Fluency Priority and accept the number.

**Fallback:** honour every `wfd_idr_request` immediately (Milestone 6) — that converts permanent corruption into a sub-100 ms glitch and is worth more perceived quality than 10 ms of pipeline. Beyond that, the sink's buffer is a hard floor and knowing that is worth more than not knowing it.

**Runners-up worth naming but not ranking:**
- **Hyprland's screencopy permission silently hangs you** with no `ready` and no `failed` (`hypr_ScreenshareFrame.cpp:150-157`). Mitigated in Milestone 3 by a watchdog. `ecosystem:enforce_permissions` is 0 today; that can change on any upgrade.
- **Direct scanout is disabled for the entire cast.** `CScreenshareSession::nextFrame()` sets `m_directScanoutBlocked = true` unconditionally, and `onOutputCommit` only clears it when no frame is pending and no session is sharing. Since the design keeps exactly one frame outstanding at all times, full composition is forced for as long as hyprcast runs. Real power cost on a 15 W part, in nobody's budget, and not codeable-around.
- **CBR stuffing.** Handled by decision #11, but if anyone "fixes" the rate control back to CBR, an idle desktop will pump 10 Mbit/s of padding across the Wi-Fi Direct link. Measured 465× today.

---

## 5. WHAT GETS DELETED

**6,313 of 8,834 lines in `src/` (71.5%) plus ~1,150 lines of packaging.** Surviving Python: **~1,650 lines, stdlib only.** All six `requirements.txt` entries become deletable.

### Whole-file deletes — 3,704 lines

| File | Lines | Why |
|---|---:|---|
| `src/server.py` | 746 | HTTP/HLS/progressive-TS bridge; exists only to feed DLNA and Cast. WFD never touches HTTP. |
| `src/diagnostics.py` | 748 | `--doctor`. Probes only pre-measured facts, **and is a hard gate**: `wfd.py:3493-3501` raises `WFDNotReady` if `wfd_candidate` is false, which requires `dnsmasq` on PATH. A missing optional binary blocks streaming. |
| `src/drivers/uibc.py` | 623 | Touch back-channel for tablet sinks. Also binds `0.0.0.0:7239`. |
| `src/drivers/wfd_lpcm_mux.py` | 591 | **Copy to `reference/mpegts_muxer.py` first** — lines 25–302 are the best dependency-free TS/RTP reference you have. Then delete: it's Microsoft-only (stream_type 0x83), and Android maps 0x83 to an AC-3 decoder. |
| `src/tray.py` | 551 | pystray + DLNA/Cast launchers + Tk About window. Carries the SIGTERM cleanup leak. |
| `src/portal_capture.py` | 450 | xdg-desktop-portal ScreenCast D-Bus — exactly what the native path replaces. |
| `src/tray_config.py` | 261 | configparser matrix for three protocols that no longer exist. |
| `src/cast.py` | 158 | pychromecast. |
| `src/dlna.py` | 127 | UPnP/AVTransport. |
| Packaging | ~1,150 | `appimage/`, `meta/pypi/`, `.github/`, `tests/test_{diagnostics,tray_config,uibc}.py` |

### Partial deletes

| File | Total | DELETE | KEEP | SIMPLIFY | → native |
|---|---:|---:|---:|---:|---:|
| `src/wfd.py` | 3,636 | 1,290 | 1,041 | 1,095 → ~600 | 210 |
| `src/capture.py` | 568 | 528 | 10 | 30 | 0 |
| `src/main.py` | 375 | 240 | 20 | 115 | 0 |

**The single largest block:** `wfd.py:1034-1745`, all four `_start_desktop_*` backends — 712 contiguous lines including the 352-line `_start_desktop_portal` with its pipewiresrc × caps combinatorial retry loop and x264 GObject property introspection.

**Also dead, confirmed zero callers:** `WFDMediaPipeline.restart_video()` at `wfd.py:849-867`.

### Kept, and why (the 1,041 lines that are the actual asset)

`wfd.py:50-63` WFD IE device info (byte-exact, 6 bytes, source flag 0x0010) · `85-113` sink WFD IE parsing (non-obvious `i+5`/`i+6` offsets) · `167-200` CEA mode table (the `native` hex codes are wire values) · `493-550` sink format parsing + profile/level selection · `695-707` `_safe_source_port` (drop it and you get an intermittent bind conflict that presents as "sometimes no video") · `1900-1963` RTSP send/CSeq/Session-header ordering · `2280-2310` M16 keepalive (20 s then 25 s) · `2633-2758` NM activation state machine · `2761-2805` scan + WFD IE extraction · `2819-2874` connection settings including `'persist': <'volatile'>` and the explicit no-`bind-activation` note · `2877-3044` wpa_supplicant GO intent + `_cleanup_step` · `3188-3255` ARP peer-IP resolution via the `p2p-*` interface.

**Two things that look like vendor quirks but aren't — strip the comment, keep the code:** `wfd.py:2029-2037` (M16 rejected → stop rescheduling but keep streaming) and `wfd.py:3223-3244` (find peer IP by scanning `p2p-*` rather than matching the discovery MAC — sinks randomise MAC between discovery and group formation).

### New files

`hyprcast-engine/` (C, ~2,600 lines: `main.c control.c capture.c dmabuf.c vaapi.c encode.c mux.c audio.c pacer.c`) · `python/hyprcast/engine.py` (~120) · `python/hyprcast/hypr.py` (~80) · `python/hyprcast/ctl.py` (~150) · `tools/mock-sink.py` (~250) · `tools/assert-ts.py` (~120) · `justfile` · `packaging/PKGBUILD` · `packaging/hyprcast.service`

---

## 6. FIRST SESSION — bare clone to Milestone 1

Everything below is verified present on this box. No `pacman -S` is needed for 1B.

### Step 0 — preserve the scratchpad (5 min, do this literally first)

`/tmp/claude-1000/-home-ilyask-projects/5bb47ba5-742e-4891-a187-b6cec9af0fed/scratchpad/` is session-scoped and **will be garbage-collected**. Everything Milestone 1 depends on is in it.

```bash
mkdir -p ~/projects/hyprcast
SP=/tmp/claude-1000/-home-ilyask-projects/5bb47ba5-742e-4891-a187-b6cec9af0fed/scratchpad
cd ~/projects/hyprcast && git init

mkdir -p reference/probes reference/hyprland reference/aosp
cp $SP/hyprcast-probe/{probe.c,vatest.c,vpptest.c,modtest.c,headless.log,stop.log} reference/probes/
cp $SP/{dmabuf_vpp.c,encbench.c,i965probe.c,rgbenc.c,vainfo.c} reference/probes/ 2>/dev/null
cp $SP/rev/{ccs.c,curs.c,vppscale.c,glob.c} reference/probes/
cp $SP/rev2/{ccs2.c,ccs3.c} reference/probes/
cp $SP/hypr_{ImageCopyCapture,ScreenshareSession,ScreenshareFrame,ScreenshareManager,ImageCaptureSource}.cpp reference/hyprland/
cp $SP/{hctl.md,perms.md} reference/hyprland/
cp $SP/{ATSParser.cpp,TSPacketizer.cpp,TSPacketizer.h,MediaSender.cpp,RTPSender.cpp,VideoFormats.cpp,WifiDisplaySource.cpp} reference/aosp/ 2>/dev/null
cp $SP/fluxcast/src/drivers/wfd_lpcm_mux.py reference/mpegts_muxer.py

git add -A && git commit -m "reference: measured probes, Hyprland 0.55.4 screenshare source, AOSP WFD"
```

### Step 1 — fork fluxcast onto a branch (10 min)

```bash
cd ~/projects/hyprcast
git remote add upstream https://github.com/IlyaP358/fluxcast.git
git fetch upstream && git checkout -b hyprcast upstream/main   # or master
wc -l src/*.py src/drivers/*.py | tail -1     # must read 8834
```
Do **not** copy from `$SP/fluxcast-src/` — it's a different, older tree (8,798 lines) and every line citation in this plan is against the 8,834-line tree.

### Step 2 — vendor the protocol XML and generate bindings (15 min)

```bash
mkdir -p engine/protocol && cd engine/protocol
WP=/usr/share/wayland-protocols
cp $WP/staging/ext-image-copy-capture/ext-image-copy-capture-v1.xml .
cp $WP/staging/ext-image-capture-source/ext-image-capture-source-v1.xml .
cp $WP/staging/ext-foreign-toplevel-list/ext-foreign-toplevel-list-v1.xml .
cp $WP/stable/linux-dmabuf/linux-dmabuf-v1.xml .
for x in *.xml; do
  b=${x%.xml}
  wayland-scanner client-header $x $b-client-protocol.h
  wayland-scanner private-code   $x $b.c
done
ls -la
```

**You must generate `ext-foreign-toplevel-list-v1` private-code even though you only use the output source manager** — `ext-image-capture-source-v1.c` references `ext_foreign_toplevel_handle_v1_interface` at lines 42 and 50, and the link fails with an undefined reference otherwise. This cost a build cycle in the investigation.

### Step 3 — meson skeleton (20 min)

`engine/meson.build`:
```meson
project('hyprcast-engine', 'c',
  default_options: ['c_std=c11', 'warning_level=3', 'b_ndebug=false'])
cc = meson.get_compiler('c')
deps = [
  dependency('wayland-client'),
  dependency('gbm'),
  dependency('libdrm'),
  dependency('libva'), dependency('libva-drm'),
  dependency('libavcodec'), dependency('libavformat'),
  dependency('libavutil'), dependency('libavdevice'),
  cc.find_library('m'),
]
proto = files(
  'protocol/ext-image-copy-capture-v1.c',
  'protocol/ext-image-capture-source-v1.c',
  'protocol/ext-foreign-toplevel-list-v1.c',
  'protocol/linux-dmabuf-v1.c',
)
executable('hyprcast-bench', ['src/bench.c','src/capture.c','src/dmabuf.c',
  'src/vaapi.c','src/encode.c','src/hist.c'] + proto,
  include_directories: include_directories('protocol','src'),
  dependencies: deps, install: false)
```
```bash
cd ~/projects/hyprcast/engine && meson setup build && ninja -C build
```
Verify the deps resolve before writing code: `pkg-config --modversion libva libavcodec gbm libdrm` → expect `1.24.x 62.28.102 …`.

### Step 4 — build `hyprcast-bench` (the day's work)

Assemble in this order, testing each against the reference probe it came from:

1. **`capture.c`** — port `reference/probes/probe.c` (14.5 KB, already correct). Registry binds: `ext_output_image_capture_source_manager_v1` v1, `ext_image_copy_capture_manager_v1` v1, `zwp_linux_dmabuf_v1` **clamped to 4** (Hyprland offers 5; v4+ stops sending the deprecated format/modifier events — `linux-dmabuf-v1.xml:111,128` mark them `deprecated-since="4"`), `wl_output` v4 for the `name` event. `create_session(source, options=1)` for `paint_cursors` — **options 0 and 1 only**; `0xFF` raises `invalid_option` and kills the connection. Accumulate constraints into a staging struct, **commit only on `done`** (`buffer_size` arrives **last**, after both `dmabuf_format` events — allocating on first `dmabuf_format` gives a 0×0 pool).
2. **`dmabuf.c`** — GBM pool of 3. Modifier array = advertised ∩ `{I915_FORMAT_MOD_Y_TILED}`, drop `DRM_FORMAT_MOD_INVALID`, fall back to LINEAR only if Y_TILED is absent. `gbm_bo_create_with_modifiers2(..., GBM_BO_USE_RENDERING)`. **`assert(gbm_bo_get_plane_count(bo) == 1)`.** Then `zwp_linux_dmabuf_v1.create_params` → `add(fd, 0, offset, stride, mod_hi, mod_lo)` → `close(fd)` → `create_immed(w, h, XR24, 0)`.
3. **`vaapi.c`** — port `reference/probes/vatest.c` + `vpptest.c`. `open("/dev/dri/renderD128", O_RDWR|O_CLOEXEC)` → `vaGetDisplayDRM` → **`vaSetDriverName(dpy,"iHD")`** → `vaInitialize` → assert vendor contains `iHD`. Import each bo with `VADRMPRIMESurfaceDescriptor{ fourcc=VA_FOURCC_BGRX, num_objects=1, objects[0].size=lseek(fd,0,SEEK_END), objects[0].drm_format_modifier=gbm_bo_get_modifier(bo), num_layers=1, layers[0].drm_format=DRM_FORMAT_XRGB8888, num_planes=1 }`, `VA_RT_FORMAT_RGB32`. **`close(fd)` after `vaCreateSurfaces`.** Then a `VAProfileNone`/`VAEntrypointVideoProc` context.
4. **`encode.c`** — `av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_VAAPI)`, set `((AVVAAPIDeviceContext*)((AVHWDeviceContext*)ref->data)->hwctx)->display = our_va_display`, `av_hwdevice_ctx_init()`. Then `av_hwframe_ctx_alloc()` with `format=AV_PIX_FMT_VAAPI, sw_format=AV_PIX_FMT_NV12, 1920×1080, initial_pool_size=6`. `av_hwframe_get_buffer()` gives an `AVFrame` whose `data[3]` is the VASurfaceID that VPP writes into. Encoder opts:
   ```
   profile=constrained_baseline  coder=cavlc  bf=0  async_depth=1
   aud=1  sei=0  level=42  g=60  idr_interval=0
   rc_mode=VBR  b=8000000  maxrate=8000000  bufsize=133333  max_frame_size=33000
   ```
5. **`hist.c`** — three reservoir histograms (capture commit→ready, VPP, encode) + a duplicate-frame counter.

### Step 5 — measure

```bash
# terminal 1
vkcube &
# terminal 2
cd ~/projects/hyprcast/engine
/usr/bin/time -v ./build/hyprcast-bench --output eDP-1 --fps 60 --seconds 60 \
    --out /tmp/bench.h264 2>&1 | tee /tmp/bench-anim.log

# baselines and A/B, same binary
MOD=LINEAR  ./build/hyprcast-bench --seconds 20 --out /dev/null   # expect ~7.4 ms, confirms the Y_TILED choice
MOD=Y_TILED ./build/hyprcast-bench --seconds 20 --out /dev/null   # expect ~4.5 ms
pkill vkcube
./build/hyprcast-bench --seconds 30 --out /dev/null | tee /tmp/bench-idle.log   # duplicate-frame ratio should approach 100%

# colour + correctness check
ffplay -autoexit /tmp/bench.h264
ffprobe -hide_banner -show_streams /tmp/bench.h264 2>&1 | grep -E "profile|has_b_frames|color_range|color_space"
ffmpeg -v error -i /tmp/bench.h264 -bsf:v trace_headers -f null - 2>&1 | grep -c "nal_unit_type.*= 5"
```

### What you are looking for

| Metric | Pass | Fail means |
|---|---|---|
| Sustained fps under vkcube | ≥ 59.5 | pacer or serialization bug |
| commit→ready p50 | 4.0–5.5 ms | you allocated the wrong modifier — check the plane-count assert |
| VPP p50 | < 1.2 ms | you're not on VEBOX/SFC |
| encode p50 / p99 | < 4.5 / < 10 ms | `async_depth` or `low_power` leaked in |
| Total p99 | < 16.67 ms | investigate before proceeding |
| CPU, whole process | < 15% of one core | a CPU copy is hiding somewhere — profile for `memcpy` |
| `has_b_frames` | **0** | `-bf 0` not applied |
| IDR count in 3600 frames | **60** | GOP not set |
| Idle-desktop bitrate | < 200 kbit/s | you're on CBR, not VBR |
| ffplay colours | correct | R/B swap → `VA_FOURCC_RGBX` instead of BGRX; washed out → `color_range` unset |

### And, the same evening: run Milestone 1A

It takes 90 minutes and it is the only thing that can make all of the above moot.

---

**File paths that matter:**
`/home/ilyask/projects/hyprcast/` (new) · `/tmp/claude-1000/-home-ilyask-projects/5bb47ba5-742e-4891-a187-b6cec9af0fed/scratchpad/hyprcast-probe/probe.c` · `/tmp/claude-1000/-home-ilyask-projects/5bb47ba5-742e-4891-a187-b6cec9af0fed/scratchpad/fluxcast/src/wfd.py` (8,834-line tree) · `/tmp/claude-1000/-home-ilyask-projects/5bb47ba5-742e-4891-a187-b6cec9af0fed/scratchpad/hypr_ScreenshareFrame.cpp` · `/usr/share/wayland-protocols/staging/ext-image-copy-capture/ext-image-copy-capture-v1.xml` · `https://github.com/IlyaP358/fluxcast/issues/19`