/*
 * hyprcast-bench -- Milestone 1B end-to-end harness.
 *
 *   ext-image-copy-capture (gbm dmabuf)
 *     -> VA import as BGRX          (zero copy, same bo)
 *     -> VAAPI VPP BGRX -> NV12     (scales capture size down to the wire size)
 *     -> h264_vaapi on that surface (zero copy)
 *     -> Annex-B out
 *
 * Per-stage histograms plus the Milestone 1B exit criteria as PASS/FAIL.
 * The CPU never touches a pixel: the only bytes read here are the bitstream.
 *
 * MOD=LINEAR|Y_TILED|X_TILED in the environment overrides the modifier we pin
 * the pool to, matching probe.c's convention, for A/B testing. The default is
 * I915_Y_TILED because Y_TILED_CCS cannot be imported by iHD and
 * gbm_bo_create_with_modifiers2() picks it whenever it is offered.
 */
#define _POSIX_C_SOURCE 200809L

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <stdbool.h>
#include <stdint.h>
#include <inttypes.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <signal.h>
#include <sys/types.h>
#include <sys/resource.h>
#include <sys/sysmacros.h>
#include <drm_fourcc.h>
#include <wayland-client.h>

/*
 * hc.h names struct zwp_linux_dmabuf_v1 only inside a prototype's parameter
 * list, so the tag must already exist at file scope for the pointer we hand
 * from hc_capture_dmabuf() to hc_pool_create() to have a compatible type.
 */
#include "linux-dmabuf-v1-client-protocol.h"

#include "hc.h"

#define HC_DRM_NODE      "/dev/dri/renderD128"
#define HC_FRAME_TIMEOUT 2000   /* ms; a hang here is the permission prompt */
#define HC_POOL_BUFS     3

/*
 * The only mutable global. The Wayland dispatch inside hc_capture_frame() is
 * blocking, so a signal flag is the sole way to unwind a run early; POSIX
 * gives us no other legal channel out of a handler.
 */
static volatile sig_atomic_t g_stop;

static void on_signal(int sig)
{
    (void)sig;
    g_stop = 1;
}

/* ------------------------------------------------------------------ opts */

struct opts {
    const char *output;
    const char *out_path;
    uint32_t    fps;
    uint32_t    seconds;
    uint32_t    width, height;   /* wire size */
    uint32_t    bitrate;
    uint32_t    gop;
    bool        cursors;
    bool        low_power;
    uint32_t qp;
};

static void usage(const char *argv0)
{
    fprintf(stderr,
        "usage: %s [options]\n"
        "  --output NAME    wl_output to capture (substring match, default first)\n"
        "  --fps N          encoder frame rate            (default 60)\n"
        "  --seconds N      run length in seconds         (default 60)\n"
        "  --width N        wire width                    (default 1280)\n"
        "  --height N       wire height                   (default 720)\n"
        "  --bitrate BPS    encoder target bitrate        (default 8000000)\n"
        "  --gop N          frames between IDR            (default 60)\n"
        "  --out PATH       write Annex-B here (file or /dev/null)\n"
        "  --cursors        composite the cursor into the capture\n"
        "  --low-power      use VAEntrypointEncSliceLP (VDEnc; forces CQP)\n"
        "  --qp N           CQP quantiser for --low-power (default 26)\n"
        "env: MOD=LINEAR|Y_TILED|X_TILED  pin the pool modifier\n",
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
        "--output", "--out", "--fps", "--seconds",
        "--width", "--height", "--bitrate", "--gop", "--qp",
    };
    for (size_t i = 0; i < sizeof with_val / sizeof *with_val; i++)
        if (!strcmp(a, with_val[i]))
            return true;
    return false;
}

