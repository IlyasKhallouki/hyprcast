/*
 * hyprcast-engine -- the media leg.
 *
 * The Python control plane owns P2P, RTSP and the sink negotiation. This
 * process owns exactly one thing: pixels and PCM from this machine to
 * dst_ip:dst_port, at a constant frame rate, with the CPU never touching a
 * pixel. It idles until the control plane says "start" and is driven entirely
 * over newline-delimited JSON on an inherited fd (3 by default).
 *
 *   capture (ext-image-copy-capture, gbm dmabuf)
 *     -> VA import as BGRX          (zero copy, same bo)
 *     -> VAAPI VPP BGRX -> NV12     (scales capture size to the frozen wire size)
 *     -> h264_vaapi on that surface (zero copy)
 *     -> MPEG-TS over RTP
 *
 * Two structural decisions deserve their reasons up front.
 *
 * 1. CFR pacing, and why capture runs on its own thread.
 *
 *    A WFD sink needs a constant frame rate; ext-image-copy-capture is purely
 *    damage-driven and produces NOTHING while the screen is still. So the
 *    encoder is driven by a 90 kHz grid we own, not by frame arrivals: on every
 *    grid slot we encode either the frame that just arrived or, if none did,
 *    the last one again. A repeat is a near-empty all-skip P frame under VBR.
 *
 *    Consuming a frame means blocking in hc_capture_wait(), and its timeout is
 *    NOT a poll: on expiry it destroys the frame object and quarantines the
 *    buffer. Waiting on it from the pacer would therefore burn a pool buffer
 *    every idle grid slot. So capture lives on its own thread, blocking as long
 *    as it likes, and hands finished buffers to the pacer through a one-slot
 *    mailbox. The pacer never blocks on the compositor.
 *
 *    That thread also carries the staleness watchdog. Hyprland stops
 *    considering a session "sharing" 500 ms after the last copy
 *    (m_shareStopTimer, ScreenshareSession.cpp:72-77), and the next create_frame
 *    then arrives with m_isFirst = true, which forces
 *    scheduleFrameForMonitor(NEEDS_FRAME) + damageMonitor()
 *    (ScreenshareFrame.cpp:121-125). Re-creating the frame object after ~550 ms
 *    of silence therefore FORCES a fresh capture instead of waiting on damage
 *    that may never come. A timeout there is normal operation, not an error --
 *    but a timeout before the very first frame of a session is the screencopy
 *    permission prompt hanging, and that is surfaced, never retried silently.
 *
 * 2. Retuning without renegotiating.
 *
 *    The wire size is frozen at "start", because it is what the sink agreed to
 *    in M3. Everything else is live: fps re-times the grid only (PTS comes from
 *    the grid, so nothing downstream notices); bitrate/qp rebuilds the encoder
 *    and nothing else -- the mux, the socket and the RTP sequence space survive
 *    -- and the next frame is forced to IDR so the fresh SPS/PPS reach the sink
 *    in band; "output" rebuilds capture and the pool against a different
 *    wl_output while VPP keeps scaling to the same frozen wire size; "audio"
 *    reopens the audio leg against another pulse source, and because the AAC
 *    parameters and PID 0x1100 are unchanged the sink cannot tell.
 */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include <unistd.h>
#include <sys/resource.h>
#include <sys/types.h>

#include <drm_fourcc.h>
#include <wayland-client.h>

/*
 * hc.h names struct zwp_linux_dmabuf_v1 only inside a prototype's parameter
 * list, so the tag must already exist at file scope for the pointer we hand
 * from hc_capture_dmabuf() to hc_pool_create() to have a compatible type.
 */
#include "linux-dmabuf-v1-client-protocol.h"

#include "hc.h"

#define HC_DRM_NODE        "/dev/dri/renderD128"
#define HC_POOL_BUFS       HC_MAX_BUFS   /* hold + consumed + outstanding + quarantine */
#define HC_WATCHDOG_MS     550           /* > Hyprland's 500 ms m_shareStopTimer */
#define HC_FIRST_FRAME_MS  4000          /* a start that never captures is fatal */
#define HC_IDLE_POLL_MS    200           /* control-fd wait while no session runs */
#define HC_STATS_NS        1000000000ull
#define HC_MAX_LATE_SLOTS  4             /* beyond this the grid resynchronises */
#define HC_QUARANTINE_NS   1000000000ull /* forced reclaim of a parked buffer */
#define HC_PTS_RING        32
#define HC_AUDIO_PER_TICK  32
#define HC_AUDIO_BITRATE   128000u       /* what the sink accepted as AAC 00000001 */
#define HC_GAIN_MAX        4.0f          /* hc_audio_set_volume clamps here too */
#define HC_EXTRADATA_MAX   256

#define HC_LOG(...)  do {                     \
        fprintf(stderr, "[eng] ");            \
        fprintf(stderr, __VA_ARGS__);         \
        fputc('\n', stderr);                  \
    } while (0)
#define HC_ERR(...)  do {                     \
        fprintf(stderr, "[eng] ERROR: ");     \
        fprintf(stderr, __VA_ARGS__);         \
        fputc('\n', stderr);                  \
    } while (0)

/*
 * The only mutable global. Wayland dispatch and clock_nanosleep are both
 * blocking, so a signal flag is the sole way to unwind; POSIX gives us no
 * other legal channel out of a handler.
 */
static volatile sig_atomic_t g_quit;

static void on_signal(int sig)
{
    (void)sig;
    g_quit = 1;
}

static void sleep_until_ns(uint64_t when)
{
    struct timespec ts;
    ts.tv_sec  = (time_t)(when / 1000000000ull);
    ts.tv_nsec = (long)(when % 1000000000ull);
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR)
        ;
}

static void sleep_ms(unsigned ms)
{
    struct timespec ts = { (time_t)(ms / 1000u), (long)(ms % 1000u) * 1000000L };
    nanosleep(&ts, NULL);
}

/* ------------------------------------------------------------ capture thread */

/*
 * Ownership rules, which is all that makes this safe without a lock around the
 * pool itself:
 *
 *   - hc_buf.in_flight is written ONLY by the capture thread. The pacer asks
 *     for a buffer to be freed by pushing its index onto `release`, and the
 *     capture thread applies that at the top of its loop, before it can hand
 *     the buffer to the compositor again.
 *   - A buffer published in the mailbox is already marked in_flight, so the
 *     compositor cannot pick it while VPP is sampling it.
 *   - Everything else about the capture session (the wl_display, the frame
 *     objects, the quarantine) stays on the capture thread. The pacer only
 *     joins it, and only from the rebuild paths.
 */
struct capthread {
    struct hc_capture *cap;
    struct hc_pool    *pool;

    pthread_t       th;
    pthread_mutex_t mu;
    bool            started;

    bool     quit;

    bool     have_frame;          /* mailbox: newest ready buffer */
    int      frame_idx;
    uint64_t frame_pres_ns;
    bool     frame_have_pres;

    int      release[HC_MAX_BUFS];
    int      n_release;

    bool     stopped;             /* thread has left its loop */
    bool     need_rebuild;        /* compositor re-sent buffer constraints */
    bool     fatal;
    char     err[192];

    uint64_t n_ok, n_failed, n_watchdog, n_stale;
};

static void cap_fatal(struct capthread *ct, const char *fmt, ...)
{
    va_list ap;
    char msg[sizeof ct->err];

    va_start(ap, fmt);
    vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);

    pthread_mutex_lock(&ct->mu);
    if (!ct->fatal) {
        ct->fatal = true;
        snprintf(ct->err, sizeof ct->err, "%s", msg);
    }
    pthread_mutex_unlock(&ct->mu);
    HC_ERR("capture: %s", msg);
}

