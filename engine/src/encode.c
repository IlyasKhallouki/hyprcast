/*
 * hyprcast engine -- h264_vaapi encode on the surfaces VPP already wrote.
 *
 * The whole point of this module is the wrapping in hc_enc_open: we hand
 * libavcodec OUR VADisplay rather than letting it open its own. Both the VPP
 * destination surfaces and the encoder input surfaces then live in the same
 * VA context, so avcodec_send_frame costs nothing but a submit -- no
 * hwupload, no transfer, no AV_PIX_FMT_NV12 software frame anywhere.
 *
 * Encoder settings are tuned for the sink we actually have: a Xiaomi/Google TV
 * Miracast receiver that negotiated 1280x720p60 H.264 Constrained Baseline
 * level 3.2. Annex-B in-band parameter sets (no AV_CODEC_FLAG_GLOBAL_HEADER),
 * no B-frames, AUD on, SEI off, and a 0.5 s VBV rather than ffmpeg's 1.0 s
 * default so a bitrate spike drains within a couple of frames.
 */
#define _GNU_SOURCE

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <libavcodec/avcodec.h>
#include <libavutil/avutil.h>
#include <libavutil/buffer.h>
#include <libavutil/frame.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_vaapi.h>
#include <libavutil/opt.h>
#include <libavutil/rational.h>

/* hc.h prototypes hc_pool_create() with an opaque Wayland type we never use
 * here; declaring it first keeps -Wpedantic from flagging the prototype. */
struct zwp_linux_dmabuf_v1;

#include "hc.h"

/* ISO C11 variadic macros; ", ##__VA_ARGS__" is a GNU extension and
 * warning_level=3 turns on -Wpedantic. */
#define HC_LOG(...)  do {                     \
        fprintf(stderr, "[enc] ");            \
        fprintf(stderr, __VA_ARGS__);         \
        fputc('\n', stderr);                  \
    } while (0)
#define HC_ERR(...)  do {                     \
        fprintf(stderr, "[enc] ERROR: ");     \
        fprintf(stderr, __VA_ARGS__);         \
        fputc('\n', stderr);                  \
    } while (0)

static const AVRational HC_NS_TB = { 1, 1000000000 };

static void log_av(const char *what, int err)
{
    char msg[AV_ERROR_MAX_STRING_SIZE];
    if (av_strerror(err, msg, sizeof msg) < 0)
        snprintf(msg, sizeof msg, "unknown error %d", err);
    HC_ERR("%s: %s", what, msg);
}

struct hc_enc {
    struct hc_enc_cfg cfg;

    AVBufferRef    *device_ref;   /* wraps the caller's VADisplay */
    AVBufferRef    *frames_ref;   /* NV12 VAAPI pool, VPP writes into it */
    AVCodecContext *avctx;
    AVPacket       *pkt;
    bool            pkt_held;     /* pkt currently holds data handed out */

    uint64_t        frames_in, packets_out;
};

/* -------------------------------------------------------------- options */

static int set_opt_str(struct hc_enc *e, const char *name, const char *val)
{
    int err = av_opt_set(e->avctx->priv_data, name, val, 0);
    if (err < 0) {
        HC_ERR("h264_vaapi rejected option %s=%s", name, val);
        log_av("av_opt_set", err);
    }
    return err;
}

static int set_opt_int(struct hc_enc *e, const char *name, int64_t val)
{
    int err = av_opt_set_int(e->avctx->priv_data, name, val, 0);
    if (err < 0) {
        HC_ERR("h264_vaapi rejected option %s=%" PRId64, name, val);
        log_av("av_opt_set_int", err);
    }
    return err;
}

/* ----------------------------------------------------------------- open */

struct hc_enc *hc_enc_open(struct hc_va *v, const struct hc_enc_cfg *cfg)
{
    struct hc_enc *e;
    const AVCodec *codec;
    AVHWDeviceContext  *devctx;
    AVVAAPIDeviceContext *vactx;
    AVHWFramesContext  *frmctx;
    VADisplay dpy;
    int err;

    if (!v || !cfg) {
        HC_ERR("hc_enc_open: null argument");
        return NULL;
    }
    if (cfg->width == 0 || cfg->height == 0 || cfg->fps == 0 ||
        cfg->bitrate_bps == 0) {
        HC_ERR("hc_enc_open: bad config %ux%u @%u fps, %u bps",
               cfg->width, cfg->height, cfg->fps, cfg->bitrate_bps);
        return NULL;
    }

