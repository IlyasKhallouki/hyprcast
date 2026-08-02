/*
 * hyprcast engine -- internal module contract.
 *
 * Data path, measured on i5-8350U / UHD 620 / Hyprland 0.55.4:
 *
 *   Hyprland composites into OUR gbm bo   (ext-image-copy-capture-v1, ~3 ms)
 *     -> same bo imported once as a VA surface, VA_FOURCC_BGRX  (no copy)
 *     -> VAAPI VPP BGRX -> NV12 on VEBOX/SFC                    (~0.9 ms)
 *     -> h264_vaapi encodes the NV12 surface directly           (~3.6 ms)
 *     -> Annex-B bitstream  (the ONLY thing the CPU ever reads)
 *
 * The CPU must never touch a pixel. If you find yourself adding
 * hwupload, memcpy, or an AV_PIX_FMT_NV12 (software) AVFrame, stop.
 *
 * Hard-won constraints, all reproduced on this box -- see STATUS.md:
 *   - Y_TILED_CCS cannot be imported by iHD ("resource allocation failed").
 *     Pin the modifier list and assert plane_count == 1.
 *   - DRM XR24 is byte-order BGRX. VA_FOURCC_RGBX imports without error and
 *     silently swaps red and blue.
 *   - close() the dmabuf fd after vaCreateSurfaces; iHD dups it.
 *   - buffer_size arrives LAST in the constraints round, after both
 *     dmabuf_format events. Commit only on `done`.
 */
#ifndef HC_H
#define HC_H

#include <stdint.h>
#include <stdbool.h>
#include <va/va.h>

#define HC_MAX_BUFS   4
#define HC_MAX_MODS  16

/* ---------------------------------------------------------------- common */

uint64_t hc_now_ns(void);
const char *hc_fourcc_str(uint32_t fourcc, char buf[5]);
const char *hc_mod_str(uint64_t modifier);

/* ------------------------------------------------------------- histogram */

struct hc_hist {
    const char *name;
    uint64_t   *v;       /* nanosecond samples, reservoir */
    int         cap, n;
    uint64_t    total;   /* count of all observations, including evicted */
};

void   hc_hist_init(struct hc_hist *h, const char *name, int cap);
void   hc_hist_add(struct hc_hist *h, uint64_t ns);
double hc_hist_pct(struct hc_hist *h, double pct);   /* returns milliseconds */
void   hc_hist_print(struct hc_hist *h);
void   hc_hist_free(struct hc_hist *h);

/* ------------------------------------------------------------ gbm buffers */

struct hc_buf {
    struct gbm_bo    *bo;
    struct wl_buffer *wl;
    uint64_t          modifier;
    uint32_t          stride, offset;
    VASurfaceID       va;        /* VA_INVALID_ID until hc_vaapi_import_pool */
    bool              in_flight;
};

struct hc_pool {
    int                drm_fd;
    struct gbm_device *gbm;
    uint32_t           width, height, fourcc;
    struct hc_buf      buf[HC_MAX_BUFS];
    int                n;
};

/*
 * Allocate `count` bos of w*h*fourcc restricted to `mods`, and wrap each in a
 * wl_buffer via zwp_linux_dmabuf create_immed. Aborts if a bo comes back with
 * more than one plane (that means a CCS modifier slipped through).
 * Takes ownership of nothing; caller keeps drm_fd open.
 */
int  hc_pool_create(struct hc_pool *p, int drm_fd,
                    struct zwp_linux_dmabuf_v1 *dmabuf,
                    uint32_t w, uint32_t h, uint32_t fourcc,
                    const uint64_t *mods, int nmods, int count);
void hc_pool_destroy(struct hc_pool *p);

/* ---------------------------------------------------------------- capture */

struct hc_capture;

struct hc_capture_caps {
    uint32_t width, height;
    uint32_t fourcc;                    /* preferred; XRGB8888 if offered */
    uint64_t mods[HC_MAX_MODS];
    int      nmods;
    bool     have_dmabuf_device;
    dev_t    dmabuf_device;
};

/*
 * Connect to Wayland, bind globals, pick `output_name` (substring match; NULL
 * = first), create source + session, and run constraint rounds to completion.
 * `paint_cursors` maps to create_session options: 0 or 1 ONLY -- any other
 * value raises invalid_option and kills the connection.
 */
struct hc_capture *hc_capture_open(const char *output_name, bool paint_cursors);
const struct hc_capture_caps *hc_capture_caps(struct hc_capture *c);
struct zwp_linux_dmabuf_v1 *hc_capture_dmabuf(struct hc_capture *c);

/* Bind the pool the session will render into. Call once, after hc_pool_create. */
void hc_capture_set_pool(struct hc_capture *c, struct hc_pool *p);