static void *cap_loop(void *arg)
{
    struct capthread *ct = arg;
    int      quarantine[HC_MAX_BUFS];
    uint64_t quar_ns[HC_MAX_BUFS];
    int      nquar = 0;
    unsigned consec_timeouts = 0;

    for (;;) {
        struct hc_inflight *tok = NULL;
        struct hc_frame fr;
        int rc, stale = -1;

        pthread_mutex_lock(&ct->mu);
        if (ct->quit) {
            pthread_mutex_unlock(&ct->mu);
            break;
        }
        for (int i = 0; i < ct->n_release; i++) {
            int idx = ct->release[i];
            if (idx >= 0 && idx < ct->pool->n)
                ct->pool->buf[idx].in_flight = false;
        }
        ct->n_release = 0;
        pthread_mutex_unlock(&ct->mu);

        rc = hc_capture_submit(ct->cap, &tok);
        if (rc == -4) {
            /*
             * No buffer free: the pacer holds one and the rest are quarantined.
             * The pacer's hold clears on its own, but the quarantine only
             * clears when a capture completes -- and none can, because there is
             * nothing to submit. Break that cycle by hand: a buffer whose frame
             * object was destroyed a whole second ago cannot still be written,
             * because the destroy was flushed before we ever stopped waiting.
             */
            uint64_t now = hc_now_ns();
            for (int i = 0; i < nquar; ) {
                if (now - quar_ns[i] < HC_QUARANTINE_NS) {
                    i++;
                    continue;
                }
                ct->pool->buf[quarantine[i]].in_flight = false;
                quarantine[i] = quarantine[nquar - 1];
                quar_ns[i]    = quar_ns[nquar - 1];
                nquar--;
            }
            sleep_ms(2);
            continue;
        }
        if (rc != 0) {
            cap_fatal(ct, "hc_capture_submit failed (%d)", rc);
            break;
        }

        memset(&fr, 0, sizeof fr);
        rc = hc_capture_wait(ct->cap, tok, &fr, HC_WATCHDOG_MS);

        if (rc == -2) {
            /*
             * Watchdog. hc_capture_wait already destroyed the frame object and
             * left the buffer reserved, because the compositor never said
             * whether it wrote to it. Park it until a later capture resolves:
             * Wayland requests are processed in order, so a `ready` for a frame
             * created after the destroy proves the destroy was handled and the
             * buffer can no longer be written.
             */
            ct->n_watchdog++;
            consec_timeouts++;
            if (fr.buf_index >= 0 && fr.buf_index < ct->pool->n &&
                nquar < HC_MAX_BUFS) {
                quarantine[nquar] = fr.buf_index;
                quar_ns[nquar]    = hc_now_ns();
                nquar++;
            }

            if (ct->n_ok == 0) {
                cap_fatal(ct, "no frame in %u ms and none ever captured -- "
                              "this is the Hyprland screencopy permission "
                              "prompt; answer it",
                          (unsigned)HC_WATCHDOG_MS * consec_timeouts);
                break;
            }
            if (nquar >= ct->pool->n) {
                cap_fatal(ct, "every pool buffer is quarantined; the "
                              "compositor has stopped responding");
                break;
            }
            continue;
        }

        /* The compositor answered, so anything parked is now safe to reuse. */
        for (int i = 0; i < nquar; i++)
            ct->pool->buf[quarantine[i]].in_flight = false;
        nquar = 0;
        consec_timeouts = 0;

        if (rc == 0 && fr.ok && fr.buf_index >= 0 && fr.buf_index < ct->pool->n) {
            /* Reserve before publishing: the pacer must never race the
             * compositor for the buffer it is about to read. */
            ct->pool->buf[fr.buf_index].in_flight = true;

            pthread_mutex_lock(&ct->mu);
            if (ct->have_frame) {
                /* The pacer has not consumed the previous one -- it is a whole
                 * grid slot old, so drop it rather than delay the fresh one. */
                stale = ct->frame_idx;
                ct->n_stale++;
            }
            ct->have_frame      = true;
            ct->frame_idx       = fr.buf_index;
            ct->frame_pres_ns   = fr.presentation_ns;
            ct->frame_have_pres = fr.have_presentation;
            ct->n_ok++;
            pthread_mutex_unlock(&ct->mu);

            if (stale >= 0)
                ct->pool->buf[stale].in_flight = false;
        } else {
            pthread_mutex_lock(&ct->mu);
            ct->n_failed++;
            pthread_mutex_unlock(&ct->mu);
            if (rc == -3) {
                cap_fatal(ct, "wayland connection error");
                break;
            }
        }

        if (hc_capture_constraints_changed(ct->cap)) {
            pthread_mutex_lock(&ct->mu);
            ct->need_rebuild = true;
            pthread_mutex_unlock(&ct->mu);
            HC_LOG("capture: constraints changed -- pool rebuild requested");
            break;
        }
    }

    pthread_mutex_lock(&ct->mu);
    ct->stopped = true;
    pthread_mutex_unlock(&ct->mu);
    return NULL;
}

static int cap_start(struct capthread *ct, struct hc_capture *cap,
                     struct hc_pool *pool)
{
    memset(ct, 0, sizeof *ct);
    ct->cap  = cap;
    ct->pool = pool;
    ct->frame_idx = -1;

    if (pthread_mutex_init(&ct->mu, NULL) != 0) {
        HC_ERR("pthread_mutex_init failed");
        return -1;
    }
    if (pthread_create(&ct->th, NULL, cap_loop, ct) != 0) {
        HC_ERR("pthread_create(capture) failed: %s", strerror(errno));
        pthread_mutex_destroy(&ct->mu);
        return -1;
    }
    ct->started = true;
    return 0;
}

static void cap_join(struct capthread *ct)
{
    if (!ct->started)
        return;
    pthread_mutex_lock(&ct->mu);
    ct->quit = true;
    pthread_mutex_unlock(&ct->mu);
    pthread_join(ct->th, NULL);
    pthread_mutex_destroy(&ct->mu);
    ct->started = false;
}

/* Take the newest ready buffer, if any. The caller owns it until cap_put(). */
static bool cap_take(struct capthread *ct, int *idx, uint64_t *pres,
                     bool *have_pres)
{
    bool got;

    pthread_mutex_lock(&ct->mu);
    got = ct->have_frame;
    if (got) {
        *idx       = ct->frame_idx;
        *pres      = ct->frame_pres_ns;
        *have_pres = ct->frame_have_pres;
        ct->have_frame = false;
    }
    pthread_mutex_unlock(&ct->mu);
    return got;
}

static void cap_put(struct capthread *ct, int idx)
{
    if (idx < 0)
        return;
    pthread_mutex_lock(&ct->mu);
    if (ct->n_release < HC_MAX_BUFS)
        ct->release[ct->n_release++] = idx;
    pthread_mutex_unlock(&ct->mu);
}

struct cap_status {
    bool     stopped, need_rebuild, fatal;
    char     err[192];
    uint64_t n_ok, n_failed, n_watchdog, n_stale;
};

static void cap_status(struct capthread *ct, struct cap_status *s)
{
    memset(s, 0, sizeof *s);
    if (!ct->started)
        return;
    pthread_mutex_lock(&ct->mu);
    s->stopped      = ct->stopped;
    s->need_rebuild = ct->need_rebuild;
    s->fatal        = ct->fatal;
    memcpy(s->err, ct->err, sizeof s->err);
    s->n_ok        = ct->n_ok;
    s->n_failed    = ct->n_failed;
    s->n_watchdog  = ct->n_watchdog;
    s->n_stale     = ct->n_stale;
    pthread_mutex_unlock(&ct->mu);
}

/* ------------------------------------------------------------------ engine */

struct engine {
    struct hc_ctl *ctl;
    int            ctl_fd;

    /* media leg */
    bool               running;
    int                drm_fd;
    int                gbm_fd;      /* the pool's own render node; see build_pool */
    struct hc_capture *cap;
    struct hc_pool     pool;
    bool               pool_live;
    struct hc_va      *va;
    struct hc_enc     *enc;
    struct hc_mux     *mux;
    struct hc_audio   *audio;
    struct capthread   ct;

    /* Live audio state, kept HERE and not in hc_audio: a fresh audio leg opens
     * at unity and unmuted, so `ctl sink NAME` would otherwise undo a volume
     * the user set ten seconds earlier. */
    float    gain;
    bool     muted;
    uint64_t last_audio_pts;     /* last access unit handed to the muxer */

    /* An audio source change in flight. See cmd_audio(). */
    bool         aud_switching;
    pthread_t    aud_open_tid;
    _Atomic bool aud_pending_done;
    struct hc_audio *aud_pending;
    char         aud_pending_dev[128];

