/*
 * hyprcast-muxtest -- the Milestone 2 harness: the whole native path, out on
 * the wire.
 *
 *   ext-image-copy-capture -> VA import -> VPP BGRX->NV12 -> h264_vaapi
 *     -> hc_mux (rtp_mpegts, its own writer thread)
 *   PipeWire monitor -> AAC-LC 48 kHz stereo (hc_audio, its own thread)
 *     -> hc_mux
 *
 * Point it at tools/mock-sink.py and validate the result with
 * tools/assert-ts.py:
 *
 *   python3 tools/mock-sink.py --rtp-port 19900 --fifo /tmp/hc.ts \
 *           --connect-timeout 25 &
 *   engine/build/hyprcast-muxtest --seconds 15
 *   python3 tools/assert-ts.py --pmt-pid 0x1000 --video-pid 0x1011 \
 *           --audio-pid 0x1100 /tmp/hc.ts
 *
 * (mock-sink binds its RTP socket before it dials the RTSP source, so it
 * records the full RTP leg while it is still retrying the connect. The engine
 * does not speak RTSP -- that half stays in the Python control plane.)
 */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/resource.h>
#include <sys/types.h>
#include <unistd.h>

#include <drm_fourcc.h>
#include <wayland-client.h>

#include "linux-dmabuf-v1-client-protocol.h"

#include "hc.h"

#define HC_DRM_NODE      "/dev/dri/renderD128"
#define HC_FRAME_TIMEOUT 2000
#define HC_POOL_BUFS     3

static volatile sig_atomic_t g_stop;

static void on_signal(int sig)
{
    (void)sig;
    g_stop = 1;
}

/* ------------------------------------------------------------------ opts */

struct opts {
    const char *output;
    const char *dst_ip;
    const char *audio_dev;
    int         dst_port, src_port;
    uint32_t    fps, seconds, width, height, bitrate, gop, qp;
    uint32_t    audio_bitrate;
    float       volume;
    bool        cursors, low_power, no_audio, muted;
};

static void usage(const char *argv0)
{
    fprintf(stderr,
        "usage: %s [options]\n"
        "  --dst-ip IP      sink address                  (default 127.0.0.1)\n"
        "  --dst-port N     sink RTP port                 (default 19900)\n"
        "  --src-port N     local RTP port to bind        (default 19002)\n"
        "  --output NAME    wl_output to capture (substring, default first)\n"
        "  --fps N          encoder frame rate            (default 60)\n"
        "  --seconds N      run length                    (default 15)\n"
        "  --width N        wire width                    (default 1280)\n"
        "  --height N       wire height                   (default 720)\n"
        "  --bitrate BPS    video target bitrate          (default 8000000)\n"
        "  --gop N          frames between IDR            (default 60)\n"
        "  --audio DEV      pulse source (default: default sink's monitor)\n"
        "  --audio-bitrate BPS                            (default 128000)\n"
        "  --volume G       0.0 .. 4.0 gain on the PCM    (default 1.0)\n"
        "  --muted          start muted\n"
        "  --no-audio       video only\n"
        "  --cursors        composite the cursor\n"
        "  --low-power      VAEntrypointEncSliceLP (VDEnc; forces CQP)\n"
        "  --qp N           CQP quantiser for --low-power (default 26)\n",
        argv0);
}

static int parse_u32(const char *s, uint32_t *out)
{
    char *end = NULL;
    errno = 0;
    unsigned long v = strtoul(s, &end, 10);
    if (errno != 0 || !end || *end != '\0' || end == s || v > 0xffffffffUL)
        return -1;
    *out = (uint32_t)v;
    return 0;
}

static bool takes_value(const char *a)
{
    static const char *const with_val[] = {
        "--dst-ip", "--dst-port", "--src-port", "--output", "--fps",
        "--seconds", "--width", "--height", "--bitrate", "--gop",
        "--audio", "--audio-bitrate", "--volume", "--qp",
    };
    for (size_t i = 0; i < sizeof with_val / sizeof *with_val; i++)
        if (!strcmp(a, with_val[i]))
            return true;
    return false;
}

