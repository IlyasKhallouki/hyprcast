/*
 * hyprcast engine -- desktop audio capture -> AAC-LC 48 kHz stereo.
 *
 * PipeWire is reached through its pulse compatibility layer with libavdevice's
 * "pulse" input, because that is the one path that exists on every PipeWire and
 * PulseAudio box without a build-time dependency on libpipewire.
 *
 * THE DEVICE. fluxcast runs 'ffmpeg -f pulse -i default', and "default" is the
 * default SOURCE -- the microphone. It casts the room, not the desktop. The
 * device we want is the default SINK's MONITOR, which is a different object
 * with a different name, so when cfg->device is NULL we derive it from
 * 'pactl get-default-sink' + ".monitor".
 *
 * THE CLOCK. Audio timestamps must share the video timestamps' origin or A/V
 * drifts, so they are not the demuxer's wallclock pts: the first PCM buffer
 * anchors an hc_now_ns() (CLOCK_MONOTONIC) origin and every later timestamp is
 * that anchor plus the sample count. If the sound card's clock and
 * CLOCK_MONOTONIC diverge by more than 250 ms the anchor is re-derived rather
 * than left to drift forever.
 *
 * THE FRAMING. Output is ADTS-framed AAC, not raw AAC: the mpegts muxer passes
 * a frame with a sync word straight through, the WFD sink parses ADTS, and
 * hc_mux therefore needs no side channel for the AudioSpecificConfig.
 *
 * Capture, gain and encode all run on this module's own thread; hc_audio_read()
 * only pops from a bounded ring, so a stalled muxer can never block capture and
 * a stalled reader can never block the sound server.
 */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

#include <libavcodec/avcodec.h>
#include <libavdevice/avdevice.h>
#include <libavformat/avformat.h>
#include <libavutil/avutil.h>
#include <libavutil/channel_layout.h>
#include <libavutil/frame.h>
#include <libavutil/mathematics.h>
#include <libavutil/opt.h>
#include <libavutil/samplefmt.h>

/* hc.h prototypes hc_pool_create() with an opaque Wayland type we never use
 * here; declaring it first keeps -Wpedantic from flagging the prototype. */
struct zwp_linux_dmabuf_v1;

#include "hc.h"

#define HC_LOG(...)  do {                     \
        fprintf(stderr, "[aud] ");            \
        fprintf(stderr, __VA_ARGS__);         \
        fputc('\n', stderr);                  \
    } while (0)
#define HC_ERR(...)  do {                     \
        fprintf(stderr, "[aud] ERROR: ");     \
        fprintf(stderr, __VA_ARGS__);         \
        fputc('\n', stderr);                  \
    } while (0)

#define HC_AUDIO_RATE      48000
#define HC_AUDIO_CHANNELS  2
#define HC_AAC_FRAME       1024          /* samples per AAC-LC access unit */
#define HC_ADTS_HDR        7

/* ~1.4 s of audio. Deep enough that a busy encode loop never loses a frame,
 * shallow enough that a reader which stops reading is noticed. */
#define HC_AUDIO_RING      64

/* Re-anchor the audio clock if the sound card and CLOCK_MONOTONIC disagree by
 * more than this. Below it, sample counting is the more stable of the two. */
#define HC_AUDIO_RESYNC_NS 250000000ull

/* How much of the sound server's start-up backlog is worth keeping. One AAC
 * access unit is 21.3 ms, so this is under a frame: enough that the first
 * encode does not have to wait for another read, little enough that the clock
 * anchor is not dragged into the past by however long the device took to open. */
#define HC_AUDIO_START_KEEP_NS 20000000ull

static void log_av(const char *what, int err)
{
    char msg[AV_ERROR_MAX_STRING_SIZE];
    if (av_strerror(err, msg, sizeof msg) < 0)
        snprintf(msg, sizeof msg, "unknown error %d", err);
    HC_ERR("%s: %s", what, msg);
}

struct hc_aframe {
    uint8_t *buf;
    int      cap, size;
    uint64_t pts_ns;
};

struct hc_audio {
    struct hc_audio_cfg cfg;
    char            *device;

    AVFormatContext *ic;
    AVCodecContext  *enc;
    AVPacket        *ipkt, *opkt;
    AVFrame         *frame;