    /* frozen session parameters */
    char     output[64];
    char     audio_dev[128];
    char     dst_ip[64];
    int      dst_port, src_port;
    bool     cursors;
    struct hc_enc_cfg ecfg;      /* .fps is the encoder's view; grid owns pacing */

    /* pacer */
    uint64_t interval_ns;
    uint64_t next_tick_ns;
    uint64_t pts_ns;
    uint32_t since_idr;
    bool     force_idr;
    int      hold_idx;           /* the buffer a repeat frame re-converts */

    /*
     * Pending capture, built off the main loop so the CFR pacer keeps feeding
     * the muxer while a new output is opened. See cmd_output().
     */
    pthread_t          cap_open_tid;
    bool               cap_switching;
    _Atomic bool       cap_pending_done;
    char               cap_pending_name[64];
    struct hc_capture *cap_pending;
    struct hc_pool     pool_pending;
    bool               pool_pending_live;
    int                gbm_fd_pending;
    int                cap_pending_rc;
    bool     ready_sent;

    uint64_t pts_ring[HC_PTS_RING];
    unsigned pts_head, pts_tail;

    /* stats window */
    uint64_t stat_ns;
    uint64_t stat_frames, stat_repeats;
    uint64_t stat_bytes;
    struct rusage stat_ru;
    uint64_t tot_idr, tot_drops;
};

/* --------------------------------------------------------------- events */

static void json_escape(const char *s, char *out, size_t cap)
{
    size_t n = 0;

    if (cap == 0)
        return;
    for (; s && *s && n + 7 < cap; s++) {
        unsigned char ch = (unsigned char)*s;
        if (ch == '"' || ch == '\\') {
            out[n++] = '\\';
            out[n++] = (char)ch;
        } else if (ch < 0x20) {
            n += (size_t)snprintf(out + n, cap - n, "\\u%04x", ch);
        } else {
            out[n++] = (char)ch;
        }
    }
    out[n] = '\0';
}

static void ev_raw(struct engine *e, const char *json)
{
    if (!e->ctl)
        return;
    if (hc_ctl_event(e->ctl, json) != 0)
        HC_ERR("could not send event: %s", json);
}

static void ev_error(struct engine *e, const char *fmt, ...)
    __attribute__((format(printf, 2, 3)));

static void ev_error(struct engine *e, const char *fmt, ...)
{
    char msg[384], esc[768], line[832];
    va_list ap;

    va_start(ap, fmt);
    vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);

    HC_ERR("%s", msg);
    json_escape(msg, esc, sizeof esc);
    snprintf(line, sizeof line, "{\"ev\":\"error\",\"msg\":\"%s\"}", esc);
    ev_raw(e, line);
}

/* ------------------------------------------------------------ annex-b SPS/PPS */

/*
 * Collect the SPS and PPS NALs out of one Annex-B access unit, start codes
 * included, so the muxer gets the same extradata libavcodec would have handed
 * us with AV_CODEC_FLAG_GLOBAL_HEADER. We deliberately do NOT set that flag --
 * the sink needs the parameter sets in band before every IDR -- so this is
 * where they come from.
 */
static int extract_params(const uint8_t *d, int n, uint8_t *out, int cap,
                          int *out_len)
{
    int i = 0, len = 0;
    bool have_sps = false, have_pps = false;

    *out_len = 0;
    while (i + 3 < n) {
        int sc, start, end, type;

        if (d[i] == 0 && d[i + 1] == 0 && d[i + 2] == 1)
            sc = 3;
        else if (i + 4 < n && d[i] == 0 && d[i + 1] == 0 && d[i + 2] == 0 &&
                 d[i + 3] == 1)
            sc = 4;
        else {
            i++;
            continue;
        }

        start = i + sc;
        if (start >= n)
            break;
        type = d[start] & 0x1f;

        /* Find the next start code, which bounds this NAL. */
        end = n;
        for (int k = start + 1; k + 2 < n; k++) {
            if (d[k] == 0 && d[k + 1] == 0 && (d[k + 2] == 1 ||
                (k + 3 < n && d[k + 2] == 0 && d[k + 3] == 1))) {
                end = k;
                break;
            }
        }

        if (type == 7 || type == 8) {
            int nal = end - start;
            if (len + 4 + nal > cap)
                return -1;
            out[len++] = 0; out[len++] = 0; out[len++] = 0; out[len++] = 1;
            memcpy(out + len, d + start, (size_t)nal);
            len += nal;
            if (type == 7) have_sps = true;
            else           have_pps = true;
        }
        i = end;
    }

    if (!have_sps || !have_pps)
        return -1;
    *out_len = len;
    return 0;
}

/* ------------------------------------------------------------------- pool */

/*
 * Pin the pool to I915_FORMAT_MOD_Y_TILED. gbm_bo_create_with_modifiers2() fed
 * Hyprland's full advertised list picks Y_TILED_CCS, which iHD then refuses to
 * import ("resource allocation failed"), so the list is pinned rather than
 * merely filtered.
 */
static int pick_mods(const struct hc_capture_caps *caps, uint64_t *mods, int max)
{
    static const uint64_t MOD_CCS   = 0x0100000000000004ull;
    static const uint64_t MOD_CCS_F = 0x0100000000000005ull;
    int n = 0;

    for (int i = 0; i < caps->nmods && n < max; i++)
        if (caps->mods[i] == I915_FORMAT_MOD_Y_TILED)
            mods[n++] = I915_FORMAT_MOD_Y_TILED;
    if (n > 0)
        return n;

    HC_ERR("Y_TILED is not advertised; falling back to the advertised list "
           "minus INVALID and CCS");
    for (int i = 0; i < caps->nmods && n < max; i++) {
        uint64_t m = caps->mods[i];
        if (m == DRM_FORMAT_MOD_INVALID || m == MOD_CCS || m == MOD_CCS_F)
            continue;
        mods[n++] = m;
    }
    return n > 0 ? n : -1;
}

static int build_pool_into(struct engine *e, struct hc_capture *cap,
                           struct hc_pool *pool, bool *pool_live, int *gbm_fd)
{
    const struct hc_capture_caps *caps = hc_capture_caps(cap);
    uint64_t mods[HC_MAX_MODS];
    int nmods;
    char fb[5];

    if (!caps || caps->nmods <= 0) {
        ev_error(e, "no dmabuf constraints from the compositor");
        return -1;
    }
    if (caps->fourcc != DRM_FORMAT_XRGB8888) {
        ev_error(e, "compositor offered '%s', not XR24",
                 hc_fourcc_str(caps->fourcc, fb));
        return -1;
    }

    nmods = pick_mods(caps, mods, HC_MAX_MODS);
    if (nmods < 0) {
        ev_error(e, "no usable dmabuf modifier survived filtering");
        return -1;
    }

    /*
     * The pool gets its own open of the render node, not the fd the VADisplay
     * was created from. MEASURED: reusing one fd across a pool rebuild --
     * gbm_device_destroy() and then gbm_create_device() on that same fd, while
     * a VADisplay built on it is still live -- makes the very next
     * gbm_bo_get_fd_for_plane() fail, which surfaces as
     * "hc_va_import_pool: buffer 0 of 4 failed" on the FIRST output switch.
     * A fresh fd per pool costs one file descriptor and makes the rebuild
     * reliable. hc_pool_destroy() deliberately leaves the fd open, so
     * drop_capture() closes it.
     */
    *gbm_fd = open(HC_DRM_NODE, O_RDWR | O_CLOEXEC);
    if (*gbm_fd < 0) {
        ev_error(e, "open %s: %s", HC_DRM_NODE, strerror(errno));
        return -1;
    }
    if (hc_pool_create(pool, *gbm_fd, hc_capture_dmabuf(cap),
                       caps->width, caps->height, caps->fourcc,
                       mods, nmods, HC_POOL_BUFS) != 0) {
        ev_error(e, "hc_pool_create %ux%u failed", caps->width, caps->height);
        close(*gbm_fd);
        *gbm_fd = -1;
        return -1;
    }
    *pool_live = true;
    hc_capture_set_pool(cap, pool);

    if (hc_va_import_pool(e->va, pool) != 0) {
        ev_error(e, "hc_va_import_pool failed");
        hc_pool_destroy(pool);
        *pool_live = false;
        close(*gbm_fd);
        *gbm_fd = -1;
        return -1;
    }

