// hyprcast capture-path probe: enumerate ext-image-copy-capture constraints,
// allocate GBM dmabufs, run a real capture loop, report pacing.
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <errno.h>
#include <poll.h>
#include <sys/types.h>
#include <sys/sysmacros.h>
#include <xf86drm.h>
#include <gbm.h>
#include <wayland-client.h>
#include "ext-image-capture-source-v1-client-protocol.h"
#include "ext-image-copy-capture-v1-client-protocol.h"
#include "linux-dmabuf-v1-client-protocol.h"

#define MAXFMT 64
#define MAXMOD 128
#define NBUF 8

static struct wl_display *dpy;
static struct wl_registry *reg;
static struct ext_output_image_capture_source_manager_v1 *src_mgr;
static struct ext_image_copy_capture_manager_v1 *cap_mgr;
static struct zwp_linux_dmabuf_v1 *dmabuf;
static struct wl_output *output;
static uint32_t output_name_id;
static struct wl_output *outs[8]; static uint32_t outnames[8]; static char outdesc[8][128]; static int nout;
static const char *want_output;
static void o_geom(void *d,struct wl_output*o,int32_t x,int32_t y,int32_t pw,int32_t ph,int32_t sp,const char*mk,const char*md,int32_t tr){(void)d;(void)o;(void)x;(void)y;(void)pw;(void)ph;(void)sp;(void)mk;(void)md;(void)tr;}
static void o_mode(void *d,struct wl_output*o,uint32_t f,int32_t w,int32_t h,int32_t r){(void)d;(void)o;(void)f;(void)w;(void)h;(void)r;}
static void o_done(void *d,struct wl_output*o){(void)d;(void)o;}
static void o_scale(void *d,struct wl_output*o,int32_t s){(void)d;(void)o;(void)s;}
static void o_name(void *d,struct wl_output*o,const char*n){(void)o; int i=(int)(long)d; snprintf(outdesc[i],128,"%s",n);}
static void o_desc(void *d,struct wl_output*o,const char*n){(void)d;(void)o;(void)n;}
static const struct wl_output_listener out_l={o_geom,o_mode,o_done,o_scale,o_name,o_desc};

struct fmt { uint32_t fourcc; uint64_t mods[MAXMOD]; int nmod; };
static struct fmt fmts[MAXFMT]; static int nfmt;
static uint32_t shmfmts[MAXFMT]; static int nshm;
static uint32_t bw, bh;
static dev_t dmabuf_dev; static int have_dev;
static int constraints_done, constraints_rounds;

static uint64_t now_ns(void) {
	struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
	return (uint64_t)t.tv_sec * 1000000000ull + t.tv_nsec;
}
static const char *fourcc_str(uint32_t f, char *b) {
	b[0]=f&0xff; b[1]=(f>>8)&0xff; b[2]=(f>>16)&0xff; b[3]=(f>>24)&0xff; b[4]=0; return b;
}
static const char *mod_str(uint64_t m) {
	static char buf[64];
	if (m == 0) return "LINEAR";
	if (m == 0x00ffffffffffffffull) return "INVALID";
	if (m == 0x0100000000000001ull) return "I915_X_TILED";
	if (m == 0x0100000000000002ull) return "I915_Y_TILED";
	if (m == 0x0100000000000004ull) return "I915_Y_TILED_CCS";
	snprintf(buf, sizeof buf, "0x%016lx", (unsigned long)m); return buf;
}