    dpy = hc_va_display(v);
    if (!dpy) {
        HC_ERR("hc_enc_open: hc_va has no VADisplay");
        return NULL;
    }

    codec = avcodec_find_encoder_by_name("h264_vaapi");
    if (!codec) {
        HC_ERR("h264_vaapi encoder is not present in this libavcodec build");
        return NULL;
    }

    e = calloc(1, sizeof *e);
    if (!e) {
        HC_ERR("hc_enc_open: out of memory");
        return NULL;
    }
    e->cfg = *cfg;

    /* --- 1. wrap the EXISTING VADisplay ---------------------------------
     * alloc, poke the display in, then init. av_hwdevice_ctx_create() would
     * open a second display on the same device and the encoder would then be
     * unable to see the surfaces VPP wrote. */
    e->device_ref = av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_VAAPI);
    if (!e->device_ref) {
        HC_ERR("av_hwdevice_ctx_alloc(VAAPI) failed");
        goto fail;
    }
    devctx = (AVHWDeviceContext *)e->device_ref->data;
    vactx  = (AVVAAPIDeviceContext *)devctx->hwctx;
    vactx->display = dpy;

    err = av_hwdevice_ctx_init(e->device_ref);
    if (err < 0) {
        log_av("av_hwdevice_ctx_init(VAAPI)", err);
        goto fail;
    }

    /* --- 2. the NV12 pool hc_va_vpp_run writes into --------------------- */
    e->frames_ref = av_hwframe_ctx_alloc(e->device_ref);
    if (!e->frames_ref) {
        HC_ERR("av_hwframe_ctx_alloc failed");
        goto fail;
    }
    frmctx = (AVHWFramesContext *)e->frames_ref->data;
    frmctx->format            = AV_PIX_FMT_VAAPI;
    frmctx->sw_format         = AV_PIX_FMT_NV12;
    frmctx->width             = (int)cfg->width;
    frmctx->height            = (int)cfg->height;
    frmctx->initial_pool_size = 6;

    err = av_hwframe_ctx_init(e->frames_ref);
    if (err < 0) {
        log_av("av_hwframe_ctx_init(NV12/VAAPI)", err);
        goto fail;
    }

    /* --- 3. the encoder ------------------------------------------------- */
    e->avctx = avcodec_alloc_context3(codec);
    if (!e->avctx) {
        HC_ERR("avcodec_alloc_context3 failed");
        goto fail;
    }

    e->avctx->pix_fmt   = AV_PIX_FMT_VAAPI;
    e->avctx->width     = (int)cfg->width;
    e->avctx->height    = (int)cfg->height;
    /* 90 kHz: the RTP/MPEG-TS clock, and fine enough that a 60 Hz capture
     * timestamp survives the rescale without jitter. */
    e->avctx->time_base = (AVRational){ 1, 90000 };
    e->avctx->framerate = (AVRational){ (int)cfg->fps, 1 };
    e->avctx->sample_aspect_ratio = (AVRational){ 1, 1 };
    e->avctx->gop_size     = (int)cfg->gop;
    e->avctx->max_b_frames = 0;                        /* bf=0 */
    e->avctx->bit_rate     = (int64_t)cfg->bitrate_bps;
    e->avctx->rc_max_rate  = (int64_t)cfg->bitrate_bps;
    e->avctx->rc_buffer_size = (int)(cfg->bitrate_bps / 2);  /* 0.5 s VBV */
    e->avctx->colorspace  = AVCOL_SPC_BT709;
    e->avctx->color_range = AVCOL_RANGE_MPEG;
    e->avctx->color_primaries = AVCOL_PRI_BT709;
    e->avctx->color_trc       = AVCOL_TRC_BT709;

    e->avctx->hw_frames_ctx = av_buffer_ref(e->frames_ref);
    if (!e->avctx->hw_frames_ctx) {
        HC_ERR("av_buffer_ref(frames) failed");
        goto fail;
    }

    /* Sink-negotiated: Constrained Baseline, level 3.2. */
    if (set_opt_str(e, "profile", "constrained_baseline") < 0) goto fail;
    if (set_opt_int(e, "level", 32) < 0) goto fail;
    /* One frame in flight: we submit and drain in lockstep, and a deeper
     * queue only adds latency to a live cast. */
    /*
     * async_depth is how many frames the encoder may have in flight.
     * Measured on this Gen9.5 part at 1280x720p60, IN SITU under GPU
     * contention (mpv decoding 1080p60 alongside):
     *   async_depth=1   encode p50 2.67 ms
     *   async_depth=2   encode p50 1.18 ms    <- 2.3x faster
     * Synthetic encode-only agrees (2.12 -> 1.61 ms).
     *
     * DEFAULT IS STILL 1, deliberately. Depth 2 is measurably burstier: the
     * same 12 s loopback capture passed assert-ts.py at depth 1 and failed
     * pat/pmt/pcr/continuity at depth 2 with a default-sized UDP receive
     * buffer, and only passed once the receiver was given 8 MB. A deeper
     * encoder queue lets frames emerge in clumps, and a Wi-Fi Direct sink's
     * receive buffer is not ours to enlarge.
     *
     * So: depth 1 for a link that already works, HC_ASYNC_DEPTH=2 when the
     * GPU is contended and encode time matters more than smooth pacing.
     */
    {
        const char *ad = getenv("HC_ASYNC_DEPTH");
        int depth = ad ? atoi(ad) : 1;
        if (depth < 1 || depth > 8)
            depth = 1;
        if (set_opt_int(e, "async_depth", depth) < 0) goto fail;
    }

    /*
     * Intel target usage, 1..7, higher is faster. Measured, 1280x720p60:
     *   EncSlice VBR : default 3.53 ms -> quality=4 2.70 ms  (-24%, free)
     *   VDEnc CQP    : default 2.14 ms -> quality=7 2.09 ms
     * quality=1 is SLOWER on both paths (3.84 / 3.01 ms), so "best quality"
     * is the wrong instinct here.
     */
    {
        const char *q = getenv("HC_QUALITY");
        int quality = q ? atoi(q) : (cfg->low_power ? 7 : 4);
        if (quality >= 1 && quality <= 7)
            if (set_opt_int(e, "quality", quality) < 0) goto fail;
    }
    /* Access unit delimiters help the sink resync; SEI is dead weight. */
    if (set_opt_int(e, "aud", 1) < 0) goto fail;
    if (set_opt_int(e, "sei", 0) < 0) goto fail;
    /*
     * Rate control depends on which encode engine we land on.
     *
     * Measured on this Gen9.5 part: VDEnc (VAEntrypointEncSliceLP, selected by
     * low_power=1) supports CQP ONLY. Both CBR and VBR fail avcodec_open2 with
     * EINVAL -- verified directly with ffmpeg for constrained_baseline and for
     * main. The full EncSlice path supports VBR normally.
     *
     * That is a real trade-off, not a bug to route around: VDEnc is a separate
     * fixed-function block from the one hardware DECODE uses, so it stays fast
     * while a video plays, but it cannot hold a bitrate target -- which is
     * exactly what a Wi-Fi Direct link wants. Pick per session.
     */
    if (cfg->low_power) {
        if (set_opt_int(e, "low_power", 1) < 0) goto fail;
        if (set_opt_str(e, "rc_mode", "CQP") < 0) goto fail;
        if (set_opt_int(e, "qp", cfg->qp ? (int)cfg->qp : 26) < 0) goto fail;
        e->avctx->bit_rate       = 0;
        e->avctx->rc_max_rate    = 0;
        e->avctx->rc_buffer_size = 0;
    } else {
        if (set_opt_str(e, "rc_mode", "VBR") < 0) goto fail;
    }

    err = avcodec_open2(e->avctx, codec, NULL);
    if (err < 0) {
        log_av("avcodec_open2(h264_vaapi)", err);
        goto fail;
    }

    e->pkt = av_packet_alloc();
    if (!e->pkt) {
        HC_ERR("av_packet_alloc failed");
        goto fail;
    }

    if (cfg->low_power)
        HC_LOG("h264_vaapi %ux%u@%u CBP L3.2 CQP qp=%u, gop %u, "
               "low_power (VDEnc -- no bitrate target)",
               cfg->width, cfg->height, cfg->fps,
               cfg->qp ? cfg->qp : 26u, cfg->gop);
    else
        HC_LOG("h264_vaapi %ux%u@%u CBP L3.2 %u bps VBR, vbv %d bits, gop %u",
               cfg->width, cfg->height, cfg->fps, cfg->bitrate_bps,
               e->avctx->rc_buffer_size, cfg->gop);
    return e;