    HC_LOG("capture %ux%u -> wire %ux%u, %d buffer(s) modifier %s",
           pool->width, pool->height, e->ecfg.width, e->ecfg.height,
           pool->n, hc_mod_str(pool->buf[0].modifier));
    return 0;
}

static int build_pool(struct engine *e)
{
    return build_pool_into(e, e->cap, &e->pool, &e->pool_live, &e->gbm_fd);
}

/* Tear down capture + pool only. The encoder, mux and audio keep running. */
static void drop_capture(struct engine *e)
{
    cap_join(&e->ct);
    e->hold_idx = -1;
    if (e->pool_live) {
        hc_pool_destroy(&e->pool);
        e->pool_live = false;
    }
    if (e->gbm_fd >= 0) {
        close(e->gbm_fd);
        e->gbm_fd = -1;
    }
    if (e->cap) {
        hc_capture_close(e->cap);
        e->cap = NULL;
    }
}

/*
 * (Re)build capture + pool against `output`. The wire size is frozen, so VPP
 * absorbs any change in capture geometry and the encoder never notices.
 */
static int make_capture(struct engine *e, const char *output)
{
    e->cap = hc_capture_open(output && output[0] ? output : NULL, e->cursors);
    if (!e->cap) {
        ev_error(e, "hc_capture_open('%s') failed",
                 output && output[0] ? output : "<first output>");
        return -1;
    }
    if (build_pool(e) != 0) {
        hc_capture_close(e->cap);
        e->cap = NULL;
        return -1;
    }
    if (cap_start(&e->ct, e->cap, &e->pool) != 0) {
        ev_error(e, "could not start the capture thread");
        hc_pool_destroy(&e->pool);
        e->pool_live = false;
        hc_capture_close(e->cap);
        e->cap = NULL;
        return -1;
    }
    return 0;
}

/* ---------------------------------------------------------------- session */

static void session_stop(struct engine *e)
{
    if (!e->running && !e->cap && !e->enc && !e->mux && !e->audio &&
        e->drm_fd < 0)
        return;

    HC_LOG("session stopping");
    drop_capture(e);

    /* An output switch may still be opening a capture. Join it first, then
     * discard whatever it produced: the session is going away regardless, and
     * leaving the thread running would let it touch freed state. */
    if (e->cap_switching) {
        pthread_join(e->cap_open_tid, NULL);
        e->cap_switching = false;
        if (e->cap_pending_rc == 0 && e->cap_pending) {
            if (e->pool_pending_live) {
                hc_pool_destroy(&e->pool_pending);
                e->pool_pending_live = false;
            }
            if (e->gbm_fd_pending >= 0) {
                close(e->gbm_fd_pending);
                e->gbm_fd_pending = -1;
            }
            hc_capture_close(e->cap_pending);
            e->cap_pending = NULL;
        }
    }

    /* An audio source change may still be opening a device. Join it before
     * anything it could be swapped into is torn down. */
    if (e->aud_switching) {
        pthread_join(e->aud_open_tid, NULL);
        e->aud_switching = false;
        if (e->aud_pending) {
            hc_audio_close(e->aud_pending);
            e->aud_pending = NULL;
        }
    }

    if (e->audio) {
        hc_audio_close(e->audio);
        e->audio = NULL;
    }
    if (e->mux) {
        hc_mux_close(e->mux);
        e->mux = NULL;
    }
    if (e->enc) {
        hc_enc_close(e->enc);
        e->enc = NULL;
    }
    if (e->va) {
        hc_va_close(e->va);
        e->va = NULL;
    }
    if (e->drm_fd >= 0) {
        close(e->drm_fd);
        e->drm_fd = -1;
    }

    e->running    = false;
    e->ready_sent = false;
    e->hold_idx   = -1;
    e->pts_head = e->pts_tail = 0;
}

static void pts_push(struct engine *e, uint64_t pts)
{
    e->pts_ring[e->pts_head % HC_PTS_RING] = pts;
    e->pts_head++;
    if (e->pts_head - e->pts_tail > HC_PTS_RING)
        e->pts_tail = e->pts_head - HC_PTS_RING;
}

static uint64_t pts_pop(struct engine *e, uint64_t fallback)
{
    uint64_t v;
    if (e->pts_head == e->pts_tail)
        return fallback;
    v = e->pts_ring[e->pts_tail % HC_PTS_RING];
    e->pts_tail++;
    return v;
}

/*
 * Drain the encoder and push every access unit at the muxer.
 *
 * The muxer is opened lazily, on the first packet, because hc_mux_open() wants
 * the SPS/PPS and the only place they exist is the bitstream itself. That also
 * makes "ready" mean what the contract says: the first frame is encoded AND
 * sent.
 */
static int drain_encoder(struct engine *e)
{
    for (;;) {
        const uint8_t *data = NULL;
        int size = 0;
        bool is_idr = false;
        uint64_t pts;
        int r = hc_enc_receive(e->enc, &data, &size, &is_idr);

        if (r == 0)
            return 0;
        if (r < 0) {
            ev_error(e, "hc_enc_receive failed (%d)", r);
            return -1;
        }
        if (size <= 0 || !data)
            continue;

        pts = pts_pop(e, e->pts_ns);

        if (!e->mux) {
            uint8_t extradata[HC_EXTRADATA_MAX];
            int extralen = 0;
            struct hc_mux_cfg mcfg;

            if (extract_params(data, size, extradata, sizeof extradata,
                               &extralen) != 0) {
                ev_error(e, "first access unit carries no SPS/PPS -- cannot "
                            "open the muxer");
                return -1;
            }

            memset(&mcfg, 0, sizeof mcfg);
            mcfg.dst_ip     = e->dst_ip;
            mcfg.dst_port   = e->dst_port;
            mcfg.src_port   = e->src_port;
            mcfg.width      = e->ecfg.width;
            mcfg.height     = e->ecfg.height;
            mcfg.fps        = e->ecfg.fps;
            mcfg.with_audio = e->audio != NULL;

            e->mux = hc_mux_open(&mcfg, extradata, extralen);
            if (!e->mux) {
                ev_error(e, "hc_mux_open(%s:%d from :%d) failed",
                         e->dst_ip, e->dst_port, e->src_port);
                return -1;
            }
            HC_LOG("mux open -> %s:%d from :%d, %d byte(s) of SPS/PPS",
                   e->dst_ip, e->dst_port, e->src_port, extralen);
        }

        /* > 0 means the muxer dropped it on purpose (ring full, or audio from
         * before t = 0). That is back-pressure, not a broken session. */
        if (hc_mux_video(e->mux, data, size, pts, is_idr) < 0) {
            ev_error(e, "hc_mux_video failed");
            return -1;
        }
        e->stat_bytes += (uint64_t)size;
        if (is_idr)
            e->tot_idr++;

        if (!e->ready_sent) {
            e->ready_sent = true;
            ev_raw(e, "{\"ev\":\"ready\"}");
            HC_LOG("ready: first frame encoded and sent");
        }
    }
}

/* One grid slot: convert a surface and hand it to the encoder. */
static int encode_slot(struct engine *e, VASurfaceID src, bool repeat)
{
    VASurfaceID dst = VA_INVALID_ID;
    void    *opaque = NULL;
    bool     idr;

    if (hc_enc_acquire(e->enc, &dst, &opaque) != 0) {
        ev_error(e, "hc_enc_acquire failed");
        return -1;
    }
    if (hc_va_vpp_run(e->va, src, dst) != 0) {
        ev_error(e, "hc_va_vpp_run failed");
        return -1;
    }

    idr = e->force_idr || (e->ecfg.gop > 0 && e->since_idr >= e->ecfg.gop);
    if (hc_enc_submit(e->enc, opaque, e->pts_ns, idr) != 0) {
        ev_error(e, "hc_enc_submit failed");
        return -1;
    }
    pts_push(e, e->pts_ns);

    if (idr) {
        e->force_idr = false;
        e->since_idr = 1;
    } else {
        e->since_idr++;
    }

    e->stat_frames++;
    if (repeat)
        e->stat_repeats++;
    return drain_encoder(e);
}

