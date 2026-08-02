/*
 * hyprcast -- ext-image-copy-capture-v1 capture front end.
 *
 * Ported from reference/probes/probe.c, which sustains 60.65 fps with 0
 * failures on this box. The file-scope globals of the probe have become a
 * heap-allocated struct hc_capture handed to every listener as user data.
 *
 * Things that are load-bearing and were learned the hard way:
 *   - create_session options takes 0 or 1 ONLY. 0xFF raises invalid_option
 *     and kills the connection.
 *   - zwp_linux_dmabuf_v1 is bound CLAMPED TO VERSION 4. Hyprland offers 5;
 *     at v4+ the deprecated format/modifier events stop being sent.
 *   - wl_output must be bound at version >= 4 to get the name event.
 *   - In a constraints round buffer_size arrives LAST, after both
 *     dmabuf_format events. Everything is accumulated into a staging struct
 *     and committed only on `done`.
 *   - Every wait is bounded. Hyprland's screencopy permission prompt can hang
 *     with NEITHER ready NOR failed; that is reported loudly, never retried
 *     silently.
 *   - The prepare_read / poll / read_events / dispatch_pending dance below is
 *     copied from probe.c lines 244-251. Reordering it deadlocks.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <poll.h>
#include <sys/types.h>

#include <drm_fourcc.h>
#include <wayland-client.h>

#include "ext-image-capture-source-v1-client-protocol.h"
#include "ext-image-copy-capture-v1-client-protocol.h"
#include "linux-dmabuf-v1-client-protocol.h"
#include "hc.h"

#define HC_MAX_OUTPUTS   8
#define HC_MAX_FORMATS  16
#define HC_OUTPUT_NAME_LEN 128

/* Bounded wait for the initial handshake and for each constraints round. */
#define HC_ROUNDTRIP_MS 3000

/* pump() results. */
#define HC_PUMP_OK       0
#define HC_PUMP_TIMEOUT (-2)
#define HC_PUMP_ERROR   (-3)

/* Frame resolution states, matching probe.c's f_state. */
#define HC_FRAME_PENDING 0
#define HC_FRAME_READY   1
#define HC_FRAME_FAILED  2

/*
 * One outstanding capture request. Listener user data is the record, not the
 * context, so several frames can be resolving concurrently without clobbering
 * each other's presentation_time / transform / damage.
 */
struct hc_inflight {
    struct hc_capture                       *c;
    struct ext_image_copy_capture_frame_v1  *fr;
    int       buf_index;
    int       state;          /* HC_FRAME_*                    */
    uint64_t  pres;
    bool      have_pres;
    uint32_t  xform;
    int       ndamage;
    bool      busy;           /* slot allocated                */
};

struct hc_out_rec {
    struct wl_output *out;
    uint32_t          global;
    char              name[HC_OUTPUT_NAME_LEN];
};

/* One constraints round, accumulated and committed only on `done`. */
struct hc_fmt_rec {
    uint32_t fourcc;
    uint64_t mods[HC_MAX_MODS];
    int      nmods;
};

struct hc_stage {
    uint32_t          width, height;
    bool              have_size;
    struct hc_fmt_rec fmt[HC_MAX_FORMATS];
    int               nfmt;
    bool              have_dev;
    dev_t             dev;
};

struct hc_capture {
    struct wl_display  *dpy;
    struct wl_registry *reg;
    int                 wl_fd;

    struct ext_output_image_capture_source_manager_v1 *src_mgr;
    struct ext_image_copy_capture_manager_v1          *cap_mgr;
    struct zwp_linux_dmabuf_v1                        *dmabuf;

    struct hc_out_rec out[HC_MAX_OUTPUTS];
    int               nout;
    struct wl_output *output;
    uint32_t          output_global;
    bool              output_gone;

    struct ext_image_capture_source_v1       *src;
    struct ext_image_copy_capture_session_v1 *sess;
    bool                                      stopped;

    struct hc_stage        stage;
    struct hc_capture_caps caps;
    int                    rounds;          /* completed constraints rounds */
    int                    sync_flag;       /* set by the `done` event       */
    bool                   caps_dirty;      /* re-sent constraints, unread   */

    struct hc_pool *pool;

    /* Per-frame state, valid only inside hc_capture_frame(). */
    int      f_state;
    uint64_t f_pres;
    bool     f_have_pres;
    uint32_t f_xform;
    int      f_ndamage;