fail:
    hc_enc_close(e);
    return NULL;
}

/* -------------------------------------------------------------- acquire */

int hc_enc_acquire(struct hc_enc *e, VASurfaceID *out, void **frame_opaque)
{
    AVFrame *f;
    int err;

    if (!e || !out || !frame_opaque) {
        HC_ERR("hc_enc_acquire: null argument");
        return -1;
    }
    *out = VA_INVALID_ID;
    *frame_opaque = NULL;

    f = av_frame_alloc();
    if (!f) {
        HC_ERR("av_frame_alloc failed");
        return -1;
    }

    err = av_hwframe_get_buffer(e->frames_ref, f, 0);
    if (err < 0) {
        log_av("av_hwframe_get_buffer", err);
        av_frame_free(&f);
        return err;
    }
    if (!f->data[3]) {
        HC_ERR("hwframe came back without a VASurfaceID");
        av_frame_free(&f);
        return -1;
    }

    /* VAAPI frames carry the surface id in data[3], not a pointer to it. */
    *out          = (VASurfaceID)(uintptr_t)f->data[3];
    *frame_opaque = f;
    return 0;
}

/* --------------------------------------------------------------- submit */

int hc_enc_submit(struct hc_enc *e, void *frame_opaque,
                  uint64_t pts_ns, bool force_idr)
{
    AVFrame *f;
    int err;

    if (!e || !frame_opaque) {
        HC_ERR("hc_enc_submit: null argument");
        return -1;
    }
    f = (AVFrame *)frame_opaque;

    f->pts = av_rescale_q((int64_t)pts_ns, HC_NS_TB, e->avctx->time_base);

    /*
     * Forcing an IDR: libavcodec's hardware encoder base has used both
     * AVFrame.pict_type == AV_PICTURE_TYPE_I and AV_FRAME_FLAG_KEY as the
     * "start a new GOP here" signal across versions. Setting both is
     * unambiguous and neither is interpreted as anything else on an input
     * frame, so this stays correct across the n8.x line.
     */
    if (force_idr) {
        f->pict_type = AV_PICTURE_TYPE_I;
        f->flags    |= AV_FRAME_FLAG_KEY;
    } else {
        f->pict_type = AV_PICTURE_TYPE_NONE;
        f->flags    &= ~AV_FRAME_FLAG_KEY;
    }

    err = avcodec_send_frame(e->avctx, f);

    /* The encoder took its own reference; ours goes back now either way. */
    av_frame_free(&f);

    if (err < 0) {
        /* EAGAIN means the output queue is full and the frame was NOT
         * consumed -- drain with hc_enc_receive before submitting again. */
        log_av("avcodec_send_frame", err);
        return err;
    }

    e->frames_in++;
    return 0;
}