    /* pending interleaved s16 PCM that has not filled an AAC frame yet */
    int16_t         *pcm;
    int              pcm_frames, pcm_cap;   /* in sample frames, not bytes */

    uint64_t         anchor_ns;      /* hc_now_ns() of PCM sample 0 */
    bool             have_anchor;
    uint64_t         captured;       /* sample frames read from the device */
    uint64_t         encoded;        /* sample frames handed to the encoder */
    uint64_t         resyncs;

    pthread_t        thread;
    pthread_mutex_t  lock;
    bool             thread_live;

    struct hc_aframe ring[HC_AUDIO_RING];
    int              head, tail, count;
    struct hc_aframe held;           /* what the last hc_audio_read handed out */

    _Atomic int      gain_milli;     /* 0..1000+, applied to the PCM */
    atomic_bool      muted;
    atomic_bool      stop;
    atomic_int       err;
    atomic_bool      reanchor;       /* hc_audio_flush asked for a fresh origin */

    uint64_t         frames_out, drops;
    uint64_t         last_drop_log_ns;
};

/* ---------------------------------------------------------- device name */

/*
 * 'pactl get-default-sink' + ".monitor". The command is a compile-time
 * constant, so there is nothing here for an argument to be injected into.
 */
static char *default_monitor_name(void)
{
    char line[256];
    FILE *p;
    size_t len;
    char *out;

    p = popen("pactl get-default-sink 2>/dev/null", "r");
    if (!p)
        return NULL;
    if (!fgets(line, sizeof line, p)) {
        pclose(p);
        return NULL;
    }
    if (pclose(p) != 0)
        return NULL;

    len = strcspn(line, "\r\n");
    line[len] = '\0';
    if (len == 0)
        return NULL;

    out = malloc(len + sizeof ".monitor");
    if (!out)
        return NULL;
    memcpy(out, line, len);
    memcpy(out + len, ".monitor", sizeof ".monitor");
    return out;
}

/* ----------------------------------------------------------------- ADTS */

/*
 * 7-byte ADTS header for AAC-LC 48 kHz stereo, no CRC.
 * profile 1 (LC), sampling_frequency_index 3 (48000), channel_config 2,
 * adts_buffer_fullness 0x7FF (variable rate), one raw data block.
 */
static void adts_header(uint8_t *h, int aac_len)
{
    int len = aac_len + HC_ADTS_HDR;

    h[0] = 0xFF;
    h[1] = 0xF1;                                     /* MPEG-4, no CRC */
    h[2] = (uint8_t)((1 << 6) | (3 << 2) | ((2 >> 2) & 0x1));
    h[3] = (uint8_t)(((2 & 0x3) << 6) | ((len >> 11) & 0x3));
    h[4] = (uint8_t)((len >> 3) & 0xFF);
    h[5] = (uint8_t)(((len & 0x7) << 5) | 0x1F);
    h[6] = 0xFC;
}

/* ------------------------------------------------------------------ ring */

/* Caller holds the lock. Grows the slot's buffer as needed; steady state does
 * not allocate. */
static int aframe_set(struct hc_aframe *f, const uint8_t *hdr, int hdr_len,
                      const uint8_t *body, int body_len, uint64_t pts_ns)
{
    int need = hdr_len + body_len;

    if (need > f->cap) {
        uint8_t *nb = realloc(f->buf, (size_t)need);
        if (!nb)
            return -1;
        f->buf = nb;
        f->cap = need;
    }
    if (hdr_len)
        memcpy(f->buf, hdr, (size_t)hdr_len);
    memcpy(f->buf + hdr_len, body, (size_t)body_len);
    f->size   = need;
    f->pts_ns = pts_ns;
    return 0;
}