    /*
     * In-flight records for the pipelined submit/wait path. One slot per pool
     * buffer: a buffer cannot be reused until its frame resolves, so the pool
     * size is the natural bound on concurrency.
     */
    struct hc_inflight infl[HC_MAX_BUFS];
};

/* ------------------------------------------------------------------ pump */

/*
 * Dispatch Wayland events until *flag is non-zero or the deadline expires.
 * This is probe.c's loop verbatim in structure, with the error paths that a
 * one-shot probe could afford to skip.
 */
static int pump(struct hc_capture *c, const int *flag, int timeout_ms)
{
    struct wl_display *d = c->dpy;
    uint64_t deadline = 0;
    bool bounded = timeout_ms >= 0;

    if (bounded)
        deadline = hc_now_ns() + (uint64_t)timeout_ms * 1000000ull;

    while (!*flag) {
        while (wl_display_prepare_read(d) != 0) {
            if (wl_display_dispatch_pending(d) < 0)
                return HC_PUMP_ERROR;
            if (*flag)
                return HC_PUMP_OK;
        }
        if (*flag) {
            wl_display_cancel_read(d);
            return HC_PUMP_OK;
        }
        if (wl_display_flush(d) < 0 && errno != EAGAIN) {
            wl_display_cancel_read(d);
            return HC_PUMP_ERROR;
        }

        int tmo = -1;
        if (bounded) {
            uint64_t now = hc_now_ns();
            tmo = now >= deadline ? 0 : (int)((deadline - now) / 1000000ull);
        }
        struct pollfd pfd = { c->wl_fd, POLLIN, 0 };
        int pr = poll(&pfd, 1, tmo);
        if (pr < 0) {
            wl_display_cancel_read(d);
            if (errno == EINTR)
                continue;
            return HC_PUMP_ERROR;
        }
        if (pr == 0) {
            wl_display_cancel_read(d);
            return HC_PUMP_TIMEOUT;
        }
        if (wl_display_read_events(d) < 0)
            return HC_PUMP_ERROR;
        if (wl_display_dispatch_pending(d) < 0)
            return HC_PUMP_ERROR;
    }
    return HC_PUMP_OK;
}

static void sync_done(void *data, struct wl_callback *cb, uint32_t serial)
{
    (void)cb;
    (void)serial;
    *(int *)data = 1;
}
static const struct wl_callback_listener sync_l = { sync_done };

/* wl_display_roundtrip() with a deadline. */
static int roundtrip(struct hc_capture *c, int timeout_ms)
{
    int done = 0;
    struct wl_callback *cb = wl_display_sync(c->dpy);
    if (!cb)
        return HC_PUMP_ERROR;
    wl_callback_add_listener(cb, &sync_l, &done);
    int rc = pump(c, &done, timeout_ms);
    wl_callback_destroy(cb);
    return rc;
}

/* ---------------------------------------------------------- wl_output ---- */

static void o_geometry(void *data, struct wl_output *o, int32_t x, int32_t y,
                       int32_t pw, int32_t ph, int32_t sub,
                       const char *make, const char *model, int32_t transform)
{
    (void)data; (void)o; (void)x; (void)y; (void)pw; (void)ph;
    (void)sub; (void)make; (void)model; (void)transform;
}
static void o_mode(void *data, struct wl_output *o, uint32_t flags,
                   int32_t w, int32_t h, int32_t refresh)
{
    (void)data; (void)o; (void)flags; (void)w; (void)h; (void)refresh;
}
static void o_done(void *data, struct wl_output *o) { (void)data; (void)o; }
static void o_scale(void *data, struct wl_output *o, int32_t s)
{
    (void)data; (void)o; (void)s;
}
static void o_name(void *data, struct wl_output *o, const char *name)
{
    struct hc_capture *c = data;
    for (int i = 0; i < c->nout; i++)
        if (c->out[i].out == o)
            snprintf(c->out[i].name, sizeof c->out[i].name, "%s", name);
}
static void o_description(void *data, struct wl_output *o, const char *d)
{
    (void)data; (void)o; (void)d;
}
static const struct wl_output_listener out_l = {
    .geometry = o_geometry, .mode = o_mode, .done = o_done,
    .scale = o_scale, .name = o_name, .description = o_description,
};

/* ------------------------------------------------------------- session --- */

