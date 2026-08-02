/*
 * hyprcast engine -- common helpers and the latency histogram.
 *
 * hc_hist keeps a reservoir of raw nanosecond samples so percentiles are
 * exact as long as the reservoir is large enough to hold the whole run
 * (bench.c sizes it that way). Once it overflows, Algorithm R replacement
 * keeps the retained sample set an unbiased draw of everything observed;
 * `total` still counts every observation.
 *
 * Nothing here allocates on the hot path except hc_hist_pct, which is only
 * ever called at teardown.
 */
#define _POSIX_C_SOURCE 200809L

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <inttypes.h>
#include <sys/types.h>

/* hc.h mentions this tag only inside a prototype's parameter list. */
struct zwp_linux_dmabuf_v1;

#include "hc.h"

/* ---------------------------------------------------------------- common */

uint64_t hc_now_ns(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t) != 0)
        return 0;
    return (uint64_t)t.tv_sec * 1000000000ull + (uint64_t)t.tv_nsec;
}

const char *hc_fourcc_str(uint32_t fourcc, char buf[5])
{
    buf[0] = (char)(fourcc & 0xffu);
    buf[1] = (char)((fourcc >> 8) & 0xffu);
    buf[2] = (char)((fourcc >> 16) & 0xffu);
    buf[3] = (char)((fourcc >> 24) & 0xffu);
    buf[4] = '\0';
    for (int i = 0; i < 4; i++)
        if (buf[i] < 0x20 || buf[i] > 0x7e)
            buf[i] = '?';
    return buf;
}

const char *hc_mod_str(uint64_t modifier)
{
    /*
     * Rotating scratch so several calls can appear in one printf argument
     * list -- the single-static-buffer version in probe.c aliases itself.
     */
    static char slot[4][32];
    static unsigned turn;

    switch (modifier) {
    case 0x0000000000000000ull: return "LINEAR";
    case 0x00ffffffffffffffull: return "INVALID";
    case 0x0100000000000001ull: return "I915_X_TILED";
    case 0x0100000000000002ull: return "I915_Y_TILED";
    case 0x0100000000000003ull: return "I915_Yf_TILED";
    case 0x0100000000000004ull: return "I915_Y_TILED_CCS";
    case 0x0100000000000005ull: return "I915_Yf_TILED_CCS";
    default: break;
    }

    char *b = slot[turn++ & 3u];
    snprintf(b, sizeof slot[0], "0x%016" PRIx64, modifier);
    return b;
}

/* ------------------------------------------------------------- histogram */

static uint64_t splitmix64(uint64_t x)
{
    x += 0x9e3779b97f4a7c15ull;
    x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ull;
    x = (x ^ (x >> 27)) * 0x94d049bb133111ebull;
    return x ^ (x >> 31);
}

static int cmp_u64(const void *a, const void *b)
{
    uint64_t x = *(const uint64_t *)a;
    uint64_t y = *(const uint64_t *)b;
    return (x > y) - (x < y);
}

void hc_hist_init(struct hc_hist *h, const char *name, int cap)
{
    if (!h)
        return;
    memset(h, 0, sizeof *h);
    h->name = name ? name : "(unnamed)";
    if (cap < 1)
        cap = 1;
    h->v = calloc((size_t)cap, sizeof *h->v);
    h->cap = h->v ? cap : 0;
    h->n = 0;
    h->total = 0;
}

void hc_hist_add(struct hc_hist *h, uint64_t ns)
{
    if (!h || !h->v || h->cap <= 0)
        return;

    uint64_t seen = h->total++;          /* 0-based index of this observation */

    if (h->n < h->cap) {
        h->v[h->n++] = ns;
        return;
    }
    /* Algorithm R: keep it with probability cap/(seen+1). */
    uint64_t j = splitmix64(seen ^ (ns << 1)) % (seen + 1ull);
    if (j < (uint64_t)h->cap)
        h->v[j] = ns;
}

double hc_hist_pct(struct hc_hist *h, double pct)
{
    if (!h || !h->v || h->n <= 0)
        return 0.0;

    size_t n = (size_t)h->n;
    uint64_t *scratch = malloc(n * sizeof *scratch);
    if (!scratch)
        return 0.0;
    memcpy(scratch, h->v, n * sizeof *scratch);
    qsort(scratch, n, sizeof *scratch, cmp_u64);

    if (pct < 0.0)
        pct = 0.0;
    if (pct > 100.0)
        pct = 100.0;

    double pos = pct / 100.0 * (double)n;
    size_t idx = (size_t)pos;
    if (idx >= n)
        idx = n - 1;

    double ms = (double)scratch[idx] / 1e6;
    free(scratch);
    return ms;
}

void hc_hist_print(struct hc_hist *h)
{
    if (!h)
        return;
    if (!h->v || h->n <= 0) {
        printf("%-10s n=0\n", h->name ? h->name : "(unnamed)");
        return;
    }

    char label[24];
    snprintf(label, sizeof label, "%s:", h->name ? h->name : "(unnamed)");

    printf("%-10s n=%-7" PRIu64 " p50=%6.2f p90=%6.2f p99=%6.2f max=%6.2f ms\n",
           label, h->total,
           hc_hist_pct(h, 50.0), hc_hist_pct(h, 90.0),
           hc_hist_pct(h, 99.0), hc_hist_pct(h, 100.0));
}

void hc_hist_free(struct hc_hist *h)
{
    if (!h)
        return;
    free(h->v);
    h->v = NULL;
    h->cap = 0;
    h->n = 0;
    h->total = 0;
}