/* ---- session constraint events ---- */
static void s_buffer_size(void *d, struct ext_image_copy_capture_session_v1 *s, uint32_t w, uint32_t h) {
	(void)d;(void)s; bw = w; bh = h; printf("  [ev] buffer_size %ux%u\n", w, h);
}
static void s_shm_format(void *d, struct ext_image_copy_capture_session_v1 *s, uint32_t f) {
	(void)d;(void)s; char b[5];
	printf("  [ev] shm_format  %u ('%s')\n", f, fourcc_str(f==0?0x34325241:(f==1?0x34325258:f), b));
	if (nshm < MAXFMT) shmfmts[nshm++] = f;
}
static void s_dmabuf_device(void *d, struct ext_image_copy_capture_session_v1 *s, struct wl_array *arr) {
	(void)d;(void)s; dev_t dv; memcpy(&dv, arr->data, sizeof dv); dmabuf_dev = dv; have_dev = 1;
	printf("  [ev] dmabuf_device dev_t=%lu (major %u minor %u)  arr_size=%zu\n",
	       (unsigned long)dv, major(dv), minor(dv), arr->size);
	drmDevicePtr dev = NULL;
	if (drmGetDeviceFromDevId(dv, 0, &dev) == 0) {
		for (int i = 0; i < DRM_NODE_MAX; i++)
			if (dev->available_nodes & (1 << i)) printf("        node[%d] = %s\n", i, dev->nodes[i]);
		drmFreeDevice(&dev);
	} else printf("        drmGetDeviceFromDevId failed: %s\n", strerror(errno));
}
static void s_dmabuf_format(void *d, struct ext_image_copy_capture_session_v1 *s, uint32_t f, struct wl_array *arr) {
	(void)d;(void)s; char b[5];
	int n = arr->size / 8; uint64_t *m = arr->data;
	printf("  [ev] dmabuf_format 0x%08x '%s'  %d modifier(s):", f, fourcc_str(f,b), n);
	for (int i = 0; i < n; i++) printf(" %s", mod_str(m[i]));
	printf("\n");
	if (nfmt < MAXFMT) {
		fmts[nfmt].fourcc = f; fmts[nfmt].nmod = n > MAXMOD ? MAXMOD : n;
		memcpy(fmts[nfmt].mods, m, fmts[nfmt].nmod * 8); nfmt++;
	}
}
static void s_done(void *d, struct ext_image_copy_capture_session_v1 *s) {
	(void)d;(void)s; constraints_done = 1; constraints_rounds++;
	printf("  [ev] done  (constraints round #%d)\n", constraints_rounds);
}
static void s_stopped(void *d, struct ext_image_copy_capture_session_v1 *s) {
	(void)d;(void)s; printf("  [ev] STOPPED\n"); exit(3);
}
static const struct ext_image_copy_capture_session_v1_listener sess_l = {
	.buffer_size = s_buffer_size, .shm_format = s_shm_format, .dmabuf_device = s_dmabuf_device,
	.dmabuf_format = s_dmabuf_format, .done = s_done, .stopped = s_stopped,
};

/* ---- frame events ---- */
static int f_state; /* 0 pending, 1 ready, 2 failed */
static uint64_t f_pres; static int f_ndmg; static uint32_t f_xform; static int f_have_pres;
static void fr_transform(void *d, struct ext_image_copy_capture_frame_v1 *f, uint32_t t) {
	(void)d;(void)f; f_xform = t;
}
static void fr_damage(void *d, struct ext_image_copy_capture_frame_v1 *f, int32_t x, int32_t y, int32_t w, int32_t h) {
	(void)d;(void)f; if (f_ndmg < 4) printf("      damage rect: %d,%d %dx%d\n",x,y,w,h); f_ndmg++;
}
static void fr_pres(void *d, struct ext_image_copy_capture_frame_v1 *f, uint32_t hi, uint32_t lo, uint32_t ns) {
	(void)d;(void)f; f_pres = (((uint64_t)hi << 32) | lo) * 1000000000ull + ns; f_have_pres = 1;
}
static void fr_ready(void *d, struct ext_image_copy_capture_frame_v1 *f) { (void)d;(void)f; f_state = 1; }
static void fr_failed(void *d, struct ext_image_copy_capture_frame_v1 *f, uint32_t r) {
	(void)d;(void)f; f_state = 2; printf("  [ev] FAILED reason=%u\n", r);
}
static const struct ext_image_copy_capture_frame_v1_listener frame_l = {
	.transform = fr_transform, .damage = fr_damage, .presentation_time = fr_pres,
	.ready = fr_ready, .failed = fr_failed,
};