static void ring_put(struct hc_audio *a, const uint8_t *hdr, int hdr_len,
                     const uint8_t *body, int body_len, uint64_t pts_ns)
{
    pthread_mutex_lock(&a->lock);

    if (a->count == HC_AUDIO_RING) {
        /* Nobody is reading. Drop the OLDEST frame: on a live cast the newest
         * audio is the only audio worth sending. */
        uint64_t now = hc_now_ns();
        a->head = (a->head + 1) % HC_AUDIO_RING;
        a->count--;
        a->drops++;
        if (now - a->last_drop_log_ns > 1000000000ull) {
            a->last_drop_log_ns = now;
            HC_ERR("ring full: dropped %" PRIu64 " AAC frame(s) -- "
                   "hc_audio_read is not being called", a->drops);
        }
    }

    if (aframe_set(&a->ring[a->tail], hdr, hdr_len, body, body_len, pts_ns) != 0) {
        pthread_mutex_unlock(&a->lock);
        HC_ERR("out of memory queueing an AAC frame");
        atomic_store(&a->err, -1);
        return;
    }
    a->tail = (a->tail + 1) % HC_AUDIO_RING;
    a->count++;
    a->frames_out++;

    pthread_mutex_unlock(&a->lock);
}

/* --------------------------------------------------------------- encode */

static void drain_encoder(struct hc_audio *a)
{
    for (;;) {
        uint8_t hdr[HC_ADTS_HDR];
        uint64_t pts_ns;
        int64_t samples;
        int err;

        err = avcodec_receive_packet(a->enc, a->opkt);
        if (err == AVERROR(EAGAIN) || err == AVERROR_EOF)
            return;
        if (err < 0) {
            log_av("avcodec_receive_packet(aac)", err);
            atomic_store(&a->err, err);
            return;
        }

        /* The encoder's time base is 1/48000, i.e. pts counts sample frames. */
        samples = a->opkt->pts == AV_NOPTS_VALUE ? 0 : a->opkt->pts;
        if (samples < 0)
            samples = 0;
        pts_ns = a->anchor_ns +
                 (uint64_t)av_rescale_q(samples, (AVRational){ 1, HC_AUDIO_RATE },
                                        (AVRational){ 1, 1000000000 });

        adts_header(hdr, a->opkt->size);
        ring_put(a, hdr, HC_ADTS_HDR, a->opkt->data, a->opkt->size, pts_ns);
        av_packet_unref(a->opkt);
    }
}

static inline float clipf(float v)
{
    if (v > 1.0f)
        return 1.0f;
    if (v < -1.0f)
        return -1.0f;
    return v;
}

/*
 * Convert HC_AAC_FRAME interleaved s16 sample frames into the encoder's planar
 * float layout, applying the live gain on the way. This is the only place the
 * PCM is touched, so volume changes take effect on the next 21 ms of audio
 * without going anywhere near the local sink's volume.
 */
static int encode_one(struct hc_audio *a)
{
    float gain;
    int err;

    err = av_frame_make_writable(a->frame);
    if (err < 0) {
        log_av("av_frame_make_writable", err);
        return err;
    }

    gain = atomic_load_explicit(&a->muted, memory_order_relaxed)
           ? 0.0f
           : (float)atomic_load_explicit(&a->gain_milli, memory_order_relaxed)
             / 1000.0f;

    {
        const int16_t *in = a->pcm;
        float *l = (float *)a->frame->data[0];
        float *r = (float *)a->frame->data[1];
        const float scale = gain / 32768.0f;

        if (gain <= 1.0f) {
            for (int i = 0; i < HC_AAC_FRAME; i++) {
                l[i] = (float)in[2 * i]     * scale;
                r[i] = (float)in[2 * i + 1] * scale;
            }
        } else {
            /* Boost can leave full-scale input outside the encoder's -1..1
             * domain. Hard-clip it here rather than hand the AAC encoder
             * samples it is not specified for. */
            for (int i = 0; i < HC_AAC_FRAME; i++) {
                l[i] = clipf((float)in[2 * i]     * scale);
                r[i] = clipf((float)in[2 * i + 1] * scale);
            }
        }
    }

    a->frame->pts = (int64_t)a->encoded;

    err = avcodec_send_frame(a->enc, a->frame);
    if (err < 0) {
        log_av("avcodec_send_frame(aac)", err);
        return err;
    }
    a->encoded += HC_AAC_FRAME;

    /* Consume the frames we just encoded. */
    a->pcm_frames -= HC_AAC_FRAME;
    if (a->pcm_frames > 0)
        memmove(a->pcm, a->pcm + (size_t)HC_AAC_FRAME * HC_AUDIO_CHANNELS,
                (size_t)a->pcm_frames * HC_AUDIO_CHANNELS * sizeof *a->pcm);

    drain_encoder(a);
    return 0;
}