struct hc_frame {
    int      buf_index;      /* index into pool->buf */
    uint64_t presentation_ns;/* compositor clock, stamped at ATOMIC COMMIT --
                              * not page flip. Label metrics commit->x. */
    bool     have_presentation;
    uint32_t transform;
    int      damage_rects;
    bool     ok;             /* false => the frame failed; buffer unusable */
};

/*
 * Capture exactly one frame into the next free pool buffer and block until
 * ready/failed, or `timeout_ms` elapses. Returns 0 on success.
 * A timeout with neither ready nor failed is the Hyprland screencopy
 * permission prompt hanging -- surface it, do not retry silently.
 *
 * Equivalent to hc_capture_submit() immediately followed by hc_capture_wait().
 * Convenient, but it CANNOT sustain 60 fps: VPP and encode then happen while
 * no capture is outstanding, so the next vblank is missed. Measured 52.2 fps
 * with a 17.3 ms serial p50 against a 16.67 ms budget. Use submit/wait.
 */
int  hc_capture_frame(struct hc_capture *c, struct hc_frame *out, int timeout_ms);

/*
 * Pipelined capture. Keep one request in flight while the previous frame is
 * being converted and encoded, so VPP+encode (~3.5 ms measured) hides inside
 * the ~13 ms the compositor takes to produce the next frame:
 *
 *     hc_capture_submit(c, &tok);
 *     for (;;) {
 *         hc_capture_wait(c, tok, &frame, 2000);
 *         hc_capture_submit(c, &next);   // BEFORE touching this frame
 *         vpp(frame); encode(frame);     // now overlapped with capture
 *         tok = next;
 *     }
 *
 * Bounded by the pool size: at most n-1 may be outstanding, so a 3-buffer pool
 * allows 2 in flight. submit returns -4 if none are free.
 * The token stays valid until wait() consumes it.
 */
struct hc_inflight;

int hc_capture_submit(struct hc_capture *c, struct hc_inflight **token);
int hc_capture_wait(struct hc_capture *c, struct hc_inflight *token,
                    struct hc_frame *out, int timeout_ms);
/* True if the compositor re-sent buffer constraints; pool must be rebuilt. */
bool hc_capture_constraints_changed(struct hc_capture *c);
void hc_capture_close(struct hc_capture *c);

/* ------------------------------------------------------------------ vaapi */

struct hc_va;

/*
 * Open /dev/dri/renderD128, force the iHD driver, and assert the vendor
 * string contains "iHD". i965 misreports RGB32 encode entrypoints on Gen9.5.
 */
struct hc_va *hc_va_open(int drm_fd);
VADisplay     hc_va_display(struct hc_va *v);

/* Import every bo in the pool as a BGRX VA surface. Fills buf[i].va. */
int hc_va_import_pool(struct hc_va *v, struct hc_pool *p);

/*
 * Create a VPP context that scales/converts BGRX -> NV12.
 * dst_w/dst_h may differ from the pool size: the sink's negotiated wire size
 * is decoupled from the capture size precisely so retuning never renegotiates.
 */
int hc_va_vpp_init(struct hc_va *v, uint32_t dst_w, uint32_t dst_h);
/* Convert one imported surface into an NV12 surface owned by the caller. */
int hc_va_vpp_run(struct hc_va *v, VASurfaceID src, VASurfaceID dst);
void hc_va_close(struct hc_va *v);

/* ----------------------------------------------------------------- encode */

struct hc_enc;

struct hc_enc_cfg {
    uint32_t width, height;      /* wire size, e.g. 1280x720 */
    uint32_t fps;                /* 60 */
    uint32_t bitrate_bps;        /* VBR target */
    uint32_t gop;                /* frames between IDR */
    bool     low_power;          /* VAEntrypointEncSliceLP -- Gen9.5 has it.
                                  * MEASURED: VDEnc on Gen9.5 accepts CQP ONLY;
                                  * CBR and VBR fail avcodec_open2 with EINVAL.
                                  * So low_power ignores bitrate_bps and uses qp. */
    uint32_t qp;                 /* CQP quantiser when low_power; 0 => 26 */
};

/*
 * Wrap our existing VADisplay in an AVHWDeviceContext so libavcodec encodes
 * the very surfaces VPP wrote, with no transfer. Allocates the NV12 hwframe
 * pool that hc_va_vpp_run writes into.
 */
struct hc_enc *hc_enc_open(struct hc_va *v, const struct hc_enc_cfg *cfg);
/* Borrow an NV12 surface from the encoder's pool as a VPP destination. */
int  hc_enc_acquire(struct hc_enc *e, VASurfaceID *out, void **frame_opaque);
/* Submit the surface previously acquired. pts_ns is the capture clock. */
int  hc_enc_submit(struct hc_enc *e, void *frame_opaque, uint64_t pts_ns, bool force_idr);
/*
 * Drain one Annex-B access unit. Returns 1 on packet, 0 if none pending,
 * negative on error. *data stays valid until the next call.
 */
