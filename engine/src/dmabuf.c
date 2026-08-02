/*
 * hyprcast -- gbm dmabuf pool.
 *
 * Ported from reference/probes/probe.c (the allocation block, lines 206-225),
 * restructured around struct hc_pool.
 *
 * The single non-obvious rule here: iHD CANNOT import Y_TILED_CCS
 * (0x0100000000000004) -- vaCreateSurfaces fails with "resource allocation
 * failed". gbm_bo_create_with_modifiers2() fed Hyprland's full advertised
 * modifier list picks exactly that one. So the list is filtered down to
 * I915_FORMAT_MOD_Y_TILED (0x0100000000000002), DRM_FORMAT_MOD_INVALID is
 * dropped, and the plane count of every bo is hard-asserted to be 1 -- a
 * second plane means a compression-control-surface modifier slipped through
 * and every later VA call would fail in a much less obvious place.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <gbm.h>
#include <wayland-client.h>

#include "linux-dmabuf-v1-client-protocol.h"
#include "hc.h"

#define HC_MOD_LINEAR   0x0000000000000000ull
#define HC_MOD_Y_TILED  0x0100000000000002ull
#define HC_MOD_INVALID  0x00ffffffffffffffull

/* Append `m` to `dst` unless it is already there or the array is full. */
static void mod_push(uint64_t *dst, int *n, uint64_t m)
{
    for (int i = 0; i < *n; i++)
        if (dst[i] == m)
            return;
    if (*n < HC_MAX_MODS)
        dst[(*n)++] = m;
}

static void pool_release_buf(struct hc_buf *b)
{
    if (b->wl) {
        wl_buffer_destroy(b->wl);
        b->wl = NULL;
    }
    if (b->bo) {
        gbm_bo_destroy(b->bo);
        b->bo = NULL;
    }
    b->va = VA_INVALID_ID;
    b->in_flight = false;
}

int hc_pool_create(struct hc_pool *p, int drm_fd,
                   struct zwp_linux_dmabuf_v1 *dmabuf,
                   uint32_t w, uint32_t h, uint32_t fourcc,
                   const uint64_t *mods, int nmods, int count)
{
    char fb[5];

    if (!p || !dmabuf || w == 0 || h == 0 || count <= 0) {
        fprintf(stderr, "hc_pool_create: bad arguments (p=%p dmabuf=%p %ux%u count=%d)\n",
                (void *)p, (void *)dmabuf, w, h, count);
        return -1;
    }
    if (!mods || nmods <= 0) {
        fprintf(stderr, "hc_pool_create: empty modifier list; refusing to allocate "
                        "with an implicit modifier (the wl_buffer would carry INVALID)\n");
        return -1;
    }
    if (count > HC_MAX_BUFS)
        count = HC_MAX_BUFS;

    memset(p, 0, sizeof *p);
    p->drm_fd = drm_fd;
    p->width  = w;
    p->height = h;
    p->fourcc = fourcc;
    for (int i = 0; i < HC_MAX_BUFS; i++) {
        p->buf[i].va = VA_INVALID_ID;
        p->buf[i].in_flight = false;
    }

    /* Pin to Y_TILED. Never INVALID, never a CCS modifier. */
    uint64_t use[HC_MAX_MODS];
    int nuse = 0;
    for (int i = 0; i < nmods; i++)
        if (mods[i] == HC_MOD_Y_TILED)
            mod_push(use, &nuse, mods[i]);
    if (nuse == 0) {
        /* No Y_TILED on offer: LINEAR is the only other modifier iHD imports
         * reliably on Gen9.5. Anything else (CCS, INVALID) is refused. */
        for (int i = 0; i < nmods; i++)
            if (mods[i] == HC_MOD_LINEAR)
                mod_push(use, &nuse, mods[i]);
        if (nuse == 0) {
            fprintf(stderr, "hc_pool_create: neither I915_Y_TILED nor LINEAR in the "
                            "%d advertised modifier(s) for '%s'; refusing to allocate:\n",
                    nmods, hc_fourcc_str(fourcc, fb));
            for (int i = 0; i < nmods; i++)
                fprintf(stderr, "    %s\n", hc_mod_str(mods[i]));
            return -1;
        }
        fprintf(stderr, "hc_pool_create: I915_Y_TILED not advertised, falling back to LINEAR\n");
    }

    p->gbm = gbm_create_device(drm_fd);
    if (!p->gbm) {
        fprintf(stderr, "hc_pool_create: gbm_create_device(fd=%d) failed\n", drm_fd);
        return -1;
    }

    for (int i = 0; i < count; i++) {
        struct hc_buf *b = &p->buf[i];

        b->bo = gbm_bo_create_with_modifiers2(p->gbm, w, h, fourcc,
                                              use, (unsigned)nuse,
                                              GBM_BO_USE_RENDERING);
        if (!b->bo) {
            fprintf(stderr, "hc_pool_create: gbm_bo_create_with_modifiers2 failed for "
                            "buffer %d (%ux%u '%s', %d modifier(s))\n",
                    i, w, h, hc_fourcc_str(fourcc, fb), nuse);
            goto fail;
        }

        uint64_t mod = gbm_bo_get_modifier(b->bo);
        int planes = gbm_bo_get_plane_count(b->bo);
        if (planes != 1) {
            fprintf(stderr,
                    "hc_pool_create: FATAL -- bo %d came back with %d planes, modifier %s.\n"
                    "  A compression-control-surface modifier slipped through the filter.\n"
                    "  iHD cannot import it (\"resource allocation failed\") and every later\n"
                    "  VA call would fail somewhere far less obvious. Aborting now.\n",
                    i, planes, hc_mod_str(mod));
            fflush(stderr);
            abort();
        }

        b->modifier = mod;
        b->stride   = gbm_bo_get_stride_for_plane(b->bo, 0);
        b->offset   = gbm_bo_get_offset(b->bo, 0);
        b->va       = VA_INVALID_ID;
        b->in_flight = false;

        int pfd = gbm_bo_get_fd_for_plane(b->bo, 0);
        if (pfd < 0) {
            fprintf(stderr, "hc_pool_create: gbm_bo_get_fd_for_plane failed for buffer %d\n", i);
            goto fail;
        }

        struct zwp_linux_buffer_params_v1 *params = zwp_linux_dmabuf_v1_create_params(dmabuf);
        if (!params) {
            close(pfd);
            fprintf(stderr, "hc_pool_create: zwp_linux_dmabuf_v1_create_params failed\n");
            goto fail;
        }
        zwp_linux_buffer_params_v1_add(params, pfd, 0, b->offset, b->stride,
                                       (uint32_t)(mod >> 32),
                                       (uint32_t)(mod & 0xffffffffu));
        close(pfd);

        b->wl = zwp_linux_buffer_params_v1_create_immed(params, (int32_t)w, (int32_t)h,
                                                        fourcc, 0);
        zwp_linux_buffer_params_v1_destroy(params);
        if (!b->wl) {
            fprintf(stderr, "hc_pool_create: create_immed failed for buffer %d\n", i);
            goto fail;
        }
        p->n = i + 1;
    }

    return 0;

fail:
    hc_pool_destroy(p);
    return -1;
}

void hc_pool_destroy(struct hc_pool *p)
{
    if (!p)
        return;
    for (int i = 0; i < HC_MAX_BUFS; i++)
        pool_release_buf(&p->buf[i]);
    p->n = 0;
    if (p->gbm) {
        gbm_device_destroy(p->gbm);
        p->gbm = NULL;
    }
    /* drm_fd is owned by the caller; deliberately left open. */
}