static int parse_args(int argc, char **argv, struct opts *o)
{
    memset(o, 0, sizeof *o);
    o->dst_ip        = "127.0.0.1";
    o->dst_port      = 19900;
    o->src_port      = 19002;
    o->fps           = 60;
    o->seconds       = 15;
    o->width         = 1280;
    o->height        = 720;
    o->bitrate       = 8000000;
    o->gop           = 60;
    o->audio_bitrate = 128000;
    o->volume        = 1.0f;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        uint32_t u;

        if (takes_value(a) && i + 1 >= argc) {
            fprintf(stderr, "muxtest: %s needs a value\n", a);
            return -1;
        }
        if (!strcmp(a, "--dst-ip")) {
            o->dst_ip = argv[++i];
        } else if (!strcmp(a, "--dst-port")) {
            if (parse_u32(argv[++i], &u) || u == 0 || u > 65535) goto bad;
            o->dst_port = (int)u;
        } else if (!strcmp(a, "--src-port")) {
            if (parse_u32(argv[++i], &u) || u == 0 || u >= 65535) goto bad;
            o->src_port = (int)u;
        } else if (!strcmp(a, "--output")) {
            o->output = argv[++i];
        } else if (!strcmp(a, "--audio")) {
            o->audio_dev = argv[++i];
        } else if (!strcmp(a, "--fps")) {
            if (parse_u32(argv[++i], &o->fps) || o->fps == 0) goto bad;
        } else if (!strcmp(a, "--seconds")) {
            if (parse_u32(argv[++i], &o->seconds) || o->seconds == 0) goto bad;
        } else if (!strcmp(a, "--width")) {
            if (parse_u32(argv[++i], &o->width) || o->width == 0) goto bad;
        } else if (!strcmp(a, "--height")) {
            if (parse_u32(argv[++i], &o->height) || o->height == 0) goto bad;
        } else if (!strcmp(a, "--bitrate")) {
            if (parse_u32(argv[++i], &o->bitrate) || o->bitrate == 0) goto bad;
        } else if (!strcmp(a, "--audio-bitrate")) {
            if (parse_u32(argv[++i], &o->audio_bitrate) || o->audio_bitrate == 0)
                goto bad;
        } else if (!strcmp(a, "--gop")) {
            if (parse_u32(argv[++i], &o->gop) || o->gop == 0) goto bad;
        } else if (!strcmp(a, "--qp")) {
            if (parse_u32(argv[++i], &o->qp) || o->qp == 0 || o->qp > 51) goto bad;
        } else if (!strcmp(a, "--volume")) {
            char *end = NULL;
            o->volume = strtof(argv[++i], &end);
            if (!end || *end != '\0') goto bad;
        } else if (!strcmp(a, "--muted")) {
            o->muted = true;
        } else if (!strcmp(a, "--no-audio")) {
            o->no_audio = true;
        } else if (!strcmp(a, "--cursors")) {
            o->cursors = true;
        } else if (!strcmp(a, "--low-power")) {
            o->low_power = true;
        } else if (!strcmp(a, "--help") || !strcmp(a, "-h")) {
            usage(argv[0]);
            return 1;
        } else {
            fprintf(stderr, "muxtest: unknown argument '%s'\n", a);
            usage(argv[0]);
            return -1;
        }
        continue;
bad:
        fprintf(stderr, "muxtest: bad value for %s: '%s'\n", a, argv[i]);
        return -1;
    }
    return 0;
}

/* ------------------------------------------------------------------ pool */

static int pick_mods(const struct hc_capture_caps *caps, uint64_t *mods, int max)
{
    static const uint64_t MOD_CCS   = 0x0100000000000004ull;
    static const uint64_t MOD_CCS_F = 0x0100000000000005ull;
    uint64_t want = I915_FORMAT_MOD_Y_TILED;
    int n = 0;

    for (int i = 0; i < caps->nmods && n < max; i++)
        if (caps->mods[i] == want)
            mods[n++] = want;
    if (n > 0)
        return n;

    fprintf(stderr, "muxtest: WARNING %s not advertised; falling back to the "
                    "advertised list minus INVALID and CCS\n", hc_mod_str(want));
    for (int i = 0; i < caps->nmods && n < max; i++) {
        uint64_t m = caps->mods[i];
        if (m == DRM_FORMAT_MOD_INVALID || m == MOD_CCS || m == MOD_CCS_F)
            continue;
        mods[n++] = m;
    }
    return n ? n : -1;
}