static int parse_args(int argc, char **argv, struct opts *o)
{
    o->output    = NULL;
    o->out_path  = NULL;
    o->fps       = 60;
    o->seconds   = 60;
    o->width     = 1280;
    o->height    = 720;
    o->bitrate   = 8000000;
    o->gop       = 60;
    o->cursors   = false;
    o->low_power = false;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (takes_value(a) && i + 1 >= argc) {
            fprintf(stderr, "bench: %s needs a value\n", a);
            return -1;
        }
        if (!strcmp(a, "--output")) {
            o->output = argv[++i];
        } else if (!strcmp(a, "--out")) {
            o->out_path = argv[++i];
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
        } else if (!strcmp(a, "--gop")) {
            if (parse_u32(argv[++i], &o->gop) || o->gop == 0) goto bad;
        } else if (!strcmp(a, "--cursors")) {
            o->cursors = true;
        } else if (!strcmp(a, "--low-power")) {
            o->low_power = true;
        } else if (!strcmp(a, "--qp")) {
            if (parse_u32(argv[++i], &o->qp) || o->qp == 0 || o->qp > 51) goto bad;
        } else if (!strcmp(a, "--help") || !strcmp(a, "-h")) {
            usage(argv[0]);
            return 1;
        } else {
            fprintf(stderr, "bench: unknown argument '%s'\n", a);
            usage(argv[0]);
            return -1;
        }
        continue;
bad:
        fprintf(stderr, "bench: bad value for %s: '%s'\n", a, argv[i]);
        return -1;
    }
    return 0;
}

/* ------------------------------------------------------------- modifiers */

/*
 * Build the modifier list we allocate the pool with.
 *
 * Default: exactly I915_Y_TILED. gbm_bo_create_with_modifiers2() fed
 * Hyprland's full advertised list picks Y_TILED_CCS, which iHD then refuses
 * to import ("resource allocation failed"), so the list is pinned rather than
 * merely filtered. If Y_TILED is somehow absent we fall back to the
 * advertised list minus INVALID and minus every CCS modifier, and say so.
 */
static int pick_mods(const struct hc_capture_caps *caps,
                     uint64_t *mods, int max)
{
    static const uint64_t MOD_CCS   = 0x0100000000000004ull;
    static const uint64_t MOD_CCS_F = 0x0100000000000005ull;

    const char *env = getenv("MOD");
    uint64_t want = I915_FORMAT_MOD_Y_TILED;
    const char *want_src = "default";

    if (env && *env) {
        if (!strcasecmp(env, "LINEAR"))
            want = DRM_FORMAT_MOD_LINEAR;
        else if (!strcasecmp(env, "Y_TILED") || !strcasecmp(env, "YTILED"))
            want = I915_FORMAT_MOD_Y_TILED;
        else if (!strcasecmp(env, "X_TILED") || !strcasecmp(env, "XTILED"))
            want = I915_FORMAT_MOD_X_TILED;
        else {
            fprintf(stderr, "bench: MOD='%s' not understood "
                            "(LINEAR|Y_TILED|X_TILED)\n", env);
            return -1;
        }
        want_src = "MOD env";
    }

    int n = 0;
    for (int i = 0; i < caps->nmods && n < max; i++) {
        if (caps->mods[i] == want)
            mods[n++] = want;
    }
    if (n > 0) {
        printf("modifier: pinned to %s (%s)\n", hc_mod_str(want), want_src);
        return n;
    }

    if (env && *env) {
        fprintf(stderr, "bench: MOD=%s -> %s is NOT in the advertised list\n",
                env, hc_mod_str(want));
        return -1;
    }

    fprintf(stderr, "bench: WARNING %s not advertised; falling back to the "
                    "advertised list minus INVALID and CCS\n",
            hc_mod_str(want));
    for (int i = 0; i < caps->nmods && n < max; i++) {
        uint64_t m = caps->mods[i];
        if (m == DRM_FORMAT_MOD_INVALID || m == MOD_CCS || m == MOD_CCS_F)
            continue;
        mods[n++] = m;
    }
    if (n == 0)
        fprintf(stderr, "bench: no usable modifier survived filtering\n");
    return n ? n : -1;
}