static void drain_audio(struct engine *e)
{
    if (!e->audio || !e->mux)
        return;
    for (int i = 0; i < HC_AUDIO_PER_TICK; i++) {
        const uint8_t *data = NULL;
        int size = 0;
        uint64_t pts = 0;
        int r = hc_audio_read(e->audio, &data, &size, &pts);

        if (r <= 0)
            return;
        if (size > 0 && data) {
            if (hc_mux_audio(e->mux, data, size, pts) < 0) {
                HC_ERR("hc_mux_audio failed; stopping the audio drain");
                return;
            }
            /* Where a replacement leg has to pick up from. */
            e->last_audio_pts = pts;
            e->stat_bytes += (uint64_t)size;
        }
    }
}

static int session_start(struct engine *e, const struct hc_ctl_msg *m)
{
    struct hc_enc_cfg cfg;
    uint64_t deadline;

    if (e->running) {
        HC_LOG("start while a session is running -- restarting it");
        session_stop(e);
    }

    if (m->s_dst_ip[0] == '\0' || m->dst_port <= 0 || m->dst_port > 65535) {
        ev_error(e, "start needs dst_ip and a dst_port in 1..65535");
        return -1;
    }
    /* The muxer binds src_port and src_port+1 (RTCP), and the sink checks the
     * source port, so it is not optional and cannot be the last one. */
    if (m->src_port <= 0 || m->src_port >= 65535) {
        ev_error(e, "start needs a src_port in 1..65534 (RTCP takes src+1), "
                    "got %d", m->src_port);
        return -1;
    }

    memset(&cfg, 0, sizeof cfg);
    cfg.width       = m->width   ? m->width   : 1280;
    cfg.height      = m->height  ? m->height  : 720;
    cfg.fps         = m->fps     ? m->fps     : 60;
    cfg.bitrate_bps = m->bitrate ? m->bitrate : 8000000;
    cfg.gop         = m->gop     ? m->gop     : cfg.fps;
    cfg.low_power   = m->has_low_power ? m->low_power : false;
    /*
     * Composite the pointer into the capture. Nothing ever set this before, so
     * e->cursors was calloc'd false and create_session got options=0, which is
     * "do not paint cursors" -- the cast showed no mouse at all. Someone
     * watching a mirrored desktop needs to see where the pointer is, so this
     * defaults ON when the control message omits it.
     */
    e->cursors      = m->has_cursors ? m->cursors : true;
    cfg.qp          = m->qp;

    if (cfg.fps > 240) {
        ev_error(e, "fps %u is not plausible", cfg.fps);
        return -1;
    }

    snprintf(e->dst_ip,    sizeof e->dst_ip,    "%s", m->s_dst_ip);
    snprintf(e->output,    sizeof e->output,    "%s", m->s_output);
    snprintf(e->audio_dev, sizeof e->audio_dev, "%s", m->s_audio);
    e->dst_port = m->dst_port;
    e->src_port = m->src_port;
    e->ecfg     = cfg;

    HC_LOG("start: %s:%d from :%d, wire %ux%u@%u, %s, gop %u, output '%s'",
           e->dst_ip, e->dst_port, e->src_port, cfg.width, cfg.height, cfg.fps,
           cfg.low_power ? "CQP low-power" : "VBR",
           cfg.gop, e->output[0] ? e->output : "<first>");

    e->drm_fd = open(HC_DRM_NODE, O_RDWR | O_CLOEXEC);
    if (e->drm_fd < 0) {
        ev_error(e, "open %s: %s", HC_DRM_NODE, strerror(errno));
        goto fail;
    }

    e->va = hc_va_open(e->drm_fd);
    if (!e->va) {
        ev_error(e, "hc_va_open failed");
        goto fail;
    }

    if (make_capture(e, e->output) != 0)
        goto fail;

    if (hc_va_vpp_init(e->va, cfg.width, cfg.height) != 0) {
        ev_error(e, "hc_va_vpp_init %ux%u failed", cfg.width, cfg.height);
        goto fail;
    }

    e->enc = hc_enc_open(e->va, &cfg);
    if (!e->enc) {
        ev_error(e, "hc_enc_open failed");
        goto fail;
    }

    if (e->audio_dev[0]) {
        struct hc_audio_cfg acfg = { e->audio_dev, HC_AUDIO_BITRATE };
        e->audio = hc_audio_open(&acfg);
        if (!e->audio) {
            ev_error(e, "hc_audio_open('%s') failed", e->audio_dev);
            goto fail;
        }
        /* A session always begins at unity and unmuted; the control plane
         * applies its configured default right after "ready". */
        e->gain  = 1.0f;
        e->muted = false;
        hc_audio_set_volume(e->audio, e->gain);
        hc_audio_set_muted(e->audio, e->muted);
    }

    /* The grid shares hc_now_ns() with the audio thread's timestamps, so video
     * and audio land in one clock domain without any rebasing. */
    e->interval_ns  = 1000000000ull / cfg.fps;
    e->pts_ns       = hc_now_ns();
    e->next_tick_ns = e->pts_ns;
    e->since_idr    = 0;
    e->force_idr    = true;
    e->hold_idx     = -1;
    e->ready_sent   = false;
    e->pts_head = e->pts_tail = 0;

    /*
     * Prime: the first real frame must arrive, be encoded and be sent before we
     * answer "ready", so a failure here is reported synchronously instead of
     * surfacing as silence on the wire.
     */
    deadline = hc_now_ns() + (uint64_t)HC_FIRST_FRAME_MS * 1000000ull;
    for (;;) {
        int idx = -1;
        uint64_t pres = 0;
        bool have_pres = false;
        struct cap_status st;

        if (cap_take(&e->ct, &idx, &pres, &have_pres)) {
            if (e->hold_idx >= 0 && e->hold_idx != idx)
                cap_put(&e->ct, e->hold_idx);
            e->hold_idx = idx;
            if (encode_slot(e, e->pool.buf[idx].va, false) != 0)
                goto fail;
            if (e->ready_sent)
                break;
            /* Encoded but nothing came out yet -- keep the grid moving. */
            e->pts_ns += e->interval_ns;
        }
        cap_status(&e->ct, &st);
        if (st.fatal) {
            ev_error(e, "%s", st.err[0] ? st.err : "capture failed");
            goto fail;
        }
        if (g_quit)
            goto fail;
        if (hc_now_ns() >= deadline) {
            ev_error(e, "no encoded frame within %d ms of start",
                     HC_FIRST_FRAME_MS);
            goto fail;
        }
        sleep_ms(2);
    }

    e->next_tick_ns = hc_now_ns() + e->interval_ns;
    e->pts_ns      += e->interval_ns;
    e->stat_ns      = hc_now_ns();
    e->stat_frames  = e->stat_repeats = e->stat_bytes = 0;
    getrusage(RUSAGE_SELF, &e->stat_ru);
    e->running = true;
    return 0;

fail:
    session_stop(e);
    return -1;
}

/* -------------------------------------------------------------- retuning */

/*
 * Rebuild ONLY the AVCodecContext. The mux, the socket, the RTP sequence space
 * and the audio leg are untouched; the next frame is forced to IDR so the sink
 * gets the fresh SPS/PPS in band before it needs them.
 */
static int retune_encoder(struct engine *e, const struct hc_enc_cfg *cfg)
{
    struct hc_enc *fresh;
    struct hc_enc_cfg prev = e->ecfg;

    fresh = hc_enc_open(e->va, cfg);
    if (!fresh) {
        ev_error(e, "retune: hc_enc_open failed; keeping the current encoder");
        return -1;
    }

    /* Only now is the old encoder expendable. Whatever it still holds is at
     * most one frame, and the forced IDR below replaces it immediately. */
    hc_enc_close(e->enc);
    e->enc      = fresh;
    e->ecfg     = *cfg;
    e->force_idr = true;
    e->since_idr = 0;
    e->pts_head = e->pts_tail = 0;

    if (prev.bitrate_bps != cfg->bitrate_bps || prev.qp != cfg->qp ||
        prev.low_power != cfg->low_power)
        HC_LOG("retune: encoder rebuilt (%u bps, qp %u, %s)",
               cfg->bitrate_bps, cfg->qp,
               cfg->low_power ? "low-power CQP" : "VBR");
    return 0;
}