static void s_buffer_size(void *data, struct ext_image_copy_capture_session_v1 *s,
                          uint32_t w, uint32_t h)
{
    struct hc_capture *c = data;
    (void)s;
    c->stage.width = w;
    c->stage.height = h;
    c->stage.have_size = true;
}

static void s_shm_format(void *data, struct ext_image_copy_capture_session_v1 *s,
                         uint32_t format)
{
    (void)data; (void)s; (void)format;   /* shm is never used: the CPU never touches a pixel */
}

static void s_dmabuf_device(void *data, struct ext_image_copy_capture_session_v1 *s,
                            struct wl_array *device)
{
    struct hc_capture *c = data;
    (void)s;
    if (device->size < sizeof(dev_t)) {
        fprintf(stderr, "hc_capture: dmabuf_device array is %zu bytes, expected %zu\n",
                device->size, sizeof(dev_t));
        return;
    }
    dev_t dv;
    memcpy(&dv, device->data, sizeof dv);
    c->stage.dev = dv;
    c->stage.have_dev = true;
}

static void s_dmabuf_format(void *data, struct ext_image_copy_capture_session_v1 *s,
                            uint32_t format, struct wl_array *modifiers)
{
    struct hc_capture *c = data;
    (void)s;
    if (c->stage.nfmt >= HC_MAX_FORMATS)
        return;
    struct hc_fmt_rec *f = &c->stage.fmt[c->stage.nfmt];
    f->fourcc = format;
    f->nmods = 0;

    size_t n = modifiers->size / sizeof(uint64_t);
    const uint64_t *m = modifiers->data;
    for (size_t i = 0; i < n && f->nmods < HC_MAX_MODS; i++)
        f->mods[f->nmods++] = m[i];
    c->stage.nfmt++;
}

/* Commit the staged round into c->caps. Called only from `done`. */
static void s_done(void *data, struct ext_image_copy_capture_session_v1 *s)
{
    struct hc_capture *c = data;
    (void)s;

    if (!c->stage.have_size || c->stage.nfmt == 0) {
        fprintf(stderr, "hc_capture: constraints round #%d incomplete "
                        "(size=%s formats=%d) -- ignoring\n",
                c->rounds + 1, c->stage.have_size ? "yes" : "NO", c->stage.nfmt);
        memset(&c->stage, 0, sizeof c->stage);
        c->rounds++;
        c->sync_flag = 1;
        return;
    }

    /* Prefer XRGB8888: DRM XR24 is byte-order BGRX, exactly what iHD imports
     * as VA_FOURCC_BGRX / VA_RT_FORMAT_RGB32 without swapping red and blue. */
    int pick = 0;
    for (int i = 0; i < c->stage.nfmt; i++) {
        if (c->stage.fmt[i].fourcc == DRM_FORMAT_XRGB8888) {
            pick = i;
            break;
        }
    }

    struct hc_capture_caps next;
    memset(&next, 0, sizeof next);
    next.width  = c->stage.width;
    next.height = c->stage.height;
    next.fourcc = c->stage.fmt[pick].fourcc;
    for (int i = 0; i < c->stage.fmt[pick].nmods; i++) {
        uint64_t m = c->stage.fmt[pick].mods[i];
        if (m == DRM_FORMAT_MOD_INVALID)     /* never allocate with INVALID */
            continue;
        if (next.nmods < HC_MAX_MODS)
            next.mods[next.nmods++] = m;
    }
    next.have_dmabuf_device = c->stage.have_dev;
    next.dmabuf_device      = c->stage.dev;

    bool changed = c->rounds > 0 && memcmp(&next, &c->caps, sizeof next) != 0;
    c->caps = next;
    if (changed)
        c->caps_dirty = true;

    memset(&c->stage, 0, sizeof c->stage);
    c->rounds++;
    c->sync_flag = 1;
}

static void s_stopped(void *data, struct ext_image_copy_capture_session_v1 *s)
{
    struct hc_capture *c = data;
    (void)s;
    c->stopped = true;
    c->sync_flag = 1;
    if (c->f_state == HC_FRAME_PENDING)
        c->f_state = HC_FRAME_FAILED;
    for (int i = 0; i < HC_MAX_BUFS; i++)
        if (c->infl[i].busy && c->infl[i].state == HC_FRAME_PENDING)
            c->infl[i].state = HC_FRAME_FAILED;
    fprintf(stderr, "hc_capture: session STOPPED by the compositor\n");
}