static void print_caps(const struct hc_capture_caps *caps)
{
    char fb[5];
    printf("capture: %ux%u fourcc '%s' (0x%08" PRIx32 ") %d modifier(s)\n",
           caps->width, caps->height, hc_fourcc_str(caps->fourcc, fb),
           caps->fourcc, caps->nmods);
    printf("  advertised:");
    for (int i = 0; i < caps->nmods; i++)
        printf(" %s", hc_mod_str(caps->mods[i]));
    printf("\n");
    if (caps->have_dmabuf_device)
        printf("  dmabuf_device: major %u minor %u\n",
               major(caps->dmabuf_device), minor(caps->dmabuf_device));
}

/*
 * (Re)build the pool from the current capture constraints and re-import it
 * into VA. Used both for the initial build and after a constraints round.
 */
static int build_pool(struct hc_capture *cap, struct hc_va *va,
                      struct hc_pool *pool, int drm_fd)
{
    const struct hc_capture_caps *caps = hc_capture_caps(cap);
    if (!caps || caps->nmods <= 0) {
        fprintf(stderr, "bench: no dmabuf constraints from the compositor\n");
        return -1;
    }
    print_caps(caps);
    if (caps->fourcc != DRM_FORMAT_XRGB8888)
        fprintf(stderr, "bench: WARNING compositor did not offer XRGB8888; "
                        "using its preferred format instead\n");

    uint64_t mods[HC_MAX_MODS];
    int nmods = pick_mods(caps, mods, HC_MAX_MODS);
    if (nmods < 0)
        return -1;

    if (hc_pool_create(pool, drm_fd, hc_capture_dmabuf(cap),
                       caps->width, caps->height, caps->fourcc,
                       mods, nmods, HC_POOL_BUFS) != 0) {
        fprintf(stderr, "bench: hc_pool_create failed\n");
        return -1;
    }
    hc_capture_set_pool(cap, pool);

    if (va && hc_va_import_pool(va, pool) != 0) {
        fprintf(stderr, "bench: hc_va_import_pool failed\n");
        hc_pool_destroy(pool);
        return -1;
    }
    printf("pool: %d buffer(s) %ux%u modifier=%s\n",
           pool->n, pool->width, pool->height,
           pool->n > 0 ? hc_mod_str(pool->buf[0].modifier) : "n/a");
    return 0;
}

/* ------------------------------------------------------------------ main */