static int append_pcm(struct hc_audio *a, const uint8_t *data, int size)
{
    int frames = size / (int)(HC_AUDIO_CHANNELS * sizeof(int16_t));
    uint64_t now, predicted;

    if (frames <= 0)
        return 0;

    /* hc_audio_flush() ran on the control thread: throw away what was captured
     * before it and start the clock again from this buffer. */
    if (atomic_exchange_explicit(&a->reanchor, false, memory_order_relaxed)) {
        a->pcm_frames  = 0;
        a->have_anchor = false;
    }

    if (a->pcm_frames + frames > a->pcm_cap) {
        int cap = a->pcm_frames + frames + HC_AAC_FRAME;
        int16_t *nb = realloc(a->pcm,
                              (size_t)cap * HC_AUDIO_CHANNELS * sizeof *nb);
        if (!nb) {
            HC_ERR("out of memory growing the PCM buffer");
            return -1;
        }
        a->pcm = nb;
        a->pcm_cap = cap;
    }
    memcpy(a->pcm + (size_t)a->pcm_frames * HC_AUDIO_CHANNELS, data,
           (size_t)frames * HC_AUDIO_CHANNELS * sizeof *a->pcm);
    a->pcm_frames += frames;

    now = hc_now_ns();
    if (!a->have_anchor) {
        /*
         * The first read hands back everything the sound server buffered while
         * the stream was being set up -- avformat_open_input, the AAC encoder,
         * the whole of hc_audio_open. That PCM is real but STALE, and since the
         * anchor is derived from it ("this buffer ended now"), keeping it puts
         * every later timestamp that far in the past: measured 116.6 ms behind
         * the PCR after a mid-session device switch, which fails assert-ts's
         * 100 ms PTS/PCR check. Keep only the newest slice and start there.
         */
        uint64_t span;
        int keep = (int)(HC_AUDIO_START_KEEP_NS * (uint64_t)HC_AUDIO_RATE
                         / 1000000000ull);

        if (a->pcm_frames > keep) {
            int drop = a->pcm_frames - keep;
            memmove(a->pcm, a->pcm + (size_t)drop * HC_AUDIO_CHANNELS,
                    (size_t)keep * HC_AUDIO_CHANNELS * sizeof *a->pcm);
            a->pcm_frames = keep;
            HC_LOG("dropped %d frame(s) (%.1f ms) buffered while the device "
                   "was opening", drop,
                   (double)drop * 1000.0 / (double)HC_AUDIO_RATE);
        }
        span = (uint64_t)a->pcm_frames * 1000000000ull / HC_AUDIO_RATE;
        a->anchor_ns   = now > span ? now - span : 0;
        a->have_anchor = true;
        a->captured    = (uint64_t)a->pcm_frames;
        return 0;
    }

    a->captured += (uint64_t)frames;
    predicted = a->anchor_ns +
                a->captured * 1000000000ull / HC_AUDIO_RATE;
    if (now > predicted + HC_AUDIO_RESYNC_NS ||
        predicted > now + HC_AUDIO_RESYNC_NS) {
        int64_t skew = (int64_t)now - (int64_t)predicted;
        a->anchor_ns = (uint64_t)((int64_t)a->anchor_ns + skew);
        a->resyncs++;
        HC_LOG("audio clock re-anchored by %+.1f ms (resync %" PRIu64 ")",
               (double)skew / 1e6, a->resyncs);
    }
    return 0;
}

/* ---------------------------------------------------------------- thread */

static int interrupt_cb(void *opaque)
{
    struct hc_audio *a = opaque;
    return atomic_load_explicit(&a->stop, memory_order_relaxed) ? 1 : 0;
}