/* ---- registry ---- */
static void g_add(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t ver) {
	(void)d;
	if (!strcmp(iface, "ext_output_image_capture_source_manager_v1"))
		src_mgr = wl_registry_bind(r, name, &ext_output_image_capture_source_manager_v1_interface, 1);
	else if (!strcmp(iface, "ext_image_copy_capture_manager_v1")) {
		printf("compositor advertises ext_image_copy_capture_manager_v1 version %u\n", ver);
		cap_mgr = wl_registry_bind(r, name, &ext_image_copy_capture_manager_v1_interface, ver > 1 ? 1 : ver);
	} else if (!strcmp(iface, "zwp_linux_dmabuf_v1")) {
		printf("compositor advertises zwp_linux_dmabuf_v1 version %u\n", ver);
		dmabuf = wl_registry_bind(r, name, &zwp_linux_dmabuf_v1_interface, ver > 4 ? 4 : ver);
	} else if (!strcmp(iface, "wl_output")) {
		struct wl_output *o = wl_registry_bind(r, name, &wl_output_interface, ver > 4 ? 4 : ver);
		if (nout < 8) { outs[nout] = o; outnames[nout] = name; wl_output_add_listener(o, &out_l, (void*)(long)nout); nout++; }
	}
}
static void g_rem(void *d, struct wl_registry *r, uint32_t n) { (void)d;(void)r;(void)n;
	printf("  [registry] global REMOVED name=%u%s\n", n, n==output_name_id?"  <-- our wl_output!":"");
}
static const struct wl_registry_listener reg_l = { g_add, g_rem };

