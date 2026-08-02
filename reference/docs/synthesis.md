# hyprcast: fork advisory

## 1. VERDICT — **companion tool, not a fork**

Build `hyprcast` as a separate binary that *drives* fluxcast over its CLI, and send the encoder fixes upstream as PRs.

**The single strongest reason:** the maintainer has already drawn the line you want to cross and declared his side of it out of bounds. Issue #14 ("Virtual monitor instead of casting") is closed `out of scope` with the rationale *"to keep the project stable and DE-agnostic"*, pointing users at `hyprctl output create headless` — and he then shipped `--monitor NAME` specifically to make that external pipeline scriptable (https://github.com/IlyaP358/fluxcast/issues/14, `documentation/DOCUMENTATION.md:166-169`). The compositor-specific layer upstream refuses to own is exactly the layer you want to own. That is a companion tool, by definition, with the upstream maintainer's stated blessing.

The supporting math is one-sided:
- **0 open PRs. 20 of 23 merged. External PRs merged in 2-29 hours, median ~13h.** There is no backlog to route around, which is the only classic justification for a hard fork.
- **55% of `src/` is compositor-agnostic protocol you'd never edit** — `wfd.py` 3,636 + `drivers/uibc.py` 623 + `drivers/wfd_lpcm_mux.py` 591 = 4,850 of 8,834 lines (measured at `8507b50`).
- **~23 lines in the entire tree are Hyprland-specific** (`wfd.py:242-245` + 4 call sites, `capture.py:94-102`). You'd inherit 4,850 lines of Samsung/LG quirks to control a backend-order list.
- **`wfd.py` grows ~300 lines/month; 11 releases in 13 weeks.** A hard fork owes users a merge-or-skip decision every ~7 days, forever.
- **All 17 existing forks are PR-staging forks with 0-1 stars.** Nobody has made a fork of this project stick.

Critically: **a fork does not fix the choppiness.** Every root cause below lives in `wfd.py`/`capture.py` and is compositor-agnostic. Forking to fix them means maintaining a divergent copy of code whose fixes upstream would merge in a day.

---

## 2. WHY IT IS CHOPPY

Default path on Hyprland: `main.py:84` (`--protocol wfd` default) → `wfd.py:258-262` returns `["wf-recorder","x11grab"]` → `_start_desktop_wf_recorder` at `wfd.py:1417`. Everything below is on that path unless noted.

### Cheap wins available today (all are ≤20-line patches, all upstreamable)

**1. The bitrate floor silently raises your bitrate and cannot be lowered.** `_quality_floor_kbits` (`wfd.py:451-461`) returns 8000 kbit at 1080p30, 14000 at 1200p30, 20000 at 1200p60, applied as `effective = max(requested, floor)` at `wfd.py:1431` (also 1092, 1544, 1656). `--bitrate 2M` on a 1080p60 monitor becomes 14 Mbit/s. Pushing 8-20 Mbit/s CBR over a Wi-Fi Direct link with **zero feedback** (grep for `rssi|signal|iw dev` over `wfd.py` returns nothing; RTCP is only ever a port number at `wfd.py:719`) is a textbook stutter generator. The LG 4 Mbit safety cap at `wfd.py:1096-1097` exists on **one of four backends** — not the Hyprland one.
*Fix:* make the floor apply only when `--bitrate` was not passed. Three lines.