static const struct ext_image_copy_capture_session_v1_listener sess_l = {
    .buffer_size   = s_buffer_size,
    .shm_format    = s_shm_format,
    .dmabuf_device = s_dmabuf_device,
    .dmabuf_format = s_dmabuf_format,
    .done          = s_done,
    .stopped       = s_stopped,
};

/* --------------------------------------------------------------- frame --- */

static void fr_transform(void *data, struct ext_image_copy_capture_frame_v1 *f,
                         uint32_t transform)
{
    struct hc_inflight *fl = data;
    (void)f;
    fl->xform = transform;
    fl->c->f_xform = transform;
}
static void fr_damage(void *data, struct ext_image_copy_capture_frame_v1 *f,
                      int32_t x, int32_t y, int32_t w, int32_t h)
{
    struct hc_inflight *fl = data;
    (void)f; (void)x; (void)y; (void)w; (void)h;
    fl->ndamage++;
    fl->c->f_ndamage++;
}
static void fr_presentation_time(void *data, struct ext_image_copy_capture_frame_v1 *f,
                                 uint32_t tv_sec_hi, uint32_t tv_sec_lo, uint32_t tv_nsec)
{
    struct hc_inflight *fl = data;
    (void)f;
    fl->pres = (((uint64_t)tv_sec_hi << 32) | tv_sec_lo) * 1000000000ull + tv_nsec;
    fl->have_pres = true;
    fl->c->f_pres = fl->pres;
    fl->c->f_have_pres = true;
}
static void fr_ready(void *data, struct ext_image_copy_capture_frame_v1 *f)
{
    struct hc_inflight *fl = data;
    (void)f;
    fl->state = HC_FRAME_READY;
    fl->c->f_state = HC_FRAME_READY;
    fl->c->sync_flag = 1;
}
static void fr_failed(void *data, struct ext_image_copy_capture_frame_v1 *f, uint32_t reason)
{
    struct hc_inflight *fl = data;
    (void)f;
    fl->state = HC_FRAME_FAILED;
    fl->c->f_state = HC_FRAME_FAILED;
    fl->c->sync_flag = 1;
    fprintf(stderr, "hc_capture: frame FAILED, reason=%u%s\n", reason,
            reason == EXT_IMAGE_COPY_CAPTURE_FRAME_V1_FAILURE_REASON_BUFFER_CONSTRAINTS
                ? " (buffer_constraints -- rebuild the pool)" : "");
}
static const struct ext_image_copy_capture_frame_v1_listener frame_l = {
    .transform         = fr_transform,
    .damage            = fr_damage,
    .presentation_time = fr_presentation_time,
    .ready             = fr_ready,
    .failed            = fr_failed,
};

/* ------------------------------------------------------------ registry --- */

static void g_add(void *data, struct wl_registry *r, uint32_t name,
                  const char *iface, uint32_t ver)
{
    struct hc_capture *c = data;

    if (!strcmp(iface, "ext_output_image_capture_source_manager_v1")) {
        c->src_mgr = wl_registry_bind(r, name,
            &ext_output_image_capture_source_manager_v1_interface, 1);
    } else if (!strcmp(iface, "ext_image_copy_capture_manager_v1")) {
        c->cap_mgr = wl_registry_bind(r, name,
            &ext_image_copy_capture_manager_v1_interface, ver > 1 ? 1 : ver);
    } else if (!strcmp(iface, "zwp_linux_dmabuf_v1")) {
        /* CLAMP TO 4: Hyprland offers 5, and v4+ drops the deprecated
         * format/modifier events we do not want anyway. */
        c->dmabuf = wl_registry_bind(r, name,
            &zwp_linux_dmabuf_v1_interface, ver > 4 ? 4 : ver);
    } else if (!strcmp(iface, "wl_output")) {
        if (c->nout >= HC_MAX_OUTPUTS)
            return;
        if (ver < 4) {
            fprintf(stderr, "hc_capture: wl_output advertised at version %u; "
                            "version >= 4 is required for the name event\n", ver);
            return;
        }
        struct wl_output *o = wl_registry_bind(r, name, &wl_output_interface,
                                               ver > 4 ? 4 : ver);
        if (!o)
            return;
        c->out[c->nout].out = o;
        c->out[c->nout].global = name;
        c->out[c->nout].name[0] = '\0';
        c->nout++;
        wl_output_add_listener(o, &out_l, c);
    }
}