static int build_pool(struct hc_capture *cap, struct hc_va *va,
                      struct hc_pool *pool, int drm_fd)
{
    const struct hc_capture_caps *caps = hc_capture_caps(cap);
    uint64_t mods[HC_MAX_MODS];
    int nmods;

    if (!caps || caps->nmods <= 0) {
        fprintf(stderr, "muxtest: no dmabuf constraints from the compositor\n");
        return -1;
    }
    nmods = pick_mods(caps, mods, HC_MAX_MODS);
    if (nmods < 0)
        return -1;

    if (hc_pool_create(pool, drm_fd, hc_capture_dmabuf(cap),
                       caps->width, caps->height, caps->fourcc,
                       mods, nmods, HC_POOL_BUFS) != 0) {
        fprintf(stderr, "muxtest: hc_pool_create failed\n");
        return -1;
    }
    hc_capture_set_pool(cap, pool);

    if (hc_va_import_pool(va, pool) != 0) {
        fprintf(stderr, "muxtest: hc_va_import_pool failed\n");
        hc_pool_destroy(pool);
        return -1;
    }
    printf("capture: %ux%u -> wire, pool %d buffer(s) modifier=%s\n",
           caps->width, caps->height, pool->n,
           pool->n > 0 ? hc_mod_str(pool->buf[0].modifier) : "n/a");
    return 0;
}