int  hc_enc_receive(struct hc_enc *e, const uint8_t **data, int *size, bool *is_idr);
void hc_enc_close(struct hc_enc *e);

/* -------------------------------------------------------------------- mux */

/*
 * MPEG-TS over RTP, exactly as the Xiaomi sink accepted it. The layout is
 * whatever we choose so long as it is self-consistent: AOSP's ATSParser reads
 * the PMT PID from the PAT and the elementary PIDs from the PMT, hardcoding
 * nothing but PID 0 (verified -- reference/aosp/ATSParser.cpp). We keep
 * fluxcast's PIDs because that combination is field-proven against this sink.
 */
struct hc_mux;

struct hc_mux_cfg {
    const char *dst_ip;
    int         dst_port;
    int         src_port;        /* bind this locally; the sink checks it */
    uint32_t    width, height, fps;
    bool        with_audio;
};

struct hc_mux *hc_mux_open(const struct hc_mux_cfg *cfg,
                           const uint8_t *extradata, int extradata_size);
/* Feed one H.264 access unit. pts_ns is the capture clock. */
int  hc_mux_video(struct hc_mux *m, const uint8_t *data, int size,
                  uint64_t pts_ns, bool is_idr);
/* Feed one encoded AAC frame from hc_audio_read(). */
int  hc_mux_audio(struct hc_mux *m, const uint8_t *data, int size, uint64_t pts_ns);
uint64_t hc_mux_bytes_sent(struct hc_mux *m);
void hc_mux_close(struct hc_mux *m);

/* ------------------------------------------------------------------ audio */

/*
 * PipeWire (via the pulse compat device) -> AAC-LC 48 kHz stereo, which is what
 * the sink advertised as `AAC 00000007` and accepted as `AAC 00000001`.
 * Runs on its own thread; hc_audio_read() pops from a bounded ring so a stalled
 * muxer can never block capture.
 */
struct hc_audio;

struct hc_audio_cfg {
    const char *device;          /* pulse source name, e.g. "...monitor" */
    uint32_t    bitrate_bps;     /* 128000 */
};

struct hc_audio *hc_audio_open(const struct hc_audio_cfg *cfg);
/* 1 = frame returned, 0 = nothing pending, negative = error. */
int  hc_audio_read(struct hc_audio *a, const uint8_t **data, int *size, uint64_t *pts_ns);
/* 0.0 .. 1.0 applied to the captured PCM before encoding; live-tunable. */
void hc_audio_set_volume(struct hc_audio *a, float gain);
void hc_audio_set_muted(struct hc_audio *a, bool muted);
void hc_audio_close(struct hc_audio *a);

/* ---------------------------------------------------------------- control */

/*
 * Newline-delimited JSON over an inherited fd (3 by default). The Python
 * control plane owns P2P and RTSP; this is how it drives the media leg.
 *
 *   -> {"cmd":"start","dst_ip":"192.168.168.42","dst_port":19900,
 *       "src_port":19002,"width":1280,"height":720,"fps":60,
 *       "bitrate":8000000,"gop":60,"output":"eDP-1","audio":"...monitor"}
 *   -> {"cmd":"retune","fps":30}          fps/bitrate/qp, no restart
 *   -> {"cmd":"idr"}                      honour the sink's IDR request
 *   -> {"cmd":"volume","gain":0.5}        or {"muted":true}
 *   -> {"cmd":"output","name":"HEADLESS-1"}   rebuild capture, keep the session
 *   -> {"cmd":"stop"} / {"cmd":"quit"}
 *
 *   <- {"ev":"ready"}
 *   <- {"ev":"stats","fps":58.2,"kbps":7502,"cpu":0.095,"drops":0,"idr":12}
 *   <- {"ev":"error","msg":"..."}
 */
struct hc_ctl;

struct hc_ctl_msg {
    char     cmd[24];
    char     s_dst_ip[64], s_output[64], s_audio[128];
    int      dst_port, src_port;
    uint32_t width, height, fps, bitrate, gop, qp;
    float    gain;
    bool     muted, has_muted;
    bool     low_power, has_low_power;
};

struct hc_ctl *hc_ctl_open(int fd);
/* 1 = message parsed, 0 = nothing pending, -1 = peer closed, -2 = error. */
int  hc_ctl_poll(struct hc_ctl *c, struct hc_ctl_msg *out);
int  hc_ctl_event(struct hc_ctl *c, const char *json);
void hc_ctl_close(struct hc_ctl *c);

#endif /* HC_H */