static void g_remove(void *data, struct wl_registry *r, uint32_t name)
{
    struct hc_capture *c = data;
    (void)r;
    if (c->output && name == c->output_global) {
        c->output_gone = true;
        fprintf(stderr, "hc_capture: our wl_output (global %u) was removed\n", name);
    }
}

static const struct wl_registry_listener reg_l = { g_add, g_remove };

/* ----------------------------------------------------------------- api --- */

struct hc_capture *hc_capture_open(const char *output_name, bool paint_cursors)
{
    struct hc_capture *c = calloc(1, sizeof *c);
    if (!c) {
        fprintf(stderr, "hc_capture_open: out of memory\n");
        return NULL;
    }

    c->dpy = wl_display_connect(NULL);
    if (!c->dpy) {
        fprintf(stderr, "hc_capture_open: wl_display_connect failed (WAYLAND_DISPLAY set?)\n");
        free(c);
        return NULL;
    }
    c->wl_fd = wl_display_get_fd(c->dpy);

    c->reg = wl_display_get_registry(c->dpy);
    if (!c->reg) {
        fprintf(stderr, "hc_capture_open: wl_display_get_registry failed\n");
        goto fail;
    }
    wl_registry_add_listener(c->reg, &reg_l, c);

    /* First roundtrip brings the globals, second the wl_output name events. */
    if (roundtrip(c, HC_ROUNDTRIP_MS) != HC_PUMP_OK ||
        roundtrip(c, HC_ROUNDTRIP_MS) != HC_PUMP_OK) {
        fprintf(stderr, "hc_capture_open: registry roundtrip failed or timed out\n");
        goto fail;
    }

    for (int i = 0; i < c->nout; i++) {
        if (!output_name || strstr(c->out[i].name, output_name)) {
            c->output = c->out[i].out;
            c->output_global = c->out[i].global;
            break;
        }
    }

    if (!c->src_mgr || !c->cap_mgr || !c->dmabuf || !c->output) {
        fprintf(stderr, "hc_capture_open: missing globals -- src_mgr=%p cap_mgr=%p "
                        "dmabuf=%p output=%p (%d output(s) seen, wanted '%s')\n",
                (void *)c->src_mgr, (void *)c->cap_mgr, (void *)c->dmabuf,
                (void *)c->output, c->nout, output_name ? output_name : "<first>");
        goto fail;
    }

    c->src = ext_output_image_capture_source_manager_v1_create_source(c->src_mgr, c->output);
    if (!c->src) {
        fprintf(stderr, "hc_capture_open: create_source failed\n");
        goto fail;
    }

    /* options is 0 or 1 ONLY -- anything else raises invalid_option and the
     * compositor kills the connection. */
    uint32_t options = paint_cursors ? 1u : 0u;
    c->sess = ext_image_copy_capture_manager_v1_create_session(c->cap_mgr, c->src, options);
    if (!c->sess) {
        fprintf(stderr, "hc_capture_open: create_session failed\n");
        goto fail;
    }
    ext_image_copy_capture_session_v1_add_listener(c->sess, &sess_l, c);

    /* Run the first constraints round to completion. buffer_size arrives LAST,
     * so we wait for `done`, not for any individual event. */
    c->sync_flag = 0;
    if (roundtrip(c, HC_ROUNDTRIP_MS) != HC_PUMP_OK)
        goto sess_timeout;
    if (!c->sync_flag && pump(c, &c->sync_flag, HC_ROUNDTRIP_MS) != HC_PUMP_OK)
        goto sess_timeout;

    if (c->stopped) {
        fprintf(stderr, "hc_capture_open: session stopped before any constraints arrived\n");
        goto fail;
    }
    if (c->rounds == 0 || c->caps.width == 0 || c->caps.height == 0) {
        fprintf(stderr, "hc_capture_open: no usable buffer constraints "
                        "(rounds=%d size=%ux%u)\n", c->rounds, c->caps.width, c->caps.height);
        goto fail;
    }
    if (c->caps.nmods == 0) {
        fprintf(stderr, "hc_capture_open: compositor advertised no usable modifiers "
                        "for the chosen format\n");
        goto fail;
    }
    c->caps_dirty = false;
    return c;

sess_timeout:
    fprintf(stderr, "hc_capture_open: TIMED OUT after %d ms waiting for buffer "
                    "constraints. If a Hyprland screencopy permission prompt is on "
                    "screen, answer it; this is not retried silently.\n", HC_ROUNDTRIP_MS);
fail:
    hc_capture_close(c);
    return NULL;
}

