# hyprcast

Casts my Hyprland desktop to my TV over Wi-Fi Display, with the GPU doing the
work so the CPU never touches a pixel.

This is a side project. I forked [fluxcast](https://github.com/IlyaP358/fluxcast)
and tuned it hard for one laptop and one television, because that is the setup I
actually own. Anything that machine does not use, I deleted. If you want
something that works across many desktops, TVs and protocols, use fluxcast
upstream. It is the more useful program; this one is the more specific one.

## Why

fluxcast pipes raw frames into an `ffmpeg` subprocess and encodes H.264 in
software with x264. On my laptop that costs 197% of one core and tops out at
84 fps on a synthetic 1080p source, which is why the picture stuttered. My
machine is a 15 W chip from 2017; fluxcast is developed against 28 W parts with
three times the multicore throughput, so it was never going to be smooth here.

hyprcast replaces that path with a C11 engine. Hyprland hands out a dmabuf
through `ext-image-copy-capture-v1`, the same buffer is imported into VA-API,
the GPU's VPP block converts BGRX to NV12, and `h264_vaapi` encodes it. Nothing
is copied and no pixel crosses the CPU. The only bytes the CPU reads are the
H.264 bitstream and the audio PCM.

The Python that remains handles Wi-Fi Direct group formation and the WFD RTSP
negotiation. That part is upstream's work and it is good; I did not want to
rewrite it and I have not.

## What it runs on

```
i5-8350U (4c/8t, Kaby Lake-R) · Intel UHD 620 (Gen9.5) · /dev/dri/renderD128
Hyprland 0.55.4 · eDP-1 1920x1080@60 · Intel 8265
PipeWire 1.6.7 · ffmpeg n8.1.2 · iHD 26.1.5 · Python 3.14
Sink: a Xiaomi box running Google TV, using its preinstalled Miracast app
```

The sink negotiates 1280x720p60, H.264 Constrained Baseline level 3.2, AAC at
48 kHz stereo. 720p is its ceiling, so the engine captures 1080p and scales to a
720p60 wire on the GPU.

## Numbers

Measured on the machine above. The load is mpv playing a 1080p60 hardware
decoded clip, which contends for the same GPU, so these are not idle figures.

| | capture p50 | vpp p50 | encode p50 | fps | CPU |
|---|---|---|---|---|---|
| VBR, `EncSlice` | 8.85 ms | 0.32 ms | 7.51 ms | 58.2 | 9.5% |
| CQP, `EncSliceLP` | 12.24 ms | 0.38 ms | 3.98 ms | 57.8 | 6.6% |

On an idle desktop the engine holds a flat 60.0 fps at 5 to 11% of one core,
with zero dropped frames. Against x264's 197%, that is roughly a 20 to 30 times
reduction in CPU.

Some other figures I keep going back to:

- `ext-image-copy-capture-v1` sustains 60.65 fps with no failures, and the
  compositor's copy takes 2.8 to 3.6 ms.
- VPP BGRX to NV12 costs 0.881 ms per frame, a ceiling of about 1135 fps.
- The whole M1 to M6 RTSP negotiation costs 333 ms. The sink then sits there for
  8.024 seconds before sending PLAY. That stall is entirely on the TV, so do not
  add a timeout that fires inside it.

Bitstream checked: Constrained Baseline, level 3.2, 1280x720, `has_b_frames=0`,
bt709 with TV range, decodes with no errors. I checked the colour against a
`grim` screenshot rather than trusting it, because a red/blue channel swap can
hide in a symmetric test. Mean RGB matched within 3 of 765.

[STATUS.md](STATUS.md) has the full record, including the dmabuf modifier survey
and the Wi-Fi regulatory constraints on this card.

## Things that cost me hours

- `Y_TILED_CCS` cannot be imported by iHD, and
  `gbm_bo_create_with_modifiers2()` picks it if you hand it Hyprland's full
  advertised list. The modifier list is pinned to `I915_FORMAT_MOD_Y_TILED` and
  `plane_count` is asserted equal to 1.
- DRM `XR24` is byte order BGRX. Importing as `VA_FOURCC_RGBX` succeeds and
  silently swaps red and blue.
- VDEnc (`low_power=1`, `VAEntrypointEncSliceLP`) accepts CQP only on Gen9.5.
  CBR and VBR both fail `avcodec_open2` with `EINVAL`.
- On Intel's target usage scale, `quality=1` is *slower* than `quality=4`.
  Reaching for "best quality" is the wrong instinct.
- The peer's IP and the `p2p-*` interface change between sessions. I have seen
  192.168.13.x, 192.168.71.x and 192.168.233.x on the same TV. Resolve the peer
  by ARP scanning `p2p-*` and never assume a subnet.
- Benchmark against a realistic load. `vkcube` saturates the same small GPU you
  are trying to measure and gives fake numbers. It cost me three rounds of
  chasing a regression that was not there.
- My TV's "Fluency" and "Definition" setting decides what it advertises.
  Fluency offers 720p30 and nothing else. Definition offers the full mode table.
  Counter-intuitively, Definition is the one that gets you 60 fps.
- A sink can advertise modes it will not accept. Mine lists 1080p60 in its CEA
  mask and then caps itself at `max_hres` 1280, `max_vres` 720 in the same
  string. fluxcast never parsed those two fields; hyprcast treats them as
  binding.

## Build

C11, meson and ninja, `warning_level=3`, no warnings.

```bash
cd engine
meson setup build
ninja -C build
```

Dependencies are `wayland-client`, `gbm`, `libdrm`, `libva`, `libva-drm` and the
ffmpeg libraries. On my Arch install every one of them was already present.

| Binary | What it does |
|---|---|
| `build/hyprcast-engine` | the real thing: capture, VPP, encode, audio, TS/RTP mux, control channel |
| `build/hyprcast-bench` | the same media path with per-stage histograms and no networking |
| `build/hyprcast-muxtest` | media path out to the wire, for pointing at the mock sink |
| `build/capture-probe` | capture rate and latency (`MOD=Y_TILED`, `OUTPUT=`, `VERIFY=1`) |
| `build/va-probe` | driver, dmabuf modifiers, encode entrypoints |
| `build/vpp-probe` | RGB to NV12 timing |

Capture is damage driven, so the probes need something moving on screen before
they measure anything meaningful.

## Run

The Python side is standard library only. There is no `requirements.txt` and
nothing to `pip install`.

```bash
ln -sf "$PWD/packaging/hyprcast" ~/.local/bin/hyprcast

hyprcast doctor        # checks only what actually blocks a cast
hyprcast scan          # is the TV advertising at all
hyprcast probe         # connect, read M3, report what it will give you, disconnect
hyprcast cast          # discover, negotiate, stream
```

While a session runs:

```bash
hyprcast ctl fps 30
hyprcast ctl bitrate 6M
hyprcast ctl volume 50
hyprcast ctl mute
hyprcast status
hyprcast waybar        # streaming JSON for a waybar custom module
```

`probe` is the one I use most. The P2P beacon a scan sees carries no mode table,
so you cannot tell 30 from 60 until you have connected and read M3. `probe` does
exactly that and then tears the link down without starting the media path.

System binaries still needed: `ffmpeg`, `iw`, `wpa_cli`, `nmcli`, `gdbus`, and
`pactl` for picking the audio monitor.

## Testing without the TV

The loopback harness replays a byte exact capture of a real session on
localhost, so the RTSP and MPEG-TS layers can be worked on with the television
switched off.

```bash
python3 tools/rtsp-loopback-source.py   # the WFD RTSP source
python3 tools/mock-sink.py              # replays the captured sink half
python3 tools/assert-ts.py stream.ts    # validates the MPEG-TS
```

Last full run: 15,915 datagrams and 111,405 TS packets with nothing lost,
reordered, duplicated or malformed, and all 12 validator checks passing. IDR
interval measured 1000.0 ms with no jitter.

## A warning about outputs

While building this I crashed Hyprland. Creating and destroying headless outputs
while a capture session was bound to them made the compositor abort:

```
#5  Screenshare::CScreenshareFrame::transform() const
#6  CImageCopyCaptureFrame::CImageCopyCaptureFrame(...)
#12 libwayland-server   <- dispatching our create_frame
```

`CScreenshareFrame::transform()` returns `m_session->monitor()->m_transform`
with no null check, so once the captured output is gone, `create_frame` takes
the whole compositor down and every client with it. hyprcast now refuses to
submit once its `wl_output` disappears. The underlying bug is Hyprland's, and
any unprivileged client can trigger it.

## Layout

```
engine/            C11 media path (meson + ninja)
  protocol/        vendored wayland-protocols XML and wayland-scanner output
  src/             hc.h is the contract between the modules
python/hyprcast/   CLI, Wi-Fi Direct, WFD RTSP, control socket, waybar (stdlib only)
reference/
  probes/          the C probes every number above came from
  hyprland/        Hyprland 0.55.4 screenshare implementation
  aosp/            AOSP WFD source, which is what actually defines the wire format
  sink/            byte exact capture of a working 720p60 session
  docs/            design notes and the original fork analysis
tools/             loopback harness: RTSP source, mock sink, MPEG-TS validator
```

## What I removed from fluxcast

DLNA, Chromecast, the HTTP and HLS bridge, the system tray, the `--doctor`
diagnostics, the UIBC touch back channel, the Microsoft-only LPCM muxer, the
`xdg-desktop-portal` capture path, and the `wf-recorder` and `x11grab` backends.
`src/` went from 8,834 lines to about 3,000, and every third party Python
dependency went with it.

The LPCM/TS muxer is kept verbatim at `reference/mpegts_muxer.py` because it is
a good dependency free MPEG-TS reference.

## Credit and licence

hyprcast is a fork of [fluxcast](https://github.com/IlyaP358/fluxcast).

**Author: IlyaP358 | Code licensed under GPL-3.0**

The Wi-Fi Direct group formation, the WFD RTSP state machine and the M1 to M6
negotiation are IlyaP358's work, carried over from upstream and largely
untouched. That is the hard part of this program and I did not write it.

Modified by Ilyas Khallouki starting 2026-08-02: added the native
capture and encode engine, the control socket and CLI, and removed the features
listed above.

Licensed under GPL-3.0-or-later, the same licence as upstream. See
[LICENSE](LICENSE) for the full text.