int main(int argc, char **argv)
{
    /* Keep stdout in step with the diagnostics on stderr when redirected. */
    setvbuf(stdout, NULL, _IOLBF, 0);

    struct opts opt;
    int rc = parse_args(argc, argv, &opt);
    if (rc != 0)
        return rc > 0 ? 0 : 2;

    struct hc_capture *cap = NULL;
    struct hc_va      *va  = NULL;
    struct hc_enc     *enc = NULL;
    struct hc_pool     pool;
    FILE              *out = NULL;
    int                drm_fd = -1;
    int                status = 1;
    bool               pool_live = false;

    memset(&pool, 0, sizeof pool);

    struct hc_hist h_cap, h_vpp, h_enc, h_tot;
    int hcap = (int)(opt.seconds * opt.fps) + 256;
    if (hcap < 4096)
        hcap = 4096;
    hc_hist_init(&h_cap, "capture", hcap);
    hc_hist_init(&h_vpp, "vpp",     hcap);
    hc_hist_init(&h_enc, "encode",  hcap);
    hc_hist_init(&h_tot, "total",   hcap);

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);

    printf("== hyprcast-bench ==\n");
    printf("wire %ux%u @ %u fps, %u bps, gop %u%s%s, %u s\n",
           opt.width, opt.height, opt.fps, opt.bitrate, opt.gop,
           opt.low_power ? ", low-power" : "",
           opt.cursors ? ", cursors" : "", opt.seconds);

    if (opt.out_path) {
        out = fopen(opt.out_path, "wb");
        if (!out) {
            fprintf(stderr, "bench: open '%s': %s\n",
                    opt.out_path, strerror(errno));
            goto done;
        }
    }

    drm_fd = open(HC_DRM_NODE, O_RDWR | O_CLOEXEC);
    if (drm_fd < 0) {
        fprintf(stderr, "bench: open %s: %s\n", HC_DRM_NODE, strerror(errno));
        goto done;
    }

    cap = hc_capture_open(opt.output, opt.cursors);
    if (!cap) {
        fprintf(stderr, "bench: hc_capture_open failed\n");
        goto done;
    }

    va = hc_va_open(drm_fd);
    if (!va) {
        fprintf(stderr, "bench: hc_va_open failed\n");
        goto done;
    }

    if (build_pool(cap, va, &pool, drm_fd) != 0)
        goto done;
    pool_live = true;

    if (hc_va_vpp_init(va, opt.width, opt.height) != 0) {
        fprintf(stderr, "bench: hc_va_vpp_init failed\n");
        goto done;
    }

    struct hc_enc_cfg cfg = {
        .width       = opt.width,
        .height      = opt.height,
        .fps         = opt.fps,
        .bitrate_bps = opt.bitrate,
        .gop         = opt.gop,
        .low_power   = opt.low_power,
        .qp          = opt.qp,
    };
    enc = hc_enc_open(va, &cfg);
    if (!enc) {
        fprintf(stderr, "bench: hc_enc_open failed\n");
        goto done;
    }

    /* ------------------------------------------------------------- loop */

    uint64_t run_ns   = (uint64_t)opt.seconds * 1000000000ull;
    uint64_t start    = hc_now_ns();
    uint64_t deadline = start + run_ns;

    uint64_t frames = 0, ok_frames = 0, failed = 0, dups = 0;
    uint64_t bytes = 0, packets = 0, idrs = 0;
    uint64_t prev_pres = 0;
    bool     have_prev_pres = false;
    bool     timed_out = false;
    bool     rebuilds = false;
    unsigned nrebuild = 0;

    struct rusage ru0;
    getrusage(RUSAGE_SELF, &ru0);

    /*
     * Pipelined: one capture is always outstanding while the previous frame is
     * converted and encoded, so VPP+encode (~3.5 ms measured) hides inside the
     * ~13 ms the compositor takes to produce the next frame. Serially this
     * measured 52.2 fps with a 17.3 ms p50 against a 16.67 ms budget.
     */
    struct hc_inflight *tok = NULL;
    if (hc_capture_submit(cap, &tok) != 0) {
        fprintf(stderr, "bench: initial hc_capture_submit failed\n");
        goto report;
    }

    while (!g_stop && hc_now_ns() < deadline) {
        struct hc_frame fr;
        memset(&fr, 0, sizeof fr);

        uint64_t t0 = hc_now_ns();
        int crc = hc_capture_wait(cap, tok, &fr, HC_FRAME_TIMEOUT);
        tok = NULL;
        uint64_t t1 = hc_now_ns();
        frames++;

        /*
         * Reserve the buffer we are about to read before asking for the next
         * frame, or the compositor may pick this very bo and overwrite it
         * while VPP is still sampling it. hc_capture_wait released it on our
         * behalf the moment the frame resolved.
         */
        bool reserved = false;
        if (crc == 0 && fr.ok && fr.buf_index >= 0 && fr.buf_index < pool.n) {
            pool.buf[fr.buf_index].in_flight = true;
            reserved = true;
        }

        /* Start capturing N+1 now; everything below overlaps with it. */
        if (!g_stop && hc_now_ns() < deadline) {
            int src = hc_capture_submit(cap, &tok);
            if (src != 0 && src != -4)
                fprintf(stderr, "bench: hc_capture_submit failed (%d)\n", src);
        }

        if (crc != 0) {
            if (reserved)
                pool.buf[fr.buf_index].in_flight = false;
            /*
             * Neither ready nor failed inside the timeout is the Hyprland
             * screencopy permission prompt hanging. Never retry silently.
             */
            fprintf(stderr,
                    "bench: hc_capture_frame TIMED OUT after %d ms on frame "
                    "%" PRIu64 " (rc=%d) -- compositor sent neither ready nor "
                    "failed. This is the screencopy permission prompt. "
                    "Aborting.\n",
                    HC_FRAME_TIMEOUT, frames, crc);
            timed_out = true;
            break;
        }
        if (!fr.ok || fr.buf_index < 0 || fr.buf_index >= pool.n) {
            failed++;
            if (reserved)
                pool.buf[fr.buf_index].in_flight = false;
            fprintf(stderr, "bench: frame %" PRIu64 " failed (buf_index=%d)\n",
                    frames, fr.buf_index);
            if (hc_capture_constraints_changed(cap))
                goto rebuild;
            continue;
        }

        if (fr.have_presentation) {
            if (have_prev_pres && fr.presentation_ns == prev_pres)
                dups++;
            prev_pres = fr.presentation_ns;
            have_prev_pres = true;
        }

        VASurfaceID dst = VA_INVALID_ID;
        void *opaque = NULL;
        if (hc_enc_acquire(enc, &dst, &opaque) != 0) {
            if (reserved)
                pool.buf[fr.buf_index].in_flight = false;
            fprintf(stderr, "bench: hc_enc_acquire failed on frame "
                            "%" PRIu64 "\n", frames);
            break;
        }
        if (hc_va_vpp_run(va, pool.buf[fr.buf_index].va, dst) != 0) {
            if (reserved)
                pool.buf[fr.buf_index].in_flight = false;
            fprintf(stderr, "bench: hc_va_vpp_run failed on frame "
                            "%" PRIu64 "\n", frames);
            break;
        }
        /* VPP has read the capture buffer; hand it back to the session. */
        if (reserved)
            pool.buf[fr.buf_index].in_flight = false;
        uint64_t t2 = hc_now_ns();

        uint64_t pts = fr.have_presentation ? fr.presentation_ns : t1;
        bool force_idr = (ok_frames % opt.gop) == 0;
        if (hc_enc_submit(enc, opaque, pts, force_idr) != 0) {
            fprintf(stderr, "bench: hc_enc_submit failed on frame "
                            "%" PRIu64 "\n", frames);
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
                fprintf(stderr, "bench: hc_enc_receive failed (%d)\n", r);
                goto drained;
            }
            if (size > 0 && data) {
                bytes += (uint64_t)size;
                packets++;
                if (is_idr)
                    idrs++;
                if (out && fwrite(data, 1, (size_t)size, out) != (size_t)size) {
                    fprintf(stderr, "bench: write to '%s': %s\n",
                            opt.out_path, strerror(errno));
                    goto drained;
                }
            }
        }