const struct hc_capture_caps *hc_capture_caps(struct hc_capture *c)
{
    return c ? &c->caps : NULL;
}

struct zwp_linux_dmabuf_v1 *hc_capture_dmabuf(struct hc_capture *c)
{
    return c ? c->dmabuf : NULL;
}

void hc_capture_set_pool(struct hc_capture *c, struct hc_pool *p)
{
    if (c)
        c->pool = p;
}

bool hc_capture_constraints_changed(struct hc_capture *c)
{
    if (!c || !c->caps_dirty)
        return false;
    c->caps_dirty = false;
    return true;
}

/*
 * Returns:
 *    0  frame ready, out->ok == true
 *   -1  frame failed or the session is unusable, out->ok == false
 *   -2  timed out with neither ready nor failed (the permission prompt hang)
 *   -3  Wayland connection error / bad arguments
 */
/* Find a free in-flight slot whose pool buffer is also free. */
static struct hc_inflight *infl_alloc(struct hc_capture *c, int *idx_out)
{
    for (int i = 0; i < c->pool->n && i < HC_MAX_BUFS; i++) {
        if (c->pool->buf[i].in_flight)
            continue;
        for (int k = 0; k < HC_MAX_BUFS; k++) {
            if (!c->infl[k].busy) {
                c->infl[k] = (struct hc_inflight){ .c = c, .buf_index = i,
                                                   .state = HC_FRAME_PENDING,
                                                   .busy = true };
                *idx_out = i;
                return &c->infl[k];
            }
        }
        return NULL;
    }
    return NULL;
}

int hc_capture_submit(struct hc_capture *c, struct hc_inflight **token)
{
    if (!c || !token)
        return -3;
    *token = NULL;

    if (!c->pool || c->pool->n <= 0) {
        fprintf(stderr, "hc_capture_submit: no pool bound (call hc_capture_set_pool)\n");
        return -3;
    }
    if (c->stopped) {
        fprintf(stderr, "hc_capture_submit: session has stopped\n");
        return -1;
    }

    /*
     * CRASHES THE COMPOSITOR IF OMITTED. Verified on Hyprland 0.55.4:
     *
     *   #5 Screenshare::CScreenshareFrame::transform() const
     *   #6 CImageCopyCaptureFrame::CImageCopyCaptureFrame(...)
     *   #12 libwayland-server  <- dispatching our create_frame
     *
     * CScreenshareFrame::transform() does
     *     case SHARE_MONITOR: return m_session->monitor()->m_transform;
     * with no null check (reference/hyprland/hypr_ScreenshareFrame.cpp:487-494),
     * and the frame constructor reaches it via nextFrame() at
     * hypr_ImageCopyCapture.cpp:355. Once the captured output is gone,
     * monitor() is null and create_frame aborts the whole compositor -- taking
     * every client with it. Reproduced 2026-08-02 16:09:30 (14.6 MB coredump).
     *
     * That is a Hyprland bug: a compositor must never abort on client input.
     * But we must not be the client that trips it.
     */
    if (c->output_gone) {
        fprintf(stderr, "hc_capture_submit: captured wl_output is gone; refusing "
                        "to create a frame (it would abort the compositor)\n");
        c->stopped = true;
        return -1;
    }

    int idx = -1;
    struct hc_inflight *fl = infl_alloc(c, &idx);
    if (!fl)
        return -4;                      /* all buffers outstanding */

    struct hc_buf *b = &c->pool->buf[idx];
    if (!b->wl) {
        fl->busy = false;
        fprintf(stderr, "hc_capture_submit: pool buffer %d has no wl_buffer\n", idx);
        return -3;
    }

    fl->fr = ext_image_copy_capture_session_v1_create_frame(c->sess);
    if (!fl->fr) {
        fl->busy = false;
        fprintf(stderr, "hc_capture_submit: create_frame failed\n");
        return -3;
    }

    b->in_flight = true;

    ext_image_copy_capture_frame_v1_add_listener(fl->fr, &frame_l, fl);
    ext_image_copy_capture_frame_v1_attach_buffer(fl->fr, b->wl);
    ext_image_copy_capture_frame_v1_damage_buffer(fl->fr, 0, 0,
                                                  (int32_t)c->pool->width,
                                                  (int32_t)c->pool->height);
    ext_image_copy_capture_frame_v1_capture(fl->fr);
    if (wl_display_flush(c->dpy) < 0 && errno != EAGAIN) {
        ext_image_copy_capture_frame_v1_destroy(fl->fr);
        b->in_flight = false;
        fl->busy = false;
        fprintf(stderr, "hc_capture_submit: wl_display_flush failed: %s\n", strerror(errno));
        return -3;
    }

    *token = fl;
    return 0;
}