/* -------------------------------------------------------------- receive */

int hc_enc_receive(struct hc_enc *e, const uint8_t **data, int *size,
                   bool *is_idr)
{
    int err;

    if (!e || !data || !size || !is_idr) {
        HC_ERR("hc_enc_receive: null argument");
        return -1;
    }
    *data   = NULL;
    *size   = 0;
    *is_idr = false;

    /* The previous packet stays valid until exactly here, per the contract. */
    if (e->pkt_held) {
        av_packet_unref(e->pkt);
        e->pkt_held = false;
    }

    err = avcodec_receive_packet(e->avctx, e->pkt);
    if (err == AVERROR(EAGAIN) || err == AVERROR_EOF)
        return 0;
    if (err < 0) {
        log_av("avcodec_receive_packet", err);
        return err;
    }

    e->pkt_held = true;
    e->packets_out++;

    *data   = e->pkt->data;
    *size   = e->pkt->size;
    *is_idr = (e->pkt->flags & AV_PKT_FLAG_KEY) != 0;
    return 1;
}

/* ---------------------------------------------------------------- close */

void hc_enc_close(struct hc_enc *e)
{
    if (!e)
        return;

    if (e->pkt) {
        if (e->pkt_held)
            av_packet_unref(e->pkt);
        av_packet_free(&e->pkt);
    }
    /* Frees avctx->hw_frames_ctx, our extra ref on frames_ref. */
    avcodec_free_context(&e->avctx);
    av_buffer_unref(&e->frames_ref);
    /* Does not vaTerminate: the VADisplay belongs to hc_va. */
    av_buffer_unref(&e->device_ref);

    free(e);
}