drained:
        {
            uint64_t t3 = hc_now_ns();
            hc_hist_add(&h_cap, t1 - t0);
            hc_hist_add(&h_vpp, t2 - t1);
            hc_hist_add(&h_enc, t3 - t2);
            hc_hist_add(&h_tot, t3 - t0);
        }

        if (!hc_capture_constraints_changed(cap))
            continue;
rebuild:
        printf("bench: constraints changed -- rebuilding pool\n");
        if (tok) {
            struct hc_frame drop;
            hc_capture_wait(cap, tok, &drop, HC_FRAME_TIMEOUT);
            tok = NULL;
        }
        hc_pool_destroy(&pool);
        pool_live = false;
        if (build_pool(cap, va, &pool, drm_fd) != 0) {
            fprintf(stderr, "bench: pool rebuild failed\n");
            break;
        }
        pool_live = true;
        rebuilds = true;
        nrebuild++;
        if (hc_capture_submit(cap, &tok) != 0) {
            fprintf(stderr, "bench: re-priming capture after rebuild failed\n");
            break;
        }
    }

    if (tok) {
        struct hc_frame drop;
        hc_capture_wait(cap, tok, &drop, HC_FRAME_TIMEOUT);
        tok = NULL;
    }

report:;

    uint64_t wall = hc_now_ns() - start;

    struct rusage ru1;
    getrusage(RUSAGE_SELF, &ru1);

    /* ---------------------------------------------------------- report */

    double wall_s = (double)wall / 1e9;
    if (wall_s <= 0.0)
        wall_s = 1e-9;

    double utime = (double)(ru1.ru_utime.tv_sec - ru0.ru_utime.tv_sec) +
                   (double)(ru1.ru_utime.tv_usec - ru0.ru_utime.tv_usec) / 1e6;
    double stime = (double)(ru1.ru_stime.tv_sec - ru0.ru_stime.tv_sec) +
                   (double)(ru1.ru_stime.tv_usec - ru0.ru_stime.tv_usec) / 1e6;
    double cpu_pct = (utime + stime) / wall_s * 100.0;

    double fps_sustained = (double)ok_frames / wall_s;
    double dup_ratio = ok_frames ? (double)dups / (double)ok_frames : 0.0;
    double mbits = (double)bytes * 8.0 / 1e6;
    double mbps = mbits / wall_s;
    double p99_total = hc_hist_pct(&h_tot, 99.0);

    printf("\n== histograms ==\n");
    hc_hist_print(&h_cap);
    hc_hist_print(&h_vpp);
    hc_hist_print(&h_enc);
    hc_hist_print(&h_tot);

    printf("\n== summary ==\n");
    printf("wall            : %.3f s\n", wall_s);
    printf("frames          : %" PRIu64 " attempted, %" PRIu64 " encoded, "
           "%" PRIu64 " failed\n", frames, ok_frames, failed);
    printf("sustained fps   : %.2f\n", fps_sustained);
    printf("dup frames      : %" PRIu64 " (%.2f%% -- presentation_ns did not "
           "advance)\n", dups, dup_ratio * 100.0);
    printf("bitstream       : %.2f Mbit in %" PRIu64 " packet(s) = %.3f Mbit/s\n",
           mbits, packets, mbps);
    printf("IDR frames      : %" PRIu64 "\n", idrs);
    printf("CPU             : %.2f%% of one core (user %.2fs sys %.2fs)\n",
           cpu_pct, utime, stime);
    if (rebuilds)
        printf("pool rebuilds   : %u\n", nrebuild);
    if (opt.out_path)
        printf("wrote           : %s\n", opt.out_path);

    uint64_t idr_expect = (uint64_t)opt.seconds * opt.fps / opt.gop;

    bool p_fps = fps_sustained >= 59.5;
    bool p_p99 = p99_total < 16.67;
    bool p_cpu = cpu_pct < 15.0;
    bool p_idr = idrs == idr_expect;
    bool all_ok = p_fps && p_p99 && p_cpu && p_idr && !timed_out;

    printf("\n== exit criteria ==\n");
    printf("[%s] sustained fps >= 59.5   : %.2f\n", p_fps ? "PASS" : "FAIL",
           fps_sustained);
    printf("[%s] total p99 < 16.67 ms    : %.2f ms\n", p_p99 ? "PASS" : "FAIL",
           p99_total);
    printf("[%s] CPU < 15%% of one core   : %.2f%%\n", p_cpu ? "PASS" : "FAIL",
           cpu_pct);
    printf("[%s] IDR == seconds*fps/gop  : %" PRIu64 " (expected %" PRIu64 ")\n",
           p_idr ? "PASS" : "FAIL", idrs, idr_expect);
    if (timed_out)
        printf("[FAIL] capture timed out -- run is not valid\n");
    printf("\n%s\n", all_ok ? "RESULT: PASS" : "RESULT: FAIL");

    status = all_ok ? 0 : 1;

done:
    if (enc)
        hc_enc_close(enc);
    if (pool_live)
        hc_pool_destroy(&pool);
    if (va)
        hc_va_close(va);
    if (cap)
        hc_capture_close(cap);
    if (out)
        fclose(out);
    if (drm_fd >= 0)
        close(drm_fd);

    hc_hist_free(&h_cap);
    hc_hist_free(&h_vpp);
    hc_hist_free(&h_enc);
    hc_hist_free(&h_tot);

    return status;
}