static void *capture_thread(void *arg)
{
    struct hc_audio *a = arg;

    while (!atomic_load_explicit(&a->stop, memory_order_relaxed)) {
        int err = av_read_frame(a->ic, a->ipkt);

        if (err == AVERROR(EAGAIN))
            continue;
        if (err < 0) {
            if (!atomic_load_explicit(&a->stop, memory_order_relaxed) &&
                err != AVERROR_EXIT) {
                log_av("av_read_frame(pulse)", err);
                atomic_store(&a->err, err);
            }
            break;
        }

        if (append_pcm(a, a->ipkt->data, a->ipkt->size) != 0) {
            av_packet_unref(a->ipkt);
            atomic_store(&a->err, -1);
            break;
        }
        av_packet_unref(a->ipkt);

        while (a->pcm_frames >= HC_AAC_FRAME) {
            if (encode_one(a) != 0) {
                atomic_store(&a->err, -1);
                return NULL;
            }
        }
    }
    return NULL;
}

/* ------------------------------------------------------------------ open */

struct hc_audio *hc_audio_open(const struct hc_audio_cfg *cfg)
{
    struct hc_audio *a;
    const AVInputFormat *ifmt;
    const AVCodec *codec;
    AVDictionary *opts = NULL;
    /* Read the caller's device BEFORE a->cfg.device is cleared below. It used
     * to be read after, which is always NULL, so every session silently
     * captured `pactl get-default-sink`.monitor no matter what it was asked
     * for -- invisible until something asked for a different source. */
    const char *want = cfg ? cfg->device : NULL;
    uint32_t bitrate;
    int err;

    a = calloc(1, sizeof *a);
    if (!a) {
        HC_ERR("hc_audio_open: out of memory");
        return NULL;
    }
    if (cfg)
        a->cfg = *cfg;
    /* device belongs to the caller; a->device is our own copy from here on. */
    a->cfg.device = NULL;
    bitrate = a->cfg.bitrate_bps ? a->cfg.bitrate_bps : 128000;
    atomic_init(&a->gain_milli, 1000);
    atomic_init(&a->muted, false);
    atomic_init(&a->stop, false);
    atomic_init(&a->err, 0);
    atomic_init(&a->reanchor, false);

    if (pthread_mutex_init(&a->lock, NULL) != 0) {
        HC_ERR("pthread_mutex_init failed");
        free(a);
        return NULL;
    }

    if (want && *want) {
        a->device = strdup(want);
    } else {
        a->device = default_monitor_name();
        if (!a->device) {
            /* pipewire-pulse and PulseAudio both resolve this alias. Never
             * fall back to "default": that is the microphone. */
            HC_ERR("pactl get-default-sink failed; falling back to "
                   "@DEFAULT_MONITOR@");
            a->device = strdup("@DEFAULT_MONITOR@");
        }
    }
    if (!a->device) {
        HC_ERR("hc_audio_open: out of memory");
        goto fail;
    }
    if (!strstr(a->device, "monitor")) {
        HC_ERR("WARNING device '%s' does not look like a monitor source -- "
               "this will capture an input, not the desktop", a->device);
    }

    avdevice_register_all();
    ifmt = av_find_input_format("pulse");
    if (!ifmt) {
        HC_ERR("libavdevice has no \"pulse\" input in this build");
        goto fail;
    }

    a->ic = avformat_alloc_context();
    if (!a->ic) {
        HC_ERR("avformat_alloc_context failed");
        goto fail;
    }
    /* So hc_audio_close() can unblock a read that is waiting on the server. */
    a->ic->interrupt_callback.callback = interrupt_cb;
    a->ic->interrupt_callback.opaque   = a;

    av_dict_set_int(&opts, "sample_rate", HC_AUDIO_RATE, 0);
    av_dict_set_int(&opts, "channels", HC_AUDIO_CHANNELS, 0);
    /*
     * Server-side buffer, in bytes: 4096 = 1024 stereo s16 sample frames = one
     * AAC access unit = 21.3 ms. ("frame_size" means the same thing and is
     * deprecated in this libavdevice.)
     */
    av_dict_set_int(&opts, "fragment_size", HC_AAC_FRAME * HC_AUDIO_CHANNELS *
                                            (int)sizeof(int16_t), 0);
    av_dict_set(&opts, "name", "hyprcast", 0);
    av_dict_set(&opts, "stream_name", "desktop cast", 0);