/* ------------------------------------------------------------------ main */

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IOLBF, 0);

    struct opts opt;
    int rc = parse_args(argc, argv, &opt);
    if (rc != 0)
        return rc > 0 ? 0 : 2;

    struct hc_capture *cap = NULL;
    struct hc_va      *va  = NULL;
    struct hc_enc     *enc = NULL;
    struct hc_mux     *mux = NULL;
    struct hc_audio   *aud = NULL;
    struct hc_pool     pool;
    int  drm_fd = -1;
    int  status = 1;
    bool pool_live = false;

    memset(&pool, 0, sizeof pool);

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);

    printf("== hyprcast-muxtest ==\n");
    printf("wire %ux%u @ %u fps -> %s:%d from :%d, %u s%s%s\n",
           opt.width, opt.height, opt.fps, opt.dst_ip, opt.dst_port,
           opt.src_port, opt.seconds,
           opt.low_power ? ", low-power" : "",
           opt.no_audio ? ", no audio" : "");

    drm_fd = open(HC_DRM_NODE, O_RDWR | O_CLOEXEC);
    if (drm_fd < 0) {
        fprintf(stderr, "muxtest: open %s: %s\n", HC_DRM_NODE, strerror(errno));
        goto done;
    }

    cap = hc_capture_open(opt.output, opt.cursors);
    if (!cap) {
        fprintf(stderr, "muxtest: hc_capture_open failed\n");
        goto done;
    }

    va = hc_va_open(drm_fd);
    if (!va) {
        fprintf(stderr, "muxtest: hc_va_open failed\n");
        goto done;
    }

    if (build_pool(cap, va, &pool, drm_fd) != 0)
        goto done;
    pool_live = true;

    if (hc_va_vpp_init(va, opt.width, opt.height) != 0) {
        fprintf(stderr, "muxtest: hc_va_vpp_init failed\n");
        goto done;
    }

    struct hc_enc_cfg ecfg = {
        .width = opt.width, .height = opt.height, .fps = opt.fps,
        .bitrate_bps = opt.bitrate, .gop = opt.gop,
        .low_power = opt.low_power, .qp = opt.qp,
    };
    enc = hc_enc_open(va, &ecfg);
    if (!enc) {
        fprintf(stderr, "muxtest: hc_enc_open failed\n");
        goto done;
    }

    if (!opt.no_audio) {
        struct hc_audio_cfg acfg = {
            .device = opt.audio_dev,
            .bitrate_bps = opt.audio_bitrate,
        };
        aud = hc_audio_open(&acfg);
        if (!aud) {
            fprintf(stderr, "muxtest: hc_audio_open failed\n");
            goto done;
        }
        hc_audio_set_volume(aud, opt.volume);
        hc_audio_set_muted(aud, opt.muted);
    }

    struct hc_mux_cfg mcfg = {
        .dst_ip = opt.dst_ip, .dst_port = opt.dst_port,
        .src_port = opt.src_port,
        .width = opt.width, .height = opt.height, .fps = opt.fps,
        .with_audio = !opt.no_audio,
    };
    /* The encoder emits in-band SPS/PPS (no AV_CODEC_FLAG_GLOBAL_HEADER), so
     * there is no extradata to hand over; every IDR carries its own. */
    mux = hc_mux_open(&mcfg, NULL, 0);
    if (!mux) {
        fprintf(stderr, "muxtest: hc_mux_open failed\n");
        goto done;
    }

    /* ------------------------------------------------------------- loop */

    uint64_t start    = hc_now_ns();
    uint64_t deadline = start + (uint64_t)opt.seconds * 1000000000ull;
    uint64_t frames = 0, ok_frames = 0, failed = 0;
    uint64_t vbytes = 0, vpackets = 0, idrs = 0;
    uint64_t abytes = 0, apackets = 0;
    uint64_t vdrop = 0, adrop = 0;
    bool timed_out = false;

    struct rusage ru0;
    getrusage(RUSAGE_SELF, &ru0);

    struct hc_inflight *tok = NULL;
    if (hc_capture_submit(cap, &tok) != 0) {
        fprintf(stderr, "muxtest: initial hc_capture_submit failed\n");
        goto report;
    }

    while (!g_stop && hc_now_ns() < deadline) {
        struct hc_frame fr;
        memset(&fr, 0, sizeof fr);

        int crc = hc_capture_wait(cap, tok, &fr, HC_FRAME_TIMEOUT);
        tok = NULL;
        frames++;

        bool reserved = false;
        if (crc == 0 && fr.ok && fr.buf_index >= 0 && fr.buf_index < pool.n) {
            pool.buf[fr.buf_index].in_flight = true;
            reserved = true;
        }

        if (!g_stop && hc_now_ns() < deadline) {
            int src = hc_capture_submit(cap, &tok);
            if (src != 0 && src != -4)
                fprintf(stderr, "muxtest: hc_capture_submit failed (%d)\n", src);
        }

        if (crc != 0) {
            if (reserved)
                pool.buf[fr.buf_index].in_flight = false;
            fprintf(stderr, "muxtest: capture TIMED OUT after %d ms on frame "
                            "%" PRIu64 " -- this is the screencopy permission "
                            "prompt. Aborting.\n", HC_FRAME_TIMEOUT, frames);
            timed_out = true;
            break;
        }
        if (!fr.ok || fr.buf_index < 0 || fr.buf_index >= pool.n) {
            failed++;
            if (reserved)
                pool.buf[fr.buf_index].in_flight = false;
            continue;
        }

        VASurfaceID dst = VA_INVALID_ID;
        void *opaque = NULL;
        if (hc_enc_acquire(enc, &dst, &opaque) != 0) {
            if (reserved)
                pool.buf[fr.buf_index].in_flight = false;
            fprintf(stderr, "muxtest: hc_enc_acquire failed\n");
            break;
        }
        if (hc_va_vpp_run(va, pool.buf[fr.buf_index].va, dst) != 0) {
            if (reserved)
                pool.buf[fr.buf_index].in_flight = false;
            fprintf(stderr, "muxtest: hc_va_vpp_run failed\n");
            break;
        }
        if (reserved)
            pool.buf[fr.buf_index].in_flight = false;

        uint64_t pts = fr.have_presentation ? fr.presentation_ns : hc_now_ns();
        bool force_idr = (ok_frames % opt.gop) == 0;
        if (hc_enc_submit(enc, opaque, pts, force_idr) != 0) {
            fprintf(stderr, "muxtest: hc_enc_submit failed\n");
            break;
        }
        ok_frames++;

        for (;;) {
            const uint8_t *data = NULL;
            int size = 0;
            bool is_idr = false;
            int r = hc_enc_receive(enc, &data, &size, &is_idr);
            if (r == 0)
                break;
            if (r < 0) {
                fprintf(stderr, "muxtest: hc_enc_receive failed (%d)\n", r);
                goto drained;
            }
            if (size <= 0 || !data)
                continue;
            vbytes += (uint64_t)size;
            vpackets++;
            if (is_idr)
                idrs++;
            r = hc_mux_video(mux, data, size, pts, is_idr);
            if (r < 0) {
                fprintf(stderr, "muxtest: hc_mux_video failed (%d)\n", r);
                goto drained;
            }
            if (r > 0)
                vdrop++;
        }
drained:
        /* Audio is pulled on the video cadence: hc_audio_read never blocks. */
        while (aud) {
            const uint8_t *adata = NULL;
            int asize = 0;
            uint64_t apts = 0;
            int r = hc_audio_read(aud, &adata, &asize, &apts);
            if (r == 0)
                break;
            if (r < 0) {
                fprintf(stderr, "muxtest: hc_audio_read failed (%d)\n", r);
                break;
            }
            abytes += (uint64_t)asize;
            apackets++;
            /* Keep draining even if the mux refuses a frame: leaving audio in
             * hc_audio's ring only makes the next frame later. */
            r = hc_mux_audio(mux, adata, asize, apts);
            if (r < 0)
                fprintf(stderr, "muxtest: hc_mux_audio failed (%d)\n", r);
            else if (r > 0)
                adrop++;
        }
    }

    if (tok) {
        struct hc_frame drop;
        hc_capture_wait(cap, tok, &drop, HC_FRAME_TIMEOUT);
        tok = NULL;
    }