int main(int argc, char **argv) {
	int nframes = argc > 1 ? atoi(argv[1]) : 60;
	int nbuf = argc > 2 ? atoi(argv[2]) : 3;
	if (nbuf > NBUF) nbuf = NBUF;

	dpy = wl_display_connect(NULL);
	if (!dpy) { fprintf(stderr, "no display\n"); return 1; }
	reg = wl_display_get_registry(dpy);
	wl_registry_add_listener(reg, &reg_l, NULL);
	want_output = getenv("OUTPUT");
	wl_display_roundtrip(dpy); wl_display_roundtrip(dpy);
	printf("outputs seen: %d\n", nout);
	for (int i=0;i<nout;i++) printf("   [%d] %s (global %u)\n", i, outdesc[i], outnames[i]);
	for (int i=0;i<nout;i++) if (!want_output || strstr(outdesc[i], want_output)) { output=outs[i]; output_name_id=outnames[i]; printf("using output: %s\n", outdesc[i]); break; }
	if (!src_mgr || !cap_mgr || !dmabuf || !output) {
		fprintf(stderr, "missing globals: src_mgr=%p cap=%p dmabuf=%p out=%p\n",
		        (void*)src_mgr,(void*)cap_mgr,(void*)dmabuf,(void*)output); return 1;
	}
	printf("\n== creating source + session ==\n");
	struct ext_image_capture_source_v1 *src =
		ext_output_image_capture_source_manager_v1_create_source(src_mgr, output);
	struct ext_image_copy_capture_session_v1 *sess =
		ext_image_copy_capture_manager_v1_create_session(cap_mgr, src, getenv("CURSORS")?0xFF:0);
	ext_image_copy_capture_session_v1_add_listener(sess, &sess_l, NULL);
	wl_display_roundtrip(dpy);
	if (!constraints_done) { wl_display_roundtrip(dpy); }
	printf("== constraints: %ux%u, %d dmabuf format(s), %d shm format(s), dev=%s ==\n\n",
	       bw, bh, nfmt, nshm, have_dev ? "yes" : "NO");
	if (!nfmt) { fprintf(stderr, "no dmabuf formats advertised\n"); return 2; }

	/* pick XRGB8888 if present else first */
	int pick = 0;
	for (int i = 0; i < nfmt; i++) if (fmts[i].fourcc == GBM_FORMAT_XRGB8888) { pick = i; break; }
	char fb[5];
	printf("chose format '%s' (0x%08x)\n", fourcc_str(fmts[pick].fourcc, fb), fmts[pick].fourcc);

	int fd = open("/dev/dri/renderD128", O_RDWR | O_CLOEXEC);
	if (fd < 0) { perror("open renderD128"); return 1; }
	struct gbm_device *gbm = gbm_create_device(fd);
	if (!gbm) { fprintf(stderr, "gbm_create_device failed\n"); return 1; }
	printf("gbm backend: %s\n", gbm_device_get_backend_name(gbm));

	/* filter out INVALID for allocation */
	uint64_t mods[MAXMOD]; int nm = 0;
	const char *fm = getenv("MOD");
	if (fm) {
		uint64_t want = !strcmp(fm,"linear")?0ull: !strcmp(fm,"xtiled")?0x0100000000000001ull:
		                !strcmp(fm,"ytiled")?0x0100000000000002ull:0x0100000000000004ull;
		for (int i = 0; i < fmts[pick].nmod; i++) if (fmts[pick].mods[i]==want) mods[nm++]=want;
		printf("MOD=%s forced -> %s in advertised list\n", fm, nm?"present":"ABSENT");
	} else for (int i = 0; i < fmts[pick].nmod; i++)
		if (fmts[pick].mods[i] != 0x00ffffffffffffffull) mods[nm++] = fmts[pick].mods[i];
	printf("allocating with %d modifier(s) (INVALID filtered out)\n", nm);

	struct gbm_bo *bos[NBUF]; struct wl_buffer *wbufs[NBUF];
	for (int i = 0; i < nbuf; i++) {
		bos[i] = nm ? gbm_bo_create_with_modifiers2(gbm, bw, bh, fmts[pick].fourcc, mods, nm, GBM_BO_USE_RENDERING)
		            : gbm_bo_create(gbm, bw, bh, fmts[pick].fourcc, GBM_BO_USE_RENDERING);
		if (!bos[i]) { fprintf(stderr, "gbm_bo_create[%d] failed\n", i); return 1; }
		uint64_t m = gbm_bo_get_modifier(bos[i]);
		int planes = gbm_bo_get_plane_count(bos[i]);
		if (i == 0) printf("bo: %ux%u modifier=%s planes=%d\n", bw, bh, mod_str(m), planes);
		struct zwp_linux_buffer_params_v1 *p = zwp_linux_dmabuf_v1_create_params(dmabuf);
		for (int pl = 0; pl < planes; pl++) {
			int pfd = gbm_bo_get_fd_for_plane(bos[i], pl);
			zwp_linux_buffer_params_v1_add(p, pfd, pl,
				gbm_bo_get_offset(bos[i], pl), gbm_bo_get_stride_for_plane(bos[i], pl),
				(uint32_t)(m >> 32), (uint32_t)(m & 0xffffffff));
			close(pfd);
		}
		wbufs[i] = zwp_linux_buffer_params_v1_create_immed(p, bw, bh, fmts[pick].fourcc, 0);
		zwp_linux_buffer_params_v1_destroy(p);
	}
	wl_display_roundtrip(dpy);
	printf("allocated %d dmabuf wl_buffers OK\n\n== capture loop (%d frames) ==\n", nbuf, nframes);

	uint64_t t0 = now_ns(), prev_ready = 0, prev_pres = 0;
	uint64_t sum_lat = 0; int nlat = 0;
	uint64_t max_gap = 0, min_gap = ~0ull;
	int nfail = 0, wl_fd = wl_display_get_fd(dpy);

	for (int i = 0; i < nframes; i++) {
		struct ext_image_copy_capture_frame_v1 *fr =
			ext_image_copy_capture_session_v1_create_frame(sess);
		ext_image_copy_capture_frame_v1_add_listener(fr, &frame_l, NULL);
		ext_image_copy_capture_frame_v1_attach_buffer(fr, wbufs[i % nbuf]);
		ext_image_copy_capture_frame_v1_damage_buffer(fr, 0, 0, bw, bh);
		uint64_t tcap = now_ns();
		ext_image_copy_capture_frame_v1_capture(fr);
		wl_display_flush(dpy);

		f_state = 0; f_ndmg = 0; f_have_pres = 0;
		while (!f_state) {
			while (wl_display_prepare_read(dpy) != 0) wl_display_dispatch_pending(dpy);
			wl_display_flush(dpy);
			struct pollfd pfd = { wl_fd, POLLIN, 0 };
			int pr = poll(&pfd, 1, 2000);
			if (pr <= 0) { wl_display_cancel_read(dpy); printf("  timeout/err waiting for frame %d\n", i); break; }
			wl_display_read_events(dpy); wl_display_dispatch_pending(dpy);
		}
		uint64_t tready = now_ns();
		if (f_state == 2) nfail++;
		if (f_state == 1) {
			uint64_t lat = f_have_pres ? (tready - f_pres) : 0;
			if (f_have_pres) { sum_lat += lat; nlat++; }
			uint64_t gap = prev_ready ? tready - prev_ready : 0;
			if (prev_ready) { if (gap > max_gap) max_gap = gap; if (gap < min_gap) min_gap = gap; }
			if (i < 12 || i % 20 == 0)
				printf("  f%-3d ready  cap->ready=%6.2fms  pres->ready=%7.2fms  gap=%6.2fms  d_pres=%6.2fms  ndamage=%d xform=%u\n",
				       i, (tready - tcap)/1e6, f_have_pres ? lat/1e6 : -1.0,
				       gap/1e6, prev_pres && f_have_pres ? (f_pres - prev_pres)/1e6 : -1.0, f_ndmg, f_xform);
			prev_ready = tready; if (f_have_pres) prev_pres = f_pres;
		}
		if (getenv("VERIFY") && f_state == 1) {
			void *map=NULL,*mdata=NULL; uint32_t mstride=0;
			void *p = gbm_bo_map(bos[i%nbuf],0,0,bw,bh,GBM_BO_TRANSFER_READ,&mstride,&map);
			if (p) { uint64_t h=1469598103934665603ull; uint32_t nz=0;
				for (uint32_t yy=0; yy<bh; yy+=8) { uint32_t *row=(uint32_t*)((char*)p+(size_t)yy*mstride);
					for (uint32_t xx=0; xx<bw; xx+=8) { h=(h^row[xx])*1099511628211ull; if(row[xx]&0xffffff) nz++; } }
				printf("      VERIFY f%d: hash=%016lx nonzero=%u/%u stride=%u\n", i,(unsigned long)h,nz,(bw/8)*(bh/8),mstride);
				gbm_bo_unmap(bos[i%nbuf],map); mdata=mdata;
			} else printf("      VERIFY f%d: gbm_bo_map FAILED\n", i);
		}
		ext_image_copy_capture_frame_v1_destroy(fr);
	}
	uint64_t total = now_ns() - t0;
	printf("\n== results ==\n");
	printf("frames=%d fails=%d  wall=%.2fs  effective fps=%.2f\n",
	       nframes, nfail, total/1e9, nframes / (total/1e9));
	printf("gap min=%.2fms max=%.2fms\n", min_gap/1e6, max_gap/1e6);
	if (nlat) printf("mean presentation_time -> ready latency = %.2f ms\n", (sum_lat/(double)nlat)/1e6);
	printf("constraints rounds seen = %d\n", constraints_rounds);
	return 0;
}