    err = avformat_open_input(&a->ic, a->device, ifmt, &opts);
    av_dict_free(&opts);
    if (err < 0) {
        log_av("avformat_open_input(pulse)", err);
        HC_ERR("device was '%s'", a->device);
        a->ic = NULL;                  /* freed by avformat_open_input */
        goto fail;
    }

    if (a->ic->nb_streams != 1 ||
        a->ic->streams[0]->codecpar->codec_id != AV_CODEC_ID_PCM_S16LE) {
        HC_ERR("pulse gave %u stream(s) of codec %d; expected one PCM_S16LE",
               a->ic->nb_streams,
               a->ic->nb_streams ? (int)a->ic->streams[0]->codecpar->codec_id : -1);
        goto fail;
    }

    /* --- AAC-LC ---------------------------------------------------------- */
    codec = avcodec_find_encoder(AV_CODEC_ID_AAC);
    if (!codec) {
        HC_ERR("no AAC encoder in this libavcodec build");
        goto fail;
    }
    a->enc = avcodec_alloc_context3(codec);
    if (!a->enc) {
        HC_ERR("avcodec_alloc_context3(aac) failed");
        goto fail;
    }
    a->enc->sample_fmt  = AV_SAMPLE_FMT_FLTP;
    a->enc->sample_rate = HC_AUDIO_RATE;
    a->enc->bit_rate    = bitrate;
    a->enc->profile     = AV_PROFILE_AAC_LOW;
    a->enc->time_base   = (AVRational){ 1, HC_AUDIO_RATE };
    av_channel_layout_default(&a->enc->ch_layout, HC_AUDIO_CHANNELS);

    err = avcodec_open2(a->enc, codec, NULL);
    if (err < 0) {
        log_av("avcodec_open2(aac)", err);
        goto fail;
    }
    if (a->enc->frame_size != HC_AAC_FRAME) {
        HC_ERR("AAC encoder wants %d samples per frame, not %d",
               a->enc->frame_size, HC_AAC_FRAME);
        goto fail;
    }

    a->ipkt  = av_packet_alloc();
    a->opkt  = av_packet_alloc();
    a->frame = av_frame_alloc();
    if (!a->ipkt || !a->opkt || !a->frame) {
        HC_ERR("av_packet_alloc / av_frame_alloc failed");
        goto fail;
    }
    a->frame->format      = AV_SAMPLE_FMT_FLTP;
    a->frame->sample_rate = HC_AUDIO_RATE;
    a->frame->nb_samples  = HC_AAC_FRAME;
    err = av_channel_layout_copy(&a->frame->ch_layout, &a->enc->ch_layout);
    if (err < 0) {
        log_av("av_channel_layout_copy", err);
        goto fail;
    }
    err = av_frame_get_buffer(a->frame, 0);
    if (err < 0) {
        log_av("av_frame_get_buffer", err);
        goto fail;
    }

    if (pthread_create(&a->thread, NULL, capture_thread, a) != 0) {
        HC_ERR("pthread_create(audio capture) failed");
        goto fail;
    }
    a->thread_live = true;

    HC_LOG("pulse '%s' -> AAC-LC %u Hz %d ch %u bps (ADTS)",
           a->device, (unsigned)HC_AUDIO_RATE, HC_AUDIO_CHANNELS, bitrate);
    return a;

fail:
    atomic_store(&a->stop, true);
    av_frame_free(&a->frame);
    av_packet_free(&a->ipkt);
    av_packet_free(&a->opkt);
    avcodec_free_context(&a->enc);
    if (a->ic)
        avformat_close_input(&a->ic);
    free(a->device);
    pthread_mutex_destroy(&a->lock);
    free(a);
    return NULL;
}

/* ------------------------------------------------------------------ read */