int hc_capture_wait(struct hc_capture *c, struct hc_inflight *fl,
                    struct hc_frame *out, int timeout_ms)
{
    if (!c || !fl || !out || !fl->busy)
        return -3;

    memset(out, 0, sizeof *out);
    out->buf_index = fl->buf_index;
    out->ok        = false;

    /*
     * pump() waits on c->sync_flag, which the frame listeners raise. With more
     * than one frame outstanding a wake may belong to a sibling, so re-arm and
     * keep pumping until THIS record resolves or the deadline passes.
     */
    int rc = HC_PUMP_OK;
    uint64_t deadline = timeout_ms < 0 ? 0
                      : hc_now_ns() + (uint64_t)timeout_ms * 1000000ull;
    while (fl->state == HC_FRAME_PENDING) {
        int slice = -1;
        if (timeout_ms >= 0) {
            uint64_t now = hc_now_ns();
            if (now >= deadline) { rc = HC_PUMP_TIMEOUT; break; }
            slice = (int)((deadline - now) / 1000000ull) + 1;
        }
        c->sync_flag = 0;
        rc = pump(c, &c->sync_flag, slice);
        if (rc == HC_PUMP_ERROR)
            break;
        if (rc == HC_PUMP_TIMEOUT && fl->state == HC_FRAME_PENDING)
            break;
    }

    struct hc_buf *b = &c->pool->buf[fl->buf_index];

    ext_image_copy_capture_frame_v1_destroy(fl->fr);
    fl->fr = NULL;

    out->presentation_ns   = fl->pres;
    out->have_presentation = fl->have_pres;
    out->transform         = fl->xform;
    out->damage_rects      = fl->ndamage;

    if (rc == HC_PUMP_ERROR) {
        b->in_flight = false;
        fl->busy = false;
        int err = wl_display_get_error(c->dpy);
        fprintf(stderr, "hc_capture_wait: wayland connection error (%d: %s)\n",
                err, strerror(err ? err : errno));
        return -3;
    }
    if (fl->state == HC_FRAME_PENDING) {
        /* Do NOT clear in_flight: the frame object is gone but the compositor
         * never said whether it wrote to the buffer. Reusing it would race. */
        fl->busy = false;
        fprintf(stderr, "hc_capture_wait: TIMED OUT after %d ms with neither ready "
                        "nor failed. This is the Hyprland screencopy permission prompt "
                        "hanging -- answer it. Buffer %d is quarantined; not retrying.\n",
                timeout_ms, fl->buf_index);
        return -2;
    }

    b->in_flight = false;
    int state = fl->state;
    fl->busy = false;

    if (state == HC_FRAME_READY) {
        out->ok = true;
        return 0;
    }
    return -1;
}

int hc_capture_frame(struct hc_capture *c, struct hc_frame *out, int timeout_ms)
{
    struct hc_inflight *tok = NULL;
    int rc = hc_capture_submit(c, &tok);
    if (rc != 0)
        return rc;
    return hc_capture_wait(c, tok, out, timeout_ms);
}

void hc_capture_close(struct hc_capture *c)
{
    if (!c)
        return;

    if (c->sess)
        ext_image_copy_capture_session_v1_destroy(c->sess);
    if (c->src)
        ext_image_capture_source_v1_destroy(c->src);
    if (c->cap_mgr)
        ext_image_copy_capture_manager_v1_destroy(c->cap_mgr);
    if (c->src_mgr)
        ext_output_image_capture_source_manager_v1_destroy(c->src_mgr);
    if (c->dmabuf)
        zwp_linux_dmabuf_v1_destroy(c->dmabuf);
    for (int i = 0; i < c->nout; i++)
        if (c->out[i].out)
            wl_output_release(c->out[i].out);
    if (c->reg)
        wl_registry_destroy(c->reg);
    if (c->dpy) {
        wl_display_flush(c->dpy);
        wl_display_disconnect(c->dpy);
    }
    free(c);
}