static void cmd_retune(struct engine *e, const struct hc_ctl_msg *m)
{
    struct hc_enc_cfg cfg = e->ecfg;
    bool rebuild = false;

    if (!e->running) {
        ev_error(e, "retune with no session running");
        return;
    }

    if (m->fps) {
        if (m->fps > 240) {
            ev_error(e, "retune: fps %u is not plausible", m->fps);
            return;
        }
        /*
         * Pure grid retiming. PTS comes from a 90 kHz grid we own, so the
         * encoder, the muxer and the RTP sequence space see nothing at all.
         */
        e->interval_ns = 1000000000ull / m->fps;
        cfg.fps        = m->fps;
        e->ecfg.fps    = m->fps;
        HC_LOG("retune: pacer now %u fps (%.2f ms slots)",
               m->fps, (double)e->interval_ns / 1e6);
    }
    if (m->bitrate && m->bitrate != cfg.bitrate_bps) {
        cfg.bitrate_bps = m->bitrate;
        rebuild = true;
    }
    if (m->qp && m->qp != cfg.qp) {
        if (m->qp > 51) {
            ev_error(e, "retune: qp %u is out of range", m->qp);
            return;
        }
        cfg.qp = m->qp;
        rebuild = true;
    }
    if (m->has_low_power && m->low_power != cfg.low_power) {
        cfg.low_power = m->low_power;
        rebuild = true;
    }
    if (m->gop && m->gop != cfg.gop) {
        cfg.gop = m->gop;
        rebuild = true;
    }

    if (rebuild && retune_encoder(e, &cfg) != 0)
        return;
    if (!rebuild)
        e->ecfg = cfg;
}

/*
 * WHY THIS IS A THREAD, same reasoning as audio_open_thread below.
 *
 * cmd_output used to drop_capture() then make_capture() inline on the main
 * loop. The pacer runs on that loop, so nothing reached the muxer for however
 * long a Wayland connect, a 4-buffer GBM pool and a VA import took. MEASURED
 * against the loopback harness across one mirror->extend->mirror round trip:
 * PAT/PMT intervals of 140.8 ms and 322.7 ms and PCR gaps of 216.7 ms and
 * 366.7 ms, against assert-ts's 100 ms limit, plus 33 dropped frames. A sink
 * is entitled to drop a session over that.
 *
 * So the new capture is opened on its own thread while the OLD one stays live.
 * The pacer keeps re-converting hold_idx out of the old pool, which is exactly
 * why the old pool must not be freed first. Create before destroy.
 */
static void *capture_open_thread(void *arg)
{
    struct engine *e = arg;
    const char *want = e->cap_pending_name[0] ? e->cap_pending_name : NULL;

    e->cap_pending_rc = -1;
    e->cap_pending = hc_capture_open(want, e->cursors);
    if (e->cap_pending) {
        if (build_pool_into(e, e->cap_pending, &e->pool_pending,
                            &e->pool_pending_live, &e->gbm_fd_pending) == 0) {
            e->cap_pending_rc = 0;
        } else {
            hc_capture_close(e->cap_pending);
            e->cap_pending = NULL;
        }
    }
    atomic_store_explicit(&e->cap_pending_done, true, memory_order_release);
    return NULL;
}

/*
 * Install a capture that finished opening. Runs on the main loop, between
 * pacer ticks, so the swap itself is the only moment without a capture and it
 * costs a cap_join plus a few frees.
 *
 * On failure the OLD capture is left exactly as it was and the session keeps
 * running. Killing a working cast because someone typo'd an output name would
 * be a poor trade.
 */

/*
 * Retiring the OLD capture, off the main loop.
 *
 * capture_swap_if_ready() used to call drop_capture() inline. That joins the
 * old capture thread (which may be mid-hc_capture_wait), destroys a 4-buffer
 * GBM pool and disconnects a Wayland client -- all while the CFR pacer, which
 * runs on the same loop, emits nothing. MEASURED on the loopback harness: a
 * 216 ms PCR gap and a 178 ms PAT gap per switch, even with the NEW capture
 * already opened in the background.
 *
 * On the real sink that gap is fatal rather than untidy: the TV froze on its
 * last frame and dropped the session about 20 s later. A decoder treats a
 * timing discontinuity as a fault; it does not simply catch up.
 *
 * So the old capture is handed to a detached thread that frees it at its own
 * pace. The main loop keeps pacing throughout, and the swap costs a few
 * pointer assignments.
 */
struct retiring {
    struct capthread    ct;
    struct hc_capture  *cap;
    struct hc_pool      pool;
    bool                pool_live;
    int                 gbm_fd;
};

static void *retire_thread(void *arg)
{
    struct retiring *r = arg;

    cap_join(&r->ct);
    if (r->pool_live)
        hc_pool_destroy(&r->pool);
    if (r->gbm_fd >= 0)
        close(r->gbm_fd);
    if (r->cap)
        hc_capture_close(r->cap);
    free(r);
    return NULL;
}

/* Returns 0 if the old capture was handed off; -1 means the caller must free
 * it itself (allocation failed), which is still correct, only slower. */
static int retire_capture_async(struct engine *e)
{
    pthread_t tid;
    struct retiring *r = calloc(1, sizeof *r);

    if (!r)
        return -1;

    r->ct        = e->ct;
    r->cap       = e->cap;
    r->pool      = e->pool;
    r->pool_live = e->pool_live;
    r->gbm_fd    = e->gbm_fd;

    /* The capture thread was handed &e->pool at cap_start; the pool has moved
     * into the bundle, so re-point it before the thread touches it again. */
    r->ct.pool = &r->pool;

    if (pthread_create(&tid, NULL, retire_thread, r) != 0) {
        free(r);
        return -1;
    }
    pthread_detach(tid);

    memset(&e->ct, 0, sizeof e->ct);
    e->cap       = NULL;
    e->pool_live = false;
    e->gbm_fd    = -1;
    return 0;
}

static void capture_swap_if_ready(struct engine *e)
{
    if (!e->cap_switching ||
        !atomic_load_explicit(&e->cap_pending_done, memory_order_acquire))
        return;

    pthread_join(e->cap_open_tid, NULL);
    e->cap_switching = false;
    atomic_store_explicit(&e->cap_pending_done, false, memory_order_release);

    if (e->cap_pending_rc != 0) {
        ev_error(e, "output '%s' could not be captured; staying on '%s'",
                 e->cap_pending_name[0] ? e->cap_pending_name : "<first output>",
                 e->output[0] ? e->output : "<first output>");
        return;
    }

    /* Hand the old capture to a reaper instead of freeing it here: the pacer
     * runs on this loop and every millisecond spent in drop_capture() is a
     * millisecond the sink gets no packets. */
    if (retire_capture_async(e) != 0) {
        HC_LOG("could not spawn the capture reaper; freeing inline");
        drop_capture(e);
    }

    e->cap       = e->cap_pending;
    e->pool      = e->pool_pending;
    e->pool_live = e->pool_pending_live;
    e->gbm_fd    = e->gbm_fd_pending;
    e->cap_pending = NULL;
    e->pool_pending_live = false;
    e->gbm_fd_pending = -1;

    /* hc_pool_create stored the address of pool_pending in the session; the
     * pool moved, so re-point it before a frame is attached to a stale one. */
    hc_capture_set_pool(e->cap, &e->pool);

    if (cap_start(&e->ct, e->cap, &e->pool) != 0) {
        ev_error(e, "could not start the capture thread after switching output");
        session_stop(e);
        return;
    }

    snprintf(e->output, sizeof e->output, "%s", e->cap_pending_name);
    e->hold_idx  = -1;
    e->force_idr = true;                /* the picture changed */
    HC_LOG("output: now capturing '%s'",
           e->output[0] ? e->output : "<first output>");
}

