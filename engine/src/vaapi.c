/*
 * hyprcast engine -- VAAPI: display, zero-copy dmabuf import, BGRX -> NV12 VPP.
 *
 * Ported from reference/probes/vatest.c and reference/probes/vpptest.c, both
 * measured working on this box (VPP 1920x1080 BGRX -> NV12 = 0.881 ms/frame).
 *
 * Non-negotiable details, each of which cost hours to find:
 *   - vaSetDriverName(dpy, "iHD") MUST happen before vaInitialize, and the
 *     vendor string is asserted afterwards. i965 misreports RGB32 encode
 *     entrypoints on Gen9.5 and produces garbage.
 *   - DRM XR24 is byte-order BGRX. Importing as VA_FOURCC_RGBX succeeds and
 *     silently swaps red and blue, so we import as VA_FOURCC_BGRX with
 *     VA_RT_FORMAT_RGB32.
 *   - VADRMPRIMESurfaceDescriptor + VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2 is
 *     the only import path that carries a modifier.
 *   - close() the dmabuf fd AFTER vaCreateSurfaces; iHD dups it. Closing
 *     before leaks nothing but fails; not closing leaks one fd per surface
 *     per renegotiation.
 *   - A CCS modifier slipping through shows up as plane_count > 1 and then as
 *     "resource allocation failed" deep inside iHD, so we reject it here with
 *     a message that names the modifier.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <drm_fourcc.h>
#include <gbm.h>

#include <va/va.h>
#include <va/va_drm.h>
#include <va/va_drmcommon.h>
#include <va/va_str.h>
#include <va/va_vpp.h>

/* hc.h prototypes hc_pool_create() with an opaque Wayland type we never use
 * here; declaring it first keeps -Wpedantic from flagging the prototype. */
struct zwp_linux_dmabuf_v1;

#include "hc.h"

/* Written as ISO C11 variadic macros: the "" , ##__VA_ARGS__ trick is a GNU
 * extension and warning_level=3 turns on -Wpedantic. */
#define HC_LOG(...)  do {                    \
        fprintf(stderr, "[va] ");            \
        fprintf(stderr, __VA_ARGS__);        \
        fputc('\n', stderr);                 \
    } while (0)
#define HC_ERR(...)  do {                    \
        fprintf(stderr, "[va] ERROR: ");     \
        fprintf(stderr, __VA_ARGS__);        \
        fputc('\n', stderr);                 \
    } while (0)

/*
 * Every VA entry point in this file funnels through here so a failure always
 * names both the call and the driver's own error string.
 */
static int va_check(VAStatus st, const char *what)
{
    if (st == VA_STATUS_SUCCESS)
        return 0;
    HC_ERR("%s: %s (0x%x)", what, vaErrorStr(st), (unsigned)st);
    return -1;
}

struct hc_va {
    int         drm_fd;                    /* borrowed; the caller owns it */
    VADisplay   dpy;
    int         major, minor;

    /* Surfaces we imported, tracked independently of the pool so that
     * hc_va_close stays correct even if the caller tore the pool down first
     * (vaDestroySurfaces on a surface whose dmabuf is gone is fine -- iHD
     * holds its own dup of the fd). */
    VASurfaceID imported[HC_MAX_BUFS];
    int         n_imported;
    uint32_t    src_w, src_h;

    VAConfigID  vpp_cfg;
    VAContextID vpp_ctx;
    uint32_t    dst_w, dst_h;
    bool        vpp_ready;
    bool         vpp_sync;   /* HC_VPP_SYNC=1 restores the blocking sync */
};

/* ------------------------------------------------------------------ open */

struct hc_va *hc_va_open(int drm_fd)
{
    struct hc_va *v;
    VAStatus st;
    const char *vendor;
    char driver[] = "iHD";                 /* vaSetDriverName takes char*   */

    if (drm_fd < 0) {
        HC_ERR("hc_va_open: bad drm fd %d", drm_fd);
        return NULL;
    }

    v = calloc(1, sizeof *v);
    if (!v) {
        HC_ERR("hc_va_open: out of memory");
        return NULL;
    }
    v->drm_fd  = drm_fd;
    v->vpp_cfg = VA_INVALID_ID;
    v->vpp_ctx = VA_INVALID_ID;
    { const char *e = getenv("HC_VPP_SYNC"); v->vpp_sync = (e && *e == '1'); }
    for (int i = 0; i < HC_MAX_BUFS; i++)
        v->imported[i] = VA_INVALID_ID;

    v->dpy = vaGetDisplayDRM(drm_fd);
    if (!v->dpy || !vaDisplayIsValid(v->dpy)) {
        HC_ERR("vaGetDisplayDRM(fd=%d) returned no usable display", drm_fd);
        free(v);
        return NULL;
    }