report:;
    {
        uint64_t wall = hc_now_ns() - start;
        double wall_s = (double)wall / 1e9;
        struct rusage ru1;

        if (wall_s <= 0.0)
            wall_s = 1e-9;
        getrusage(RUSAGE_SELF, &ru1);

        double utime = (double)(ru1.ru_utime.tv_sec - ru0.ru_utime.tv_sec) +
                       (double)(ru1.ru_utime.tv_usec - ru0.ru_utime.tv_usec) / 1e6;
        double stime = (double)(ru1.ru_stime.tv_sec - ru0.ru_stime.tv_sec) +
                       (double)(ru1.ru_stime.tv_usec - ru0.ru_stime.tv_usec) / 1e6;

        uint64_t sent = hc_mux_bytes_sent(mux);

        printf("\n== summary ==\n");
        printf("wall            : %.3f s\n", wall_s);
        printf("frames          : %" PRIu64 " attempted, %" PRIu64 " encoded, "
               "%" PRIu64 " failed\n", frames, ok_frames, failed);
        printf("sustained fps   : %.2f\n", (double)ok_frames / wall_s);
        printf("video AUs       : %" PRIu64 " (%" PRIu64 " IDR), %.2f Mbit, "
               "%" PRIu64 " dropped by the mux\n", vpackets, idrs,
               (double)vbytes * 8.0 / 1e6, vdrop);
        printf("audio AUs       : %" PRIu64 ", %.2f Mbit, %" PRIu64 " dropped "
               "by the mux\n", apackets, (double)abytes * 8.0 / 1e6, adrop);
        printf("on the wire     : %" PRIu64 " bytes = %.3f Mbit/s\n",
               sent, (double)sent * 8.0 / 1e6 / wall_s);
        printf("CPU             : %.2f%% of one core (user %.2fs sys %.2fs)\n",
               (utime + stime) / wall_s * 100.0, utime, stime);

        /* adrop is not a failure by itself: the audio thread starts before the
         * first captured frame, and everything it produced before t=0 is
         * dropped by design. A dropped VIDEO frame means the wire is behind. */
        bool ok = !timed_out && ok_frames > 0 && sent > 0 && vdrop == 0 &&
                  (opt.no_audio || apackets > 0);
        printf("\n%s\n", ok ? "RESULT: PASS" : "RESULT: FAIL");
        status = ok ? 0 : 1;
    }

done:
    if (mux)
        hc_mux_close(mux);
    if (aud)
        hc_audio_close(aud);
    if (enc)
        hc_enc_close(enc);
    if (pool_live)
        hc_pool_destroy(&pool);
    if (va)
        hc_va_close(va);
    if (cap)
        hc_capture_close(cap);
    if (drm_fd >= 0)
        close(drm_fd);

    return status;
}