static void cmd_output(struct engine *e, const struct hc_ctl_msg *m)
{
    char want[sizeof e->output];

    if (!e->running) {
        ev_error(e, "output with no session running");
        return;
    }
    snprintf(want, sizeof want, "%s", m->s_output);

    if (e->cap_switching) {
        ev_error(e, "an output switch is already in progress");
        return;
    }

    HC_LOG("output: opening '%s' alongside the live capture",
           want[0] ? want : "<first output>");
    snprintf(e->cap_pending_name, sizeof e->cap_pending_name, "%s", want);
    e->cap_pending      = NULL;
    e->pool_pending_live = false;
    e->gbm_fd_pending   = -1;
    atomic_store_explicit(&e->cap_pending_done, false, memory_order_release);

    if (pthread_create(&e->cap_open_tid, NULL, capture_open_thread, e) != 0) {
        ev_error(e, "could not start the output-switch thread");
        return;
    }
    e->cap_switching = true;
    /* The wire size is frozen, so VPP absorbs the new capture geometry and the
     * encoder never notices the change when the swap lands. */
}

/*
 * Reopen the audio leg against a different pulse source. Only the audio leg:
 * the encoder, the muxer, the RTP socket and the sequence space are all
 * untouched, so the sink never learns the desktop changed sound card.
 *
 * WHY THIS IS A THREAD. hc_audio_open() takes ~120 ms -- avformat_open_input on
 * the pulse device, then the AAC encoder. Doing that inline stops the audio
 * stream for that long, and libavformat's interleaver then holds VIDEO back
 * waiting for the missing audio (max_interleave_delta is 2 s, and it has to be:
 * see mux.c). MEASURED against the loopback harness: a 120 ms audio hole put
 * the first access unit after it 118 ms behind the PCR at its byte offset and
 * failed assert-ts's 100 ms PTS/PCR check, twice per switch.
 *
 * So the replacement is opened on its own thread while the OLD leg keeps
 * feeding the muxer -- create before destroy, the rule this whole engine runs
 * on -- and the swap happens in one tick of the main loop once it is ready.
 */
static void *audio_open_thread(void *arg)
{
    struct engine *e = arg;
    struct hc_audio_cfg acfg = { e->aud_pending_dev, HC_AUDIO_BITRATE };

    e->aud_pending = hc_audio_open(&acfg);
    atomic_store_explicit(&e->aud_pending_done, true, memory_order_release);
    return NULL;
}

static void cmd_audio(struct engine *e, const struct hc_ctl_msg *m)
{
    if (!e->running) {
        ev_error(e, "audio with no session running");
        return;
    }
    if (!e->audio) {
        /* The mux was opened without an audio stream; there is no PID 0x1100
         * to feed and the sink was told so in the M4 SET_PARAMETER. */
        ev_error(e, "this session has no audio leg -- it was started without "
                    "one, and the sink cannot be told about a new stream mid-session");
        return;
    }
    if (!m->s_audio[0]) {
        ev_error(e, "audio needs a source name");
        return;
    }
    if (e->aud_switching) {
        ev_error(e, "an audio source change is already in flight");
        return;
    }
    if (!strcmp(m->s_audio, e->audio_dev)) {
        HC_LOG("audio: already capturing '%s'", e->audio_dev);
        return;
    }

    snprintf(e->aud_pending_dev, sizeof e->aud_pending_dev, "%s", m->s_audio);
    e->aud_pending = NULL;
    atomic_store_explicit(&e->aud_pending_done, false, memory_order_relaxed);
    if (pthread_create(&e->aud_open_tid, NULL, audio_open_thread, e) != 0) {
        ev_error(e, "could not start the audio open thread; still capturing '%s'",
                 e->audio_dev);
        return;
    }
    e->aud_switching = true;
    HC_LOG("audio: opening '%s' in the background", e->aud_pending_dev);
}

/* One tick's worth of "is the replacement leg ready yet?". */
static void poll_audio_switch(struct engine *e)
{
    struct hc_audio *fresh, *old;

    if (!e->aud_switching ||
        !atomic_load_explicit(&e->aud_pending_done, memory_order_acquire))
        return;

    pthread_join(e->aud_open_tid, NULL);
    e->aud_switching = false;
    fresh = e->aud_pending;
    e->aud_pending = NULL;

    if (!fresh) {
        ev_error(e, "hc_audio_open('%s') failed; still capturing '%s'",
                 e->aud_pending_dev, e->audio_dev);
        return;
    }
    if (!e->audio) {                      /* the session stopped while it opened */
        hc_audio_close(fresh);
        return;
    }

    /* Carry the live gain over: a fresh leg starts at unity. */
    hc_audio_set_volume(fresh, e->gain);
    hc_audio_set_muted(fresh, e->muted);
    /* Empty the outgoing leg into the muxer first: those access units are
     * already encoded, and dropping them would widen the hole at the switch by
     * the whole depth of its ring. It also fixes where the new leg starts. */
    drain_audio(e);

    /* The replacement has been recording since it opened, so it holds audio
     * that overlaps what was just sent, at timestamps already spent. Cut it at
     * the last one delivered and the two legs join with no gap and no overlap. */
    HC_LOG("switch: last delivered pts is %.1f ms old",
           (double)(hc_now_ns() - e->last_audio_pts) / 1e6);
    hc_audio_flush(fresh, e->last_audio_pts + 1);

    /* drain_audio() runs on this thread, so the swap needs no lock. */
    old = e->audio;
    e->audio = fresh;
    hc_audio_close(old);

    snprintf(e->audio_dev, sizeof e->audio_dev, "%s", e->aud_pending_dev);
    HC_LOG("audio: capturing '%s' (gain %.2f%s)", e->audio_dev,
           (double)e->gain, e->muted ? ", muted" : "");
}

/* The capture thread asked for a pool rebuild, or died. */
static int handle_capture_state(struct engine *e)
{
    struct cap_status st;

    cap_status(&e->ct, &st);
    if (!st.stopped)
        return 0;

    if (st.need_rebuild) {
        HC_LOG("rebuilding capture after a constraints change");
        drop_capture(e);
        if (make_capture(e, e->output) != 0) {
            ev_error(e, "capture rebuild failed; session stopping");
            session_stop(e);
            return -1;
        }
        e->force_idr = true;
        return 0;
    }

    ev_error(e, "capture stopped: %s", st.err[0] ? st.err : "unknown reason");
    session_stop(e);
    return -1;
}

/* ------------------------------------------------------------------ stats */

static void maybe_stats(struct engine *e)
{
    uint64_t now = hc_now_ns();
    double   secs;
    struct rusage ru;
    struct cap_status cs;
    double   cpu;
    char     line[320];

    if (now - e->stat_ns < HC_STATS_NS)
        return;
    secs = (double)(now - e->stat_ns) / 1e9;
    if (secs <= 0.0)
        secs = 1e-9;

    getrusage(RUSAGE_SELF, &ru);
    cpu = ((double)(ru.ru_utime.tv_sec  - e->stat_ru.ru_utime.tv_sec) +
           (double)(ru.ru_utime.tv_usec - e->stat_ru.ru_utime.tv_usec) / 1e6 +
           (double)(ru.ru_stime.tv_sec  - e->stat_ru.ru_stime.tv_sec) +
           (double)(ru.ru_stime.tv_usec - e->stat_ru.ru_stime.tv_usec) / 1e6) / secs;

    cap_status(&e->ct, &cs);
    snprintf(line, sizeof line,
             "{\"ev\":\"stats\",\"fps\":%.1f,\"kbps\":%.0f,\"cpu\":%.3f,"
             "\"drops\":%" PRIu64 ",\"idr\":%" PRIu64 ",\"repeats\":%" PRIu64
             ",\"bytes\":%" PRIu64 ",\"cap_failed\":%" PRIu64
             ",\"watchdog\":%" PRIu64 "}",
             (double)e->stat_frames / secs,
             (double)e->stat_bytes * 8.0 / 1000.0 / secs,
             cpu, e->tot_drops, e->tot_idr, e->stat_repeats,
             e->mux ? hc_mux_bytes_sent(e->mux) : 0,
             cs.n_failed, cs.n_watchdog);
    ev_raw(e, line);

    e->stat_ns      = now;
    e->stat_frames  = 0;
    e->stat_repeats = 0;
    e->stat_bytes   = 0;
    e->stat_ru      = ru;
}

/* ------------------------------------------------------------ control loop */