    /* Must precede vaInitialize: it overrides driver probing entirely. */
    st = vaSetDriverName(v->dpy, driver);
    if (va_check(st, "vaSetDriverName(\"iHD\")") < 0) {
        vaTerminate(v->dpy);
        free(v);
        return NULL;
    }

    st = vaInitialize(v->dpy, &v->major, &v->minor);
    if (va_check(st, "vaInitialize") < 0) {
        vaTerminate(v->dpy);
        free(v);
        return NULL;
    }

    vendor = vaQueryVendorString(v->dpy);
    if (!vendor || !strstr(vendor, "iHD")) {
        HC_ERR("driver is \"%s\", not iHD. i965 misreports RGB32 encode "
               "entrypoints on Gen9.5 -- refusing to continue.",
               vendor ? vendor : "(null)");
        vaTerminate(v->dpy);
        free(v);
        return NULL;
    }

    HC_LOG("VA-API %d.%d, driver: %s", v->major, v->minor, vendor);
    return v;
}

VADisplay hc_va_display(struct hc_va *v)
{
    return v ? v->dpy : NULL;
}

/* ---------------------------------------------------------------- import */

static void release_imported(struct hc_va *v)
{
    if (v->n_imported > 0) {
        vaDestroySurfaces(v->dpy, v->imported, v->n_imported);
        for (int i = 0; i < v->n_imported; i++)
            v->imported[i] = VA_INVALID_ID;
        v->n_imported = 0;
    }
    v->src_w = v->src_h = 0;
}

/*
 * Import one gbm bo as a BGRX VA surface. Returns 0 and stores the surface in
 * *out on success. The dmabuf fd is closed on every path.
 */
static int import_bo(struct hc_va *v, struct gbm_bo *bo,
                     uint32_t w, uint32_t h, VASurfaceID *out)
{
    VADRMPRIMESurfaceDescriptor d;
    VASurfaceAttrib attr[2];
    VASurfaceID surf = VA_INVALID_ID;
    VAStatus st;
    off_t sz;
    int fd, planes;
    uint64_t mod;

    planes = gbm_bo_get_plane_count(bo);
    mod    = gbm_bo_get_modifier(bo);
    if (planes != 1) {
        HC_ERR("bo has %d planes (modifier %s / 0x%016llx) -- a compressed "
               "(CCS) modifier slipped into the allocation; iHD cannot import "
               "it. Pin the modifier list to I915_FORMAT_MOD_Y_TILED.",
               planes, hc_mod_str(mod), (unsigned long long)mod);
        return -1;
    }

    fd = gbm_bo_get_fd_for_plane(bo, 0);
    if (fd < 0) {
        HC_ERR("gbm_bo_get_fd_for_plane failed: %s", strerror(errno));
        return -1;
    }

    /* The real dmabuf size; stride*height under-reports for tiled buffers
     * with padding and iHD rejects the import. */
    sz = lseek(fd, 0, SEEK_END);
    if (sz <= 0)
        sz = (off_t)gbm_bo_get_stride_for_plane(bo, 0) * (off_t)h;

    memset(&d, 0, sizeof d);
    d.fourcc      = VA_FOURCC_BGRX;        /* DRM XR24 is byte-order BGRX */
    d.width       = w;
    d.height      = h;
    d.num_objects = 1;
    d.objects[0].fd                  = fd;
    d.objects[0].size                = (uint32_t)sz;
    d.objects[0].drm_format_modifier = mod;
    d.num_layers  = 1;
    d.layers[0].drm_format   = DRM_FORMAT_XRGB8888;
    d.layers[0].num_planes   = 1;
    d.layers[0].object_index[0] = 0;
    d.layers[0].offset[0]    = gbm_bo_get_offset(bo, 0);
    d.layers[0].pitch[0]     = gbm_bo_get_stride_for_plane(bo, 0);

    memset(attr, 0, sizeof attr);
    attr[0].type           = VASurfaceAttribMemoryType;
    attr[0].flags          = VA_SURFACE_ATTRIB_SETTABLE;
    attr[0].value.type     = VAGenericValueTypeInteger;
    attr[0].value.value.i  = VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2;
    attr[1].type           = VASurfaceAttribExternalBufferDescriptor;
    attr[1].flags          = VA_SURFACE_ATTRIB_SETTABLE;
    attr[1].value.type     = VAGenericValueTypePointer;
    attr[1].value.value.p  = &d;

    st = vaCreateSurfaces(v->dpy, VA_RT_FORMAT_RGB32, w, h, &surf, 1, attr, 2);

    /* AFTER vaCreateSurfaces, unconditionally: iHD dups the fd. */
    close(fd);

    if (va_check(st, "vaCreateSurfaces(import BGRX)") < 0) {
        HC_ERR("  import was %ux%u stride=%u offset=%u modifier=%s",
               w, h, d.layers[0].pitch[0], d.layers[0].offset[0],
               hc_mod_str(mod));
        return -1;
    }

    *out = surf;
    return 0;
}

