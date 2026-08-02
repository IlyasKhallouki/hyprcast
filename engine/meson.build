project('hyprcast-engine', 'c',
  version: '0.1.0',
  default_options: ['c_std=c11', 'warning_level=3', 'b_ndebug=false'])

cc = meson.get_compiler('c')

deps = [
  dependency('threads'),
  dependency('wayland-client'),
  dependency('gbm'),
  dependency('libdrm'),
  dependency('libva'),
  dependency('libva-drm'),
  dependency('libavcodec'),
  dependency('libavformat'),
  dependency('libavutil'),
  dependency('libavdevice'),
  cc.find_library('m'),
]

# Wayland protocol glue, generated from vendored XML by wayland-scanner.
# ext-foreign-toplevel-list is required at link time even though we only use the
# output capture source: ext-image-capture-source-v1.c references
# ext_foreign_toplevel_handle_v1_interface.
proto = files(
  'protocol/ext-image-copy-capture-v1.c',
  'protocol/ext-image-capture-source-v1.c',
  'protocol/ext-foreign-toplevel-list-v1.c',
  'protocol/linux-dmabuf-v1.c',
)

proto_inc = include_directories('protocol')

# ---------------------------------------------------------------------------
# capture-probe: the measured reference client. Builds today, proves the
# toolchain and the compositor's ext-image-copy-capture support.
#   MOD=LINEAR|Y_TILED  OUTPUT=<name>  VERIFY=1  CURSORS=0|1
# ---------------------------------------------------------------------------
executable('capture-probe',
  ['../reference/probes/probe.c'] + proto,
  include_directories: proto_inc,
  dependencies: deps,
  install: false)

# ---------------------------------------------------------------------------
# va-probe / vpp-probe: VAAPI import + RGB->NV12 VPP timing on this GPU.
# ---------------------------------------------------------------------------
executable('va-probe',
  ['../reference/probes/vatest.c'],
  dependencies: deps,
  install: false)

executable('vpp-probe',
  ['../reference/probes/vpptest.c'],
  dependencies: deps,
  install: false)

# ---------------------------------------------------------------------------
# hyprcast-bench (Milestone 1B) — capture -> import -> VPP -> encode -> sink,
# with per-stage histograms. Sources land here as they are written.
# ---------------------------------------------------------------------------
fs = import('fs')

bench_names = ['bench', 'capture', 'dmabuf', 'vaapi', 'encode', 'hist']
bench_src = []
bench_ready = true
foreach n : bench_names
  if fs.exists('src' / n + '.c')
    bench_src += files('src' / n + '.c')
  else
    bench_ready = false
  endif
endforeach

if bench_ready
  executable('hyprcast-bench',
    bench_src + proto,
    include_directories: [proto_inc, include_directories('src')],
    dependencies: deps,
    install: false)
else
  message('Milestone 1B sources incomplete — hyprcast-bench target skipped.')
endif

# ---------------------------------------------------------------------------
# hyprcast-engine — the shipping binary. Same media path as the bench, plus the
# CFR pacer, the muxer, the audio leg and the fd-3 JSON control channel.
# capture/dmabuf/vaapi/encode/hist are shared verbatim with hyprcast-bench.
# ---------------------------------------------------------------------------
engine_names = ['main', 'control', 'capture', 'dmabuf', 'vaapi', 'encode',
                'hist', 'mux', 'audio']
engine_src = []
engine_missing = []
foreach n : engine_names
  if fs.exists('src' / n + '.c')
    engine_src += files('src' / n + '.c')
  else
    engine_missing += n + '.c'
  endif
endforeach

if engine_missing.length() == 0
  executable('hyprcast-engine',
    engine_src + proto,
    include_directories: [proto_inc, include_directories('src')],
    dependencies: deps + [dependency('threads')],
    install: true)
else
  message('hyprcast-engine target skipped — missing: ' + ', '.join(engine_missing))
endif

# ---------------------------------------------------------------------------
# hyprcast-muxtest (Milestone 2) — the same native path, but out on the wire
# through hc_mux (rtp_mpegts) with hc_audio's AAC alongside it. Point it at
# tools/mock-sink.py and check the result with tools/assert-ts.py.
# ---------------------------------------------------------------------------
mux_names = ['muxtest', 'mux', 'audio', 'capture', 'dmabuf', 'vaapi', 'encode',
             'hist']
mux_src = []
mux_ready = true
foreach n : mux_names
  if fs.exists('src' / n + '.c')
    mux_src += files('src' / n + '.c')
  else
    mux_ready = false
  endif
endforeach

if mux_ready
  executable('hyprcast-muxtest',
    mux_src + proto,
    include_directories: [proto_inc, include_directories('src')],
    dependencies: deps,
    install: false)
else
  message('Milestone 2 sources incomplete — hyprcast-muxtest target skipped.')
endif