**2. `-p pix_fmt=yuv420p` is a verified no-op, so the pipe carries 4 bytes/pixel.** `wfd.py:1447` and `capture.py:293`. `pix_fmt` is not an `AVCodecContext` AVOption (0 hits in `ffmpeg -h full`'s AVCodecContext section); wf-recorder logs `Setting codec option: pix_fmt=yuv420p` and still emits `bgr0`. Measured: 8.29 MB/frame → ~249 MB/s at 1080p30 through a **64 KiB pipe** (`wfd.py:1516-1522`, `Popen(stdout=PIPE)` → `Popen(stdin=wf_proc.stdout)`; measured `F_GETPIPE_SZ` = 65536, no `F_SETPIPE_SZ` anywhere in `src/`). When the pipe backs up, wf-recorder's screencopy callback blocks — that *is* the choppiness.
*Fix:* `-x yuv420p` instead of `-p pix_fmt=...` (measured 2.65x reduction, 84.4 → 31.9 MB/s) plus `fcntl(fd, F_SETPIPE_SZ, 1048576)`. Then `-vf format=yuv420p` at `wfd.py:1471` becomes a genuine no-op and can be dropped.

**3. `-tune zerolatency` forces sliced threading.** x264 banner on the live run: `threads=8 lookahead_threads=8 sliced_threads=1 slices=8`. Sliced threads are materially slower than frame threads, on the path that is already CPU-bound. Hardcoded at `wfd.py:1478`, `capture.py:289`, and `sliced-threads=true` at `wfd.py:1003`, `wfd.py:1707`.
*Fix:* drop `tune=zerolatency`, set the knobs explicitly: `bframes=0:rc-lookahead=0:sync-lookahead=0:sliced-threads=0`.

**4. `-thread_queue_size 1024` in front of a rawvideo input, and zero low-latency flags.** `wfd.py:1454`. Each packet is a full uncompressed frame. `grep -rn "nobuffer\|low_delay\|rtbufsize\|avioflags\|max_delay\|fps_mode\|vsync" --include=*.py src/` returns **nothing**; `_ffmpeg_sender_args` (`wfd.py:768-775`) is just `ffmpeg -hide_banner -y -loglevel warning`.
*Fix:* video input `-thread_queue_size 8` (keep 1024 for pulse), add `-fflags nobuffer -flags low_delay -avioflags direct -max_delay 0`.

**5. 2-second VBV.** `_vbv_bufsize` multiplies bitrate by 2.0 for every non-LG sink (`wfd.py:417-424`), applied at `wfd.py:1489` next to `-maxrate` at `1488`. `bufsize == 2×maxrate` is two seconds of VBV by definition — it undoes the carefully-zeroed `-muxdelay 0 -muxpreload 0` at `wfd.py:876-891`.
*Fix:* 0.5-1.0x.

**6. Three stacked frame-rate conversions and discarded capture timestamps.** `wfd.py:1443` (`-r` on wf-recorder, injects `fps=30` into its filter chain — confirmed in the run log), `wfd.py:1453` (`-fflags +genpts` throws away the compositor's PTS), `wfd.py:1482` (`-r` again on ffmpeg output). Any hiccup becomes a dup/drop rather than a frame arriving 5 ms late.
*Fix:* keep wf-recorder's `-r`, drop `+genpts`, replace the output `-r` with `-fps_mode passthrough`. Pass `-B <monitor.refresh>` using the refresh rate already parsed at `capture.py:176-180` and currently only printed.

**7. 30 fps by default, and `--fps` gets overwritten.** `main.py:99` defaults to 30 → `wants_60 = config.fps > 30` (`wfd.py:568`) is False → a p30 mode is chosen → `wfd.py:2213` writes `mode.fps` **back onto the config**. 30 fps mirroring reads as choppy on its own, and `--fps 60` only works if the sink advertises the p60 bit.

**8. IDR requests are thrown away.** `wfd.py:2119-2123` discards `wfd_idr_request` with a comment. With `-g fps` and `-sc_threshold 0` (`wfd.py:470`, `1483-1485`), every packet-loss event costs on average 500 ms and up to 1 s of corrupted video the source could have fixed in one frame. Combined with unpaced output (`udpsink sync=false async=false`, `-flush_packets 1`), the once-per-second IDR burst overruns the Wi-Fi queue and *causes* the loss it then can't repair. This is the signature of a once-per-second glitch.

**9. Health monitoring stops permanently on first failure.** `_probe_tx` re-arms via `_schedule_probe(5.0)` only inside the healthy branch (`wfd.py:2363-2364`); the unhealthy branch (`wfd.py:2366-2370`) prints one warning and never checks again. The tx counter is measured against the *session baseline*, not the previous sample (`wfd.py:2325-2326`), so a stalled-but-alive encoder is undetectable. Meanwhile `--wfd-ffmpeg-stats` already exists (`main.py:137-138`) and is off by default — the cheapest possible diagnostic win.

**10. Portal backend hard-excluded on Hyprland.** `capture.py:94-102` and `wfd.py:258-270` both return `["wf-recorder","x11grab"]`; the branch at `wfd.py:266` is dead code (line 261 already returned). The portal path (`wfd.py:1064`) is single-process — no 64 KiB pipe, no NUT mux/demux round-trip. It is *not* dmabuf end-to-end (`videoconvert ! videoscale ! caps ! videoconvert ! x264enc` at `wfd.py:1228-1233`), so don't oversell it, but it is worth trying first.

**Also, and unrelated to choppiness — report these upstream today:**
- **RTSP binds `0.0.0.0` with no peer authentication.** `wfd.py:2402`, bound at `2433`; `handle()` drives the full M1-M7 for any client and streams to `client_address[0]` (`wfd.py:2230`); `wfd_content_protection: none` hardcoded at `wfd.py:1997`. Any host on your LAN that speaks RTSP gets your screen.
- **Audio falls back to Pulse device `"default"`, which is the microphone.** `capture.py:66/75/79`, `wfd.py:239`. Empirically verified: `ffmpeg -f pulse -i default` opens `alsa_input.…analog-stereo`. Silent privacy failure.
- **The tray's Stop button leaks everything.** `tray.py:154-158` sends SIGTERM; `main.py:264-271` returns from the WFD branch *before* `signal.signal` at `main.py:309-310`; `wfd.py:3617` only catches `KeyboardInterrupt`. So the entire `finally` at `wfd.py:3619-3636` — media shutdown, firewall close, NM deactivate, GO-intent restore — is skipped. Reproduced: `rc = -15`, cleanup never ran.

### Requires rearchitecting

**A. Hardware encode.** Every encoder in the tree is software x264 (`capture.py:284`, `wfd.py:1445`+`1476`, `wfd.py:1233`, `wfd.py:1699`, `capture.py:422`). VAAPI/NVENC/QSV appear exactly once, in a diagnostics list at `diagnostics.py:156-162`, and are never wired to a pipeline. This machine has `/dev/dri/renderD128` and ffmpeg reports `h264_vaapi`, `h264_nvenc`, `h264_qsv` (verified just now). Worse: passing `-c rawvideo` *actively defeats* wf-recorder's own zero-copy path — its man page says it keeps buffers on the GPU only *"if using a GPU encoder"*. So every frame is a full GPU→CPU readback plus a software encode.

Scale of the prize, from wl-screenrec's published benchmark (4Kp60, i9-11900H): wf-recorder software **~500% CPU**, wf-recorder `-c h264_vaapi` **~75%**, wl-screenrec **~2.5%** (https://github.com/russelltg/wl-screenrec). The gap between the last two is that wf-recorder still downloads and swscales; wl-screenrec never lets pixels touch the CPU.

Caveat you must respect: upstream issue #19 has a contributor who *measured* `h264_vaapi` as **1-2s higher latency** (CPU 40-50% → 25%), and the maintainer correctly called that a dealbreaker for WFD as a default. That is almost certainly `async_depth`/VBV/queue depth, not a law of physics — but it means "just add VAAPI" is not a patch, it's a project.

**B. Rate adaptation.** Resolution, fps and bitrate are frozen at PLAY (`wfd.py:2209-2215`) and never revisited. There is no control channel into a `subprocess.Popen`'d command line, so adaptation is *not expressible* in the current design. This requires splitting "session" (P2P/RTSP) from "encoder leg" so bitrate changes restart only the encoder.

**C. Single-clock capture.** The wf-recorder and x11grab backends run two independent live ffmpeg inputs with no shared clock; commit `cfc28f8` papered this over with `-af aresample=async=1` at three call sites (`wfd.py:938`, `1495`, `1599`) — which fills/trims but does **not** correct drift, and was pasted onto the synthetic lavfi test-pattern path while being omitted from `capture.py:545` where the same bug is live. The portal and gst-x11 paths already have the correct single-pipeline model.

---

## 3. WHAT THE FORK WOULD ACTUALLY HAVE TO BUILD

Honest estimates for one competent developer.

### Hyprland-native capture path — 6-10 weeks, one GPU vendor

| Item | Effort | Notes |
|---|---|---|
| Latency measurement harness | **2 days — do this first** | Nobody has published end-to-end ms figures for Wayland screencast. `ext_image_copy_capture_frame_v1`'s `presentation_time` minus your encoder output timestamp is the only trustworthy number you'll get. Every decision below is unfalsifiable without it. |
| `ext-image-copy-capture-v1` client | **2-4 weeks** | Verified live on this box: Hyprland 0.55.4 advertises `ext_image_copy_capture_manager_v1` and `ext_output_image_capture_source_manager_v1`. Manager + source + frame + constraints state machine, GBM BO rotation (2-3 deep), `damage_buffer` for partial copies, handling mid-session constraint re-send. **Not viable in Python** — this is a C or Rust helper binary. Require Hyprland ≥0.55.0 (0.54.0 landed it, 0.55.0 fixed stop handling). |
| VAAPI encode chain | **1-2 weeks + tuning** | DRM hwdevice → `av_hwdevice_ctx_create_derived` VAAPI → `av_hwframe_map(AV_HWFRAME_MAP_DIRECT)` → `scale_vaapi format=nv12` → `h264_vaapi`. Do **not** shell out: `pipewiregrab` does not exist in Arch's ffmpeg 8.1.2 (verified). Tuning is where issue #19's 1-2s regression gets solved or doesn't. |
| NVENC backend | **2-3 weeks** | Entirely separate. `nvidia-vaapi-driver` is decode-only (https://github.com/elFarto/nvidia-vaapi-driver/issues/222), so it's EGL/CUDA import plus explicit-sync handling. You cannot test it on this machine (Intel UHD 620). |
| Rotation / fractional scale / CFR pacer | **1 week** | Mesa VAAPI has no transform support — this is why wl-screenrec breaks on rotated AMD monitors. Damage-driven capture needs a repeat-last-frame pacer for CFR consumers. |
| wlr-screencopy fallback | **3-5 days** | For Hyprland <0.55 and other wlroots compositors. |

Note: going via `xdg-desktop-portal-hyprland` does **not** get you the modern protocol. Verified by dumping symbols from the installed 1.3.12: zero `ext_image*`, still `zwlr_screencopy_manager_v1` + `hyprland_toplevel_export_manager_v1`. Portal = deprecated protocol + D-Bus handshake + chooser UI + 120fps cap + PipeWire buffer-pool exhaustion (see OBS #11580).

### Control surface (IPC + GUI) — 6-9 weeks

| Item | Effort | Notes |
|---|---|---|
| Daemon (`hyprcastd`) | **1-2 weeks** | ~400-600 lines. Owns lifecycle; replaces `tray.py`'s `Popen` + module globals at `tray.py:36-51`. |
| IPC + schema | **1 week** | JSON-lines unix socket at `$XDG_RUNTIME_DIR/hyprcast.sock`. D-Bus optional. Version the schema. |
| **Live reconfiguration** | **2-3 weeks** | The expensive part and the prerequisite for everything else. Split session from encoder leg so `SetBitrate`/`SetMonitor`/`SetAudioSink` restart only the encoder (~2s) not the session (~30s). Also the prerequisite for rate adaptation. |
| hyprctl monitor enumeration | **1-2 days** | Replaces `capture.py:133-188`, which globs `/tmp/.X*-lock` and shells `xrandr` over XWayland. `hyprctl monitors -j` gives `refreshRate`, `transform`, `scale`, `currentFormat`, `dpmsStatus`, `description`. Huge correctness win for tiny effort. |
| Audio enumeration + routing | **3-5 days** | `pactl -f json list sinks`, explicit device threaded into all backends, mute. Live switching needs the daemon. |
| Telemetry aggregation | **1 week** | Reuse the existing `--wfd-latency-log` JSONL writer (`wfd.py:488`). |
| GTK4/libadwaita GUI | **2-3 weeks** | Pure API client. `diagnostics.run_diagnostics` already returns structured `Check` objects with `to_dict()`, so the health page is nearly free. |

### Waybar module — 1-2 days of code, 3 weeks of prerequisites

The module itself is trivial: a long-running `hyprcast --waybar` subcommand emitting one JSON object per line (`text`/`alt`/`class`/`tooltip`), copying `swaync-client -swb` exactly. Use the stream, not poll+signal — a TV dropping the link must hit the bar immediately. If you write it in Python, `flush=True` on every emit or the bar silently freezes.

But it is **worthless without the daemon and telemetry underneath**. Today the only status signal is `proc.poll()` (`tray.py:427-429`), which happily reports `● Casting: Samsung TV` for a session that failed its RTSP handshake 20 seconds ago. Don't ship a bar module that lies.

**Total honest fork cost: 4-6 months** before you have something demonstrably better than upstream-plus-patches.

---

## 4. WHAT YOU WOULD INHERIT AND NOT WANT TO MAINTAIN

**4,850 of 8,834 lines in `src/`** — `wfd.py` (3,636), `drivers/uibc.py` (623), `drivers/wfd_lpcm_mux.py` (591). None of it is Hyprland-specific. All of it is the genuinely hard part: Wi-Fi Direct bring-up, the M1-M7 RTSP handshake, MPEG-TS/RTP framing, UIBC, vendor quirks. There is no alternative implementation to fall back on — MiracleCast has had one substantive commit in 2026 and no Wayland capture path; gnome-network-displays needs the *mutter* screencast portal and otherwise falls back to X11 frame grabbing.

Specific landmines you'd own on day one:

- **No test coverage of any of it.** `tests/test_wfd.py` is 132 lines covering ffmpeg progress args and the firewalld helper. Zero coverage of the RTSP state machine, mode selection, or IE construction — the exact pure functions that contain the bugs below.
- **Vendor quirks keyed on substring-matching `peer.name`** — LG bitrate cap (`wfd.py:1096-1097`), LG VBV (`wfd.py:1182-1204`), Microsoft LPCM branch (`wfd.py:1317`). The `wpa_cli` fallback builds peers with `name=_parse_peer_name(details)` (`wfd.py:3180`), which returns `''` when the output lacks a device_name line — **silently disabling every quirk with no warning**.
- **A hand-written MPEG-TS muxer** with a bit-by-bit Python CRC32 (`wfd_lpcm_mux.py:63-73`), `0x00` stuffing where the spec (and its own docstring at `:81`) wants `0xFF` (`wfd_lpcm_mux.py:106-107`), an unbound RTP socket ignoring the negotiated ports, and PIDs (`PID_PMT=0x0100`, `PID_VID=0x1000`) that directly contradict the codebase's own shouting comment at `wfd.py:880-882`: *"WFD receivers (notably Samsung) are sensitive to MPEG-TS layout. Keep PMT/video/audio PID values aligned with the working gst path!!!"*
- **NetworkManager access via `gdbus` subprocess + regex-scraping GLib's pretty-printer** (`wfd.py:2550-2571`, `2633-2636`) — ~420 fork+execs per 35s activation (`wfd.py:2684-2709`), correctness dependent on a CLI output format staying stable.
- **A WFD IE bug**: subelement ID 10 is Alternate MAC Address (fixed 6 bytes); `wfd.py:66-72` stuffs a 6-32 byte hostname into it. Strict sinks may reject the whole IE — which is what carries the RTSP port.
- **The active RTSP probe kills its own stream ~8s after PLAY** (`wfd.py:3298` `settimeout(8.0)`, `TimeoutError` is an `OSError`, caught at `3482`, unwinds to `media.stop()`) — on the exact device class (Samsung 2024+) the probe was written for.

None of that is why your screen is choppy. You would be signing up to maintain Miracast device quirks in perpetuity to gain control over `_wfd_capture_backend_order`.

---

## 5. NAMING — `hyprcast` is safe and good. Take it.

Every registry is empty: PyPI 404, npm 404, no crate on crates.io, **0 AUR packages**. The only GitHub hit is `emilpriver/hyprcast` — 1 star, 0 forks, a CSS "launcher", last pushed 2025-05-24. Not worth contesting or contacting.

There is no rule to violate. `hyprwm/.github` contains only `FUNDING.yml`, `policies/AI_USAGE.md`, `policies/CODE_OF_CONDUCT.md` and vouch files — **no naming or trademark policy exists**. Precedent is overwhelming: hyprkeys, hyprclip, hyprcap, hyprkit, hyprshade, hyprmoncfg, hypruler, hyprwhspr, hyprlogin are all unaffiliated AUR packages. The prefix reads as "targets Hyprland", and the community treats it that way.

Three conditions:

1. **Put "hyprcast is a community project, not affiliated with or endorsed by hyprwm/Hyprland" in the README's first paragraph.** The real risk isn't etiquette, it's collision — hyprwm shipped hyprlauncher, hyprpwcenter, hyprshutdown and hyprsysteminfo across 2025-26 and clearly doesn't consider apps out of scope.
2. **Disambiguate from `hyprcap`** (existing AUR screenshot/recording tool). One clause.
3. **Claim PyPI + AUR (`hyprcast` *and* `hyprcast-git`) + crates.io simultaneously** so the names never diverge.

**Hard constraint on outreach:** hyprwm's AI policy permits AI-written patches but issues **permanent bans** for AI-generated PR descriptions, issues, or agent-submitted communication (https://github.com/hyprwm/.github/blob/main/policies/AI_USAGE.md). Any wiki PR, discussion post, or issue aimed at hyprwm repos must be hand-written. Getting this wrong costs you the ecosystem standing that no amount of code quality recovers.

---

## 6. THE SHARPEST ALTERNATIVE PLAN

Three tracks. Run A and B in parallel; C only if A's measurements justify it.

### Track A — upstream PRs, this week, ~1 day of work

Measure first using tools that already exist: `--wfd-ffmpeg-stats` (`main.py:137-138`) plus `--wfd-latency-log` (`wfd.py:488`). Then send these as separate small PRs; median external merge time is ~13 hours.

1. `-x yuv420p` replacing the no-op `-p pix_fmt=yuv420p` (`wfd.py:1447`, `capture.py:293`) + `F_SETPIPE_SZ` 1 MiB on the wf-recorder pipe (`wfd.py:1516`).
2. Bitrate floor applies only when `--bitrate` was not passed (`wfd.py:1431`, plus 1092/1544/1656).
3. Drop `tune=zerolatency`, set `bframes=0:rc-lookahead=0:sync-lookahead=0` without sliced threads (`wfd.py:1478`).
4. `-thread_queue_size 8` on the video input + `-fflags nobuffer -flags low_delay -avioflags direct -max_delay 0` in `_ffmpeg_sender_args` (`wfd.py:768-775`, `1454`).
5. VBV multiplier 2.0 → 1.0 (`wfd.py:417-424`).
6. Drop `-fflags +genpts`, output `-r` → `-fps_mode passthrough`, pass `-B <refresh>` (`wfd.py:1453`, `1482`).
7. Reschedule `_probe_tx` unconditionally; alarm on tx *rate*, not cumulative delta (`wfd.py:2325-2326`, `2366-2370`).
8. Portal first on Hyprland when `xdg-desktop-portal-hyprland` is running; delete the dead branch at `wfd.py:266`.
9. **Bug reports, not PRs:** the `0.0.0.0` RTSP bind with no peer auth, the `"default"` → microphone audio fallback, and the tray SIGTERM leak.

### Track B — hyprcast v0.1, weeks 2-6, zero lines of fluxcast touched

A separate binary that execs `fluxcast` and parses its output. Because it never imports fluxcast code, the GPL boundary is clean and you may license it as you like (matching GPL-3.0 is still the friendly choice).

- **hyprctl-native monitor enumeration.** `hyprctl monitors -j` for name/scale/transform/refreshRate/currentFormat. Fixes the `xrandr`-over-XWayland blindness at `capture.py:133-188` from the outside.
- **Extend, don't mirror.** `hyprctl output create headless HYPRCAST`, sized to the mode fluxcast negotiated, then `fluxcast --protocol wfd --monitor HYPRCAST --wfd-peer <mac> --wfd-latency-log /run/user/$UID/hyprcast.jsonl`. This is the headline feature no cross-desktop tool can offer, and it is exactly what upstream endorsed in issue #14.
- **Session supervisor.** Owns teardown — which fixes the SIGTERM leak from outside by sending SIGINT to a process group instead of SIGTERM to a bare child. Tears down the headless output on session drop; restores workspaces.
- **Status socket + `hyprcast --waybar`.** JSON lines from tailing the JSONL plus your own state machine. `alt` states: idle / discovering / connecting / casting / error.
- **One keybind** (`$mainMod+K` → device picker). `asuramaya/kast` proves this UX thesis for GNOME; nobody has done it for Hyprland.
- **Hyprland ≥0.55 permission handling.** `screencopy` defaults to ASK; a denied wf-recorder produces a black frame with "permission denied" burned into the stream. Detect it and ship the exact `hl.permission({ binary = ..., type = "screencopy", mode = "allow" })` Lua snippet, including the NixOS regex form. All config examples must be **Lua**, not hyprlang — hyprlang is deprecated as of 0.55.
- **Ship:** AUR `hyprcast-git`, `flake.nix` with a home-manager module, PR to `hyprland-community/awesome-hyprland` (5.2k stars — the single highest-leverage discovery channel), and a waybar snippet short enough to paste unedited into a `modules.jsonc`.

### Track C — the only thing that justifies real engineering

Build **`hyprcast-capture`**: a standalone C or Rust binary doing `ext-image-copy-capture-v1` → dmabuf → VAAPI → H.264 Annex-B on stdout or straight to an RTP socket. Then offer it to fluxcast as a fifth capture backend behind `--wfd-capture-backend hyprcast`.

Why this shape is right:
- It's where the 30x CPU win lives, and it's the only way to get it (wf-recorder v0.6.0 has no `ext-image-copy-capture` support and is frozen on the deprecated protocol; xdph hasn't migrated either).
- It's compositor-specific, so it's precisely the code upstream said it doesn't want inline — no scope fight.
- It's a separate process, so it cannot rot fluxcast's protocol code and the GPL boundary stays clean.
- **Publish the latency numbers.** Nobody has. That alone is a reason for the project to exist.

Gate it on Track A's measurements: if `-x yuv420p` + pipe resize + dropping sliced threads gets you to a smooth 1080p30, Track C is a 6-10 week optimization, not a rescue.

### Do not hard fork. Revisit only if:

1. A change you need lives inside `wfd.py`'s RTSP/RTP state machine and is rejected twice on scope grounds;
2. Upstream goes 90+ days without a release (baseline: 11 in 13 weeks);
3. The bus factor materializes — 132 of 144 commits are from one person.

A hostile relicense is not on the list because it isn't available: GPL-3.0-or-later with no CLA and 13 external copyright holders. Waiting costs nothing and the fork option never expires.

**If you ever do vendor code:** GPL-3.0 §5 requires a dated "modified from fluxcast `<sha>`" notice, and you must keep `src/tray.py:389` (`"Author: IlyaP358 | Code licensed under GPL-3.0"`) — it is the *only* attribution string in the entire codebase and the §5(d) Appropriate Legal Notice for the interactive UI. Removing it while rebranding is both a license violation and the fastest route to a public fight you cannot win. Don't use the logo, `fluxcast.dev`, or `fluxcast.secweb.cloud`; link his Ko-fi rather than shadowing it.