int hc_va_import_pool(struct hc_va *v, struct hc_pool *p)
{
    char fcc[5];
    int i;

    if (!v || !p) {
        HC_ERR("hc_va_import_pool: null argument");
        return -1;
    }
    if (p->n <= 0 || p->n > HC_MAX_BUFS) {
        HC_ERR("hc_va_import_pool: pool has %d buffers", p->n);
        return -1;
    }
    if (p->width == 0 || p->height == 0) {
        HC_ERR("hc_va_import_pool: pool is %ux%u -- buffer_size arrives last "
               "in the constraints round; commit only on `done`.",
               p->width, p->height);
        return -1;
    }
    if (p->fourcc != DRM_FORMAT_XRGB8888) {
        HC_ERR("hc_va_import_pool: pool fourcc is %s, expected XR24",
               hc_fourcc_str(p->fourcc, fcc));
        return -1;
    }

    /* Renegotiation: drop whatever we imported last time first. */
    release_imported(v);

    for (i = 0; i < p->n; i++) {
        if (!p->buf[i].bo) {
            HC_ERR("hc_va_import_pool: buffer %d has no bo", i);
            goto fail;
        }
        if (import_bo(v, p->buf[i].bo, p->width, p->height,
                      &v->imported[i]) < 0) {
            HC_ERR("hc_va_import_pool: buffer %d of %d failed", i, p->n);
            goto fail;
        }
        v->n_imported = i + 1;
        p->buf[i].va  = v->imported[i];
    }

    v->src_w = p->width;
    v->src_h = p->height;
    HC_LOG("imported %d bo%s as %ux%u BGRX surfaces (modifier %s)",
           p->n, p->n == 1 ? "" : "s", p->width, p->height,
           hc_mod_str(p->buf[0].modifier));
    return 0;

fail:
    for (int j = 0; j < p->n; j++)
        p->buf[j].va = VA_INVALID_ID;
    release_imported(v);
    return -1;
}

/* ------------------------------------------------------------------- vpp */

int hc_va_vpp_init(struct hc_va *v, uint32_t dst_w, uint32_t dst_h)
{
    VAStatus st;
    VAEntrypoint *eps = NULL;
    int n_eps, found = 0;

    if (!v) {
        HC_ERR("hc_va_vpp_init: null display");
        return -1;
    }
    if (dst_w == 0 || dst_h == 0) {
        HC_ERR("hc_va_vpp_init: destination is %ux%u", dst_w, dst_h);
        return -1;
    }
    if (v->vpp_ready) {
        if (v->dst_w == dst_w && v->dst_h == dst_h)
            return 0;
        /* Wire size retune: rebuild the context, nothing else. */
        vaDestroyContext(v->dpy, v->vpp_ctx);
        vaDestroyConfig(v->dpy, v->vpp_cfg);
        v->vpp_ctx   = VA_INVALID_ID;
        v->vpp_cfg   = VA_INVALID_ID;
        v->vpp_ready = false;
    }

    n_eps = vaMaxNumEntrypoints(v->dpy);
    if (n_eps > 0) {
        eps = calloc((size_t)n_eps, sizeof *eps);
        if (!eps) {
            HC_ERR("hc_va_vpp_init: out of memory");
            return -1;
        }
        if (vaQueryConfigEntrypoints(v->dpy, VAProfileNone,
                                     eps, &n_eps) == VA_STATUS_SUCCESS) {
            for (int i = 0; i < n_eps; i++)
                if (eps[i] == VAEntrypointVideoProc)
                    found = 1;
        }
        free(eps);
    }
    if (!found) {
        HC_ERR("driver reports no VAEntrypointVideoProc for VAProfileNone -- "
               "there is no VPP on this device");
        return -1;
    }

    st = vaCreateConfig(v->dpy, VAProfileNone, VAEntrypointVideoProc,
                        NULL, 0, &v->vpp_cfg);
    if (va_check(st, "vaCreateConfig(VideoProc)") < 0) {
        v->vpp_cfg = VA_INVALID_ID;
        return -1;
    }

    /*
     * No render targets: the NV12 destinations come from libavcodec's hwframe
     * pool and are not known here. VPP contexts accept a null target list.
     */
    st = vaCreateContext(v->dpy, v->vpp_cfg, (int)dst_w, (int)dst_h,
                         VA_PROGRESSIVE, NULL, 0, &v->vpp_ctx);
    if (va_check(st, "vaCreateContext(VideoProc)") < 0) {
        vaDestroyConfig(v->dpy, v->vpp_cfg);
        v->vpp_cfg = VA_INVALID_ID;
        v->vpp_ctx = VA_INVALID_ID;
        return -1;
    }

    v->dst_w     = dst_w;
    v->dst_h     = dst_h;
    v->vpp_ready = true;
    HC_LOG("VPP ready: BGRX %ux%u -> NV12 %ux%u",
           v->src_w, v->src_h, dst_w, dst_h);
    return 0;
}

