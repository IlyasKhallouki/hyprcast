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
 */
int  hc_capture_frame(struct hc_capture *c, struct hc_frame *out, int timeout_ms);
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
    bool     low_power;          /* VAEntrypointEncSliceLP -- Gen9.5 has it */
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

#endif /* HC_H */