int hc_audio_read(struct hc_audio *a, const uint8_t **data, int *size,
                  uint64_t *pts_ns)
{
    struct hc_aframe *src;
    int err;

    if (!a || !data || !size || !pts_ns) {
        HC_ERR("hc_audio_read: null argument");
        return -1;
    }
    *data   = NULL;
    *size   = 0;
    *pts_ns = 0;

    err = atomic_load_explicit(&a->err, memory_order_relaxed);

    pthread_mutex_lock(&a->lock);
    if (a->count == 0) {
        pthread_mutex_unlock(&a->lock);
        return err < 0 ? err : 0;
    }
    src = &a->ring[a->head];
    /* Copy into the held slot: the contract is that the pointer stays valid
     * until the next call, and the ring slot is about to be reused. */
    if (aframe_set(&a->held, NULL, 0, src->buf, src->size, src->pts_ns) != 0) {
        pthread_mutex_unlock(&a->lock);
        HC_ERR("out of memory in hc_audio_read");
        return -1;
    }
    a->head = (a->head + 1) % HC_AUDIO_RING;
    a->count--;
    pthread_mutex_unlock(&a->lock);

    *data   = a->held.buf;
    *size   = a->held.size;
    *pts_ns = a->held.pts_ns;
    return 1;
}

/* ---------------------------------------------------------------- volume */

void hc_audio_set_volume(struct hc_audio *a, float gain)
{
    int milli;

    if (!a)
        return;
    if (!(gain >= 0.0f))              /* also catches NaN */
        gain = 0.0f;
    if (gain > 4.0f)                  /* 12 dB of headroom, then clamp */
        gain = 4.0f;
    milli = (int)lrintf(gain * 1000.0f);
    atomic_store_explicit(&a->gain_milli, milli, memory_order_relaxed);
}

void hc_audio_set_muted(struct hc_audio *a, bool muted)
{
    if (!a)
        return;
    atomic_store_explicit(&a->muted, muted, memory_order_relaxed);
}

/* ----------------------------------------------------------------- flush */

/*
 * Drop queued access units older than `floor_ns`; return how many survive.
 *
 * This is what makes a device change seamless, and the return value is what
 * makes it safe to act on. A replacement leg is opened while the old one is
 * still feeding the muxer, and for the first ~90 ms it has NOTHING queued:
 * MEASURED, the device, one fragment, 1024 samples and the encoder's own frame
 * of delay all have to happen before it can speak. Swapping into that silence
 * is what left a 95 ms hole in the stream, and a hole is what makes
 * libavformat's interleaver hold video back and put the next access unit past
 * assert-ts's 100 ms PTS/PCR limit.
 *
 * So the caller waits until this returns non-zero: the cut is then made exactly
 * at the last timestamp already sent, and the two legs join with neither a gap
 * nor an overlap -- one sample-boundary discontinuity, which is what changing
 * sound device sounds like anyway.
 *
 * Pass 0 to drop everything queued and re-anchor the clock at the next buffer.
 */
int hc_audio_flush(struct hc_audio *a, uint64_t floor_ns)
{
    int kept;

    if (!a)
        return 0;

    pthread_mutex_lock(&a->lock);
    if (floor_ns == 0) {
        a->head = a->tail = a->count = 0;
    } else {
        while (a->count > 0 && a->ring[a->head].pts_ns < floor_ns) {
            a->head = (a->head + 1) % HC_AUDIO_RING;
            a->count--;
        }
    }
    kept = a->count;
    pthread_mutex_unlock(&a->lock);

    if (floor_ns == 0)
        /* The capture thread owns pcm/anchor, so it does the reset itself. */
        atomic_store_explicit(&a->reanchor, true, memory_order_relaxed);
    return kept;
}

/* ----------------------------------------------------------------- close */

void hc_audio_close(struct hc_audio *a)
{
    if (!a)
        return;

    atomic_store(&a->stop, true);
    if (a->thread_live) {
        pthread_join(a->thread, NULL);
        a->thread_live = false;
    }

    HC_LOG("closed: %" PRIu64 " AAC frame(s) produced, %" PRIu64 " dropped, "
           "%" PRIu64 " clock resync(s)", a->frames_out, a->drops, a->resyncs);

    av_frame_free(&a->frame);
    av_packet_free(&a->ipkt);
    av_packet_free(&a->opkt);
    avcodec_free_context(&a->enc);
    if (a->ic)
        avformat_close_input(&a->ic);

    for (int i = 0; i < HC_AUDIO_RING; i++)
        free(a->ring[i].buf);
    free(a->held.buf);
    free(a->pcm);
    free(a->device);
    pthread_mutex_destroy(&a->lock);
    free(a);
}