int hc_va_vpp_run(struct hc_va *v, VASurfaceID src, VASurfaceID dst)
{
    VAProcPipelineParameterBuffer p;
    VARectangle src_r, dst_r;
    VABufferID pb = VA_INVALID_ID;
    VAStatus st;
    int rc = -1;

    if (!v || !v->vpp_ready) {
        HC_ERR("hc_va_vpp_run: VPP not initialised");
        return -1;
    }
    if (src == VA_INVALID_ID || dst == VA_INVALID_ID) {
        HC_ERR("hc_va_vpp_run: invalid surface (src=0x%x dst=0x%x)",
               (unsigned)src, (unsigned)dst);
        return -1;
    }

    src_r.x = 0; src_r.y = 0;
    src_r.width  = (uint16_t)v->src_w;
    src_r.height = (uint16_t)v->src_h;
    dst_r.x = 0; dst_r.y = 0;
    dst_r.width  = (uint16_t)v->dst_w;
    dst_r.height = (uint16_t)v->dst_h;

    memset(&p, 0, sizeof p);
    p.surface = src;
    /* A null region means "the whole surface"; use it if the source size was
     * never recorded (hc_va_import_pool not called for this surface). */
    p.surface_region = (v->src_w && v->src_h) ? &src_r : NULL;
    p.output_region  = &dst_r;
    p.surface_color_standard = VAProcColorStandardNone;   /* RGB in  */
    p.output_color_standard  = VAProcColorStandardBT709;  /* 720p out */
    p.output_color_properties.color_range = VA_SOURCE_RANGE_REDUCED;
    p.filter_flags = VA_FRAME_PICTURE | VA_FILTER_SCALING_HQ;

    st = vaCreateBuffer(v->dpy, v->vpp_ctx, VAProcPipelineParameterBufferType,
                        sizeof p, 1, &p, &pb);
    if (va_check(st, "vaCreateBuffer(VPP pipeline)") < 0)
        return -1;

    st = vaBeginPicture(v->dpy, v->vpp_ctx, dst);
    if (va_check(st, "vaBeginPicture(VPP)") < 0)
        goto out;

    st = vaRenderPicture(v->dpy, v->vpp_ctx, &pb, 1);
    if (va_check(st, "vaRenderPicture(VPP)") < 0) {
        vaEndPicture(v->dpy, v->vpp_ctx);
        goto out;
    }

    st = vaEndPicture(v->dpy, v->vpp_ctx);
    if (va_check(st, "vaEndPicture(VPP)") < 0)
        goto out;

    /*
     * Deliberately NOT syncing here by default.
     *
     * vaSyncSurface blocks the CPU until the GPU has retired the VPP, then the
     * encoder submits and blocks again -- two full round-trips per frame on a
     * part that only has to do ~3 ms of work. VA-API tracks the dependency on
     * `dst` internally, so the encode is ordered after the VPP without us
     * stalling; this is why ffmpeg's own vf_scale_vaapi does not sync either.
     *
     * Measured on UHD 620, 1080p capture -> 720p60 encode, pipelined:
     *   sync   : vpp p50 2.76 ms, encode p50 5.53 ms, 39.7 fps
     *   no sync: see STATUS.md
     * Set HC_VPP_SYNC=1 to restore the blocking behaviour for A/B.
     */
    if (v->vpp_sync) {
        st = vaSyncSurface(v->dpy, dst);
        if (va_check(st, "vaSyncSurface(VPP dst)") < 0)
            goto out;
    }

    rc = 0;
out:
    /* iHD frees pipeline buffers itself on vaRenderPicture; destroying an
     * already-freed id just returns INVALID_BUFFER, so the status is ignored
     * on purpose. Drivers without that behaviour need this call. */
    if (pb != VA_INVALID_ID)
        (void)vaDestroyBuffer(v->dpy, pb);
    return rc;
}

/* ----------------------------------------------------------------- close */

void hc_va_close(struct hc_va *v)
{
    if (!v)
        return;

    release_imported(v);

    if (v->vpp_ctx != VA_INVALID_ID)
        vaDestroyContext(v->dpy, v->vpp_ctx);
    if (v->vpp_cfg != VA_INVALID_ID)
        vaDestroyConfig(v->dpy, v->vpp_cfg);
    if (v->dpy)
        vaTerminate(v->dpy);

    /* drm_fd belongs to the caller. */
    free(v);
}