/* Returns 0 to keep running, 1 on "quit" or a closed control channel. */
static int handle_control(struct engine *e)
{
    for (;;) {
        struct hc_ctl_msg m;
        int r = hc_ctl_poll(e->ctl, &m);

        if (r == 0)
            return 0;
        if (r == -1) {
            HC_LOG("control channel closed by the peer; exiting");
            return 1;
        }
        if (r < 0) {
            HC_ERR("control channel error; exiting");
            return 1;
        }

        if (!strcmp(m.cmd, "start")) {
            session_start(e, &m);
        } else if (!strcmp(m.cmd, "retune")) {
            cmd_retune(e, &m);
        } else if (!strcmp(m.cmd, "idr")) {
            if (e->running)
                e->force_idr = true;
        } else if (!strcmp(m.cmd, "volume")) {
            /*
             * Each key stands alone. {"muted":true} used to arrive with gain
             * defaulted to 0.0 and silence the stream permanently: unmuting
             * afterwards restored a gain of nothing.
             */
            if (m.has_gain) {
                float g = m.gain;
                if (!(g >= 0.0f))            /* also catches NaN */
                    g = 0.0f;
                if (g > HC_GAIN_MAX)
                    g = HC_GAIN_MAX;
                e->gain = g;
            }
            if (m.has_muted)
                e->muted = m.muted;
            if (!e->audio) {
                ev_error(e, "volume with no audio leg");
            } else {
                if (m.has_gain)
                    hc_audio_set_volume(e->audio, e->gain);
                if (m.has_muted)
                    hc_audio_set_muted(e->audio, e->muted);
            }
        } else if (!strcmp(m.cmd, "audio")) {
            cmd_audio(e, &m);
        } else if (!strcmp(m.cmd, "output")) {
            cmd_output(e, &m);
        } else if (!strcmp(m.cmd, "stop")) {
            session_stop(e);
        } else if (!strcmp(m.cmd, "quit")) {
            return 1;
        } else {
            ev_error(e, "unknown command '%s'", m.cmd);
        }
    }
}

static void run(struct engine *e)
{
    while (!g_quit) {
        int idx = -1;
        uint64_t pres = 0, now;
        bool have_pres = false;

        if (handle_control(e) != 0)
            return;
        poll_audio_switch(e);
        capture_swap_if_ready(e);

        if (!e->running) {
            /* Nothing to pace: wait on the control fd instead of spinning. */
            struct pollfd pfd = { e->ctl_fd, POLLIN, 0 };
            poll(&pfd, 1, HC_IDLE_POLL_MS);
            continue;
        }

        sleep_until_ns(e->next_tick_ns);
        if (g_quit)
            return;

        if (handle_capture_state(e) != 0)
            continue;
        if (!e->running)
            continue;

        if (cap_take(&e->ct, &idx, &pres, &have_pres)) {
            if (e->hold_idx >= 0 && e->hold_idx != idx)
                cap_put(&e->ct, e->hold_idx);
            e->hold_idx = idx;
            if (encode_slot(e, e->pool.buf[idx].va, false) != 0) {
                session_stop(e);
                continue;
            }
        } else if (e->hold_idx >= 0) {
            /*
             * CFR: the screen produced nothing this slot, so re-encode the last
             * good frame. Under VBR that is a near-empty all-skip P frame, and
             * it is the only thing keeping the sink from stalling on an idle
             * desktop.
             */
            if (encode_slot(e, e->pool.buf[e->hold_idx].va, true) != 0) {
                session_stop(e);
                continue;
            }
        }

        drain_audio(e);

        e->next_tick_ns += e->interval_ns;
        e->pts_ns       += e->interval_ns;

        now = hc_now_ns();
        if (now > e->next_tick_ns + HC_MAX_LATE_SLOTS * e->interval_ns) {
            /*
             * We fell far enough behind that catching up would mean a burst of
             * back-to-back frames. Skip the missed slots instead: the PTS grid
             * keeps its spacing, the sink keeps its cadence, and the loss is
             * visible in "drops".
             */
            uint64_t late = (now - e->next_tick_ns) / e->interval_ns;
            e->tot_drops    += late;
            e->next_tick_ns += late * e->interval_ns;
            e->pts_ns       += late * e->interval_ns;
        }

        maybe_stats(e);
    }
}

/* ------------------------------------------------------------------- main */

static void usage(const char *argv0)
{
    printf(
        "usage: %s [--control-fd N]\n"
        "\n"
        "hyprcast-engine is the media leg of hyprcast: capture -> VAAPI VPP ->\n"
        "h264_vaapi -> MPEG-TS over RTP, with the CPU never touching a pixel.\n"
        "It idles until the control plane sends \"start\".\n"
        "\n"
        "  --control-fd N   inherited fd carrying newline-delimited JSON "
        "(default 3)\n"
        "  -h, --help       this text\n"
        "\n"
        "control channel, one JSON object per line:\n"
        "  -> {\"cmd\":\"start\",\"dst_ip\":\"192.168.168.42\",\"dst_port\":19900,\n"
        "      \"src_port\":19002,\"width\":1280,\"height\":720,\"fps\":60,\n"
        "      \"bitrate\":8000000,\"gop\":60,\"output\":\"eDP-1\","
        "\"audio\":\"...monitor\"}\n"
        "  -> {\"cmd\":\"retune\",\"fps\":30}      re-time the pacer only\n"
        "  -> {\"cmd\":\"retune\",\"bitrate\":6000000}  rebuild the encoder only\n"
        "  -> {\"cmd\":\"idr\"}                  force a keyframe\n"
        "  -> {\"cmd\":\"volume\",\"gain\":0.5,\"muted\":false}   1.0 is unity;\n"
        "                                      an absent key is left alone\n"
        "  -> {\"cmd\":\"audio\",\"audio\":\"...monitor\"}  reopen the audio "
        "leg only\n"
        "  -> {\"cmd\":\"output\",\"name\":\"HEADLESS-1\"}   different wl_output\n"
        "  -> {\"cmd\":\"stop\"} / {\"cmd\":\"quit\"}\n"
        "  <- {\"ev\":\"ready\"} / {\"ev\":\"stats\",...} / {\"ev\":\"error\","
        "\"msg\":\"...\"}\n"
        "\n"
        "\"audio\" is opt-in: omit it and the session is video-only. Pass a "
        "pulse\nmonitor source (\"@DEFAULT_MONITOR@\" resolves the current "
        "sink's monitor).\n"
        "Only \"width\"/\"height\" are frozen for the life of a session -- "
        "they are what\nthe sink agreed to in M3. Everything else retunes "
        "live.\n",
        argv0);
}

int main(int argc, char **argv)
{
    struct engine e;
    int ctl_fd = 3;
    struct sigaction sa;

    setvbuf(stdout, NULL, _IOLBF, 0);

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (!strcmp(a, "--help") || !strcmp(a, "-h")) {
            usage(argv[0]);
            return 0;
        }
        if (!strcmp(a, "--control-fd")) {
            char *end = NULL;
            long v;
            if (i + 1 >= argc) {
                fprintf(stderr, "engine: --control-fd needs a value\n");
                return 2;
            }
            errno = 0;
            v = strtol(argv[++i], &end, 10);
            if (errno != 0 || !end || *end != '\0' || v < 0 || v > 1024) {
                fprintf(stderr, "engine: bad --control-fd '%s'\n", argv[i]);
                return 2;
            }
            ctl_fd = (int)v;
            continue;
        }
        fprintf(stderr, "engine: unknown argument '%s'\n", a);
        usage(argv[0]);
        return 2;
    }

    memset(&e, 0, sizeof e);
    e.drm_fd  = -1;
    e.gbm_fd  = -1;
    e.hold_idx = -1;
    e.ctl_fd  = ctl_fd;
    e.gain    = 1.0f;      /* unity, so a volume-less session is never quiet */

    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    /* A dead control peer or a closed socket must not kill the process mid
     * teardown; every write path already checks its return value. */
    sa.sa_handler = SIG_IGN;
    sigaction(SIGPIPE, &sa, NULL);

    e.ctl = hc_ctl_open(ctl_fd);
    if (!e.ctl) {
        fprintf(stderr, "engine: no control channel on fd %d. Pass "
                        "--control-fd N, or run with --help.\n", ctl_fd);
        return 2;
    }

    HC_LOG("hyprcast-engine up on control fd %d; waiting for \"start\"", ctl_fd);
    run(&e);

    session_stop(&e);
    hc_ctl_close(e.ctl);
    HC_LOG("exit");
    return 0;
}
